#!/usr/bin/env python3
"""Missing result, malformed result, cancellation, timeout, and cleanup tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.contract import atomic_write_json, canonical_sha256, load_json_regular, validate_contract
from agent_runtime.supervisor import _anchor_ok, run
from agent_runtime.worker import RuntimeBudget, WorkerFailure, _bounded_output_schema
from agent_runtime_testlib import default_final, environment, make_task

FAILURES = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def execute(root: Path, script, limits=None):
    task, bundle, script_path = make_task(root, "triage.issue.local", script=script)
    if limits:
        task["spec"]["limits"].update(limits)
        atomic_write_json(bundle / "task.json", task)
    result_path = bundle / "result.json"
    events_path = bundle / "events.ndjson"
    with environment(WHEELHOUSE_AGENT_TEST_SANDBOX="1", WHEELHOUSE_FAKE_ADAPTER_SCRIPT=str(script_path)):
        result = run(str(bundle / "task.json"), str(bundle), str(result_path), str(events_path))
    validate_contract(result, "AgentResult")
    return result, result_path, events_path, bundle


def main():
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        result, path, events, bundle = execute(base / "success", {})
        check("lifecycle: fake adapter succeeds", result["status"] == "succeeded")
        check("lifecycle: final atomically delivered", path.is_file() and result.get("final"))
        check("lifecycle: final independent from transcript retention", result["final"]["value"] and result["artifacts"][0]["role"] == "normalized-events")
        check("lifecycle: raw transcript discarded", not list(bundle.rglob("*transcript*")))
        check("lifecycle: normalized terminal emitted once", events.read_text().count('"type":"execution.completed"') == 1)
        check("provenance: observed provider retained", result["selection"]["actualProvider"] == "fake-provider")

        task = load_json_regular(bundle / "task.json")
        with mock.patch.object(Path, "read_text", side_effect=OSError("fixture read failure")):
            check("output: unreadable evidence target fails closed", _anchor_ok(default_final("triage.issue.local"), task, bundle) is False)

        invalid = {
            "summary": "missing most fields",
        }
        result, _, _, _ = execute(base / "schema", {"final": invalid})
        check("output: malformed schema fails", result["error"]["code"] == "output.schema_invalid")
        check("output: delivered candidate remains independent for bounded repair", result["delivered"]["value"] == invalid)
        check("output: malformed candidate never becomes final", "final" not in result)

        result, _, _, _ = execute(base / "post-spend-validation", {"nonCanonicalFinal": True, "spendStarted": True})
        check("output: post-spend validation failure is not rewritten as rejection", result["status"] == "failed" and result["error"]["spendStarted"] is True)
        check("output: post-spend usage provenance is preserved", result["usage"]["providerRequests"] == 1 and result["usage"]["inputTokens"] == 10)
        check("output: non-canonical repair candidate remains bounded", isinstance(result["delivered"]["value"], str) and '"score":1.5' in result["delivered"]["value"])

        result, _, _, _ = execute(base / "missing", {"final": None})
        check("output: missing final classified stably", result["error"]["code"] == "output.missing")
        check("output: missing final has no delivered candidate", "delivered" not in result)

        secret_final = {
            "summary": "github_pat_abcdefghijklmnopqrstuvwxyz123456",
            "product_implications": "p",
            "recommended_action": "hold",
            "recommended_reason": "r",
            "evidence": 'target.txt: "fixture evidence anchor text for runtime tests"',
        }
        result, _, _, _ = execute(base / "secret-final", {"final": secret_final})
        check("output: secret-like final fails closed", result["error"]["code"] == "sandbox.violation")
        check("output: rejected secret is not retained as delivered", "delivered" not in result)

        result, _, _, _ = execute(base / "malformed-worker", {"malformedResult": True})
        check("output: malformed atomic worker result classified", result["error"]["code"] == "harness.protocol")

        result, _, _, _ = execute(base / "non-object-worker", {"nonObjectResult": True})
        check("output: non-object worker result fails closed", result["status"] == "failed" and result["error"]["code"] == "harness.protocol" and "final" not in result)
        check("output: non-object result preserves checkpoint spend", result["error"]["spendStarted"] is True and result["usage"]["providerRequests"] == 2)
        check("output: non-object result preserves checkpoint provenance", result["selection"]["actualModel"] == "fake-model" and result["usage"]["inputTokens"] == 12)

        result, _, events, _ = execute(base / "truncated-events", {"truncatedEvents": True, "nonObjectResult": True})
        check("events: truncated UTF-8 cannot erase checkpoint-backed failure", result["status"] == "failed" and result["error"]["spendStarted"] is True and result["usage"]["providerRequests"] == 2)
        check("events: truncated diagnostics emit content-free warning", "adapter-events-unavailable" in events.read_text(encoding="utf-8"))

        original_open = Path.open

        def fail_adapter_events(path, *args, **kwargs):
            if path.name == "adapter-events.ndjson" and kwargs.get("encoding") == "utf-8":
                raise OSError("fixture event read failure")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", fail_adapter_events):
            result, _, events, _ = execute(base / "unreadable-events", {"nonObjectResult": True})
        check("events: OSError cannot erase checkpoint-backed failure", result["status"] == "failed" and result["error"]["spendStarted"] is True and result["usage"]["providerRequests"] == 2)
        check("events: unreadable diagnostics emit content-free warning", "adapter-events-unavailable" in events.read_text(encoding="utf-8"))

        with mock.patch("agent_runtime.supervisor._read_worker_events", side_effect=RecursionError("fixture event nesting")):
            result, _, _, _ = execute(base / "event-ingestion-exception", {"nonObjectResult": True})
        check("events: escaping ingestion failure remains post-spend", result["status"] == "failed" and result["error"]["spendStarted"] is True)
        check("events: escaping ingestion failure preserves checkpoint usage", result["usage"]["providerRequests"] == 2 and result["usage"]["inputTokens"] == 12)
        check("events: escaping ingestion failure preserves checkpoint provenance", result["selection"]["actualModel"] == "fake-model" and result["selection"]["actualProvider"] == "fake-provider" and result["selection"]["actualEffort"] == "high")
        check("events: escaping ingestion failure preserves negotiated proof", result["proof"]["sandboxPolicySha256"] != canonical_sha256({"status": "not-started"}))
        check("events: escaping ingestion failure preserves attempt timing", result["usage"]["durationMs"] > 0 and result["startedAt"] < result["completedAt"])

        stale_root = base / "stale-checkpoint"
        stale_task, stale_bundle, stale_script = make_task(stale_root, "triage.issue.local", script={})
        stale_task["spec"]["selection"]["candidates"][0]["adapter"] = "unavailable"
        atomic_write_json(stale_bundle / "task.json", stale_task)
        stale_output = stale_bundle / "output"
        stale_output.mkdir()
        atomic_write_json(
            stale_output / "worker-state.json",
            {
                "executionId": stale_task["metadata"]["executionId"],
                "requestSha256": canonical_sha256(stale_task),
                "attemptId": "stale-attempt",
                "spendStarted": True,
                "actualModel": "stale-model",
                "actualProvider": "stale-provider",
                "actualEffort": "stale-effort",
                "usage": {"providerRequests": 9},
            },
        )
        with environment(WHEELHOUSE_AGENT_TEST_SANDBOX="1", WHEELHOUSE_FAKE_ADAPTER_SCRIPT=str(stale_script)):
            stale_result = run(str(stale_bundle / "task.json"), str(stale_bundle), str(stale_bundle / "result.json"), str(stale_bundle / "events.ndjson"))
        check("recovery: stale checkpoint cannot reclassify preflight failure", stale_result["status"] == "rejected" and stale_result["error"]["spendStarted"] is False and stale_result["usage"]["providerRequests"] == 0)

        fast = {"softDeadlineMs": 1000, "hardDeadlineMs": 1800, "cancelGraceMs": 200}
        result, _, events, _ = execute(base / "cancel", {"sleepMs": 5000}, fast)
        check("cancel: soft deadline requests adapter-native cancel", result["status"] == "cancelled" and result["error"]["code"] == "lifecycle.cancelled")
        check("cancel: normalized cancellation event emitted", "cancellation.requested" in events.read_text())

        result, _, _, _ = execute(base / "timeout", {"hang": True, "ignoreCancel": True}, fast)
        check("timeout: ignored cancel receives process-group SIGTERM", result["error"]["code"] == "lifecycle.timeout")

        hard = {"softDeadlineMs": 1000, "hardDeadlineMs": 1500, "cancelGraceMs": 200}
        result, _, _, _ = execute(base / "hard-kill", {"hang": True, "ignoreCancel": True, "ignoreTerm": True}, hard)
        check("timeout: ignored SIGTERM receives hard kill", result["error"]["code"] == "lifecycle.hard_kill")

        result, _, _, _ = execute(base / "crash", {"crash": True})
        check("crash: no guessed final after adapter crash", result["error"]["code"] == "harness.crash" and "final" not in result)

        turn_budget = RuntimeBudget({"maxTurns": 1, "maxProviderRequests": 2, "maxInputTokens": 20, "maxOutputTokens": 10})
        turn_budget.begin_provider_request()
        try:
            turn_budget.begin_provider_request()
            turn_limited = False
        except WorkerFailure:
            turn_limited = True
        check("limits: turn budget enforced before another provider request", turn_limited and turn_budget.provider_requests == 1)

        request_budget = RuntimeBudget({"maxTurns": 2, "maxProviderRequests": 1, "maxInputTokens": 20, "maxOutputTokens": 10})
        request_budget.begin_provider_request()
        try:
            request_budget.begin_provider_request()
            request_limited = False
        except WorkerFailure:
            request_limited = True
        check("limits: provider-request budget enforced externally", request_limited and request_budget.provider_requests == 1)

        token_budget = RuntimeBudget({"maxTurns": 2, "maxProviderRequests": 2, "maxInputTokens": 20, "maxOutputTokens": 10})
        try:
            token_budget.observe_tokens({"total": {"inputTokens": 21, "outputTokens": 4}})
            input_limited = False
        except WorkerFailure:
            input_limited = True
        try:
            token_budget.observe_tokens({"total": {"inputTokens": 20, "outputTokens": 11}})
            output_limited = False
        except WorkerFailure:
            output_limited = True
        check("limits: observed input-token budget enforced", input_limited)
        check("limits: observed output-token budget enforced", output_limited)

        continuation_budget = RuntimeBudget({"maxTurns": 3, "maxProviderRequests": 3, "maxInputTokens": 20, "maxOutputTokens": 10})
        continuation_budget.begin_provider_request()
        try:
            continuation_budget.begin_provider_request({"total": {"inputTokens": 20, "outputTokens": 9}})
            continuation_limited = False
        except WorkerFailure:
            continuation_limited = True
        check("limits: continuation stops at observed token ceiling", continuation_limited and continuation_budget.provider_requests == 1 and continuation_budget.turns == 1)

        schema = {"type": "object", "properties": {"summary": {"type": "string"}, "nested": {"anyOf": [{"type": "string", "maxLength": 50}, {"type": "null"}]}}}
        bounded_schema = _bounded_output_schema(schema, 10)
        check("limits: native output schema carries conservative string ceilings", bounded_schema["properties"]["summary"]["maxLength"] == 10 and bounded_schema["properties"]["nested"]["anyOf"][0]["maxLength"] == 10)
        check("limits: output schema source remains immutable", "maxLength" not in schema["properties"]["summary"])

    if FAILURES:
        raise SystemExit("%d agent runtime lifecycle checks failed" % len(FAILURES))
    print("\nall agent runtime lifecycle tests passed")


if __name__ == "__main__":
    main()
