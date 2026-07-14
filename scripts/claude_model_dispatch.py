#!/usr/bin/env python3
"""Dispatch and supervise the read-only Claude model workflow."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import time
from pathlib import Path


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, check=True, text=True, capture_output=capture)
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


def cancel_and_wait(run_id: str) -> None:
    subprocess.run(("gh", "run", "cancel", run_id), check=False)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        value = json.loads(run("gh", "run", "view", run_id, "--json", "status,conclusion", capture=True))
        if value.get("status") == "completed":
            return
        time.sleep(2)
    raise SystemExit("cancelled Claude model workflow did not terminate")


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
        while not run_id:
            match = matching_run(os.environ["GITHUB_REPOSITORY"], title, args.expected_sha)
            if match:
                run_id = str(match["id"])
                break
            time.sleep(2)
        cancel_and_wait(run_id)
        raise SystemExit("read-only Claude model workflow exceeded its hard deadline")
    destination = Path(args.out).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    run("gh", "run", "download", run_id, "--name", args.output_artifact, "--dir", str(destination))
    proof = destination / "enforcement.json"
    if proof.is_file():
        value = json.loads(proof.read_text(encoding="utf-8"))
        value["controller"] = {"parentRunId": os.environ["GITHUB_RUN_ID"], "parentRunAttempt": os.environ["GITHUB_RUN_ATTEMPT"], "modelRunId": run_id, "hardDeadlineMs": hard_ms, "conclusion": conclusion, "dispatchRef": args.dispatch_ref, "expectedCommitSha": args.expected_sha, "correlationId": correlation}
        temporary = proof.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, proof)
    output("execution_file", str(destination / "execution.json"))
    output("delivered_file", str(destination / "decision.json") if (destination / "decision.json").is_file() else "")
    output("enforcement_file", str(proof))
    output("model_run_id", run_id)
    if conclusion != "success":
        raise SystemExit("read-only Claude model workflow concluded %s" % conclusion)


if __name__ == "__main__":
    main()
