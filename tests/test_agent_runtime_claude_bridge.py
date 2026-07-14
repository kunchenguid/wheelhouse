#!/usr/bin/env python3
"""Offline Claude Action bridge contract and provenance tests."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.claude_bridge import ACTION_COMMIT, ACTION_VERSION, CLAUDE_CODE_VERSION, IMMUTABLE_MODEL, bridge
from agent_runtime.config import resolve_selection
from agent_runtime.contract import canonical_sha256, file_sha256, validate_contract
from agent_runtime.task_builder import build_task

FAILURES = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def make_bundle(root: Path, action: str = "deep-review.local"):
    root.mkdir(parents=True)
    prompt = root / "prompt.txt"
    target = root / "target.txt"
    prompt.write_text("Return the bounded result.\n", encoding="utf-8")
    target.write_text("fixture target\n", encoding="utf-8")
    bundle = root / "bundle"
    task = build_task(
        action=action,
        selection=resolve_selection(action, "repo"),
        prompt_path=str(prompt),
        bundle_dir=str(bundle),
        output_path=str(bundle / "task.json"),
        owner="owner",
        repo="repo",
        number=7,
        target_kind="pr-review",
        revision="abcdef1",
        wheelhouse_revision="30271b6907e568419cdc48694a11b0c2f699b433",
        target_file=str(target),
    )
    return task, bundle


def transcript(path: Path, model: str, text: str, duration_ms: int = 2500):
    path.write_text(
        json.dumps(
            [
                {"type": "system", "subtype": "init", "model": model},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
                {"type": "result", "subtype": "success", "is_error": False, "result": text, "duration_ms": duration_ms, "num_turns": 2},
            ]
        ),
        encoding="utf-8",
    )


def run_bridge(bundle: Path, execution: Path, suffix: str):
    result = bundle / ("result-%s.json" % suffix)
    events = bundle / ("events-%s.ndjson" % suffix)
    task = json.loads((bundle / "task.json").read_text(encoding="utf-8"))
    enforcement = bundle / ("enforcement-%s.json" % suffix)
    handoff_sha256 = "a" * 64
    enforcement.write_text(json.dumps({"version": 1, "boundary": "separate-read-only-github-job", "jobPermissions": {"actions": "read", "contents": "read", "issues": "none"}, "writeCapableGithubTokenAvailable": False, "fleetTokenAvailable": False, "spendStarted": True, "action": task["metadata"]["action"], "taskSha256": canonical_sha256(task), "handoffManifestSha256": handoff_sha256, "transcriptSha256": file_sha256(execution), "subprocessIsolation": "dependencies-verified", "controller": {"parentRunId": "1", "parentRunAttempt": "1", "modelRunId": "2", "hardDeadlineMs": task["spec"]["limits"]["hardDeadlineMs"], "conclusion": "success"}}), encoding="utf-8")
    value = bridge(str(bundle / "task.json"), str(bundle), str(execution), "", str(enforcement), handoff_sha256, str(result), str(events))
    validate_contract(value, "AgentResult")
    return value, events


def main():
    lock = json.loads(Path("agent_runtime/runtime.lock.json").read_text(encoding="utf-8"))["claudeProduction"]
    check("bridge: action and harness pins match the runtime lock", lock["actionCommit"] == ACTION_COMMIT and lock["actionRelease"] == "v" + ACTION_VERSION and lock["claudeCodeVersion"] == CLAUDE_CODE_VERSION and lock["model"] == IMMUTABLE_MODEL and lock["allowModelAlias"] is False)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        task, bundle = make_bundle(root / "success")
        execution = root / "success.json"
        transcript(execution, IMMUTABLE_MODEL, "HOLD\n\n- Reviewed the bounded target.")
        result, events = run_bridge(bundle, execution, "success")
        check("bridge: immutable Claude task validates", task["spec"]["selection"]["candidates"][0]["allowModelAlias"] is False)
        check("bridge: observed model accepted", result["status"] == "succeeded" and result["selection"]["actualModel"] == IMMUTABLE_MODEL)
        check("bridge: usage remains unavailable when action omits tokens", result["usage"]["inputTokens"] is None and result["usage"]["providerRequests"] is None)
        check("bridge: timing comes from the terminal action event", result["usage"]["durationMs"] == 2500 and result["startedAt"] < result["completedAt"])
        check("bridge: normalized events contain no delivered text", "Reviewed the bounded target" not in events.read_text(encoding="utf-8"))

        _, partial_bundle = make_bundle(root / "partial")
        partial_execution = root / "partial.json"
        partial_execution.write_text(json.dumps([{"type": "system", "subtype": "init", "model": IMMUTABLE_MODEL}, {"type": "assistant", "message": {"content": [{"type": "text", "text": "HOLD"}]}}]), encoding="utf-8")
        partial, _ = run_bridge(partial_bundle, partial_execution, "partial")
        check("bridge: partial assistant output fails closed", partial["status"] == "failed" and partial["error"]["code"] == "harness.protocol" and "delivered" not in partial and "final" not in partial)

        _, duplicate_bundle = make_bundle(root / "duplicate")
        duplicate_execution = root / "duplicate.json"
        duplicate_execution.write_text(json.dumps([{"type": "system", "subtype": "init", "model": IMMUTABLE_MODEL}, {"type": "result", "subtype": "success", "is_error": False, "result": "HOLD", "duration_ms": 10}, {"type": "result", "subtype": "success", "is_error": False, "result": "HOLD", "duration_ms": 10}]), encoding="utf-8")
        duplicate, _ = run_bridge(duplicate_bundle, duplicate_execution, "duplicate")
        check("bridge: duplicate terminal results fail closed", duplicate["status"] == "failed" and duplicate["error"]["code"] == "harness.protocol" and "final" not in duplicate)

        _, mismatch_bundle = make_bundle(root / "mismatch")
        mismatch_execution = root / "mismatch.json"
        transcript(mismatch_execution, "claude-substituted-model", "HOLD")
        mismatch, _ = run_bridge(mismatch_bundle, mismatch_execution, "mismatch")
        check("bridge: observed model substitution fails closed", mismatch["status"] == "failed" and mismatch["error"]["code"] == "model.mismatch" and "final" not in mismatch)

        _, unobserved_bundle = make_bundle(root / "unobserved")
        unobserved_execution = root / "unobserved.json"
        unobserved_execution.write_text(json.dumps([{"type": "result", "is_error": False, "result": "HOLD"}]), encoding="utf-8")
        unobserved, _ = run_bridge(unobserved_bundle, unobserved_execution, "unobserved")
        check("bridge: missing observed model fails closed", unobserved["status"] == "failed" and unobserved["error"]["code"] == "model.mismatch" and not unobserved["selection"]["actualModel"])

        _, malformed_bundle = make_bundle(root / "malformed")
        malformed_execution = root / "malformed.json"
        malformed_execution.write_text("{malformed", encoding="utf-8")
        malformed, _ = run_bridge(malformed_bundle, malformed_execution, "malformed")
        check("bridge: malformed spent execution emits stable failure", malformed["status"] == "failed" and malformed["error"]["code"] == "harness.protocol" and malformed["error"]["spendStarted"] is True)

        _, empty_bundle = make_bundle(root / "empty")
        empty_execution = root / "empty.json"
        empty_execution.write_text("[]", encoding="utf-8")
        empty, empty_events = run_bridge(empty_bundle, empty_execution, "empty")
        check("bridge: empty transcript preserves spend agreement", empty["error"]["spendStarted"] is True and '"spendStarted":true' in empty_events.read_text(encoding="utf-8"))

        _, overflow_bundle = make_bundle(root / "overflow")
        overflow_execution = root / "overflow.json"
        transcript(overflow_execution, IMMUTABLE_MODEL, "HOLD", 10**100)
        overflow, _ = run_bridge(overflow_bundle, overflow_execution, "overflow")
        check("bridge: oversized duration emits stable protocol failure", overflow["status"] == "failed" and overflow["error"]["code"] == "harness.protocol")

    if FAILURES:
        raise SystemExit("%d Claude bridge checks failed" % len(FAILURES))
    print("\nall Claude bridge tests passed")


if __name__ == "__main__":
    main()
