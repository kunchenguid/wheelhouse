#!/usr/bin/env python3
"""Capability negotiation, selection, pin, and authentication-gate tests."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.adapters.codex import CodexAppServerAdapter, CodexProbeError, _load_lock, _protocol_digest
from agent_runtime.capabilities import CapabilityError, codex_descriptor, negotiate
from agent_runtime.config import ConfigError, resolve_selection
from agent_runtime.supervisor import _error, _preflight_code
from agent_runtime.contract import canonical_sha256
from agent_runtime_testlib import make_task

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
    check("selection: Codex is named primary", __import__("agent_runtime.config", fromlist=["load_runtime_config"]).load_runtime_config()["primary_profile"] == "codex-subscription-pinned")
    check("selection: current public production remains explicit legacy", selection["mode"] == "legacy")
    check("selection: direct Claude bridge is explicit profile", selection["profile"]["adapter"] == "claude-action-compat")
    check("selection: fallback disabled", selection["fallback"] == "none")
    check("selection: unsupported emergency provider override rejected", fails(lambda: resolve_selection("triage.issue.local", emergency="codex"), ConfigError))

    runtime = __import__("agent_runtime.config", fromlist=["load_runtime_config"]).load_runtime_config()
    gate = runtime["codex_auth_gate"]
    check("auth audit: Pro plus public topology recorded unavailable", gate["audit_status"] == "unavailable-pro-public")
    check("auth audit: no captain alternative inferred", gate["captain_alternative"] == "none")
    check("auth audit: private credential boundary not invented", gate["private_credential_boundary"] is False)
    check("auth audit: nonproduction proof not fabricated", gate["nonproduction_proof"] == "not-run")
    check("auth audit: production activation false", runtime["production_activation"] is False)
    check("automerge: alternate PR verdict semantic gate remains pending", gate["pr_automerge_semantic_parity"] == "pending")
    check("automerge: selection code requires separate approval", "pr_automerge_semantic_parity" in Path("agent_runtime/config.py").read_text(encoding="utf-8"))

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
    cli = str(Path(__file__).resolve().parents[1] / "scripts" / "agent_runtime.py")
    package_args = [sys.executable, cli, "verify-package", "--package", lock["codex"]["npmPackage"], "--integrity", lock["codex"]["npmPackageIntegrity"], "--platform", "linux-x64", "--platform-package", "@openai/codex@0.144.0-linux-x64", "--platform-integrity", lock["codex"]["linuxX64BinaryPackageIntegrity"]]
    valid_package = subprocess.run(package_args, capture_output=True, text=True, check=False)
    check("pins: exact npm and executable platform package integrity accepted", valid_package.returncode == 0)
    invalid_package = subprocess.run(package_args[:-1] + ["sha512-wrong"], capture_output=True, text=True, check=False)
    check("pins: executable platform package integrity mismatch rejected", invalid_package.returncode != 0)

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
