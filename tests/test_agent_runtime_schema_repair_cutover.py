#!/usr/bin/env python3
"""Synthetic end-to-end proof for the direct schema-repair production profile."""

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

from agent_runtime.adapters.claude import ClaudeCliAdapter, _load_lock
from agent_runtime.capabilities import negotiate
from agent_runtime.config import resolve_selection
from agent_runtime.contract import ContractError, atomic_write_json, file_sha256, load_json_regular, verify_result_binding
from agent_runtime.supervisor import _validate_worker, write_controller_failure_result, write_direct_install_failure_result
from agent_runtime.task_builder import build_task
from agent_runtime.worker import InternalEvents, _run_claude

FAILURES: list[str] = []
TOKEN = "synthetic-oauth-canary-value-123456789"
WHEELHOUSE_REVISION = "30271b6907e568419cdc48694a11b0c2f699b433"


def check(name: str, condition: bool) -> None:
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def build_direct_task(root: Path) -> tuple[dict, Path]:
    prompt = root / "prompt.txt"
    prompt.write_text(
        "Repair this malformed candidate: {summary: missing required fields}. Return only the strict repaired object.\n",
        encoding="utf-8",
    )
    bundle = root / "bundle"
    task = build_task(
        action="triage.schema-repair",
        selection=resolve_selection("triage.schema-repair", "repo"),
        prompt_path=str(prompt),
        bundle_dir=str(bundle),
        output_path=str(bundle / "task.json"),
        owner="owner",
        repo="repo",
        number=7,
        target_kind="schema-repair",
        revision="fixture-revision-1",
        wheelhouse_revision=WHEELHOUSE_REVISION,
        event_key="a" * 64,
        repair_kind="issue",
    )
    return task, bundle


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        task, bundle = build_direct_task(root)
        schema_path = bundle / task["spec"]["output"]["schemaArtifact"]
        prompt_path = bundle / task["spec"]["prompt"]["userArtifact"]
        credential = root / "credential"
        credential.write_text(TOKEN, encoding="utf-8")
        credential.chmod(0o600)
        binary = root / "claude"
        repaired = {
            "summary": "Repaired bounded candidate.",
            "product_implications": "No product behavior changes.",
            "recommended_action": "hold",
            "recommended_reason": "Synthetic schema proof.",
            "evidence": "The malformed candidate omitted required fields.",
        }
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "if '--version' in sys.argv:\n"
            "    print('2.1.197 (Claude Code)')\n"
            "    raise SystemExit(0)\n"
            "assert os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')\n"
            "assert not os.environ.get('GITHUB_TOKEN')\n"
            "assert '--fallback-model' not in sys.argv\n"
            "assert sys.stdin.read()\n"
            "print(json.dumps({'type':'system','subtype':'init','model':'claude-sonnet-4-6'}, separators=(',', ':')))\n"
            "print(json.dumps({'type':'result','subtype':'success','structured_output':"
            + repr(repaired)
            + ",'usage':{'input_tokens':9,'output_tokens':7}}, separators=(',', ':')))\n",
            encoding="utf-8",
        )
        binary.chmod(0o700)
        lock = copy.deepcopy(_load_lock())
        lock["claude"]["platforms"]["synthetic"] = {
            "url": "https://downloads.claude.ai/fixture",
            "sha256": file_sha256(binary),
        }
        adapter = ClaudeCliAdapter()
        probe_environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "WHEELHOUSE_CLAUDE_CREDENTIAL_FILE": str(credential),
        }
        with (
            mock.patch("agent_runtime.adapters.claude.shutil.which", return_value=str(binary)),
            mock.patch("agent_runtime.adapters.claude._platform_key", return_value="synthetic"),
            mock.patch("agent_runtime.adapters.claude._load_lock", return_value=lock),
            mock.patch.dict(os.environ, probe_environment, clear=True),
        ):
            probe = adapter.probe(task, schema_path.read_bytes())
        host = {
            "implementation": "bubblewrap-network-namespace-v1",
            "externalSandbox": True,
            "networkProxy": True,
            "denyHostHome": True,
            "processGroupCleanup": True,
        }
        negotiated = negotiate(task, probe.descriptor.value, host)
        plan = adapter.compile(task, negotiated.proof, probe)
        plan["attemptId"] = "b" * 32
        plan["claude"]["stdinArtifact"] = str(prompt_path)
        output = root / "worker-output"
        output.mkdir()
        internal_events_path = output / "adapter-events.ndjson"
        events = InternalEvents(internal_events_path, 1024 * 1024)
        runtime_environment = {
            "PATH": str(root) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root / "home"),
            "WHEELHOUSE_AUTH_SOURCE": str(credential),
            "WHEELHOUSE_PROVIDER_SOCKET": str(root / "provider.sock"),
        }
        with mock.patch.dict(os.environ, runtime_environment, clear=True):
            worker = _run_claude(plan, output, events, output / "cancel.request")
        events.close()
        final, delivered, error = _validate_worker(
            task,
            bundle,
            worker,
            negotiated.proof["structuredOutputMechanism"],
        )
        serialized = json.dumps(
            {"plan": plan, "worker": worker, "final": final},
            sort_keys=True,
        ) + internal_events_path.read_text(encoding="utf-8")
        check("synthetic: both schema-repair actions resolve to the direct profile", all(resolve_selection(action)["profile"]["adapter"] == "claude-cli" for action in ("triage.schema-repair", "nl-decision.schema-repair")))
        check("synthetic: malformed candidate yields trusted repaired output", error is None and delivered is not None and final is not None and final["value"] == repaired)
        check("synthetic: native structured output is explicitly revalidated", negotiated.proof["structuredOutputMechanism"] == "native-schema" and {row["name"] for row in final["validation"]} >= {"native-schema", "json-schema"})
        check("synthetic: no tool, shell, or fallback lane is available", plan["tools"]["tools"] == [] and plan["limits"]["maxToolCalls"] == 0 and worker["usage"]["toolCalls"] == 0 and "--fallback-model" not in plan["claude"]["argv"] and "claude-action-compat" not in serialized)
        check("synthetic: exact model and provider are observed", worker["actualModel"] == "claude-sonnet-4-6" and worker["actualProvider"] == "anthropic")
        check("synthetic: OAuth canary is absent from durable data", TOKEN not in serialized)

        binary.write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nraise SystemExit(7)\n",
            encoding="utf-8",
        )
        binary.chmod(0o700)
        failed_worker_output = root / "failed-worker-output"
        failed_plan = root / "failed-plan.json"
        atomic_write_json(failed_plan, plan)
        failed_environment = os.environ.copy()
        failed_environment.update(runtime_environment)
        failed_environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        subprocess.run(
            [sys.executable, "-m", "agent_runtime.worker", "--plan", str(failed_plan), "--output-dir", str(failed_worker_output)],
            env=failed_environment,
            check=True,
            capture_output=True,
            text=True,
        )
        failed_worker = load_json_regular(failed_worker_output / "worker-result.json")
        failed_final, _failed_delivered, failed_error = _validate_worker(
            task,
            bundle,
            failed_worker,
            negotiated.proof["structuredOutputMechanism"],
        )
        check("failure: provider exit is atomically classified without a final", failed_worker["error"]["code"] == "provider.unavailable" and failed_worker["spendStarted"] is True and failed_final is None and failed_error["code"] == "provider.unavailable")
        check("failure: OAuth canary is absent from worker failure records", TOKEN not in json.dumps(failed_worker, sort_keys=True) and TOKEN not in (failed_worker_output / "adapter-events.ndjson").read_text(encoding="utf-8"))

        failure_result = root / "failure-result.json"
        failure_events = root / "failure-events.ndjson"
        failed = write_controller_failure_result(
            str(bundle / "task.json"),
            str(bundle),
            "harness.crash",
            str(failure_result),
            str(failure_events),
        )
        verify_result_binding(task, failed)
        check("failure: direct failure is durable, classified, and non-consumed", failed["status"] == "failed" and failed["error"]["code"] == "harness.crash" and failed["error"]["spendStarted"] is True and "final" not in failed)
        check("failure: action and model fallback stay disabled", failed["selection"]["fallbackUsed"] is False and failed["selection"]["profile"] == "claude-cli-pinned" and failed["proof"]["executionProfile"] == "claude-cli-pinned")
        check("failure: durable record exposes missing provider evidence honestly", failed["selection"]["actualModel"] == "" and failed["proof"]["structuredOutputMechanism"] == "unavailable-after-controller-failure")

        install_failure_result = root / "install-failure-result.json"
        install_failure_events = root / "install-failure-events.ndjson"
        install_failed = write_direct_install_failure_result(
            str(bundle / "task.json"),
            str(bundle),
            str(install_failure_result),
            str(install_failure_events),
        )
        verify_result_binding(task, install_failed)
        check("failure: direct installation failure is durable and pre-spend", install_failed["status"] == "rejected" and install_failed["error"]["code"] == "harness.install_failed" and install_failed["error"]["spendStarted"] is False and install_failed["usage"]["providerRequests"] == 0 and "final" not in install_failed)
        check("failure: installation record never claims child runtime controls", install_failed["proof"]["sandboxImplementation"] == "direct-runtime-install-failed" and install_failed["proof"]["credentialIsolation"] == "not-materialized" and install_failed["proof"]["structuredOutputMechanism"] == "unavailable-before-negotiation")

        mismatched = copy.deepcopy(failed)
        mismatched["selection"]["profile"] = "claude-action-current-pinned"
        mismatched["proof"]["executionProfile"] = "claude-action-current-pinned"
        try:
            verify_result_binding(task, mismatched)
        except ContractError:
            check("binding: consumer rejects an execution-profile mismatch", True)
        else:
            check("binding: consumer rejects an execution-profile mismatch", False)

    if FAILURES:
        raise SystemExit("%d schema-repair cutover checks failed" % len(FAILURES))
    print("\nall schema-repair cutover tests passed")


if __name__ == "__main__":
    main()
