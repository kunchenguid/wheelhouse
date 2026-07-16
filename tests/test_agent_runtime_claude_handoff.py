#!/usr/bin/env python3
"""Offline bounded Claude model handoff checks.

Production-faithful coverage for the packaged-runtime self-mutation defect:
pack, real ZIP transport, extract, and hydrate from an unrelated clean working
directory in a fresh interpreter must leave the signed tree byte-identical
while writing bytecode only under a disjoint PYTHONPYCACHEPREFIX.
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


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def _resolve_python(candidate: str) -> str | None:
    if os.path.sep in candidate or candidate.startswith("."):
        path = Path(candidate)
        if path.is_file():
            return str(path.resolve())
        return None
    found = shutil.which(candidate)
    return str(Path(found).resolve()) if found else None


def available_pythons() -> list[tuple[str, str]]:
    """Return (version, absolute path) for Python 3.12 and 3.13 when present."""
    candidates = [
        "python3.12",
        "python3.13",
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12",
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13",
        str(Path.home() / ".local/share/uv/python/cpython-3.12-macos-aarch64-none/bin/python3.12"),
        str(Path.home() / ".local/share/uv/python/cpython-3.13-macos-aarch64-none/bin/python3.13"),
        ACTIVE_PYTHON,
    ]
    # Prefer `uv python find` when available.
    for minor in ("3.12", "3.13"):
        try:
            found = subprocess.run(
                ["uv", "python", "find", minor],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.strip()
            if found:
                candidates.insert(0, found)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

    by_version: dict[str, str] = {}
    for candidate in candidates:
        path = _resolve_python(candidate)
        if not path:
            continue
        try:
            version = subprocess.run(
                [path, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        if version in ("3.12", "3.13") and version not in by_version:
            by_version[version] = path
            print("python available: %s (%s)" % (path, version))
    ordered = [(version, by_version[version]) for version in ("3.12", "3.13") if version in by_version]
    if not ordered:
        # Fall back to active interpreter so the suite still runs elsewhere.
        try:
            version = subprocess.run(
                [ACTIVE_PYTHON, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            version = "unknown"
        ordered = [(version, str(Path(ACTIVE_PYTHON).resolve()))]
        print("python available (fallback): %s (%s)" % (ordered[0][1], version))
    return ordered


def build_fixture(root: Path, action: str = "deep-review.search", include_vision: bool = False) -> tuple[Path, dict, dict]:
    prompt = root / "prompt.txt"
    target = root / "target.txt"
    repository = root / "repository"
    prompt.write_text("Inspect the immutable input.\n", encoding="utf-8")
    target.write_text("bounded target\n", encoding="utf-8")
    vision = root / "vision.md"
    if include_vision:
        vision.write_text("Project vision.\n", encoding="utf-8")
    repository.mkdir()
    (repository / "source.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "wheelhouse-test@example.com"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Wheelhouse Test"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
    repo_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().lower()
    subprocess.run(["git", "checkout", "--detach", repo_commit], cwd=repository, check=True, capture_output=True)
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
        revision=repo_commit,
        wheelhouse_revision="30271b6907e568419cdc48694a11b0c2f699b433",
        event_key="a" * 64,
        target_file=str(target),
        repository_dir=str(repository),
        repository_commit=repo_commit,
        vision_file=str(vision) if include_vision else None,
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
    pycache_prefix: Path | None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(handoff / "runtime")
    # Bytecode stays enabled: production robustness is a redirected cache, not -B.
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    env.pop("PYTHONPYCACHEPREFIX", None)
    if pycache_prefix is not None:
        pycache_prefix.mkdir(parents=True, exist_ok=True)
        env["PYTHONPYCACHEPREFIX"] = str(pycache_prefix)
    command = [
        python,
        "-m",
        "agent_runtime.claude_handoff",
        "hydrate",
        "--handoff",
        str(handoff),
        "--workspace",
        str(workspace),
    ]
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def cache_files(prefix: Path) -> list[Path]:
    if not prefix.exists():
        return []
    return [path for path in prefix.rglob("*") if path.is_file()]


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
        declared_inputs = sorted(item["logicalPath"] for item in task["spec"]["inputs"])
        check(
            "handoff: fresh workspace receives all and only declared inputs",
            sorted(path.name for path in workspace.iterdir()) == declared_inputs
            and hydrated["action"] == "deep-review.search",
        )
        before = workspace_input_observation(task, str(workspace))
        signed_paths = {item["logicalPath"] for item in task["spec"]["inputs"]}
        check(
            "handoff: signed inputs are target.txt, target-src, repository-provenance.json",
            signed_paths == {"target.txt", "target-src", "repository-provenance.json"},
        )

        # Option A: unrelated scratch / declared outputs / .git must not change signed evidence.
        (workspace / "search-request.json").write_text('{"query":"related issues"}\n', encoding="utf-8")
        (workspace / "decision.json").write_text('{"mode":"action"}\n', encoding="utf-8")
        (workspace / "search-response.json").write_text('{"hits":[]}\n', encoding="utf-8")
        (workspace / "rate-state.json").write_text("{}\n", encoding="utf-8")
        (workspace / ".claude").mkdir()
        (workspace / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
        (workspace / ".git" / "config").write_text("[user]\n\tname = wheelhouse\n", encoding="utf-8")
        after_scratch = workspace_input_observation(task, str(workspace))
        check(
            "handoff: undeclared scratch plus unchanged signed inputs keeps equal observation",
            after_scratch == before,
        )
        check(
            "handoff: search-request.json does not change signed-input observation",
            after_scratch == before,
        )
        check(
            "handoff: decision.json does not change signed-input observation",
            after_scratch == before,
        )
        check(
            "handoff: .git metadata does not change signed-input observation",
            after_scratch == before,
        )

        vision_root = root / "vision-case"
        vision_root.mkdir()
        vision_handoff, _, vision_task = build_fixture(vision_root, include_vision=True)
        vision_workspace = vision_root / "workspace"
        hydrate(str(vision_handoff), str(vision_workspace))
        vision_before = workspace_input_observation(vision_task, str(vision_workspace))
        vision_paths = {item["logicalPath"] for item in vision_task["spec"]["inputs"]}
        (vision_workspace / "vision.md").chmod(0o600)
        (vision_workspace / "vision.md").write_text("Changed vision scratch.\n", encoding="utf-8")
        check(
            "handoff: vision.md is hydrated but excluded from signed-input observation",
            "vision.md" in vision_paths
            and workspace_input_observation(vision_task, str(vision_workspace)) == vision_before,
        )

        def observation_fails(label: str, mutator) -> None:
            mutator()
            try:
                workspace_input_observation(task, str(workspace))
            except ContractError:
                check(label, True)
            else:
                check(label, False)

        (workspace / "target.txt").chmod(0o600)
        (workspace / "target.txt").write_text("mutated target\n", encoding="utf-8")
        observation_fails(
            "handoff: post-action target.txt byte mutation fails closed",
            lambda: None,
        )
        (workspace / "target.txt").write_text("bounded target\n", encoding="utf-8")
        (workspace / "target.txt").chmod(0o400)
        check("handoff: stable input observation is deterministic", workspace_input_observation(task, str(workspace)) == before)

        observation_fails(
            "handoff: target.txt mode change fails closed",
            lambda: (workspace / "target.txt").chmod(0o600),
        )
        (workspace / "target.txt").chmod(0o400)

        original_target = (workspace / "target.txt").read_bytes()
        (workspace / "target.txt").unlink()
        observation_fails(
            "handoff: target.txt deletion fails closed",
            lambda: None,
        )
        (workspace / "target.txt").write_bytes(original_target)
        (workspace / "target.txt").chmod(0o400)

        (workspace / "target.txt").unlink()
        (workspace / "target.txt").symlink_to("/etc/hosts")
        observation_fails(
            "handoff: target.txt symlink replacement fails closed",
            lambda: None,
        )
        (workspace / "target.txt").unlink()
        (workspace / "target.txt").write_bytes(original_target)
        (workspace / "target.txt").chmod(0o400)

        provenance = workspace / "repository-provenance.json"
        original_provenance = provenance.read_bytes()
        provenance.chmod(0o600)
        provenance.write_text(json.dumps({"tampered": True}) + "\n", encoding="utf-8")
        observation_fails(
            "handoff: repository-provenance.json mutation fails closed",
            lambda: None,
        )
        provenance.write_bytes(original_provenance)
        provenance.chmod(0o400)

        target_src = workspace / "target-src"
        nested = target_src / "source.py"
        original_nested = nested.read_bytes()
        nested.chmod(0o600)
        nested.write_text("value = 2\n", encoding="utf-8")
        observation_fails(
            "handoff: target-src file byte mutation fails closed",
            lambda: None,
        )
        nested.write_bytes(original_nested)
        nested.chmod(0o400)

        observation_fails(
            "handoff: target-src file mode change fails closed",
            lambda: nested.chmod(0o600),
        )
        nested.chmod(0o400)

        os.chmod(target_src, 0o700)
        extra = target_src / "extra_untracked.py"
        extra.write_text("x = 1\n", encoding="utf-8")
        extra.chmod(0o400)
        os.chmod(target_src, 0o500)
        observation_fails(
            "handoff: extra file under target-src fails closed",
            lambda: None,
        )
        os.chmod(target_src, 0o700)
        extra.unlink()
        os.chmod(target_src, 0o500)

        os.chmod(target_src, 0o700)
        nested.unlink()
        nested.symlink_to("/etc/hosts")
        os.chmod(target_src, 0o500)
        observation_fails(
            "handoff: target-src symlink introduction fails closed",
            lambda: None,
        )
        os.chmod(target_src, 0o700)
        nested.unlink()
        nested.write_bytes(original_nested)
        nested.chmod(0o400)
        os.chmod(target_src, 0o500)

        # Directory root mode change (0500 required).
        os.chmod(target_src, 0o700)
        observation_fails(
            "handoff: target-src directory mode change fails closed",
            lambda: None,
        )
        os.chmod(target_src, 0o500)

        check(
            "handoff: signed-input observation restored after fail-closed cases",
            workspace_input_observation(task, str(workspace)) == before,
        )

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
        for path in root.rglob("*"):
            if not path.is_symlink():
                os.chmod(path, 0o700 if path.is_dir() else 0o600)

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source_handoff, metadata, task = build_fixture(root)
        baseline_files = signed_files(source_handoff)
        baseline_manifest = metadata["manifestSha256"]
        archive = zip_round_trip(source_handoff, root / "zipped-once")
        check("handoff: real ZIP archive is non-empty", archive.is_file() and archive.stat().st_size > 0)

        pythons = available_pythons()
        versions = {version for version, _ in pythons}
        check("handoff: Python 3.12 is available for fresh-process hydrate", "3.12" in versions)
        check("handoff: Python 3.13 is available for fresh-process hydrate", "3.13" in versions)

        clean_cwd = root / "unrelated-cwd"
        clean_cwd.mkdir()

        # Without a redirected cache, a fresh interpreter self-mutates and fail-closed.
        mutated = root / "mutated-extract"
        zip_round_trip(source_handoff, mutated)
        before_mut = signed_files(mutated)
        mut_result = hydrate_subprocess(
            pythons[0][1],
            mutated,
            root / "mutated-workspace",
            clean_cwd,
            pycache_prefix=None,
        )
        after_mut = signed_files(mutated)
        pycache_created = any("__pycache__" in path or path.endswith(".pyc") for path in after_mut)
        check(
            "handoff: fresh interpreter without PYTHONPYCACHEPREFIX self-mutates signed tree",
            mut_result.returncode != 0 and pycache_created and after_mut != before_mut,
        )
        check(
            "handoff: self-mutation is rejected by complete file-set verify",
            "handoff manifest verification failed" in (mut_result.stderr or mut_result.stdout or ""),
        )

        # With a disjoint PYTHONPYCACHEPREFIX, ZIP hydrate succeeds: nonzero external
        # cache, zero handoff mutation. Bytecode remains enabled.
        for version, python in pythons:
            extract = root / ("clean-extract-%s" % version.replace(".", "_"))
            zip_round_trip(source_handoff, extract)
            before = signed_files(extract)
            workspace = root / ("clean-workspace-%s" % version.replace(".", "_"))
            cache = root / ("external-cache-%s" % version.replace(".", "_"))
            result = hydrate_subprocess(
                python,
                extract,
                workspace,
                clean_cwd,
                pycache_prefix=cache,
            )
            after = signed_files(extract)
            external = cache_files(cache)
            check(
                "handoff: Python %s ZIP hydrate succeeds with disjoint PYTHONPYCACHEPREFIX" % version,
                result.returncode == 0 and "deep-review.search" in (result.stdout or ""),
            )
            check(
                "handoff: Python %s leaves signed tree identical (zero mutation)" % version,
                before == after and not any("__pycache__" in path or path.endswith(".pyc") for path in after),
            )
            check(
                "handoff: Python %s writes nonzero external bytecode cache" % version,
                len(external) > 0 and any(path.suffix == ".pyc" for path in external),
            )
            check(
                "handoff: Python %s external cache is path-disjoint from signed handoff" % version,
                all(not str(path.resolve()).startswith(str(extract.resolve()) + os.sep) for path in external),
            )
            verified, _ = verify(str(extract))
            check(
                "handoff: Python %s re-verify matches packed manifest" % version,
                verified["taskSha256"] == metadata["taskSha256"] and baseline_manifest == metadata["manifestSha256"],
            )

        # Twenty repeated fresh-process round trips on the primary interpreter.
        primary_version, primary_python = pythons[0]
        repeated_ok = True
        for attempt in range(20):
            extract = root / ("repeat-%d" % attempt)
            if extract.exists():
                shutil.rmtree(extract)
            zip_round_trip(source_handoff, extract)
            before = signed_files(extract)
            cache = root / ("repeat-cache-%d" % attempt)
            result = hydrate_subprocess(
                primary_python,
                extract,
                root / ("repeat-ws-%d" % attempt),
                clean_cwd,
                pycache_prefix=cache,
            )
            after = signed_files(extract)
            if (
                result.returncode != 0
                or before != after
                or before != baseline_files
                or not cache_files(cache)
            ):
                repeated_ok = False
                break
            try:
                _, _ = verify(str(extract))
            except ContractError:
                repeated_ok = False
                break
        check(
            "handoff: 20 repeated fresh-process ZIP hydrates keep identical signed files with external cache",
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

        extract = root / "dotdot"
        zip_round_trip(source_handoff, extract)
        for path in extract.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                os.chmod(path, 0o700)
            elif path.is_file() and not path.is_symlink():
                os.chmod(path, 0o600)
        sneaky = (extract / "runtime" / "agent_runtime" / ".." / "sneaky.py").resolve()
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
        good_result = hydrate_subprocess(
            primary_python,
            good,
            root / "good-ws",
            clean_cwd,
            pycache_prefix=root / "good-cache",
        )
        check("handoff: control hydrate after fail-closed cases still succeeds", good_result.returncode == 0)
        check(
            "handoff: control hydrate still uses nonzero external cache",
            len(cache_files(root / "good-cache")) > 0,
        )

    if FAILURES:
        raise SystemExit("%d Claude handoff checks failed:\n- %s" % (len(FAILURES), "\n- ".join(FAILURES)))
    print("\nall Claude handoff tests passed")


if __name__ == "__main__":
    main()
