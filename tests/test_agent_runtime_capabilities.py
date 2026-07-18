#!/usr/bin/env python3
"""Capability negotiation, selection, pin, and authentication-gate tests."""

from __future__ import annotations

import copy
import base64
import hashlib
import io
import os
import tarfile
import tempfile
from pathlib import Path
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_runtime.config as runtime_config
from agent_runtime.adapters.codex import CodexAppServerAdapter, CodexProbeError, _load_lock, _protocol_digest
from agent_runtime.capabilities import CapabilityError, codex_descriptor, negotiate
from agent_runtime.config import ConfigError, resolve_selection
from agent_runtime.supervisor import _error, _preflight_code
from agent_runtime_testlib import make_task
from scripts.agent_runtime import _verify_package_tarball

FAILURES = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def fails(call, exception=Exception):
    try:
        call()
    except exception:
        return True
    return False


def main():
    selection = resolve_selection("triage.issue.local", "lavish-axi")
    runtime = __import__("agent_runtime.config", fromlist=["load_runtime_config"]).load_runtime_config()
    check("selection: Claude is named production primary", runtime["primary_profile"] == "claude-action-current-pinned")
    check("selection: current production target resolves as Claude", selection["mode"] == "claude")
    check("selection: non-repair action remains on pinned action", selection["profile"]["adapter"] == "claude-action-compat")
    check("selection: fallback disabled", selection["fallback"] == "none")
    check("selection: unsupported emergency provider override rejected", fails(lambda: resolve_selection("triage.issue.local", emergency="codex"), ConfigError))
    check("selection: former legacy override rejected", fails(lambda: resolve_selection("triage.issue.local", emergency="legacy"), ConfigError))
    check("selection: Codex absent from active profiles", "codex-subscription-pinned" not in runtime["profiles"])
    schema_actions = {"triage.schema-repair", "nl-decision.schema-repair"}
    direct = {action: resolve_selection(action) for action in schema_actions}
    pinned = {action: resolve_selection(action) for action in runtime_config.ACTIONS - schema_actions}
    check("selection: both schema-repair actions use direct Claude CLI", all(row["profileName"] == "claude-cli-pinned" and row["profile"]["adapter"] == "claude-cli" for row in direct.values()))
    check("selection: every other action remains on pinned action", len(pinned) == 8 and all(row["profileName"] == "claude-action-current-pinned" and row["profile"]["adapter"] == "claude-action-compat" for row in pinned.values()))
    check("selection: Codex recorded only as disabled adapter evidence", runtime["disabled_adapters"] == {"codex-app-server": "unsupported-public-chatgpt-pro-auth"})
    check("selection: activation is limited to the complete schema-repair profile", runtime["production_activation"] == {action: "claude-cli-pinned" for action in schema_actions} and runtime["temporary_rollback_profile"] is None and "codex_auth_gate" not in runtime)
    check("selection: every action remains on Claude", all(row["target"] == "claude" for row in runtime["actions"].values()))
    core = Path("agent_runtime/config.py").read_text(encoding="utf-8")
    check("selection: core contains no OpenCode or Z.AI policy", "OpenCode" not in core and "Z.AI" not in core and "glm" not in core.lower())
    invalid_target = copy.deepcopy(runtime)
    invalid_target["actions"]["triage.issue.local"]["target"] = "codex"
    with mock.patch.object(runtime_config, "load_runtime_config", return_value=invalid_target):
        check("selection: any Codex-targeted action invalidates configuration", fails(lambda: resolve_selection("triage.issue.local"), ConfigError))
    reachable_direct = copy.deepcopy(runtime)
    reachable_direct["production_activation"]["triage.issue.local"] = "claude-cli-pinned"
    with mock.patch.object(runtime_config, "load_runtime_config", return_value=reachable_direct):
        check("selection: direct Claude CLI cannot reach another action", fails(lambda: resolve_selection("triage.issue.local"), ConfigError))
    split_profile = copy.deepcopy(runtime)
    del split_profile["production_activation"]["nl-decision.schema-repair"]
    with mock.patch.object(runtime_config, "load_runtime_config", return_value=split_profile):
        check("selection: schema-repair profile cannot be split", fails(lambda: resolve_selection("triage.schema-repair"), ConfigError))
    rollback = copy.deepcopy(runtime)
    rollback["temporary_rollback_profile"] = "claude-action-current-pinned"
    with mock.patch.object(runtime_config, "load_runtime_config", return_value=rollback):
        check("selection: one rollback setting restores both schema repairs", all(resolve_selection(action)["profile"]["adapter"] == "claude-action-compat" for action in schema_actions))
        check("selection: rollback leaves the other eight pinned", all(resolve_selection(action)["profile"]["adapter"] == "claude-action-compat" for action in runtime_config.ACTIONS - schema_actions))

    with tempfile.TemporaryDirectory() as directory:
        task, _, _ = make_task(Path(directory), "triage.issue.local")
        candidate = task["spec"]["selection"]["candidates"][0]
        candidate.update(adapter="codex-app-server", harness="codex-cli", provider="openai", authProfile="codex-subscription", authMechanism="codex-access-token", expectedWorkspaceId="workspace", model="gpt-test-pinned")
        descriptor = codex_descriptor("0.144.0", "a" * 64, "b" * 64)
        host = {"externalSandbox": True, "networkProxy": True, "denyHostHome": True, "processGroupCleanup": True}
        proof = negotiate(task, descriptor, host)
        check("capability: complete exact proof accepted", proof.proof["fallback"] == "none")
        check("capability: exact typed tools retained", proof.proof["exactTools"] == ["fs.read", "fs.grep", "fs.glob"])
        check("capability: native strict schema selected", proof.proof["structuredOutputMechanism"] == "native-schema")
        check("capability: generic limit enforcement survives negotiation", proof.proof["limitEnforcement"] == task["spec"]["limits"]["enforcement"])

        bad = copy.deepcopy(task)
        bad["spec"]["selection"]["fallback"]["mode"] = "declared"
        check("capability: non-none fallback rejected before spend", fails(lambda: negotiate(bad, descriptor, host), CapabilityError))
        bad = copy.deepcopy(task)
        bad["spec"]["selection"]["candidates"][0]["allowModelAlias"] = True
        check("capability: model alias rejected", fails(lambda: negotiate(bad, descriptor, host), CapabilityError))
        weak_host = dict(host, networkProxy=False)
        check("capability: missing provider-only network proof rejected", fails(lambda: negotiate(task, descriptor, weak_host), CapabilityError))
        weak_host = dict(host, denyHostHome=False)
        check("capability: host home exposure rejected", fails(lambda: negotiate(task, descriptor, weak_host), CapabilityError))
        bad = copy.deepcopy(task)
        bad["spec"]["tools"]["tools"].append({"name": "final.triage", "version": 1, "maxResultBytes": 10, "inputSchemaSha256": "a" * 64, "terminating": True})
        check("capability: undeclared tool power rejected", fails(lambda: negotiate(bad, descriptor, host), CapabilityError))

        check("auth: missing credential maps to stable preflight error", _preflight_code(CodexProbeError("codex-subscription credential handoff is missing")) == ("auth.missing", "probing"))
        check("auth: invalid credential maps to stable preflight error", _preflight_code(CodexProbeError("codex-subscription auth profile is invalid")) == ("auth.invalid", "probing"))

        credential = Path(directory) / "credential"
        credential.write_text("at-not-a-real-token-for-metadata-test", encoding="utf-8")
        credential.chmod(0o600)
        adapter = CodexAppServerAdapter()
        with mock.patch.dict(os.environ, {"WHEELHOUSE_CODEX_CREDENTIAL_FILE": str(credential), "OPENAI_API_KEY": "forbidden"}, clear=False):
            check("auth: ambient Platform API key rejected", fails(lambda: adapter.probe(task), CodexProbeError))
        with mock.patch.dict(os.environ, {"WHEELHOUSE_CODEX_CREDENTIAL_FILE": str(credential), "CODEX_ACCESS_TOKEN": "forbidden"}, clear=False):
            check("auth: ambient access token rejected outside named handoff", fails(lambda: adapter.probe(task), CodexProbeError))
        credential.chmod(0o644)
        with mock.patch.dict(os.environ, {"WHEELHOUSE_CODEX_CREDENTIAL_FILE": str(credential)}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("CODEX_ACCESS_TOKEN", None)
            check("auth: broad credential file permissions rejected", fails(lambda: adapter.probe(task), CodexProbeError))

    check("retry: provider overload is classified retryable", _error("provider.overloaded", "overloaded")["retryable"] is True)
    check("retry: auth failure is never fallback eligible", _error("auth.invalid", "invalid")["fallbackEligible"] is False)
    check("retry: delivered schema failure is never fallback eligible", _error("output.schema_invalid", "invalid")["fallbackEligible"] is False)
    check("retry: policy failure is never fallback eligible", _error("sandbox.violation", "violation")["fallbackEligible"] is False)

    lock = _load_lock()
    digest = _protocol_digest(lock)
    check("pins: Codex CLI version exact", lock["codex"]["binaryVersion"] == "0.144.0")
    check("pins: source commit exact", lock["codex"]["sourceCommit"] == "767822446c7a594caa19609ca435281a9ec67e0d")
    check("pins: protocol schema bundle verifies", len(digest) == 64)
    check("pins: account metadata request schema included", "GetAccountParams.json" in lock["codex"]["protocolSchemas"])
    check("pins: account metadata response schema included", "GetAccountResponse.json" in lock["codex"]["protocolSchemas"])
    check("pins: initialization request and response schemas included", {"InitializeParams.json", "InitializeResponse.json"}.issubset(lock["codex"]["protocolSchemas"]))
    check("pins: quota response schema included", "GetAccountRateLimitsResponse.json" in lock["codex"]["protocolSchemas"])
    check("pins: thread and turn response schemas included", {"ThreadStartResponse.json", "TurnStartResponse.json"}.issubset(lock["codex"]["protocolSchemas"]))
    check("pins: executable package identities committed", lock["codex"]["linuxX64BinaryPackage"] == "@openai/codex@0.144.0-linux-x64" and lock["codex"]["linuxArm64BinaryPackage"] == "@openai/codex@0.144.0-linux-arm64")
    with tempfile.TemporaryDirectory() as directory:
        package = Path(directory) / "package.tgz"
        with tarfile.open(package, "w:gz") as archive:
            payload = b'{"name":"fixture"}'
            member = tarfile.TarInfo("package/package.json")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(package.read_bytes()).digest()).decode("ascii")
        check("pins: exact verified tarball bytes accepted", _verify_package_tarball(str(package), integrity))
        package.write_bytes(package.read_bytes() + b"tampered")
        check("pins: changed tarball bytes rejected", not _verify_package_tarball(str(package), integrity))

        unsafe = Path(directory) / "unsafe.tgz"
        with tarfile.open(unsafe, "w:gz") as archive:
            payload = b"escape"
            member = tarfile.TarInfo("package/../escape")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        unsafe_integrity = "sha512-" + base64.b64encode(hashlib.sha512(unsafe.read_bytes()).digest()).decode("ascii")
        check("pins: unsafe verified archive rejected", not _verify_package_tarball(str(unsafe), unsafe_integrity))

    worker = Path("agent_runtime/worker.py").read_text(encoding="utf-8")
    check("auth: forced ChatGPT login configured", 'forced_login_method = "chatgpt"' in worker)
    check("auth: expected workspace restriction configured", "forced_chatgpt_workspace_id" in worker)
    check("auth: account/read never proactively refreshes", '"refreshToken": False' in worker)
    check("auth: app-server account type checked", 'get("type") != "chatgpt"' in worker)
    check("auth: eligible access-token plan checked", 'planType' in worker and 'business' in worker and 'enterprise' in worker)
    check("auth: internal chatgptAuthTokens path absent", "chatgptAuthTokens" not in worker)
    check("auth: Platform API credentials absent from worker environment", '"OPENAI_API_KEY"' not in worker and '"CODEX_API_KEY"' not in worker)

    if FAILURES:
        raise SystemExit("%d agent runtime capability checks failed" % len(FAILURES))
    print("\nall agent runtime capability tests passed")


if __name__ == "__main__":
    main()
