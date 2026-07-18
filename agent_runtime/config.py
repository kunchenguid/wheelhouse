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
    }
)


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
    if runtime.get("target") != "claude" or runtime.get("primary_profile") != "claude-action-current-pinned":
        raise ConfigError("Claude must remain the captain-approved production primary")
    if any(name in runtime for name in ("production_activation", "temporary_rollback_profile", "codex_auth_gate")):
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
    if any(not isinstance(row, dict) or row.get("target") != "claude" or row.get("profile") != primary_profile for row in actions.values()):
        raise ConfigError("every agent runtime action must target the Claude production profile")
    action_config = actions.get(action) or {}
    target = (
        _repo_override(runtime, repo, action)
        or str(action_config.get("target") or "")
    )
    if target != "claude":
        raise ConfigError("only the captain-approved Claude production target is selectable")
    profile_name = str(action_config.get("profile") or "")
    profiles = runtime.get("profiles") or {}
    direct_profile = profiles.get("claude-cli-unreachable-pinned")
    if not isinstance(direct_profile, dict):
        raise ConfigError("unreachable direct Claude profile evidence is missing")
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
    if direct_profile != expected_direct or any(row.get("profile") == "claude-cli-unreachable-pinned" for row in actions.values()):
        raise ConfigError("direct Claude profile must remain exact and unreachable")
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
    if profile_name != runtime.get("primary_profile") or profile["adapter"] != "claude-action-compat":
        raise ConfigError("Claude production selection must use the pinned direct action profile")
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
