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
    return str(action_row.get("rollout") or "")


def resolve_selection(action: str, repo: str = "", emergency: str = "") -> dict[str, Any]:
    if action not in ACTIONS:
        raise ConfigError("unsupported agent runtime action")
    runtime = load_runtime_config()
    contract = runtime.get("contract")
    if contract != "wheelhouse.agent-runtime/v1alpha1":
        raise ConfigError("agent runtime contract pin is invalid")
    if runtime.get("fallback") != "none":
        raise ConfigError("automatic fallback must remain disabled")

    emergency = emergency or os.environ.get("WHEELHOUSE_AGENT_ROLLOUT_PROFILE", "")
    if emergency not in ("", "legacy"):
        raise ConfigError("only the allowlisted legacy emergency rollback is accepted")
    actions = runtime.get("actions") or {}
    action_config = actions.get(action) or {}
    if not isinstance(action_config, dict):
        raise ConfigError("agent runtime action configuration must be an object")
    rollout = "legacy" if emergency == "legacy" else (
        _repo_override(runtime, repo, action)
        or str(action_config.get("rollout") or runtime.get("rollout") or "legacy")
    )
    if rollout not in ("legacy", "codex"):
        raise ConfigError("agent runtime rollout must be legacy or codex")
    if rollout == "codex" and runtime.get("production_activation") is not True:
        raise ConfigError("Codex production activation is pending an explicit reviewed config change")

    profile_name = (
        str(runtime.get("temporary_rollback_profile") or "")
        if rollout == "legacy"
        else str(action_config.get("profile") or runtime.get("primary_profile") or "")
    )
    profiles = runtime.get("profiles") or {}
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
    if rollout == "codex":
        auth_gate = runtime.get("codex_auth_gate") or {}
        prerequisites = (
            runtime.get("production_activation") is True,
            auth_gate.get("captain_alternative") in {"business-access-token", "private-managed-auth-json", "platform-api-key"},
            auth_gate.get("security_review") == "approved",
            auth_gate.get("private_credential_boundary") is True,
            auth_gate.get("nonproduction_proof") == "passed",
            bool(auth_gate.get("credential_owner")),
            bool(auth_gate.get("rotation_date")),
            bool(auth_gate.get("revocation_owner")),
        )
        if not all(prerequisites):
            raise ConfigError("Codex authentication and production activation prerequisites are not approved")
        if auth_gate.get("captain_alternative") == "platform-api-key":
            raise ConfigError("Platform API-key billing is not implemented or approved by this subscription profile")
        if profile["auth_mechanism"] == "codex-access-token" and auth_gate.get("captain_alternative") != "business-access-token":
            raise ConfigError("Codex access-token profile requires the approved Business or Enterprise alternative")
        if profile["auth_mechanism"] == "managed-auth-json":
            raise ConfigError("managed auth.json activation is unavailable until serialized secure refresh persistence is implemented and approved")
        if not profile.get("expected_workspace_id"):
            raise ConfigError("Codex activation requires an expected ChatGPT workspace id")
        if action.startswith("triage.pr.") and auth_gate.get("pr_automerge_semantic_parity") != "approved":
            raise ConfigError("Codex PR-triage activation requires separate auto-merge semantic parity approval")
        if profile["adapter"] != "codex-app-server" or profile["harness"] != "codex-cli":
            raise ConfigError("Codex rollout must select the pinned Codex adapter")
        if profile["provider"] != "openai" or profile["auth_profile"] != "codex-subscription":
            raise ConfigError("Codex rollout must use the ChatGPT subscription auth profile")
        if profile["cost_class"] != "subscription" or profile["allow_model_alias"] is not False:
            raise ConfigError("Codex rollout forbids billing and model substitution")
    else:
        if profile["adapter"] != "claude-action-compat":
            raise ConfigError("legacy rollout must remain the exact direct Claude Action bridge")
    return {
        "mode": rollout,
        "profileName": profile_name,
        "profile": dict(profile),
        "fallback": "none",
        "productionActivation": bool(runtime.get("production_activation")),
        "source": "emergency-legacy" if emergency == "legacy" else "wheelhouse.config.yml",
    }
