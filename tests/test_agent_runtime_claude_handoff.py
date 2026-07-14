#!/usr/bin/env python3
"""Offline bounded Claude model handoff checks."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.claude_handoff import hydrate, pack, verify
from agent_runtime.config import resolve_selection
from agent_runtime.contract import ContractError, canonical_sha256
from agent_runtime.task_builder import build_task

FAILURES = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        prompt = root / "prompt.txt"
        target = root / "target.txt"
        repository = root / "repository"
        prompt.write_text("Inspect the immutable input.\n", encoding="utf-8")
        target.write_text("bounded target\n", encoding="utf-8")
        repository.mkdir()
        (repository / "source.py").write_text("value = 1\n", encoding="utf-8")
        bundle = root / "bundle"
        task = build_task(
            action="deep-review.search",
            selection=resolve_selection("deep-review.search", "repo"),
            prompt_path=str(prompt),
            bundle_dir=str(bundle),
            output_path=str(bundle / "task.json"),
            owner="owner",
            repo="repo",
            number=9,
            target_kind="pr-review",
            revision="abcdef1",
            wheelhouse_revision="30271b6907e568419cdc48694a11b0c2f699b433",
            target_file=str(target),
            repository_dir=str(repository),
            repository_commit="abcdef1",
        )
        handoff = root / "handoff"
        metadata = pack(str(bundle / "task.json"), str(bundle), str(handoff), '["owner/repo"]')
        checked, checked_task = verify(str(handoff))
        check("handoff: immutable task is content-bound", checked["taskSha256"] == canonical_sha256(task) == canonical_sha256(checked_task))
        check("handoff: search scope is explicit and bounded", metadata["allowedRepos"] == ["owner/repo"])
        workspace = root / "workspace"
        hydrated = hydrate(str(handoff), str(workspace))
        check("handoff: fresh workspace receives only declared inputs", sorted(path.name for path in workspace.iterdir()) == ["target-src", "target.txt"] and hydrated["action"] == "deep-review.search")
        for path in workspace.rglob("*"):
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        artifact = next(path for path in (handoff / "bundle" / "artifacts" / "sha256").iterdir() if path.is_file())
        os.chmod(artifact, 0o600)
        original = artifact.read_bytes()
        artifact.write_bytes(original + b"tamper")
        rejected = False
        try:
            verify(str(handoff))
        except ContractError:
            rejected = True
        check("handoff: artifact tampering fails closed", rejected)
        for path in root.rglob("*"):
            if not path.is_symlink():
                os.chmod(path, 0o700 if path.is_dir() else 0o600)

    if FAILURES:
        raise SystemExit("%d Claude handoff checks failed" % len(FAILURES))
    print("\nall Claude handoff tests passed")


if __name__ == "__main__":
    main()
