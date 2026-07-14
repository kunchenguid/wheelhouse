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
from agent_runtime.contract import validate_contract
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


def transcript(path: Path, model: str, text: str):
    path.write_text(
        json.dumps(
            [
                {"type": "system", "subtype": "init", "model": model},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
                {"type": "result", "subtype": "success", "is_error": False, "result": text, "duration_ms": 12, "num_turns": 2},
            ]
        ),
        encoding="utf-8",
    )


def run_bridge(bundle: Path, execution: Path, suffix: str):
    result = bundle / ("result-%s.json" % suffix)
    events = bundle / ("events-%s.ndjson" % suffix)
    value = bridge(str(bundle / "task.json"), str(bundle), str(execution), "", str(result), str(events))
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
        check("bridge: normalized events contain no delivered text", "Reviewed the bounded target" not in events.read_text(encoding="utf-8"))

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

    if FAILURES:
        raise SystemExit("%d Claude bridge checks failed" % len(FAILURES))
    print("\nall Claude bridge tests passed")


if __name__ == "__main__":
    main()
