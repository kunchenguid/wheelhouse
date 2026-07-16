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


def cancellation(conclusion="cancelled", request_status="accepted", return_code=0):
    return {
        "requestStatus": request_status,
        "requestReturnCode": return_code,
        "terminalStatus": "completed" if conclusion else "",
        "terminalConclusion": conclusion,
        "cancellationConfirmed": conclusion == "cancelled",
    }


def arguments():
    return argparse.Namespace(
        task="bundle/task.json",
        input_artifact="input",
        output_artifact="output",
        invocation_id="invocation",
        handoff_sha256="a" * 64,
        dispatch_ref="main",
        expected_sha="b" * 40,
        bundle="bundle",
        out="result",
    )


def task():
    return {"spec": {"limits": {"hardDeadlineMs": None, "dispatchDeadlineMs": 1000, "childExecutionTimeoutMs": 60000, "enforcement": {"hardDeadlineMs": "unavailable", "dispatchDeadlineMs": "externally-enforced", "childExecutionTimeoutMs": "externally-enforced"}}}}


def exercise(initial_error):
    calls = []
    saved = {name: getattr(dispatch, name) for name in ("run", "discover_run", "cancel_and_wait", "recover_attempt", "emit_stage")}
    try:
        dispatch.emit_stage = lambda *_args, **_kwargs: None
        dispatch.run = lambda *args, **kwargs: (_ for _ in ()).throw(initial_error)
        dispatch.discover_run = lambda *args, **kwargs: {"id": 42, "head_sha": "b" * 40, "head_branch": "main"}
        dispatch.cancel_and_wait = lambda run_id: (calls.append(("cancel", run_id)), cancellation())[1]
        dispatch.recover_attempt = lambda run_id, conclusion, reason, outcome=None: calls.append(("recover", run_id, conclusion, reason))
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

    saved = {name: getattr(dispatch, name) for name in ("run", "matching_run", "download_artifact", "finalize_proof", "cancel_and_wait", "recover_attempt", "emit_stage")}
    saved_sleep = dispatch.time.sleep
    finalized = []
    calls = 0
    try:
        dispatch.run = lambda *args, **kwargs: ""
        dispatch.emit_stage = lambda *_args, **_kwargs: None

        def matching(*_args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise json.JSONDecodeError("malformed", "x", 0)
            return {"id": 7, "display_title": "ignored", "status": "completed", "conclusion": "success", "head_sha": "b" * 40, "head_branch": "main"}

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

    saved = {name: getattr(dispatch, name) for name in ("run", "matching_run", "cancel_and_wait", "recover_attempt", "write_revision_mismatch_result", "output", "emit_stage")}
    mismatch_calls = []
    try:
        dispatch.run = lambda *args, **kwargs: ""
        dispatch.emit_stage = lambda *_args, **_kwargs: None
        dispatch.matching_run = lambda *_args: {"id": 8, "status": "queued", "conclusion": None, "head_sha": "c" * 40, "head_branch": "main"}
        dispatch.cancel_and_wait = lambda run_id: (mismatch_calls.append(("cancel", run_id)), cancellation())[1]
        dispatch.recover_attempt = lambda run_id, conclusion, reason, outcome=None: mismatch_calls.append(("recover", run_id, conclusion, reason))
        dispatch.write_revision_mismatch_result = lambda *args: mismatch_calls.append(("result",) + args[2:8])
        dispatch.output = lambda name, value: mismatch_calls.append(("output", name, value))
        try:
            dispatch.supervise(arguments(), task())
        except SystemExit:
            pass
    finally:
        for name, value in saved.items():
            setattr(dispatch, name, value)
    check("dispatch: branch advance remains correlated by title", ("cancel", "8") in mismatch_calls)
    check("dispatch: SHA mismatch result follows bounded cancellation", mismatch_calls.index(("cancel", "8")) < next(index for index, row in enumerate(mismatch_calls) if row[0] == "result"))
    check("dispatch: SHA mismatch result uses trusted parent metadata", any(row[:6] == ("result", "b" * 40, "c" * 40, "8", "main", dispatch.STATE["correlation"]) and row[6]["cancellationConfirmed"] is True for row in mismatch_calls))
    check("dispatch: SHA mismatch exposes the parent result", any(row[:2] == ("output", "result_file") for row in mismatch_calls))
    check("dispatch: SHA mismatch skips impossible checkpoint recovery", not any(row[0] == "recover" for row in mismatch_calls))

    def failed_mismatch_cancel(error):
        calls = []
        saved_values = {name: getattr(dispatch, name) for name in ("run", "matching_run", "cancel_and_wait", "recover_attempt", "write_revision_mismatch_result", "output", "emit_stage")}
        try:
            dispatch.run = lambda *args, **kwargs: ""
            dispatch.emit_stage = lambda *_args, **_kwargs: None
            dispatch.matching_run = lambda *_args: {"id": 8, "status": "queued", "conclusion": None, "head_sha": "c" * 40, "head_branch": "main"}
            outcome = cancellation("success", "failed", 1) if isinstance(error, subprocess.CalledProcessError) else cancellation("", "unavailable", None)
            dispatch.cancel_and_wait = lambda _run_id: outcome
            dispatch.recover_attempt = lambda *_args: calls.append(("recover",))
            dispatch.write_revision_mismatch_result = lambda *args: calls.append(("result", args[7]["cancellationConfirmed"]))
            dispatch.output = lambda name, value: calls.append(("output", name, value))
            try:
                dispatch.supervise(arguments(), task())
            except SystemExit:
                pass
        finally:
            for name, value in saved_values.items():
                setattr(dispatch, name, value)
        return calls

    api_failure = failed_mismatch_cancel(subprocess.CalledProcessError(1, "gh"))
    timeout_failure = failed_mismatch_cancel(subprocess.TimeoutExpired("gh", 20))
    check("dispatch: mismatch API cancellation failure preserves spend result", ("result", False) in api_failure and any(row[:2] == ("output", "result_file") for row in api_failure))
    check("dispatch: mismatch cancellation timeout preserves spend result", ("result", False) in timeout_failure and any(row[:2] == ("output", "result_file") for row in timeout_failure))
    check("dispatch: failed mismatch cancellation still skips checkpoint recovery", not any(row[0] == "recover" for row in api_failure + timeout_failure))

    saved = {name: getattr(dispatch, name) for name in ("run", "matching_run", "cancel_and_wait", "recover_attempt", "write_controller_failure_result", "output", "emit_stage")}
    malformed_calls = []
    try:
        dispatch.run = lambda *args, **kwargs: ""
        dispatch.emit_stage = lambda *_args, **_kwargs: None
        dispatch.matching_run = lambda *_args: (_ for _ in ()).throw(dispatch.RunMetadataError("malformed", "11"))
        dispatch.cancel_and_wait = lambda run_id: (malformed_calls.append(("cancel", run_id)), cancellation())[1]
        dispatch.recover_attempt = lambda run_id, conclusion, reason, outcome=None: malformed_calls.append(("recover", run_id, conclusion, reason))
        dispatch.write_controller_failure_result = lambda *args, **kwargs: malformed_calls.append(("result", args[2], args[5]["cancellationConfirmed"]))
        dispatch.output = lambda name, value: malformed_calls.append(("output", name, value))
        try:
            dispatch.supervise(arguments(), task())
        except SystemExit:
            pass
    finally:
        for name, value in saved.items():
            setattr(dispatch, name, value)
    check("dispatch: malformed correlated metadata cancels identified run", ("cancel", "11") in malformed_calls)
    check("dispatch: malformed correlated metadata skips untrusted checkpoint recovery", not any(row[0] == "recover" for row in malformed_calls))
    check("dispatch: malformed metadata emits stable parent protocol result", ("result", "11", True) in malformed_calls)

    saved = {name: getattr(dispatch, name) for name in ("run", "matching_run", "run_status", "download_artifact", "cancel_and_wait", "recover_attempt", "emit_controller_failure", "emit_stage", "output")}
    missing_artifact_calls = []
    try:
        dispatch.run = lambda *args, **kwargs: ""
        dispatch.emit_stage = lambda *_args, **_kwargs: None
        dispatch.matching_run = lambda *_args: {"id": 12, "status": "queued", "conclusion": None, "head_sha": "b" * 40, "head_branch": "main"}
        dispatch.run_status = lambda *_args: {"status": "completed", "conclusion": "success"}
        dispatch.download_artifact = lambda *_args: False
        dispatch.cancel_and_wait = lambda *_args: FAILURES.append("dispatch: naturally completed child was cancelled")
        dispatch.recover_attempt = lambda *_args: (_ for _ in ()).throw(SystemExit("missing"))
        dispatch.emit_controller_failure = lambda *args, **kwargs: missing_artifact_calls.append((args[1], args[3]["terminalConclusion"], kwargs.get("reason")))
        dispatch.output = lambda *_args: None
        try:
            dispatch.supervise(arguments(), task())
        except SystemExit:
            pass
    finally:
        for name, value in saved.items():
            setattr(dispatch, name, value)
    check("dispatch: accepted child without final/checkpoint emits exactly one conservative result", missing_artifact_calls == [("12", "success", "child-artifact-unavailable")])

    saved_run = dispatch.run
    lookup_calls = []
    try:
        def lookup_run(*args, **_kwargs):
            lookup_calls.append(args)
            rows = [{"display_title": "other", "id": index + 100, "head_sha": "a" * 40, "head_branch": "main", "status": "completed", "conclusion": "success"} for index in range(100)] if len(lookup_calls) == 1 else [{"display_title": "wanted", "id": 9, "head_sha": "c" * 40, "head_branch": "main", "status": "queued", "conclusion": None}]
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
            return json.dumps({"workflow_runs": [{"display_title": "wanted", "id": 10, "head_sha": "b" * 40, "head_branch": "main", "status": "queued", "conclusion": None}] + [{"display_title": "other", "id": index + 100, "head_sha": "a" * 40, "head_branch": "main", "status": "completed", "conclusion": "success"} for index in range(99)]})

        dispatch.run = first_page_run
        dispatch.matching_run("owner/repo", "wanted", "main", "2026-01-01T00:00:00Z")
    finally:
        dispatch.run = saved_run
    check("dispatch: successful correlation stops API pagination", len(lookup_calls) == 1)

    saved_run = dispatch.run
    try:
        dispatch.run = lambda *_args, **_kwargs: json.dumps({"workflow_runs": ["malformed"]})
        try:
            dispatch.matching_run("owner/repo", "wanted", "main", "2026-01-01T00:00:00Z")
        except dispatch.RunMetadataError as error:
            check("dispatch: non-object run rows fail with bounded protocol error", not error.run_id)
        else:
            check("dispatch: non-object run rows fail with bounded protocol error", False)

        dispatch.run = lambda *_args, **_kwargs: json.dumps({"workflow_runs": [{"display_title": "wanted", "head_sha": "c" * 40, "head_branch": "main", "status": "queued", "conclusion": None}]})
        try:
            dispatch.matching_run("owner/repo", "wanted", "main", "2026-01-01T00:00:00Z")
        except dispatch.RunMetadataError as error:
            check("dispatch: correlated rows require a cancellable run id", not error.run_id)
        else:
            check("dispatch: correlated rows require a cancellable run id", False)
    finally:
        dispatch.run = saved_run

    if FAILURES:
        raise SystemExit("%d Claude dispatch checks failed" % len(FAILURES))
    print("\nall Claude dispatcher tests passed")


if __name__ == "__main__":
    main()
