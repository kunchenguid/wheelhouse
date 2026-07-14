#!/usr/bin/env python3
"""Deterministic Claude model dispatcher cleanup checks."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import claude_model_dispatch as dispatch

FAILURES = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def arguments():
    return argparse.Namespace(
        input_artifact="input",
        output_artifact="output",
        invocation_id="invocation",
        handoff_sha256="a" * 64,
        dispatch_ref="main",
        expected_sha="b" * 40,
        out="result",
    )


def task():
    return {"spec": {"limits": {"hardDeadlineMs": 1000, "enforcement": {"hardDeadlineMs": "externally-enforced"}}}}


def exercise(initial_error):
    calls = []
    saved = {name: getattr(dispatch, name) for name in ("run", "discover_run", "cancel_and_wait", "recover_attempt")}
    try:
        dispatch.run = lambda *args, **kwargs: (_ for _ in ()).throw(initial_error)
        dispatch.discover_run = lambda *args, **kwargs: {"id": 42}
        dispatch.cancel_and_wait = lambda run_id: calls.append(("cancel", run_id))
        dispatch.recover_attempt = lambda run_id, conclusion, reason: calls.append(("recover", run_id, conclusion, reason))
        try:
            dispatch.supervise(arguments(), task())
        except SystemExit:
            pass
    finally:
        for name, value in saved.items():
            setattr(dispatch, name, value)
    return calls


def main():
    os.environ.update({"GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1"})
    timed_out = exercise(subprocess.TimeoutExpired("gh", 20))
    check("dispatch: timeout after acceptance cancels correlated child", ("cancel", "42") in timed_out)
    check("dispatch: timeout after acceptance recovers checkpoint", ("recover", "42", "failure", "controller-failure") in timed_out)

    cancelled = exercise(dispatch.ParentCancelled())
    check("dispatch: parent cancellation uses shared cleanup", ("cancel", "42") in cancelled)
    check("dispatch: parent cancellation preserves cancelled checkpoint", ("recover", "42", "cancelled", "parent-sigterm") in cancelled)

    saved = {name: getattr(dispatch, name) for name in ("run", "matching_run", "download_artifact", "finalize_proof", "cancel_and_wait", "recover_attempt")}
    saved_sleep = dispatch.time.sleep
    finalized = []
    calls = 0
    try:
        dispatch.run = lambda *args, **kwargs: ""

        def matching(*_args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise json.JSONDecodeError("malformed", "x", 0)
            return {"id": 7, "status": "completed", "conclusion": "success"}

        dispatch.matching_run = matching
        dispatch.download_artifact = lambda *args: True
        dispatch.finalize_proof = lambda *args: finalized.append(args)
        dispatch.cancel_and_wait = lambda *_args: FAILURES.append("dispatch: completed run was cancelled")
        dispatch.recover_attempt = lambda *_args: FAILURES.append("dispatch: completed result used checkpoint recovery")
        dispatch.time.sleep = lambda *_args: None
        dispatch.supervise(arguments(), task())
    finally:
        for name, value in saved.items():
            setattr(dispatch, name, value)
        dispatch.time.sleep = saved_sleep
    check("dispatch: malformed transient poll is retried", calls == 2 and bool(finalized))

    if FAILURES:
        raise SystemExit("%d Claude dispatch checks failed" % len(FAILURES))
    print("\nall Claude dispatcher tests passed")


if __name__ == "__main__":
    main()
