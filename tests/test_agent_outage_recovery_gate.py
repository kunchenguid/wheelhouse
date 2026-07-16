#!/usr/bin/env python3
"""Production-faithful provider-free gate for all seven agent path groups."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.admission import event_key_sha256, normalized_event_identity
from agent_runtime.claude_handoff import pack, verify
from agent_runtime.config import resolve_selection
from agent_runtime.contract import canonical_sha256, validate_contract
from agent_runtime.task_builder import build_task

FAILURES = []
WHEELHOUSE_SHA = "a" * 40


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True).stdout.strip()


def python_minors() -> list[str]:
    found = []
    for name in ("python3.12", "python3.13", sys.executable):
        path = shutil.which(name) if os.path.sep not in name else name
        if not path or not Path(path).is_file():
            continue
        minor = subprocess.run([path, "-c", "import sys;print('%d.%d'%sys.version_info[:2])"], check=True, text=True, capture_output=True).stdout.strip()
        if minor in ("3.12", "3.13") and all(row[0] != minor for row in found):
            found.append((minor, str(Path(path).resolve())))
    return [path for _minor, path in sorted(found)]


def zip_copy(source: Path, destination: Path) -> None:
    archive = destination.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                handle.write(path, path.relative_to(source).as_posix())
    destination.mkdir()
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(destination)


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repo = root / "repository"
        repo.mkdir()
        git(repo, "init")
        git(repo, "config", "user.email", "gate@example.invalid")
        git(repo, "config", "user.name", "Recovery Gate")
        (repo / "AGENTS.md").write_text("# bounded instructions\n", encoding="utf-8")
        (repo / ".agents/skills/demo").mkdir(parents=True)
        (repo / ".agents/skills/demo/SKILL.md").write_text("bounded skill\n", encoding="utf-8")
        (repo / "CLAUDE.md").symlink_to("AGENTS.md")
        (repo / ".claude").mkdir()
        (repo / ".claude/skills").symlink_to("../.agents/skills")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "synthetic links")
        commit = git(repo, "rev-parse", "HEAD")
        check("gate: synthetic source is branch-attached like issue default checkout", git(repo, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD")

        prompt = root / "prompt.txt"
        target = root / "target.txt"
        prompt.write_text("Read only bounded files and return the strict schema.\n", encoding="utf-8")
        target.write_text('fixture evidence anchor text for runtime tests\n', encoding="utf-8")

        cases = []
        for action in ("triage.pr.local", "triage.pr.search", "triage.issue.local", "triage.issue.search"):
            cases.append((action, "pr-review" if ".pr." in action else "issue-triage", "pr", True, True))
        cases.extend([
            ("triage.schema-repair", "schema-repair", "pr", False, False),
            ("triage.schema-repair", "schema-repair", "issue", False, False),
        ])
        for action in ("deep-review.local", "deep-review.search", "nl-decision.local", "nl-decision.search"):
            for kind in ("pr-review", "issue-triage"):
                cases.append((action, kind, "pr", action.startswith("deep-review"), True))

        interpreters = python_minors()
        check("gate: Python 3.12 available", any("3.12" in subprocess.run([path, "--version"], text=True, capture_output=True).stdout + subprocess.run([path, "--version"], text=True, capture_output=True).stderr for path in interpreters))
        check("gate: Python 3.13 available", any("3.13" in subprocess.run([path, "--version"], text=True, capture_output=True).stdout + subprocess.run([path, "--version"], text=True, capture_output=True).stderr for path in interpreters))

        for index, (action, kind, repair_kind, with_repo, with_target) in enumerate(cases, 1):
            revision = commit if kind == "pr-review" else "2026-07-15T00:00:00Z"
            event_id = ""
            if action.startswith("nl-decision"):
                event_id = "comment:%d" % (1000 + index)
            elif action.startswith("deep-review"):
                event_id = "manual:%d" % (2000 + index)
            identity = normalized_event_identity(
                action=action,
                owner="owner",
                repo="repo",
                number=index,
                card_issue=5000 + index,
                revision=revision,
                event_id=event_id,
            )
            event_key = event_key_sha256(identity)
            bundle = root / ("bundle-%02d" % index)
            task = build_task(
                action=action,
                selection=resolve_selection(action, "repo"),
                prompt_path=str(prompt),
                bundle_dir=str(bundle),
                output_path=str(bundle / "task.json"),
                owner="owner",
                repo="repo",
                number=index,
                target_kind=kind,
                revision=revision,
                wheelhouse_revision=WHEELHOUSE_SHA,
                event_key=event_key,
                target_file=str(target) if with_target else "",
                repository_dir=str(repo) if with_repo else "",
                repository_commit=commit if with_repo else "",
                repair_kind=repair_kind,
            )
            validate_contract(task, "AgentTask")
            candidate = task["spec"]["selection"]["candidates"][0]
            check("gate %02d: %s exact Claude-only selection" % (index, action), candidate["adapter"] == "claude-action-compat" and candidate["provider"] == "anthropic" and candidate["model"] == "claude-sonnet-4-6" and candidate["allowModelAlias"] is False and task["spec"]["selection"]["fallback"] == {"mode": "none"})
            check("gate %02d: event key bound into task" % index, task["metadata"]["idempotencyKey"] == event_key)
            handoff = root / ("handoff-%02d" % index)
            packed = pack(str(bundle / "task.json"), str(bundle), str(handoff), '["owner/repo"]' if action.endswith(".search") else "[]")
            before = canonical_sha256(json.loads((handoff / "manifest.json").read_text(encoding="utf-8")))
            for python in interpreters:
                extracted = root / ("extract-%02d-%s" % (index, Path(python).name))
                zip_copy(handoff, extracted)
                workspace = root / ("workspace-%02d-%s" % (index, Path(python).name))
                cwd = root / ("cwd-%02d-%s" % (index, Path(python).name))
                cache = root / ("cache-%02d-%s" % (index, Path(python).name))
                cwd.mkdir()
                cache.mkdir()
                env = os.environ.copy()
                env.pop("PYTHONDONTWRITEBYTECODE", None)
                env["PYTHONPYCACHEPREFIX"] = str(cache)
                env["PYTHONPATH"] = str(extracted / "runtime")
                result = subprocess.run(
                    [python, "-m", "agent_runtime.claude_handoff", "hydrate", "--handoff", str(extracted), "--workspace", str(workspace)],
                    cwd=cwd,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=90,
                )
                check("gate %02d: %s fresh hydrate" % (index, Path(python).name), result.returncode == 0)
                check("gate %02d: %s external bytecode" % (index, Path(python).name), any(path.is_file() for path in cache.rglob("*.pyc")))
                _checked, checked_task = verify(str(extracted))
                extracted_manifest = json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))
                check("gate %02d: %s handoff unchanged" % (index, Path(python).name), extracted_manifest["manifestSha256"] == packed["manifestSha256"] and canonical_sha256(checked_task) == canonical_sha256(task))
            after = canonical_sha256(json.loads((handoff / "manifest.json").read_text(encoding="utf-8")))
            check("gate %02d: source handoff unchanged" % index, before == after)

    if FAILURES:
        raise SystemExit("%d outage recovery gate checks failed" % len(FAILURES))
    print("\nall outage recovery gate tests passed")


if __name__ == "__main__":
    main()
