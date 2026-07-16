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


def path_is_hidden(relative: str) -> bool:
    """Match pinned actions/upload-artifact@v4 excludeHiddenFiles path semantics."""
    return any(part.startswith(".") for part in Path(relative).parts)


def upload_artifact_copy(source: Path, destination: Path, *, include_hidden_files: bool) -> None:
    """Emulate actions/upload-artifact@v4 hosted transport over a signed handoff tree."""
    archive = destination.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source).as_posix()
            if not include_hidden_files and path_is_hidden(relative):
                continue
            handle.write(path, relative)
    destination.mkdir()
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(destination)


def inline_preimport_scan(root: Path, expected_manifest_sha: str) -> str | None:
    """Child claude-model.yml pre-import exact-manifest scan (stdlib only)."""
    import hashlib

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return "handoff manifest identity mismatch"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    canonical = json.dumps(manifest["files"], sort_keys=True, separators=(",", ":")).encode()
    if manifest.get("manifestSha256") != expected_manifest_sha or hashlib.sha256(canonical).hexdigest() != expected_manifest_sha:
        return "handoff manifest identity mismatch"
    expected = {row["path"]: row for row in manifest["files"]}
    if len(expected) != len(manifest["files"]) or len(expected) > 32000 or sum(row["bytes"] for row in expected.values()) > 220000000:
        return "handoff manifest bound exceeded"
    observed = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            return "handoff symlink rejected"
        if path.is_file() and path.name != "manifest.json":
            rel = path.relative_to(root).as_posix()
            size = path.stat().st_size
            if rel not in expected or size != expected[rel]["bytes"]:
                return "handoff file bound mismatch"
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1048576), b""):
                    digest.update(chunk)
            observed[rel] = {"path": rel, "bytes": size, "sha256": digest.hexdigest()}
    if observed != expected:
        return "handoff manifest mismatch"
    return None


def missing_hidden_tops(source: Path, delivered: Path) -> dict[str, int]:
    """Count missing signed files by first hidden path segment (prod #1366 shape)."""
    signed = {
        path.relative_to(source).as_posix()
        for path in source.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    present = {
        path.relative_to(delivered).as_posix()
        for path in delivered.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    missing = sorted(signed - present)
    tops: dict[str, int] = {}
    for rel in missing:
        hidden_parts = [part for part in Path(rel).parts if part.startswith(".")]
        if not hidden_parts:
            # Unexpected non-hidden miss: surface under a fail marker key.
            tops["__non_hidden__"] = tops.get("__non_hidden__", 0) + 1
            continue
        top = hidden_parts[0]
        tops[top] = tops.get(top, 0) + 1
    return tops


def assert_immutable_handoff_upload_includes_hidden_files() -> None:
    action = Path(__file__).resolve().parents[1] / ".github/actions/claude-model-call/action.yml"
    text = action.read_text(encoding="utf-8")
    marker = "name: Upload immutable model handoff"
    start = text.find(marker)
    check("gate: immutable handoff upload step present", start >= 0)
    if start < 0:
        return
    rest = text[start:]
    next_step = rest.find("\n    - ", len(marker))
    block = rest if next_step < 0 else rest[:next_step]
    check(
        "gate: immutable handoff upload explicitly sets include-hidden-files: true",
        "include-hidden-files: true" in block
        and "uses: actions/upload-artifact@v4" in block,
    )


def main():
    assert_immutable_handoff_upload_includes_hidden_files()
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
        (repo / ".agents/skills/demo/helper.py").write_text("print('demo')\n", encoding="utf-8")
        (repo / ".gitignore").write_text(".pycache/\n", encoding="utf-8")
        (repo / ".github/workflows").mkdir(parents=True)
        (repo / ".github/workflows/ci.yml").write_text("name: ci\non: push\n", encoding="utf-8")
        (repo / "src").mkdir()
        (repo / "src/app.py").write_text("print('app')\n", encoding="utf-8")
        (repo / "bin").mkdir()
        (repo / "bin/tool").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
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

        # Hosted upload-artifact@v4 transport: default drops signed dot-paths (prod #1366).
        transport_action = "triage.pr.search"
        transport_identity = normalized_event_identity(
            action=transport_action,
            owner="owner",
            repo="repo",
            number=9001,
            card_issue=9001,
            revision=commit,
            event_id="",
        )
        transport_event_key = event_key_sha256(transport_identity)
        transport_bundle = root / "bundle-transport"
        transport_task = build_task(
            action=transport_action,
            selection=resolve_selection(transport_action, "repo"),
            prompt_path=str(prompt),
            bundle_dir=str(transport_bundle),
            output_path=str(transport_bundle / "task.json"),
            owner="owner",
            repo="repo",
            number=9001,
            target_kind="pr-review",
            revision=commit,
            wheelhouse_revision=WHEELHOUSE_SHA,
            event_key=transport_event_key,
            target_file=str(target),
            repository_dir=str(repo),
            repository_commit=commit,
            repair_kind="pr",
        )
        validate_contract(transport_task, "AgentTask")
        transport_handoff = root / "handoff-transport"
        transport_packed = pack(
            str(transport_bundle / "task.json"),
            str(transport_bundle),
            str(transport_handoff),
            '["owner/repo"]',
        )
        signed_paths = {
            row["path"]
            for row in json.loads((transport_handoff / "manifest.json").read_text(encoding="utf-8"))["files"]
        }
        # Materialized Git-object snapshot embeds committed hidden roots + link expansions.
        required_hidden_suffixes = (
            "/.agents/skills/demo/SKILL.md",
            "/.agents/skills/demo/helper.py",
            "/.claude/skills/demo/SKILL.md",
            "/.claude/skills/demo/helper.py",
            "/.github/workflows/ci.yml",
            "/.gitignore",
        )
        check(
            "gate transport: signed paths include .agents/.claude/.github/.gitignore materializations",
            all(any(path.endswith(suffix) for path in signed_paths) for suffix in required_hidden_suffixes),
        )

        filtered = root / "extract-transport-hidden-false"
        upload_artifact_copy(transport_handoff, filtered, include_hidden_files=False)
        filtered_error = inline_preimport_scan(filtered, transport_packed["manifestSha256"])
        tops = missing_hidden_tops(transport_handoff, filtered)
        check(
            "gate transport: include-hidden-files false yields handoff manifest mismatch",
            filtered_error == "handoff manifest mismatch",
        )
        check(
            "gate transport: filtered missing set is only signed hidden tops",
            filtered_error == "handoff manifest mismatch"
            and set(tops) >= {".agents", ".claude", ".github", ".gitignore"}
            and all(part.startswith(".") for part in tops)
            and tops.get(".agents", 0) >= 1
            and tops.get(".claude", 0) >= 1
            and tops.get(".github", 0) >= 1
            and tops.get(".gitignore", 0) >= 1,
        )
        filtered_present = {
            path.relative_to(filtered).as_posix()
            for path in filtered.rglob("*")
            if path.is_file() and path.name != "manifest.json"
        }
        check(
            "gate transport: filtered delivery has zero extras vs signed",
            filtered_present <= signed_paths,
        )

        included = root / "extract-transport-hidden-true"
        upload_artifact_copy(transport_handoff, included, include_hidden_files=True)
        included_error = inline_preimport_scan(included, transport_packed["manifestSha256"])
        check(
            "gate transport: include-hidden-files true passes exact pre-import equality",
            included_error is None,
        )
        included_paths = {
            path.relative_to(included).as_posix()
            for path in included.rglob("*")
            if path.is_file() and path.name != "manifest.json"
        }
        check(
            "gate transport: include-hidden-files true preserves every signed path",
            included_paths == signed_paths,
        )
        for python in interpreters:
            workspace = root / ("workspace-transport-%s" % Path(python).name)
            cwd = root / ("cwd-transport-%s" % Path(python).name)
            cache = root / ("cache-transport-%s" % Path(python).name)
            delivered = root / ("extract-transport-hydrate-%s" % Path(python).name)
            upload_artifact_copy(transport_handoff, delivered, include_hidden_files=True)
            cwd.mkdir()
            cache.mkdir()
            env = os.environ.copy()
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            env["PYTHONPYCACHEPREFIX"] = str(cache)
            env["PYTHONPATH"] = str(delivered / "runtime")
            result = subprocess.run(
                [python, "-m", "agent_runtime.claude_handoff", "hydrate", "--handoff", str(delivered), "--workspace", str(workspace)],
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=90,
            )
            check(
                "gate transport: %s fresh hydrate after include-hidden true" % Path(python).name,
                result.returncode == 0,
            )
            check(
                "gate transport: %s external bytecode after include-hidden true" % Path(python).name,
                any(path.is_file() for path in cache.rglob("*.pyc")),
            )
            _checked, checked_task = verify(str(delivered))
            delivered_manifest = json.loads((delivered / "manifest.json").read_text(encoding="utf-8"))
            check(
                "gate transport: %s handoff unchanged after hydrate" % Path(python).name,
                delivered_manifest["manifestSha256"] == transport_packed["manifestSha256"]
                and canonical_sha256(checked_task) == canonical_sha256(transport_task),
            )

        # Fail-closed denials stay strict under the correct transport.
        deny_missing = root / "extract-transport-deny-missing"
        upload_artifact_copy(transport_handoff, deny_missing, include_hidden_files=True)
        (deny_missing / "handoff.json").unlink()
        check(
            "gate transport: missing signed file remains fail-closed",
            inline_preimport_scan(deny_missing, transport_packed["manifestSha256"]) == "handoff manifest mismatch",
        )
        deny_extra = root / "extract-transport-deny-extra"
        upload_artifact_copy(transport_handoff, deny_extra, include_hidden_files=True)
        (deny_extra / "extra-untrusted.txt").write_text("nope\n", encoding="utf-8")
        check(
            "gate transport: extra file remains fail-closed",
            inline_preimport_scan(deny_extra, transport_packed["manifestSha256"]) == "handoff file bound mismatch",
        )
        deny_tamper = root / "extract-transport-deny-tamper"
        upload_artifact_copy(transport_handoff, deny_tamper, include_hidden_files=True)
        handoff_json = deny_tamper / "handoff.json"
        original = handoff_json.read_bytes()
        handoff_json.write_bytes(original + b"\n")
        # Size change trips the bound check before digest equality when bytes differ.
        tamper_error = inline_preimport_scan(deny_tamper, transport_packed["manifestSha256"])
        check(
            "gate transport: tampered file remains fail-closed",
            tamper_error in ("handoff file bound mismatch", "handoff manifest mismatch"),
        )

    if FAILURES:
        raise SystemExit("%d outage recovery gate checks failed" % len(FAILURES))
    print("\nall outage recovery gate tests passed")


if __name__ == "__main__":
    main()
