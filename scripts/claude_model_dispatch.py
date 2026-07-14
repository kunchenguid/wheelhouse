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
import time
from pathlib import Path

COMMAND_TIMEOUT_SECONDS = 20
DISCOVERY_GRACE_SECONDS = 30
CANCEL_WAIT_SECONDS = 30
STATE: dict[str, object] = {}


class ParentCancelled(Exception):
    pass


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, check=True, text=True, capture_output=capture, timeout=COMMAND_TIMEOUT_SECONDS)
    return result.stdout if capture else ""


def output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT", "")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%s=%s\n" % (name, value.replace("\n", " ")))


def matching_run(repo: str, title: str, expected_sha: str) -> dict | None:
    pages = json.loads(run(
        "gh", "api", "--paginate", "--slurp",
        "repos/%s/actions/workflows/claude-model.yml/runs?event=workflow_dispatch&per_page=100" % repo,
        capture=True,
    ))
    rows = [row for page in pages for row in page.get("workflow_runs", [])]
    matches = [row for row in rows if row.get("display_title") == title and row.get("head_sha") == expected_sha]
    if len(matches) > 1:
        raise SystemExit("ambiguous Claude model workflow correlation")
    return matches[0] if matches else None


def discover_run(repo: str, title: str, expected_sha: str, deadline: float) -> dict | None:
    while time.monotonic() < deadline:
        try:
            match = matching_run(repo, title, expected_sha)
        except (json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            time.sleep(2)
            continue
        if match:
            return match
        time.sleep(2)
    return None


def cancel_and_wait(run_id: str) -> None:
    subprocess.run(("gh", "run", "cancel", run_id), check=False, timeout=COMMAND_TIMEOUT_SECONDS)
    deadline = time.monotonic() + CANCEL_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            value = json.loads(run("gh", "run", "view", run_id, "--json", "status,conclusion", capture=True))
        except (json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            time.sleep(2)
            continue
        if value.get("status") == "completed":
            return
        time.sleep(2)
    raise SystemExit("cancelled Claude model workflow did not terminate")


def download_artifact(run_id: str, name: str, destination: Path) -> bool:
    destination.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(("gh", "run", "download", run_id, "--name", name, "--dir", str(destination)), check=False, text=True, capture_output=True, timeout=COMMAND_TIMEOUT_SECONDS)
    return result.returncode == 0


def finalize_proof(destination: Path, run_id: str, conclusion: str, termination_reason: str) -> Path:
    proof = destination / "enforcement.json"
    if proof.is_file():
        value = json.loads(proof.read_text(encoding="utf-8"))
        value["controller"] = {
            "parentRunId": os.environ["GITHUB_RUN_ID"],
            "parentRunAttempt": os.environ["GITHUB_RUN_ATTEMPT"],
            "modelRunId": run_id,
            "hardDeadlineMs": STATE["hard_ms"],
            "enforcedLimits": STATE["enforced_limits"],
            "conclusion": conclusion,
            "terminationReason": termination_reason,
            "dispatchRef": STATE["dispatch_ref"],
            "expectedCommitSha": STATE["expected_sha"],
            "correlationId": STATE["correlation"],
        }
        temporary = proof.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, proof)
    output("execution_file", str(destination / "execution.json"))
    output("delivered_file", str(destination / "decision.json") if (destination / "decision.json").is_file() else "")
    output("enforcement_file", str(proof))
    output("model_run_id", run_id)
    return proof


def recover_attempt(run_id: str, conclusion: str, termination_reason: str) -> None:
    destination = Path(str(STATE["destination"]))
    shutil.rmtree(destination, ignore_errors=True)
    if not download_artifact(run_id, str(STATE["output_artifact"]) + "-attempt", destination):
        raise SystemExit("Claude model attempt checkpoint was unavailable")
    finalize_proof(destination, run_id, conclusion, termination_reason)


def terminate_parent(_signum: int, _frame: object) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise ParentCancelled


def supervise(args: argparse.Namespace, task: dict) -> None:
    hard_ms = int(task["spec"]["limits"]["hardDeadlineMs"])
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
    STATE.update({"hard_ms": hard_ms, "enforced_limits": enforced_limits, "correlation": correlation, "title": title, "repo": repo, "dispatch_ref": args.dispatch_ref, "expected_sha": args.expected_sha, "output_artifact": args.output_artifact, "destination": str(destination), "dispatched": False})
    signal.signal(signal.SIGTERM, terminate_parent)
    deadline = time.monotonic() + hard_ms / 1000
    run_id = ""
    completed = False
    final_delivered = False
    failure = ""
    cleanup_failure = ""
    recovery_conclusion = "failure"
    termination_reason = "controller-failure"
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
        )
        conclusion = ""
        while time.monotonic() < deadline:
            try:
                match = matching_run(repo, title, args.expected_sha)
            except (json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                time.sleep(2)
                continue
            if match:
                run_id = str(match["id"])
                if match.get("status") == "completed":
                    completed = True
                    conclusion = str(match.get("conclusion") or "")
                    break
            time.sleep(2)
        if not completed:
            recovery_conclusion = "timed_out"
            termination_reason = "hard-deadline"
            failure = "read-only Claude model workflow exceeded its hard deadline"
        else:
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
                try:
                    match = discover_run(repo, title, args.expected_sha, time.monotonic() + DISCOVERY_GRACE_SECONDS)
                    run_id = str(match["id"]) if match else ""
                except (json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError, SystemExit) as error:
                    cleanup_failure = "Claude model run correlation failed: %s" % type(error).__name__
            if run_id:
                try:
                    if not completed:
                        cancel_and_wait(run_id)
                except (json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError, SystemExit) as error:
                    cleanup_failure = "Claude model cancellation failed: %s" % type(error).__name__
                finally:
                    try:
                        recover_attempt(run_id, recovery_conclusion, termination_reason)
                    except (json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError, SystemExit) as error:
                        cleanup_failure = "Claude model attempt checkpoint recovery failed: %s" % type(error).__name__
            elif not cleanup_failure:
                cleanup_failure = "Claude model run correlation was unavailable"
    if cleanup_failure:
        raise SystemExit(cleanup_failure)
    if failure:
        raise SystemExit(failure)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
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
