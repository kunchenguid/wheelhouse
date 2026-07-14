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
    return {"spec": {"limits": {"hardDeadlineMs": None, "dispatchDeadlineMs": 1000, "childExecutionTimeoutMs": 60000, "enforcement": {"hardDeadlineMs": "unavailable", "dispatchDeadlineMs": "externally-enforced", "childExecutionTimeoutMs": "externally-enforced"}}}}


def exercise(initial_error):
    calls = []
    saved = {name: getattr(dispatch, name) for name in ("run", "discover_run", "cancel_and_wait", "recover_attempt")}
    try:
        dispatch.run = lambda *args, **kwargs: (_ for _ in ()).throw(initial_error)
        dispatch.discover_run = lambda *args, **kwargs: {"id": 42, "head_sha": "b" * 40, "head_branch": "main"}
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
            return {"id": 7, "status": "completed", "conclusion": "success", "head_sha": "b" * 40, "head_branch": "main"}

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

    saved = {name: getattr(dispatch, name) for name in ("run", "matching_run", "cancel_and_wait", "recover_attempt")}
    mismatch_calls = []
    try:
        dispatch.run = lambda *args, **kwargs: ""
        dispatch.matching_run = lambda *_args: {"id": 8, "status": "queued", "conclusion": None, "head_sha": "c" * 40, "head_branch": "main"}
        dispatch.cancel_and_wait = lambda run_id: mismatch_calls.append(("cancel", run_id))
        dispatch.recover_attempt = lambda run_id, conclusion, reason: mismatch_calls.append(("recover", run_id, conclusion, reason))
        try:
            dispatch.supervise(arguments(), task())
        except SystemExit:
            pass
    finally:
        for name, value in saved.items():
            setattr(dispatch, name, value)
    check("dispatch: branch advance remains correlated by title", ("cancel", "8") in mismatch_calls)
    check("dispatch: SHA mismatch is cancelled and checkpointed", ("recover", "8", "failure", "revision-mismatch") in mismatch_calls)

    saved_run = dispatch.run
    lookup_calls = []
    try:
        def lookup_run(*args, **_kwargs):
            lookup_calls.append(args)
            rows = [{"display_title": "other", "head_branch": "main"}] * 100 if len(lookup_calls) == 1 else [{"display_title": "wanted", "id": 9, "head_sha": "c" * 40, "head_branch": "main"}]
            return json.dumps({"workflow_runs": rows})

        dispatch.run = lookup_run
        found = dispatch.matching_run("owner/repo", "wanted", "main", "2026-01-01T00:00:00Z")
    finally:
        dispatch.run = saved_run
    endpoints = [call[-1] for call in lookup_calls]
    check("dispatch: lookup retains mismatched SHA for trusted validation", found and found["id"] == 9)
    check("dispatch: lookup is branch and time bounded", all("branch=main" in endpoint and "created=%3E%3D2026-01-01" in endpoint for endpoint in endpoints))
    check("dispatch: lookup has a finite API page cap", len(lookup_calls) == dispatch.MAX_LOOKUP_PAGES == 2 and all("--paginate" not in call for call in lookup_calls))

    saved_run = dispatch.run
    lookup_calls = []
    try:
        def first_page_run(*args, **_kwargs):
            lookup_calls.append(args)
            return json.dumps({"workflow_runs": [{"display_title": "wanted", "id": 10, "head_sha": "b" * 40, "head_branch": "main"}] + [{"display_title": "other", "head_branch": "main"}] * 99})

        dispatch.run = first_page_run
        dispatch.matching_run("owner/repo", "wanted", "main", "2026-01-01T00:00:00Z")
    finally:
        dispatch.run = saved_run
    check("dispatch: successful correlation stops API pagination", len(lookup_calls) == 1)

    if FAILURES:
        raise SystemExit("%d Claude dispatch checks failed" % len(FAILURES))
    print("\nall Claude dispatcher tests passed")


if __name__ == "__main__":
    main()
