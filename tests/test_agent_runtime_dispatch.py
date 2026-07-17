#!/usr/bin/env python3
"""Offline source-A/main-B reusable workflow race regression."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

FAILURES: list[str] = []
ROOT = Path(__file__).resolve().parents[1]


def check(name: str, condition: bool) -> None:
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()


def workflow_at(repo: Path, sha: str, path: str) -> str:
    return git(repo, "show", "%s:%s" % (sha, path))


def local_reusable_workflow(repo: Path, caller_sha: str, uses: str) -> str:
    if not uses.startswith("./.github/workflows/"):
        raise ValueError("test fixture requires a same-repository reusable workflow")
    return workflow_at(repo, caller_sha, uses[2:])


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory) / "repo"
        (repo / ".github/workflows").mkdir(parents=True)
        git(repo, "init", "--initial-branch=main")
        git(repo, "config", "user.email", "race@example.invalid")
        git(repo, "config", "user.name", "Source Race")
        for relative in (
            ".github/workflows/claude-model.yml",
            ".github/workflows/triage.yml",
            ".github/workflows/deep-review.yml",
            ".github/workflows/decision-handler.yml",
        ):
            source = ROOT / relative
            destination = repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        git(repo, "add", ".")
        git(repo, "commit", "-m", "source A")
        source_a = git(repo, "rev-parse", "HEAD")

        model_path = repo / ".github/workflows/claude-model.yml"
        model_path.write_text(model_path.read_text(encoding="utf-8") + "\n# source B marker\n", encoding="utf-8")
        git(repo, "add", str(model_path.relative_to(repo)))
        git(repo, "commit", "-m", "advance main to source B")
        source_b = git(repo, "rev-parse", "HEAD")

        caller_text = workflow_at(repo, source_a, ".github/workflows/triage.yml")
        caller = yaml.safe_load(caller_text)
        jobs = [job for job in caller["jobs"].values() if job.get("uses") == "./.github/workflows/claude-model.yml"]
        called_at_a = local_reusable_workflow(repo, source_a, jobs[0]["uses"])
        called_at_b = workflow_at(repo, source_b, ".github/workflows/claude-model.yml")
        called = yaml.safe_load(called_at_a)
        bind = next(step for step in called["jobs"]["model"]["steps"] if step.get("id") == "source")
        provider_steps = [step for step in called["jobs"]["model"]["steps"] if str(step.get("uses", "")).startswith("anthropics/claude-code-action@")]

        check("race: fixture advances main from source A to distinct source B", source_a != source_b and git(repo, "rev-parse", "main") == source_b)
        check("race: source-A caller uses the same-commit relative workflow boundary", len(jobs) == 2 and all(job.get("uses") == "./.github/workflows/claude-model.yml" for job in jobs))
        check("race: source-B workflow bytes differ after main advances", called_at_a != called_at_b and "source B marker" not in called_at_a and "source B marker" in called_at_b)
        check("race: admitted source-A caller still loads model workflow source A", yaml.safe_load(called_at_a) == called and source_a != source_b)
        check("race: source gate legitimately observes source A equal expected A", bind.get("env", {}).get("EXPECTED_COMMIT_SHA") == "${{ inputs.expected_commit_sha }}" and 'GITHUB_SHA" = "$EXPECTED_COMMIT_SHA' in bind.get("run", ""))
        check("race: provider remains gated behind successful source-A hydration", provider_steps and all("steps.hydrate.outputs.action" in str(step.get("if", "")) for step in provider_steps))
        check("race: mutable main is absent from the model execution path", "github.ref_name" not in caller_text + called_at_a and "--ref" not in called_at_a)

    if FAILURES:
        raise SystemExit("%d source revision race checks failed" % len(FAILURES))
    print("\nall source revision race checks passed")


if __name__ == "__main__":
    main()
