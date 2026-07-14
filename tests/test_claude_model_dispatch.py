#!/usr/bin/env python3
"""Deterministic controller discovery and cancellation regressions."""

from __future__ import annotations

from unittest import mock
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.claude_model_dispatch as dispatch

FAILURES = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def main():
    with mock.patch.object(dispatch, "matching_run", return_value=None), mock.patch.object(dispatch.time, "sleep"), mock.patch.object(dispatch.time, "monotonic", side_effect=[0, 0, 31]):
        check("dispatch: missing correlation lookup is bounded", dispatch.discover_run("owner/repo", "marker", "a" * 40, 30) is None)

    calls = []
    dispatch.STATE.clear()
    dispatch.STATE.update({"dispatched": True, "repo": "owner/repo", "title": "marker", "expected_sha": "a" * 40})
    with mock.patch.object(dispatch, "discover_run", return_value={"id": 7}), mock.patch.object(dispatch, "cancel_and_wait", side_effect=lambda run_id: calls.append(("cancel", run_id))), mock.patch.object(dispatch, "recover_attempt", side_effect=lambda run_id, conclusion, reason: calls.append(("recover", run_id, conclusion, reason))), mock.patch.object(dispatch.signal, "signal"):
        try:
            dispatch.terminate_parent(15, None)
        except SystemExit as error:
            check("dispatch: SIGTERM cancels and checkpoints correlated child", error.code == 143 and calls == [("cancel", "7"), ("recover", "7", "cancelled", "parent-sigterm")])
        else:
            check("dispatch: SIGTERM cancels and checkpoints correlated child", False)

    source = Path("scripts/claude_model_dispatch.py").read_text(encoding="utf-8")
    check("dispatch: hard deadline is the sole enforced Claude runtime limit", '"enforcedLimits": {"hardDeadlineMs": STATE["hard_ms"]}' in source)
    check("dispatch: command and final discovery operations are bounded", "COMMAND_TIMEOUT_SECONDS" in source and "DISCOVERY_GRACE_SECONDS" in source and "while not run_id" not in source)

    if FAILURES:
        raise SystemExit("%d Claude dispatch checks failed" % len(FAILURES))
    print("\nall Claude dispatch tests passed")


if __name__ == "__main__":
    main()
