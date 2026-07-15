#!/usr/bin/env python3
"""Dispatch and supervise the read-only Claude model workflow."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.claude_bridge import write_controller_failure_result, write_revision_mismatch_result

COMMAND_TIMEOUT_SECONDS = 20
CANCEL_WAIT_SECONDS = 30
LOOKUP_WINDOW_SECONDS = 120
MAX_LOOKUP_PAGES = 2
POLL_SECONDS = 3
STATE: dict[str, object] = {}


class ParentCancelled(Exception):
    pass


class RunMetadataError(ValueError):
    def __init__(self, message: str, run_id: str = "") -> None:
        super().__init__(message)
        self.run_id = run_id


def run(*args: str, capture: bool = False, timeout_seconds: float = COMMAND_TIMEOUT_SECONDS) -> str:
    result = subprocess.run(args, check=True, text=True, capture_output=capture, timeout=max(0.1, min(COMMAND_TIMEOUT_SECONDS, timeout_seconds)))
    return result.stdout if capture else ""


def output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT", "")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%s=%s\n" % (name, value.replace("\n", " ")))


def validate_run_row(row: object, title: str, branch: str) -> dict:
    if not isinstance(row, dict):
        raise RunMetadataError("Claude model workflow run row was not an object")
    correlated = row.get("display_title") == title and row.get("head_branch") == branch
    value_id = row.get("id")
    run_id = str(value_id) if correlated and isinstance(value_id, int) and not isinstance(value_id, bool) and value_id > 0 else ""
    head_sha = row.get("head_sha")
    conclusion = row.get("conclusion")
    if (
        not isinstance(value_id, int)
        or isinstance(value_id, bool)
        or value_id <= 0
        or not isinstance(row.get("display_title"), str)
        or not isinstance(row.get("head_branch"), str)
        or not isinstance(head_sha, str)
        or len(head_sha) != 40
        or any(character not in "0123456789abcdef" for character in head_sha)
        or not isinstance(row.get("status"), str)
        or not row.get("status")
        or (conclusion is not None and not isinstance(conclusion, str))
    ):
        raise RunMetadataError("Claude model workflow run row was malformed", run_id)
    return row


def matching_run(repo: str, title: str, branch: str, created_after: str, deadline: float | None = None) -> dict | None:
    matches = []
    for page in range(1, MAX_LOOKUP_PAGES + 1):
        query = urlencode({"event": "workflow_dispatch", "branch": branch, "created": ">=" + created_after, "per_page": 100, "page": page})
        remaining = COMMAND_TIMEOUT_SECONDS if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            return None
        value = json.loads(run("gh", "api", "repos/%s/actions/workflows/claude-model.yml/runs?%s" % (repo, query), capture=True, timeout_seconds=remaining))
        page_rows = value.get("workflow_runs") if isinstance(value, dict) else None
        if not isinstance(page_rows, list):
            raise ValueError("Claude model workflow lookup response is invalid")
        validated_rows = [validate_run_row(row, title, branch) for row in page_rows]
        matches.extend(row for row in validated_rows if row["display_title"] == title and row["head_branch"] == branch)
        if len(matches) > 1:
            raise SystemExit("ambiguous Claude model workflow correlation")
        if matches:
            return matches[0]
        if len(page_rows) < 100:
            break
    return None


def discover_run(repo: str, title: str, branch: str, created_after: str, deadline: float) -> dict | None:
    malformed = None
    while time.monotonic() < deadline:
        try:
            match = matching_run(repo, title, branch, created_after, deadline)
        except RunMetadataError as error:
            if error.run_id:
                raise
            malformed = error
            time.sleep(POLL_SECONDS)
            continue
        except (json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            time.sleep(POLL_SECONDS)
            continue
        if match:
            return match
        time.sleep(POLL_SECONDS)
    if malformed is not None:
        raise malformed
    return None


def cancel_and_wait(run_id: str) -> dict[str, object]:
    """Request cancellation and report what GitHub actually proves.

    A completed child is not evidence that cancellation succeeded. The terminal
    conclusion is authoritative, and a failed cancel request remains visible
    even when the child later completes naturally.
    """

    request_status = "failed"
    request_return_code: int | None = None
    try:
        requested = subprocess.run(
            ("gh", "run", "cancel", run_id),
            check=False,
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        request_return_code = requested.returncode
        request_status = "accepted" if requested.returncode == 0 else "failed"
    except (OSError, subprocess.TimeoutExpired):
        request_status = "unavailable"
    deadline = time.monotonic() + CANCEL_WAIT_SECONDS
    terminal_status = ""
    terminal_conclusion = ""
    while time.monotonic() < deadline:
        try:
            value = json.loads(run("gh", "run", "view", run_id, "--json", "status,conclusion", capture=True))
            status = value.get("status") if isinstance(value, dict) else None
            conclusion = value.get("conclusion") if isinstance(value, dict) else None
            if not isinstance(status, str) or not status or (conclusion is not None and not isinstance(conclusion, str)):
                raise ValueError("Claude model workflow terminal status was malformed")
        except (json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            time.sleep(2)
            continue
        if status == "completed":
            terminal_status = status
            terminal_conclusion = conclusion or ""
            break
        time.sleep(2)
    return {
        "requestStatus": request_status,
        "requestReturnCode": request_return_code,
        "terminalStatus": terminal_status,
        "terminalConclusion": terminal_conclusion,
        "cancellationConfirmed": terminal_status == "completed" and terminal_conclusion == "cancelled",
    }


def run_status(run_id: str, deadline: float) -> dict:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {}
    value = json.loads(run("gh", "run", "view", run_id, "--json", "status,conclusion", capture=True, timeout_seconds=remaining))
    if not isinstance(value, dict):
        raise ValueError("Claude model workflow status response is invalid")
    return value


def download_artifact(run_id: str, name: str, destination: Path) -> bool:
    destination.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(("gh", "run", "download", run_id, "--name", name, "--dir", str(destination)), check=False, text=True, capture_output=True, timeout=COMMAND_TIMEOUT_SECONDS)
    return result.returncode == 0


def finalize_proof(
    destination: Path,
    run_id: str,
    conclusion: str,
    termination_reason: str,
    cancellation: dict[str, object] | None = None,
) -> Path:
    proof = destination / "enforcement.json"
    if proof.is_file():
        value = json.loads(proof.read_text(encoding="utf-8"))
        value["controller"] = {
            "parentRunId": os.environ["GITHUB_RUN_ID"],
            "parentRunAttempt": os.environ["GITHUB_RUN_ATTEMPT"],
            "modelRunId": run_id,
            "hardDeadlineMs": None,
            "dispatchDeadlineMs": STATE["dispatch_ms"],
            "childExecutionTimeoutMs": STATE["child_timeout_ms"],
            "enforcedLimits": STATE["enforced_limits"],
            "conclusion": conclusion,
            "terminationReason": termination_reason,
            "dispatchRef": STATE["dispatch_ref"],
            "expectedCommitSha": STATE["expected_sha"],
            "observedCommitSha": STATE.get("observed_sha"),
            "correlationId": STATE["correlation"],
            "cancellation": cancellation,
        }
        temporary = proof.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, proof)
    output("execution_file", str(destination / "execution.json"))
    output("delivered_file", str(destination / "decision.json") if (destination / "decision.json").is_file() else "")
    output("enforcement_file", str(proof))
    output("model_run_id", run_id)
    return proof


def recover_attempt(
    run_id: str,
    conclusion: str,
    termination_reason: str,
    cancellation: dict[str, object] | None = None,
) -> None:
    destination = Path(str(STATE["destination"]))
    shutil.rmtree(destination, ignore_errors=True)
    if not download_artifact(run_id, str(STATE["output_artifact"]) + "-attempt", destination):
        raise SystemExit("Claude model attempt checkpoint was unavailable")
    finalize_proof(destination, run_id, conclusion, termination_reason, cancellation)


def terminate_parent(_signum: int, _frame: object) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise ParentCancelled


def emit_revision_mismatch(
    args: argparse.Namespace,
    run_id: str,
    observed_sha: str,
    correlation: str,
    cancellation: dict[str, object],
    destination: Path,
) -> None:
    result_path = destination / "result.json"
    events_path = destination / "events.ndjson"
    write_revision_mismatch_result(
        args.task,
        args.bundle,
        args.expected_sha,
        observed_sha,
        run_id,
        args.dispatch_ref,
        correlation,
        cancellation,
        str(result_path),
        str(events_path),
    )
    output("result_file", str(result_path))
    output("events_file", str(events_path))


def emit_controller_failure(
    args: argparse.Namespace,
    run_id: str,
    correlation: str,
    cancellation: dict[str, object],
    destination: Path,
    *,
    reason: str = "malformed-run-metadata",
) -> None:
    result_path = destination / "result.json"
    events_path = destination / "events.ndjson"
    write_controller_failure_result(
        args.task,
        args.bundle,
        run_id,
        args.dispatch_ref,
        correlation,
        cancellation,
        str(result_path),
        str(events_path),
        reason=reason,
    )
    output("result_file", str(result_path))
    output("events_file", str(events_path))


def supervise(args: argparse.Namespace, task: dict) -> None:
    dispatch_ms = int(task["spec"]["limits"]["dispatchDeadlineMs"])
    child_timeout_ms = int(task["spec"]["limits"]["childExecutionTimeoutMs"])
    if child_timeout_ms % 60_000:
        raise ValueError("Claude child execution timeout must use whole minutes")
    enforced_limits = {
        name: task["spec"]["limits"][name]
        for name, quality in task["spec"]["limits"]["enforcement"].items()
        if quality == "externally-enforced"
    }
    correlation = secrets.token_hex(16)
    title = "wheelhouse-claude-%s" % correlation
    destination = Path(args.out).resolve()
    repo = os.environ["GITHUB_REPOSITORY"]
    STATE.clear()
    created_after = (datetime.now(timezone.utc) - timedelta(seconds=LOOKUP_WINDOW_SECONDS)).isoformat(timespec="seconds").replace("+00:00", "Z")
    STATE.update({"dispatch_ms": dispatch_ms, "child_timeout_ms": child_timeout_ms, "enforced_limits": enforced_limits, "correlation": correlation, "title": title, "repo": repo, "dispatch_ref": args.dispatch_ref, "expected_sha": args.expected_sha, "observed_sha": None, "output_artifact": args.output_artifact, "destination": str(destination), "dispatched": False})
    signal.signal(signal.SIGTERM, terminate_parent)
    dispatch_deadline = time.monotonic() + dispatch_ms / 1000
    run_id = ""
    completed = False
    final_delivered = False
    failure = ""
    cleanup_failure = ""
    recovery_conclusion = "failure"
    termination_reason = "controller-failure"
    metadata_failure = False
    cancellation: dict[str, object] = {
        "requestStatus": "not-requested",
        "requestReturnCode": None,
        "terminalStatus": "",
        "terminalConclusion": "",
        "cancellationConfirmed": False,
    }
    try:
        STATE["dispatched"] = True
        run(
            "gh", "workflow", "run", "claude-model.yml", "--ref", args.dispatch_ref,
            "-f", "parent_run_id=%s" % os.environ["GITHUB_RUN_ID"],
            "-f", "parent_run_attempt=%s" % os.environ["GITHUB_RUN_ATTEMPT"],
            "-f", "input_artifact=%s" % args.input_artifact,
            "-f", "handoff_sha256=%s" % args.handoff_sha256,
            "-f", "output_artifact=%s" % args.output_artifact,
            "-f", "invocation_id=%s" % args.invocation_id,
            "-f", "expected_commit_sha=%s" % args.expected_sha,
            "-f", "correlation_id=%s" % correlation,
            "-f", "child_timeout_minutes=%d" % (child_timeout_ms // 60000),
            timeout_seconds=dispatch_deadline - time.monotonic(),
        )
        conclusion = ""
        match = None
        while time.monotonic() < dispatch_deadline:
            try:
                match = matching_run(repo, title, args.dispatch_ref, created_after, dispatch_deadline)
            except RunMetadataError as error:
                metadata_failure = True
                if error.run_id:
                    run_id = error.run_id
                    termination_reason = "controller-failure"
                    failure = "Claude model workflow returned malformed correlated run metadata"
                    break
                failure = "Claude model workflow returned malformed run metadata"
                time.sleep(POLL_SECONDS)
                continue
            except (json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
                time.sleep(POLL_SECONDS)
                continue
            if match:
                metadata_failure = False
                run_id = str(match["id"])
                STATE["observed_sha"] = match.get("head_sha")
                break
            time.sleep(POLL_SECONDS)
        if metadata_failure:
            failure = failure or "Claude model workflow returned malformed run metadata"
        elif not run_id:
            failure = failure or "Claude model run correlation was unavailable within its dispatch deadline"
        elif STATE["observed_sha"] != args.expected_sha:
            termination_reason = "revision-mismatch"
            failure = "Claude model workflow revision did not match the trusted parent"
        else:
            child_deadline = time.monotonic() + child_timeout_ms / 1000
            while time.monotonic() < child_deadline:
                if match and match.get("status") == "completed":
                    completed = True
                    conclusion = str(match.get("conclusion") or "")
                    break
                try:
                    match = run_status(run_id, child_deadline)
                except (json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
                    time.sleep(POLL_SECONDS)
                    continue
                if match and match.get("status") == "completed":
                    completed = True
                    conclusion = str(match.get("conclusion") or "")
                    break
                time.sleep(POLL_SECONDS)
        if run_id and STATE["observed_sha"] == args.expected_sha and not completed:
            recovery_conclusion = "timed_out"
            termination_reason = "child-timeout"
            failure = "read-only Claude model workflow exceeded its child execution timeout"
        elif completed:
            normalized = "success" if conclusion == "success" else "failure"
            if download_artifact(run_id, args.output_artifact, destination):
                finalize_proof(destination, run_id, normalized, "completed")
                final_delivered = True
            else:
                failure = "Claude model result artifact was unavailable"
            if conclusion != "success":
                failure = "read-only Claude model workflow concluded %s" % conclusion
    except ParentCancelled:
        recovery_conclusion = "cancelled"
        termination_reason = "parent-sigterm"
        failure = "read-only Claude model workflow was cancelled by its parent"
    except (json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError) as error:
        failure = "Claude model workflow supervision failed closed: %s" % type(error).__name__
    finally:
        if STATE.get("dispatched") and not final_delivered:
            if not run_id:
                if time.monotonic() < dispatch_deadline:
                    try:
                        match = discover_run(repo, title, args.dispatch_ref, created_after, dispatch_deadline)
                        run_id = str(match["id"]) if match else ""
                        if match:
                            STATE["observed_sha"] = match.get("head_sha")
                            if STATE["observed_sha"] != args.expected_sha:
                                recovery_conclusion = "failure"
                                termination_reason = "revision-mismatch"
                    except RunMetadataError as error:
                        metadata_failure = True
                        if error.run_id:
                            run_id = error.run_id
                        termination_reason = "controller-failure"
                        failure = failure or "Claude model workflow returned malformed run metadata"
                        if not error.run_id:
                            cleanup_failure = "Claude model workflow metadata was malformed"
                    except (json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError, SystemExit) as error:
                        cleanup_failure = "Claude model run correlation failed: %s" % type(error).__name__
            if run_id:
                if completed:
                    cancellation = {
                        "requestStatus": "not-requested",
                        "requestReturnCode": None,
                        "terminalStatus": "completed",
                        "terminalConclusion": conclusion,
                        "cancellationConfirmed": False,
                    }
                else:
                    cancellation = cancel_and_wait(run_id)

                # A cancel request can lose a race with natural completion. In
                # that case prefer the child's trusted final artifact and never
                # describe the run as cancelled.
                natural_conclusion = str(cancellation.get("terminalConclusion") or "")
                if (
                    termination_reason != "revision-mismatch"
                    and STATE.get("observed_sha") == args.expected_sha
                    and natural_conclusion
                    and natural_conclusion != "cancelled"
                    and download_artifact(run_id, args.output_artifact, destination)
                ):
                    normalized = "success" if natural_conclusion == "success" else "failure"
                    finalize_proof(destination, run_id, normalized, "completed", cancellation)
                    final_delivered = True

                if termination_reason == "revision-mismatch":
                    try:
                        emit_revision_mismatch(args, run_id, str(STATE["observed_sha"]), correlation, cancellation, destination)
                    except (json.JSONDecodeError, OSError, ValueError) as error:
                        cleanup_failure = "Claude mismatch result was unavailable: %s" % type(error).__name__
                elif not final_delivered and metadata_failure:
                    try:
                        emit_controller_failure(args, run_id, correlation, cancellation, destination)
                    except (json.JSONDecodeError, OSError, ValueError) as error:
                        cleanup_failure = "Claude protocol failure result was unavailable: %s" % type(error).__name__
                elif not final_delivered:
                    try:
                        recover_attempt(run_id, recovery_conclusion, termination_reason, cancellation)
                    except (json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError, SystemExit):
                        # No trusted child artifact means checkpoint passage is
                        # unknowable. Emit one task-bound possible-spend result.
                        try:
                            emit_controller_failure(
                                args,
                                run_id,
                                correlation,
                                cancellation,
                                destination,
                                reason="child-artifact-unavailable",
                            )
                        except (json.JSONDecodeError, OSError, ValueError) as error:
                            cleanup_failure = "Claude recovery result was unavailable: %s" % type(error).__name__
            elif not cleanup_failure:
                cleanup_failure = "Claude model run correlation was unavailable"
    if cleanup_failure:
        raise SystemExit(cleanup_failure)
    if failure:
        raise SystemExit(failure)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--input-artifact", required=True)
    parser.add_argument("--output-artifact", required=True)
    parser.add_argument("--invocation-id", required=True)
    parser.add_argument("--handoff-sha256", required=True)
    parser.add_argument("--dispatch-ref", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    task = json.loads(Path(args.task).read_text(encoding="utf-8"))
    supervise(args, task)


if __name__ == "__main__":
    main()
