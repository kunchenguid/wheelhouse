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
        match = matching_run(repo, title, expected_sha)
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
            "enforcedLimits": {"hardDeadlineMs": STATE["hard_ms"]},
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
    if not STATE.get("dispatched"):
        raise SystemExit(143)
    match = discover_run(str(STATE["repo"]), str(STATE["title"]), str(STATE["expected_sha"]), time.monotonic() + DISCOVERY_GRACE_SECONDS)
    if match is None:
        raise SystemExit("Claude model run correlation was unavailable during parent cancellation")
    run_id = str(match["id"])
    cancel_and_wait(run_id)
    recover_attempt(run_id, "cancelled", "parent-sigterm")
    raise SystemExit(143)


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
    hard_ms = int(task["spec"]["limits"]["hardDeadlineMs"])
    correlation = secrets.token_hex(16)
    title = "wheelhouse-claude-%s" % correlation
    destination = Path(args.out).resolve()
    STATE.update({"hard_ms": hard_ms, "correlation": correlation, "title": title, "repo": os.environ["GITHUB_REPOSITORY"], "dispatch_ref": args.dispatch_ref, "expected_sha": args.expected_sha, "output_artifact": args.output_artifact, "destination": str(destination), "dispatched": False})
    signal.signal(signal.SIGTERM, terminate_parent)
    STATE["dispatched"] = True
    try:
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
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        match = discover_run(os.environ["GITHUB_REPOSITORY"], title, args.expected_sha, time.monotonic() + DISCOVERY_GRACE_SECONDS)
        if match:
            cancel_and_wait(str(match["id"]))
        raise SystemExit("Claude model workflow dispatch failed closed")
    deadline = time.monotonic() + hard_ms / 1000
    run_id = ""
    conclusion = ""
    while time.monotonic() < deadline:
        match = matching_run(os.environ["GITHUB_REPOSITORY"], title, args.expected_sha)
        if match:
            run_id = str(match["id"])
            if match.get("status") == "completed":
                conclusion = str(match.get("conclusion") or "")
                break
        time.sleep(2)
    if not run_id or not conclusion:
        match = discover_run(os.environ["GITHUB_REPOSITORY"], title, args.expected_sha, time.monotonic() + DISCOVERY_GRACE_SECONDS) if not run_id else {"id": run_id}
        if match is None:
            raise SystemExit("Claude model run correlation was unavailable after its hard deadline")
        run_id = str(match["id"])
        cancel_and_wait(run_id)
        recover_attempt(run_id, "timed_out", "hard-deadline")
        raise SystemExit("read-only Claude model workflow exceeded its hard deadline")
    normalized_conclusion = "success" if conclusion == "success" else "failure"
    if not download_artifact(run_id, args.output_artifact, destination):
        recover_attempt(run_id, normalized_conclusion, "completed")
    else:
        finalize_proof(destination, run_id, normalized_conclusion, "completed")
    if conclusion != "success":
        raise SystemExit("read-only Claude model workflow concluded %s" % conclusion)


if __name__ == "__main__":
    main()
