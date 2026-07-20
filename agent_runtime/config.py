"""Deterministic profile selection for the Wheelhouse agent runtime."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


ACTIONS = frozenset(
    {
        "triage.issue.local",
        "triage.issue.search",
        "triage.pr.local",
        "triage.pr.search",
        "triage.schema-repair",
        "deep-review.local",
        "deep-review.search",
        "nl-decision.local",
        "nl-decision.search",
        "nl-decision.schema-repair",
        "advisory-review.public",
    }
)
SCHEMA_REPAIR_ACTIONS = frozenset(
    {"triage.schema-repair", "nl-decision.schema-repair"}
)
DIRECT_ACTIONS = SCHEMA_REPAIR_ACTIONS | {"advisory-review.public"}
PRIMARY_PROFILE = "claude-action-current-pinned"
DIRECT_PROFILE = "claude-cli-pinned"


def load_runtime_config() -> dict[str, Any]:
    try:
        import yaml
    except ImportError as error:
        raise ConfigError("PyYAML is required by trusted runtime configuration") from error
    root = Path(__file__).resolve().parents[1]
    try:
        with (root / "wheelhouse.config.yml").open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except (OSError, ValueError) as error:
        raise ConfigError("wheelhouse runtime configuration is unreadable") from error
    runtime = config.get("agent_runtime")
    if not isinstance(runtime, dict):
        raise ConfigError("agent_runtime configuration is missing")
    return runtime


def _repo_override(runtime: dict[str, Any], repo: str, action: str) -> str:
    overrides = runtime.get("repo_overrides") or {}
    if not isinstance(overrides, dict):
        raise ConfigError("agent_runtime.repo_overrides must be an object")
    row = overrides.get(repo) or {}
    if not isinstance(row, dict):
        raise ConfigError("agent runtime repository override must be an object")
    actions = row.get("actions") or {}
    if not isinstance(actions, dict):
        raise ConfigError("agent runtime repository actions must be an object")
    action_row = actions.get(action) or {}
    if action_row and not isinstance(action_row, dict):
        raise ConfigError("agent runtime action override must be an object")
    return str(action_row.get("target") or "")


def resolve_selection(action: str, repo: str = "", emergency: str = "") -> dict[str, Any]:
    if action not in ACTIONS:
        raise ConfigError("unsupported agent runtime action")
    runtime = load_runtime_config()
    contract = runtime.get("contract")
    if contract != "wheelhouse.agent-runtime/v1alpha1":
        raise ConfigError("agent runtime contract pin is invalid")
    if runtime.get("fallback") != "none":
        raise ConfigError("automatic fallback must remain disabled")
    if runtime.get("target") != "claude" or runtime.get("primary_profile") != PRIMARY_PROFILE:
        raise ConfigError("Claude must remain the captain-approved production primary")
    if "codex_auth_gate" in runtime:
        raise ConfigError("retired provider activation settings are forbidden")
    if runtime.get("disabled_adapters") != {"codex-app-server": "unsupported-public-chatgpt-pro-auth"}:
        raise ConfigError("Codex must remain disabled non-target adapter evidence")

    emergency = emergency or os.environ.get("WHEELHOUSE_AGENT_ROLLOUT_PROFILE", "")
    if emergency:
        raise ConfigError("agent runtime provider overrides are disabled")
    actions = runtime.get("actions") or {}
    if not isinstance(actions, dict) or set(actions) != ACTIONS:
        raise ConfigError("agent runtime actions must be explicitly and completely configured")
    primary_profile = runtime["primary_profile"]
    if any(
        not isinstance(row, dict)
        or row.get("target") != "claude"
        or row.get("profile") != primary_profile
        for row in actions.values()
    ):
        raise ConfigError("every base action must retain the pinned rollback profile")
    activation = runtime.get("production_activation")
    if (
        not isinstance(activation, dict)
        or set(activation) != DIRECT_ACTIONS
        or any(value != DIRECT_PROFILE for value in activation.values())
    ):
        raise ConfigError("only reviewed direct-runtime action profiles may be activated")
    rollback = runtime.get("temporary_rollback_profile")
    if rollback not in (None, PRIMARY_PROFILE):
        raise ConfigError("temporary rollback must select the pinned action profile")
    if action == "advisory-review.public" and rollback is not None:
        raise ConfigError(
            "public advisory review is disabled while the direct runtime is rolled back"
        )
    action_config = actions.get(action) or {}
    target = (
        _repo_override(runtime, repo, action)
        or str(action_config.get("target") or "")
    )
    if target != "claude":
        raise ConfigError("only the captain-approved Claude production target is selectable")
    profile_name = str(
        rollback
        or activation.get(action)
        or action_config.get("profile")
        or ""
    )
    profiles = runtime.get("profiles") or {}
    direct_profile = profiles.get(DIRECT_PROFILE)
    if not isinstance(direct_profile, dict):
        raise ConfigError("direct Claude profile is missing")
    expected_direct = {
        "adapter": "claude-cli",
        "harness": "claude-code",
        "provider": "anthropic",
        "auth_profile": "anthropic-subscription",
        "auth_mechanism": "claude-code-oauth-token",
        "expected_workspace_id": "",
        "model": "claude-sonnet-4-6",
        "effort": "provider-default",
        "cost_class": "subscription",
        "data_boundary": "anthropic-subscription",
        "allow_model_alias": False,
        "provider_hosts": ["api.anthropic.com"],
    }
    if direct_profile != expected_direct:
        raise ConfigError("direct Claude profile must remain exact")
    profile = profiles.get(profile_name)
    if not isinstance(profile, dict):
        raise ConfigError("selected agent runtime profile is missing")
    required = (
        "adapter",
        "harness",
        "provider",
        "auth_profile",
        "auth_mechanism",
        "expected_workspace_id",
        "model",
        "effort",
        "cost_class",
        "data_boundary",
        "allow_model_alias",
    )
    for field in required:
        if field not in profile:
            raise ConfigError("selected agent runtime profile is incomplete")
    expected_profile = DIRECT_PROFILE if action in DIRECT_ACTIONS and rollback is None else PRIMARY_PROFILE
    expected_adapter = "claude-cli" if expected_profile == DIRECT_PROFILE else "claude-action-compat"
    if profile_name != expected_profile or profile["adapter"] != expected_adapter:
        raise ConfigError("Claude production selection does not match the guarded action profile")
    if profile["provider"] != "anthropic" or profile["auth_profile"] != "anthropic-subscription":
        raise ConfigError("Claude production selection must use subscription authentication")
    if profile["model"] != "claude-sonnet-4-6" or profile["allow_model_alias"] is not False:
        raise ConfigError("Claude production selection must use the immutable model pin")
    return {
        "mode": target,
        "profileName": profile_name,
        "profile": dict(profile),
        "fallback": "none",
        "source": "wheelhouse.config.yml",
    }
