#!/usr/bin/env python3
"""Task/result/event selection binding regressions."""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.contract import ContractError, verify_result_binding
from agent_runtime.events import EventError, verify_result_event_binding
from agent_runtime_testlib import run_fake

FAILURES = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first_root = root / "first"
        second_root = root / "second"
        first = run_fake(first_root, "nl-decision.local")
        second = run_fake(second_root, "nl-decision.local")
        first_task = __import__("json").loads((first_root / "bundle" / "task.json").read_text(encoding="utf-8"))
        second_task = __import__("json").loads((second_root / "bundle" / "task.json").read_text(encoding="utf-8"))
        first_events = first_root / "bundle" / "events.ndjson"

        verify_result_binding(first_task, first)
        verify_result_event_binding(first, str(first_events))
        check("binding: matching task, result, and terminal event accepted", True)

        try:
            verify_result_binding(second_task, first)
        except ContractError:
            check("binding: schema-valid swapped task/result rejected", True)
        else:
            check("binding: schema-valid swapped task/result rejected", False)

        stale_result = copy.deepcopy(first)
        stale_result["executionId"] = second["executionId"]
        try:
            verify_result_binding(first_task, stale_result)
        except ContractError:
            check("binding: stale execution id rejected", True)
        else:
            check("binding: stale execution id rejected", False)

        swapped_events = second_root / "bundle" / "events.ndjson"
        try:
            verify_result_event_binding(first, str(swapped_events))
        except EventError:
            check("binding: swapped valid event stream rejected", True)
        else:
            check("binding: swapped valid event stream rejected", False)

        lines = first_events.read_text(encoding="utf-8").splitlines()
        first_events.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        try:
            verify_result_event_binding(first, str(first_events))
        except EventError:
            check("binding: missing terminal projection rejected", True)
        else:
            check("binding: missing terminal projection rejected", False)

    receiver = Path(".github/actions/claude-model-result/action.yml").read_text(encoding="utf-8")
    check("binding: receiving boundary verifies task/result/events", "agent_runtime.py verify-result" in receiver and "--task \"$task\" --result \"$result\" --events \"$events\"" in receiver)
    check(
        "binding: multiple results in one consumer use disjoint directories",
        "invocation-id: {required: true}" in receiver
        and "wheelhouse-claude-result-${{ inputs.invocation-id }}" in receiver
        and "github.job" not in receiver,
    )
    check("binding: schema-only final selection was removed", "validate --path \"$result\" --kind AgentResult" not in receiver)

    if FAILURES:
        raise SystemExit("%d Agent Runtime result binding checks failed" % len(FAILURES))
    print("\nall Agent Runtime result binding tests passed")


if __name__ == "__main__":
    main()
