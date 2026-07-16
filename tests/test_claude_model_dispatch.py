#!/usr/bin/env python3
"""Deterministic controller discovery and cancellation regressions."""

from __future__ import annotations

from unittest import mock
import json
import subprocess
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
        check("dispatch: missing correlation lookup is bounded", dispatch.discover_run("owner/repo", "marker", "main", "2026-01-01T00:00:00Z", 30) is None)

    with mock.patch.object(dispatch.signal, "signal"):
        try:
            dispatch.terminate_parent(15, None)
        except dispatch.ParentCancelled:
            check("dispatch: SIGTERM enters shared bounded cleanup", True)
        else:
            check("dispatch: SIGTERM enters shared bounded cleanup", False)

    with mock.patch.object(dispatch.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "denied")), mock.patch.object(dispatch, "run", return_value=json.dumps({"status": "completed", "conclusion": "success"})):
        outcome = dispatch.cancel_and_wait("42")
    check("dispatch: failed cancel plus natural success is not confirmed cancellation", outcome["requestStatus"] == "failed" and outcome["requestReturnCode"] == 1 and outcome["terminalConclusion"] == "success" and outcome["cancellationConfirmed"] is False)

    with mock.patch.object(dispatch.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), mock.patch.object(dispatch, "run", return_value=json.dumps({"status": "completed", "conclusion": "failure"})):
        outcome = dispatch.cancel_and_wait("43")
    check("dispatch: accepted cancel plus natural failure is not confirmed cancellation", outcome["requestStatus"] == "accepted" and outcome["terminalConclusion"] == "failure" and outcome["cancellationConfirmed"] is False)

    with mock.patch.object(dispatch.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), mock.patch.object(dispatch, "run", return_value=json.dumps({"status": "completed", "conclusion": "cancelled"})):
        outcome = dispatch.cancel_and_wait("44")
    check("dispatch: only terminal cancelled confirms cancellation", outcome["requestStatus"] == "accepted" and outcome["terminalConclusion"] == "cancelled" and outcome["cancellationConfirmed"] is True)

    source = Path("scripts/claude_model_dispatch.py").read_text(encoding="utf-8")
    check("dispatch: end-to-end hard deadline is not claimed", '"hardDeadlineMs": None' in source)
    check("dispatch: correlation and child timeout limits are separate", '"dispatchDeadlineMs"' in source and '"childExecutionTimeoutMs"' in source)
    check("dispatch: command and final discovery operations are bounded", "COMMAND_TIMEOUT_SECONDS" in source and "dispatch_deadline" in source and "while not run_id" not in source)
    check(
        "dispatch: only controller failures may accept natural late completion",
        'termination_reason == "controller-failure"' in source,
    )

    if FAILURES:
        raise SystemExit("%d Claude dispatch checks failed" % len(FAILURES))
    print("\nall Claude dispatch tests passed")


if __name__ == "__main__":
    main()
