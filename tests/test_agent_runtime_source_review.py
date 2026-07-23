#!/usr/bin/env python3
"""Production-shaped source-review AgentTask builder regressions."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.contract import ArtifactError, file_sha256
from agent_runtime.task_builder import build_task
from agent_runtime_testlib import WHEELHOUSE_REVISION, codex_selection

FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    if condition:
        print("ok  ", name)
    else:
        print("FAIL", name)
        FAILURES.append(name)


def git_repository(root: Path) -> tuple[Path, str]:
    repository = root / "repository"
    repository.mkdir()
    (repository / "source.py").write_text("value = 1\n", encoding="utf-8")
    commands = (
        ("git", "init"),
        ("git", "config", "user.email", "wheelhouse-test@example.com"),
        ("git", "config", "user.name", "Wheelhouse Test"),
        ("git", "config", "commit.gpgsign", "false"),
        ("git", "add", "-A"),
        ("git", "commit", "-m", "fixture"),
    )
    for command in commands:
        subprocess.run(command, cwd=repository, check=True, capture_output=True)
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ("git", "checkout", "--detach", commit),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return repository, commit


def fixture(root: Path) -> dict[str, str]:
    repository, commit = git_repository(root)
    files = {
        "prompt": root / "prompt.txt",
        "target": root / "target.txt",
        "vision": root / "VISION.md",
        "facts": root / "target-facts.json",
    }
    files["prompt"].write_text("Review the bounded source.\n", encoding="utf-8")
    files["target"].write_text(
        "fixture evidence anchor text for source review\n", encoding="utf-8"
    )
    files["vision"].write_text("# Vision\nPreserve safe behavior.\n", encoding="utf-8")
    files["facts"].write_text('{"diffComplete":true}\n', encoding="utf-8")
    return {
        "repository": str(repository),
        "commit": commit,
        **{name: str(path) for name, path in files.items()},
    }


def build(root: Path, action: str, values: dict[str, str], **overrides: str):
    arguments = {
        "action": action,
        "selection": codex_selection(),
        "prompt_path": values["prompt"],
        "bundle_dir": str(root / (action.replace(".", "-") + "-bundle")),
        "output_path": str(root / (action.replace(".", "-") + "-bundle/task.json")),
        "owner": "kunchenguid",
        "repo": "no-mistakes",
        "number": 549,
        "target_kind": "pr-review",
        "revision": values["commit"],
        "wheelhouse_revision": WHEELHOUSE_REVISION,
        "event_key": "a" * 64,
        "target_file": values["target"],
        "repository_dir": values["repository"],
        "repository_commit": values["commit"],
        "vision_file": values["vision"],
        "target_facts_file": values["facts"],
        "base_sha": "b" * 40,
        "vision_sha": "c" * 40,
    }
    arguments.update(overrides)
    return build_task(**arguments)


def assert_source_review(action: str, task: dict, values: dict[str, str]) -> None:
    source_review = task["metadata"]["sourceReview"]
    inputs = {row["id"]: row for row in task["spec"]["inputs"]}
    check(
        action + ": source review binds all identities",
        source_review
        == {
            "baseSha": "b" * 40,
            "visionSha": "c" * 40,
            "visionContentSha256": file_sha256(values["vision"]),
            "targetFactsSha256": file_sha256(values["facts"]),
            "targetRepositoryCommit": values["commit"],
        },
    )
    check(
        action + ": source review binds trusted paths",
        inputs["vision"]["logicalPath"] == "vision.md"
        and inputs["target-facts"]["logicalPath"] == "target-facts.json"
        and inputs["repository"]["logicalPath"] == "target-src"
        and inputs["repository"]["git"]["commit"] == values["commit"],
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        values = fixture(root)
        for action in ("triage.pr.local", "triage.pr.search"):
            assert_source_review(action, build(root, action, values), values)

        malformed = {
            "base SHA": {"base_sha": "not-a-sha"},
            "VISION SHA": {"vision_sha": "not-a-sha"},
            "repository SHA": {"repository_commit": "not-a-sha"},
        }
        for name, overrides in malformed.items():
            case_root = root / ("malformed-" + name.lower().replace(" ", "-"))
            try:
                build(case_root, "triage.pr.search", values, **overrides)
            except ArtifactError:
                check("malformed " + name + " raises ArtifactError", True)
            except Exception as error:
                check(
                    "malformed " + name + " never leaks " + type(error).__name__,
                    False,
                )
            else:
                check("malformed " + name + " cannot build a task", False)

        cli_bundle = root / "cli-bundle"
        cli_task = cli_bundle / "task.json"
        command = (
            sys.executable,
            "scripts/agent_runtime.py",
            "build-task",
            "--action",
            "triage.pr.search",
            "--prompt",
            values["prompt"],
            "--bundle",
            str(cli_bundle),
            "--out",
            str(cli_task),
            "--owner",
            "kunchenguid",
            "--repo",
            "no-mistakes",
            "--number",
            "549",
            "--kind",
            "pr-review",
            "--revision",
            values["commit"],
            "--wheelhouse-revision",
            WHEELHOUSE_REVISION,
            "--event-key",
            "d" * 64,
            "--target-file",
            values["target"],
            "--repository-dir",
            values["repository"],
            "--repository-commit",
            values["commit"],
            "--vision-file",
            values["vision"],
            "--target-facts-file",
            values["facts"],
            "--base-sha",
            "b" * 40,
            "--vision-sha",
            "c" * 40,
        )
        result = subprocess.run(command, capture_output=True, text=True)
        check("CLI build-task reaches the real source-review builder", result.returncode == 0)
        if result.returncode == 0:
            assert_source_review(
                "CLI triage.pr.search", json.loads(cli_task.read_text()), values
            )
        else:
            print(result.stdout)
            print(result.stderr)

    if FAILURES:
        raise SystemExit("%d source-review test(s) failed" % len(FAILURES))


if __name__ == "__main__":
    main()
