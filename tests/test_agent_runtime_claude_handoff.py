#!/usr/bin/env python3
"""Offline bounded Claude model handoff checks.

Production-faithful coverage for the packaged-runtime self-mutation defect:
pack, real ZIP transport, extract, and hydrate from an unrelated clean working
directory in a fresh interpreter must leave the signed tree byte-identical.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.claude_handoff import hydrate, pack, verify, workspace_input_observation
from agent_runtime.config import resolve_selection
from agent_runtime.contract import ContractError, canonical_sha256
from agent_runtime.task_builder import build_task

FAILURES = []
ACTIVE_PYTHON = sys.executable
CANDIDATE_PYTHONS = [
    ACTIVE_PYTHON,
    "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13",
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12",
    "python3.12",
    "python3.13",
]


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def available_pythons() -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for candidate in CANDIDATE_PYTHONS:
        path = shutil.which(candidate) if os.path.sep not in candidate else candidate
        if not path or not Path(path).is_file():
            continue
        resolved = str(Path(path).resolve())
        if resolved in seen:
            continue
        try:
            version = subprocess.run(
                [resolved, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        seen.add(resolved)
        found.append(resolved)
        print("python available: %s (%s)" % (resolved, version))
    return found or [ACTIVE_PYTHON]


def build_fixture(root: Path, action: str = "deep-review.search") -> tuple[Path, dict, dict]:
    prompt = root / "prompt.txt"
    target = root / "target.txt"
    repository = root / "repository"
    prompt.write_text("Inspect the immutable input.\n", encoding="utf-8")
    target.write_text("bounded target\n", encoding="utf-8")
    repository.mkdir()
    (repository / "source.py").write_text("value = 1\n", encoding="utf-8")
    bundle = root / "bundle"
    task = build_task(
        action=action,
        selection=resolve_selection(action, "repo"),
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
    return handoff, metadata, task


def signed_files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def zip_round_trip(source: Path, destination: Path) -> Path:
    archive = destination.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for path in source.rglob("*"):
            if path.is_file():
                handle.write(path, path.relative_to(source).as_posix())
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(destination)
    return archive


def hydrate_subprocess(
    python: str,
    handoff: Path,
    workspace: Path,
    cwd: Path,
    *,
    disable_bytecode: bool,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(handoff / "runtime")
    # Production host may otherwise enable bytecode; prove the flag wins.
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    if disable_bytecode:
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [python]
    if disable_bytecode:
        command.append("-B")
    command.extend(
        [
            "-m",
            "agent_runtime.claude_handoff",
            "hydrate",
            "--handoff",
            str(handoff),
            "--workspace",
            str(workspace),
        ]
    )
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        handoff, metadata, task = build_fixture(root)
        checked, checked_task = verify(str(handoff))
        check(
            "handoff: immutable task is content-bound",
            checked["taskSha256"] == canonical_sha256(task) == canonical_sha256(checked_task),
        )
        check("handoff: search scope is explicit and bounded", metadata["allowedRepos"] == ["owner/repo"])
        workspace = root / "workspace"
        hydrated = hydrate(str(handoff), str(workspace))
        check(
            "handoff: fresh workspace receives only declared inputs",
            sorted(path.name for path in workspace.iterdir()) == ["target-src", "target.txt"]
            and hydrated["action"] == "deep-review.search",
        )
        before = workspace_input_observation(task, str(workspace))
        (workspace / "target.txt").chmod(0o600)
        (workspace / "target.txt").write_text("mutated target\n", encoding="utf-8")
        try:
            workspace_input_observation(task, str(workspace))
        except ContractError:
            check("handoff: post-action input mutation fails closed", True)
        else:
            check("handoff: post-action input mutation fails closed", False)
        (workspace / "target.txt").write_text("bounded target\n", encoding="utf-8")
        (workspace / "target.txt").chmod(0o400)
        check("handoff: stable input observation is deterministic", workspace_input_observation(task, str(workspace)) == before)

        # Tamper, extra file, symlink, digest mismatch remain fail-closed.
        artifact = next(path for path in (handoff / "bundle" / "artifacts" / "sha256").iterdir() if path.is_file())
        for path in handoff.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                os.chmod(path, 0o700)
            elif path.is_file() and not path.is_symlink():
                os.chmod(path, 0o600)
        original = artifact.read_bytes()
        artifact.write_bytes(original + b"tamper")
        rejected = False
        try:
            verify(str(handoff))
        except ContractError:
            rejected = True
        check("handoff: artifact tampering fails closed", rejected)
        artifact.write_bytes(original)
        for path in handoff.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                os.chmod(path, 0o700)
            elif path.is_file() and not path.is_symlink():
                os.chmod(path, 0o600)

        extra = handoff / "runtime" / "agent_runtime" / "extra_unmanifested.py"
        extra.write_text("x = 1\n", encoding="utf-8")
        extra_rejected = False
        try:
            verify(str(handoff))
        except ContractError:
            extra_rejected = True
        check("handoff: unmanifested extra file fails closed", extra_rejected)
        extra.unlink()

        link = handoff / "runtime" / "agent_runtime" / "live_link.py"
        link.symlink_to("claude_handoff.py")
        link_rejected = False
        try:
            verify(str(handoff))
        except ContractError:
            link_rejected = True
        check("handoff: live symlink fails closed", link_rejected)
        link.unlink()

        manifest_path = handoff / "manifest.json"
        for path in handoff.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                os.chmod(path, 0o700)
            elif path.is_file() and not path.is_symlink():
                os.chmod(path, 0o600)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        bad = dict(manifest)
        bad["manifestSha256"] = "0" * 64
        manifest_path.write_text(json.dumps(bad, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        digest_rejected = False
        try:
            verify(str(handoff))
        except ContractError:
            digest_rejected = True
        check("handoff: manifest digest mismatch fails closed", digest_rejected)
        # Restore a correct manifest for later ZIP work by repacking.
        for path in root.rglob("*"):
            if not path.is_symlink():
                os.chmod(path, 0o700 if path.is_dir() else 0o600)

    # Fresh pack for transport tests (previous fixture may be mode-mutated).
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source_handoff, metadata, task = build_fixture(root)
        baseline_files = signed_files(source_handoff)
        baseline_manifest = metadata["manifestSha256"]
        archive = zip_round_trip(source_handoff, root / "zipped-once")
        check("handoff: real ZIP archive is non-empty", archive.is_file() and archive.stat().st_size > 0)

        pythons = available_pythons()
        versions = []
        for python in pythons:
            try:
                versions.append(
                    subprocess.run(
                        [python, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    ).stdout.strip()
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                versions.append("unknown")
        check(
            "handoff: exercises active parent Python and Python 3.12 when available",
            ACTIVE_PYTHON in pythons or any(Path(p).resolve() == Path(ACTIVE_PYTHON).resolve() for p in pythons),
        )
        if any(version.startswith("3.12") for version in versions):
            check("handoff: Python 3.12 is available for fresh-process hydrate", True)
        else:
            check("handoff: Python 3.12 unavailable here; active parent Python still covered", True)

        # Without -B, a fresh interpreter self-mutates and fail-closed (card 631 class).
        mutated = root / "mutated-extract"
        zip_round_trip(source_handoff, mutated)
        clean_cwd = root / "unrelated-cwd"
        clean_cwd.mkdir()
        before_mut = signed_files(mutated)
        mut_result = hydrate_subprocess(
            pythons[0],
            mutated,
            root / "mutated-workspace",
            clean_cwd,
            disable_bytecode=False,
        )
        after_mut = signed_files(mutated)
        pycache_created = any("__pycache__" in path or path.endswith(".pyc") for path in after_mut)
        check(
            "handoff: fresh interpreter without -B self-mutates signed tree",
            mut_result.returncode != 0 and pycache_created and after_mut != before_mut,
        )
        check(
            "handoff: self-mutation is rejected by complete file-set verify",
            "handoff manifest verification failed" in (mut_result.stderr or mut_result.stdout or ""),
        )

        # With -B / PYTHONDONTWRITEBYTECODE, ZIP round-trip hydrate succeeds and is pure.
        for index, python in enumerate(pythons):
            extract = root / ("clean-extract-%d" % index)
            zip_round_trip(source_handoff, extract)
            before = signed_files(extract)
            workspace = root / ("clean-workspace-%d" % index)
            result = hydrate_subprocess(python, extract, workspace, clean_cwd, disable_bytecode=True)
            after = signed_files(extract)
            check(
                "handoff: fresh %s ZIP hydrate succeeds with no-bytecode" % Path(python).name,
                result.returncode == 0 and "deep-review.search" in (result.stdout or ""),
            )
            check(
                "handoff: fresh %s leaves signed tree identical" % Path(python).name,
                before == after and not any("__pycache__" in path or path.endswith(".pyc") for path in after),
            )
            verified, _ = verify(str(extract))
            check(
                "handoff: fresh %s re-verify matches packed manifest" % Path(python).name,
                verified["taskSha256"] == metadata["taskSha256"] and baseline_manifest == metadata["manifestSha256"],
            )

        # Twenty repeated fresh-process round trips: identical manifest, zero new signed files.
        repeated_ok = True
        for attempt in range(20):
            extract = root / ("repeat-%d" % attempt)
            if extract.exists():
                shutil.rmtree(extract)
            zip_round_trip(source_handoff, extract)
            before = signed_files(extract)
            result = hydrate_subprocess(
                pythons[0],
                extract,
                root / ("repeat-ws-%d" % attempt),
                clean_cwd,
                disable_bytecode=True,
            )
            after = signed_files(extract)
            if result.returncode != 0 or before != after or before != baseline_files:
                repeated_ok = False
                break
            try:
                _, _ = verify(str(extract))
            except ContractError:
                repeated_ok = False
                break
        check(
            "handoff: 20 repeated fresh-process ZIP hydrates keep identical signed files",
            repeated_ok,
        )

        # Path pollution / source mismatch stay fail-closed after extract.
        extract = root / "path-attack"
        zip_round_trip(source_handoff, extract)
        for path in extract.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                os.chmod(path, 0o700)
            elif path.is_file() and not path.is_symlink():
                os.chmod(path, 0o600)
        handoff_json = extract / "handoff.json"
        meta = json.loads(handoff_json.read_text(encoding="utf-8"))
        meta["taskSha256"] = "f" * 64
        handoff_json.write_text(json.dumps(meta, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        source_rejected = False
        try:
            verify(str(extract))
        except ContractError:
            source_rejected = True
        check("handoff: source/task binding mismatch fails closed", source_rejected)

        extract = root / "traversal"
        zip_round_trip(source_handoff, extract)
        for path in extract.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                os.chmod(path, 0o700)
            elif path.is_file() and not path.is_symlink():
                os.chmod(path, 0o600)
        escape = extract / "bundle" / "escape.txt"
        escape.write_text("escape\n", encoding="utf-8")
        traversal_rejected = False
        try:
            verify(str(extract))
        except ContractError:
            traversal_rejected = True
        check("handoff: path pollution outside artifact set fails closed", traversal_rejected)

        # Nested absolute-looking traversal file under runtime must also fail closed.
        extract = root / "dotdot"
        zip_round_trip(source_handoff, extract)
        for path in extract.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                os.chmod(path, 0o700)
            elif path.is_file() and not path.is_symlink():
                os.chmod(path, 0o600)
        sneaky = extract / "runtime" / "agent_runtime" / ".." / "sneaky.py"
        sneaky = sneaky.resolve()
        if str(sneaky).startswith(str(extract.resolve())):
            sneaky.write_text("sneaky=1\n", encoding="utf-8")
            sneaky_rejected = False
            try:
                verify(str(extract))
            except ContractError:
                sneaky_rejected = True
            check("handoff: resolved parent-segment path pollution fails closed", sneaky_rejected)
        else:
            check("handoff: resolved parent-segment path pollution fails closed", False)

        good = root / "good-extract"
        zip_round_trip(source_handoff, good)
        good_result = hydrate_subprocess(pythons[0], good, root / "good-ws", clean_cwd, disable_bytecode=True)
        check("handoff: control hydrate after fail-closed cases still succeeds", good_result.returncode == 0)

    if FAILURES:
        raise SystemExit("%d Claude handoff checks failed:\n- %s" % (len(FAILURES), "\n- ".join(FAILURES)))
    print("\nall Claude handoff tests passed")


if __name__ == "__main__":
    main()
