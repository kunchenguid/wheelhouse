#!/usr/bin/env python3
"""Dispatch and supervise the read-only Claude model workflow."""

from __future__ import annotations

import argparse
import json
import os
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--input-artifact", required=True)
    parser.add_argument("--output-artifact", required=True)
    parser.add_argument("--invocation-id", required=True)
    parser.add_argument("--handoff-sha256", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    task = json.loads(Path(args.task).read_text(encoding="utf-8"))
    hard_ms = int(task["spec"]["limits"]["hardDeadlineMs"])
    title = "wheelhouse-claude-%s-%s-%s" % (os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], args.invocation_id)
    run(
        "gh", "workflow", "run", "claude-model.yml", "--ref", args.ref,
        "-f", "parent_run_id=%s" % os.environ["GITHUB_RUN_ID"],
        "-f", "parent_run_attempt=%s" % os.environ["GITHUB_RUN_ATTEMPT"],
        "-f", "input_artifact=%s" % args.input_artifact,
        "-f", "handoff_sha256=%s" % args.handoff_sha256,
        "-f", "output_artifact=%s" % args.output_artifact,
        "-f", "invocation_id=%s" % args.invocation_id,
    )
    deadline = time.monotonic() + hard_ms / 1000
    run_id = ""
    conclusion = ""
    while time.monotonic() < deadline:
        rows = json.loads(run("gh", "run", "list", "--workflow", "claude-model.yml", "--limit", "40", "--json", "databaseId,displayTitle,status,conclusion,headSha", capture=True))
        match = next((row for row in rows if row.get("displayTitle") == title and row.get("headSha") == args.ref), None)
        if match:
            run_id = str(match["databaseId"])
            if match.get("status") == "completed":
                conclusion = str(match.get("conclusion") or "")
                break
        time.sleep(2)
    if not run_id or not conclusion:
        if run_id:
            subprocess.run(("gh", "run", "cancel", run_id), check=False)
        raise SystemExit("read-only Claude model workflow exceeded its hard deadline")
    destination = Path(args.out).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    run("gh", "run", "download", run_id, "--name", args.output_artifact, "--dir", str(destination))
    proof = destination / "enforcement.json"
    if proof.is_file():
        value = json.loads(proof.read_text(encoding="utf-8"))
        value["controller"] = {"parentRunId": os.environ["GITHUB_RUN_ID"], "parentRunAttempt": os.environ["GITHUB_RUN_ATTEMPT"], "modelRunId": run_id, "hardDeadlineMs": hard_ms, "conclusion": conclusion}
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
