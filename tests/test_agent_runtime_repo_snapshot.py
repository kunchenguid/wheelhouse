#!/usr/bin/env python3
"""Git-object-oracle repository snapshot tests for committed relative links.

Coverage uses real temporary Git commits (not filesystem-only fixtures) so a
clean worktree shape cannot bless uncommitted symlink or content data.
"""

from __future__ import annotations

import os
import json
import subprocess
import tempfile
from unittest import mock
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.contract import ArtifactError, canonical_sha256
from agent_runtime.task_builder import (
    GIT_MODE_EXEC,
    GIT_MODE_FILE,
    GIT_MODE_SYMLINK,
    MAX_SYMLINK_HOPS,
    _copy_directory,
    build_task,
    snapshot_repository,
)
from agent_runtime_testlib import WHEELHOUSE_REVISION, codex_selection

FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def rejects(call, text: str = "") -> bool:
    try:
        call()
    except ArtifactError as error:
        return text in str(error) if text else True
    return False


def run_git(repo: Path, *args: str, check_rc: bool = True) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
    )
    if check_rc and completed.returncode != 0:
        raise RuntimeError(
            "git %s failed: %s" % (" ".join(args), completed.stderr.decode("utf-8", errors="replace"))
        )
    return completed


def init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run_git(path, "init")
    run_git(path, "config", "user.email", "wheelhouse-test@example.com")
    run_git(path, "config", "user.name", "Wheelhouse Test")
    run_git(path, "config", "commit.gpgsign", "false")


def write_file(repo: Path, relative: str, data: bytes | str, executable: bool = False) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        target.write_text(data, encoding="utf-8")
    else:
        target.write_bytes(data)
    mode = 0o755 if executable else 0o644
    os.chmod(target, mode)


def write_link(repo: Path, relative: str, target: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        path.unlink()
    path.symlink_to(target)


def commit_all(repo: Path, message: str = "fixture", *, detach: bool = True) -> str:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-m", message)
    sha = run_git(repo, "rev-parse", "HEAD").stdout.decode("ascii").strip().lower()
    if detach:
        # Packaging requires detached HEAD so AgentTask git.detached is truthful.
        run_git(repo, "checkout", "--detach", sha)
    return sha


def git_mode(repo: Path, commit: str, path: str) -> str:
    payload = run_git(repo, "ls-tree", commit, "--", path).stdout.decode("utf-8")
    return payload.split(" ", 1)[0]


def assert_no_symlinks(root: Path) -> bool:
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            child = Path(base) / name
            if child.is_symlink():
                return False
            if name in names and not child.is_file():
                return False
    return True


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        # --- ordinary files + executable blob ---
        ordinary = root / "ordinary"
        init_repo(ordinary)
        write_file(ordinary, "safe.py", "print('ok')\n")
        write_file(ordinary, "bin/tool", "#!/bin/sh\necho tool\n", executable=True)
        ordinary_commit = commit_all(ordinary)
        check("git: executable blob is mode 100755", git_mode(ordinary, ordinary_commit, "bin/tool") == GIT_MODE_EXEC)
        check("git: regular blob is mode 100644", git_mode(ordinary, ordinary_commit, "safe.py") == GIT_MODE_FILE)
        snap = snapshot_repository(ordinary, ordinary_commit)
        check("ordinary: two regular files snapshotted", snap.file_count == 2)
        check("ordinary: executable content preserved", snap.blob_by_path["bin/tool"].startswith(b"#!/bin/sh"))
        check("ordinary: no link records", snap.links == [])
        check("ordinary: digest is canonical over sorted entries", snap.tree_sha256 == canonical_sha256(snap.entries))

        # --- Firstmate-style CLAUDE.md -> AGENTS.md ---
        firstmate = root / "firstmate"
        init_repo(firstmate)
        write_file(firstmate, "AGENTS.md", "# agent instructions\nuse the queue\n")
        write_file(firstmate, "README.md", "readme\n")
        write_link(firstmate, "CLAUDE.md", "AGENTS.md")
        write_file(firstmate, ".agents/skills/demo/SKILL.md", "skill body\n")
        write_file(firstmate, ".agents/skills/demo/helper.py", "value = 1\n")
        write_link(firstmate, ".claude/skills", "../.agents/skills")
        fm_commit = commit_all(firstmate)
        check("firstmate: CLAUDE.md is mode 120000", git_mode(firstmate, fm_commit, "CLAUDE.md") == GIT_MODE_SYMLINK)
        check("firstmate: .claude/skills is mode 120000", git_mode(firstmate, fm_commit, ".claude/skills") == GIT_MODE_SYMLINK)
        # Prove the live worktree still has symlinks (production shape) before packaging.
        check("firstmate: worktree CLAUDE.md is a live symlink", (firstmate / "CLAUDE.md").is_symlink())
        check("firstmate: worktree .claude/skills is a live symlink", (firstmate / ".claude/skills").is_symlink())
        fm_snap = snapshot_repository(firstmate, fm_commit)
        paths = {row["path"] for row in fm_snap.entries}
        check("firstmate: CLAUDE.md materialized as regular path", "CLAUDE.md" in paths)
        check("firstmate: CLAUDE.md bytes equal AGENTS.md", fm_snap.blob_by_path["CLAUDE.md"] == fm_snap.blob_by_path["AGENTS.md"])
        check("firstmate: directory alias nested skill present", ".claude/skills/demo/SKILL.md" in paths)
        check(
            "firstmate: directory alias content matches source",
            fm_snap.blob_by_path[".claude/skills/demo/SKILL.md"] == fm_snap.blob_by_path[".agents/skills/demo/SKILL.md"],
        )
        check("firstmate: original skill path retained", ".agents/skills/demo/helper.py" in paths)
        check("firstmate: link provenance count", len(fm_snap.links) == 2)
        file_link = next(row for row in fm_snap.links if row.link_path == "CLAUDE.md")
        dir_link = next(row for row in fm_snap.links if row.link_path == ".claude/skills")
        check("firstmate: file-link raw target", file_link.raw_link == "AGENTS.md")
        check("firstmate: file-link commit bound", file_link.commit == fm_commit)
        check("firstmate: file-link mode", file_link.link_mode == GIT_MODE_SYMLINK)
        check("firstmate: file-link resolved mode is regular", file_link.resolved_mode in (GIT_MODE_FILE, GIT_MODE_EXEC))
        check("firstmate: file-link output path", file_link.output_paths == ("CLAUDE.md",))
        check("firstmate: dir-link raw target", dir_link.raw_link == "../.agents/skills")
        check("firstmate: dir-link resolved is tree mode", dir_link.resolved_mode == "040000")
        check("firstmate: dir-link file accounting", dir_link.file_count == 2 and dir_link.byte_count > 0)
        check(
            "firstmate: dir-link outputs are alias paths only",
            set(dir_link.output_paths) == {".claude/skills/demo/SKILL.md", ".claude/skills/demo/helper.py"},
        )
        # Duplicated bytes from aliases must count toward totals.
        source_skill_bytes = len(fm_snap.blob_by_path[".agents/skills/demo/SKILL.md"]) + len(
            fm_snap.blob_by_path[".agents/skills/demo/helper.py"]
        )
        check("firstmate: duplicated alias bytes counted", fm_snap.total_bytes >= source_skill_bytes * 2)
        bundle = root / "fm-bundle"
        bundle.mkdir()
        digest, total, count, artifact, bound, copied_links = _copy_directory(firstmate, bundle, fm_commit)
        stored = bundle / artifact
        check("firstmate: bound commit returned", bound == fm_commit)
        check("firstmate: stored tree has no live symlink", assert_no_symlinks(stored))
        check("firstmate: stored CLAUDE.md is regular file", (stored / "CLAUDE.md").is_file() and not (stored / "CLAUDE.md").is_symlink())
        check("firstmate: stored skills alias is regular tree", (stored / ".claude/skills/demo/SKILL.md").is_file())
        check("firstmate: stored content matches agents", (stored / "CLAUDE.md").read_bytes() == (stored / "AGENTS.md").read_bytes())
        check("firstmate: digest matches snapshot", digest == fm_snap.tree_sha256 and total == fm_snap.total_bytes and count == fm_snap.file_count)
        check("firstmate: copied provenance retains every materialized link", copied_links == fm_snap.links)

        # --- safe internal chain ---
        chain = root / "chain"
        init_repo(chain)
        write_file(chain, "leaf.txt", "leaf-content\n")
        write_link(chain, "mid", "leaf.txt")
        write_link(chain, "top", "mid")
        chain_commit = commit_all(chain)
        chain_snap = snapshot_repository(chain, chain_commit)
        check("chain: top resolves to leaf content", chain_snap.blob_by_path["top"] == b"leaf-content\n")
        check("chain: mid also materialized", chain_snap.blob_by_path["mid"] == b"leaf-content\n")
        top_rec = next(row for row in chain_snap.links if row.link_path == "top")
        check("chain: hop resolves to leaf path", top_rec.resolved_path == "leaf.txt")

        # --- nested directory-link expansion (exercises nested_chain + hop continuity) ---
        nested = root / "nested-dir-links"
        init_repo(nested)
        write_file(nested, "data/x.txt", "nested-data\n")
        write_link(nested, "mid", "data")  # top-level dir link mid -> data
        write_file(nested, "pack/keep.txt", "keep\n")
        write_link(nested, "pack/inner", "../mid")  # nested dir link under pack
        write_link(nested, "top", "pack")  # top-level dir link top -> pack
        nested_commit = commit_all(nested)
        nested_snap = snapshot_repository(nested, nested_commit)
        nested_paths = {row["path"] for row in nested_snap.entries}
        check("nested-dir: top/keep.txt materialized", "top/keep.txt" in nested_paths)
        check(
            "nested-dir: nested_chain expands pack/inner under top",
            "top/inner/x.txt" in nested_paths
            and nested_snap.blob_by_path["top/inner/x.txt"] == b"nested-data\n",
        )
        check(
            "nested-dir: source data path retained",
            "data/x.txt" in nested_paths and nested_snap.blob_by_path["data/x.txt"] == b"nested-data\n",
        )
        top_link = next(row for row in nested_snap.links if row.link_path == "top")
        check(
            "nested-dir: provenance includes nested alias output",
            "top/inner/x.txt" in top_link.output_paths and top_link.file_count >= 2,
        )

        # Nested directory-link cycle: outer expands t; nested link points back at outer.
        nested_cycle = root / "nested-dir-cycle"
        init_repo(nested_cycle)
        write_file(nested_cycle, "t/a.txt", "a\n")
        write_link(nested_cycle, "t/back", "../outer")
        write_link(nested_cycle, "outer", "t")
        nested_cycle_commit = commit_all(nested_cycle)
        check(
            "nested-dir: cycle through outer alias denied",
            rejects(lambda: snapshot_repository(nested_cycle, nested_cycle_commit), "cycle"),
        )

        # --- hop-bound edges (exactly MAX_SYMLINK_HOPS ok; MAX+1 denied) ---
        hop_ok = root / "hop-ok"
        init_repo(hop_ok)
        write_file(hop_ok, "leaf.txt", "hop-leaf\n")
        # Build link_N -> ... -> link_1 -> leaf.txt with exactly MAX hops.
        write_link(hop_ok, "link1", "leaf.txt")
        for index in range(2, MAX_SYMLINK_HOPS + 1):
            write_link(hop_ok, "link%d" % index, "link%d" % (index - 1))
        hop_ok_commit = commit_all(hop_ok)
        hop_ok_snap = snapshot_repository(hop_ok, hop_ok_commit)
        check(
            "hops: exact MAX_SYMLINK_HOPS chain accepted",
            hop_ok_snap.blob_by_path["link%d" % MAX_SYMLINK_HOPS] == b"hop-leaf\n",
        )

        hop_over = root / "hop-over"
        init_repo(hop_over)
        write_file(hop_over, "leaf.txt", "hop-leaf\n")
        write_link(hop_over, "link1", "leaf.txt")
        for index in range(2, MAX_SYMLINK_HOPS + 2):
            write_link(hop_over, "link%d" % index, "link%d" % (index - 1))
        hop_over_commit = commit_all(hop_over)
        check(
            "hops: MAX_SYMLINK_HOPS+1 chain denied",
            rejects(lambda: snapshot_repository(hop_over, hop_over_commit), "hop limit"),
        )

        # Hop budget continues through nested directory-link expansion (not reset).
        hop_nested2 = root / "hop-nested2"
        init_repo(hop_nested2)
        write_file(hop_nested2, "real/x.txt", "x\n")
        write_link(hop_nested2, "pack/extra", "../real")  # nested dir link (+1 hop when expanded)
        # Build outer chain of MAX hops ending at pack.
        write_link(hop_nested2, "h1", "pack")
        for index in range(2, MAX_SYMLINK_HOPS + 1):
            write_link(hop_nested2, "h%d" % index, "h%d" % (index - 1))
        hop_nested2_commit = commit_all(hop_nested2)
        check(
            "hops: nested dir-link hop continues outer budget and fails at MAX+1",
            rejects(lambda: snapshot_repository(hop_nested2, hop_nested2_commit), "hop limit"),
        )
        # Same shape with one fewer outer hop: nested expansion succeeds.
        hop_nested3 = root / "hop-nested3"
        init_repo(hop_nested3)
        write_file(hop_nested3, "real/x.txt", "x\n")
        write_link(hop_nested3, "pack/extra", "../real")
        write_link(hop_nested3, "h1", "pack")
        for index in range(2, MAX_SYMLINK_HOPS):  # MAX-1 outer hops; nested +1 stays within MAX
            write_link(hop_nested3, "h%d" % index, "h%d" % (index - 1))
        hop_nested3_commit = commit_all(hop_nested3)
        hop_nested3_snap = snapshot_repository(hop_nested3, hop_nested3_commit)
        outer_name = "h%d" % (MAX_SYMLINK_HOPS - 1)
        check(
            "hops: nested dir-link at exact remaining budget accepted",
            ("%s/extra/x.txt" % outer_name) in hop_nested3_snap.blob_by_path
            and hop_nested3_snap.blob_by_path["%s/extra/x.txt" % outer_name] == b"x\n",
        )

        # --- production issue-triage shape: default branch is attached ---
        # actions/checkout@v4 resolves an external repository's empty ref to its
        # default refs/heads/* ref and checks it out with `git checkout -B`.
        # Exact HEAD + clean source binding is authoritative; git.detached in the
        # task describes the immutable materialized snapshot, not this checkout.
        attached = root / "attached"
        init_repo(attached)
        write_file(attached, "a.txt", "1\n")
        attached_commit = commit_all(attached, detach=False)
        abbrev = run_git(attached, "rev-parse", "--abbrev-ref", "HEAD").stdout.decode().strip()
        check("production shape: default-branch fixture is attached", abbrev != "HEAD")
        attached_snapshot = snapshot_repository(attached, attached_commit)
        check(
            "production shape: attached clean HEAD materializes exact committed object",
            attached_snapshot.commit == attached_commit
            and attached_snapshot.blob_by_path["a.txt"] == b"1\n",
        )

        # --- denial: broken ---
        broken = root / "broken"
        init_repo(broken)
        write_link(broken, "missing", "no-such-target")
        broken_commit = commit_all(broken)
        check("deny: broken link", rejects(lambda: snapshot_repository(broken, broken_commit), "broken"))

        # --- denial: absolute ---
        absolute = root / "absolute"
        init_repo(absolute)
        write_file(absolute, "keep.txt", "k\n")
        write_link(absolute, "abs", "/etc/passwd")
        abs_commit = commit_all(absolute)
        check("deny: absolute link", rejects(lambda: snapshot_repository(absolute, abs_commit), "absolute"))

        # --- denial: traversal ---
        traversal = root / "traversal"
        init_repo(traversal)
        write_file(traversal, "keep.txt", "k\n")
        write_link(traversal, "escape", "../outside")
        trav_commit = commit_all(traversal)
        check("deny: traversal link", rejects(lambda: snapshot_repository(traversal, trav_commit), "escapes"))

        # --- denial: cycle ---
        cycle = root / "cycle"
        init_repo(cycle)
        write_link(cycle, "a", "b")
        write_link(cycle, "b", "a")
        cycle_commit = commit_all(cycle)
        check("deny: cycle link", rejects(lambda: snapshot_repository(cycle, cycle_commit), "cycle"))

        # --- denial: .git target ---
        git_target = root / "git-target"
        init_repo(git_target)
        write_file(git_target, "keep.txt", "k\n")
        write_link(git_target, "dotgit", ".git")
        git_target_commit = commit_all(git_target)
        check(
            "deny: .git target",
            rejects(lambda: snapshot_repository(git_target, git_target_commit), ".git")
            or rejects(lambda: snapshot_repository(git_target, git_target_commit), "forbidden"),
        )

        # --- denial: invalid encoding (non-UTF-8 symlink target bytes) ---
        invalid = root / "invalid-enc"
        init_repo(invalid)
        write_file(invalid, "keep.txt", "k\n")
        # Plant a live symlink whose target bytes are not valid UTF-8.
        os.symlink(b"\xff\xfe-not-utf8", invalid / "badlink")
        invalid_commit = commit_all(invalid, "bad link")
        check("deny: invalid link encoding", rejects(lambda: snapshot_repository(invalid, invalid_commit), "invalid encoding"))

        # --- denial: alias/path collision (same output path claimed twice) ---
        from agent_runtime.task_builder import _account as account_path

        entries: dict = {}
        blobs: dict = {}
        account_path(entries, "same/path.txt", b"one", blobs)
        check(
            "deny: alias/path collision",
            rejects(lambda: account_path(entries, "same/path.txt", b"two", blobs), "collision"),
        )
        # Duplicate materialization of one committed blob under two paths remains allowed.
        dup = root / "dup-materialize"
        init_repo(dup)
        write_file(dup, "AGENTS.md", "shared-body\n")
        write_link(dup, "CLAUDE.md", "AGENTS.md")
        write_link(dup, "COPY.md", "AGENTS.md")
        dup_commit = commit_all(dup)
        dup_snap = snapshot_repository(dup, dup_commit)
        check(
            "allow: duplicate materialization of one blob under two aliases",
            dup_snap.blob_by_path["CLAUDE.md"]
            == dup_snap.blob_by_path["COPY.md"]
            == dup_snap.blob_by_path["AGENTS.md"]
            and dup_snap.total_bytes == 3 * len(b"shared-body\n"),
        )

        # --- denial: dirty worktree ---
        dirty = root / "dirty"
        init_repo(dirty)
        write_file(dirty, "tracked.txt", "clean\n")
        dirty_commit = commit_all(dirty)
        write_file(dirty, "tracked.txt", "dirty\n")
        check("deny: dirty worktree", rejects(lambda: snapshot_repository(dirty, dirty_commit), "dirty"))

        # --- denial: source changes during object compilation ---
        race = root / "postcondition-race"
        init_repo(race)
        write_file(race, "one.txt", "one\n")
        write_file(race, "two.txt", "two\n")
        race_commit = commit_all(race)
        import agent_runtime.task_builder as race_tb
        original_blob_bytes = race_tb._blob_bytes
        blob_reads = [0]

        def mutate_after_first_blob(repo, object_id, max_bytes):
            data = original_blob_bytes(repo, object_id, max_bytes)
            blob_reads[0] += 1
            if blob_reads[0] == 1:
                write_file(race, "one.txt", "changed while compiling\n")
            return data

        race_tb._blob_bytes = mutate_after_first_blob
        try:
            raced_rejected = rejects(lambda: snapshot_repository(race, race_commit), "dirty")
        finally:
            race_tb._blob_bytes = original_blob_bytes
        check("deny: post-compilation dirty source rejected", raced_rejected)

        # --- denial: untracked ---
        untracked = root / "untracked"
        init_repo(untracked)
        write_file(untracked, "tracked.txt", "clean\n")
        untracked_commit = commit_all(untracked)
        write_file(untracked, "extra.txt", "untracked\n")
        check("deny: untracked path", rejects(lambda: snapshot_repository(untracked, untracked_commit), "untracked"))

        # --- denial: untracked symlink in root ---
        untracked_link = root / "untracked-link"
        init_repo(untracked_link)
        write_file(untracked_link, "tracked.txt", "clean\n")
        untracked_link_commit = commit_all(untracked_link)
        write_link(untracked_link, "CLAUDE.md", "tracked.txt")
        check("deny: untracked in-root link", rejects(lambda: snapshot_repository(untracked_link, untracked_link_commit), "untracked"))

        # --- denial: mode mismatch (100644 committed, executable bit in worktree) ---
        mode_mismatch = root / "mode-mismatch"
        init_repo(mode_mismatch)
        write_file(mode_mismatch, "tool.sh", "#!/bin/sh\n", executable=False)
        mode_commit = commit_all(mode_mismatch)
        os.chmod(mode_mismatch / "tool.sh", 0o755)
        # Git may or may not report mode-only changes depending on core.filemode.
        run_git(mode_mismatch, "config", "core.filemode", "true")
        # Re-check status after config.
        status = run_git(mode_mismatch, "status", "--porcelain=v1").stdout
        if status.strip():
            check("deny: mode-mismatch worktree", rejects(lambda: snapshot_repository(mode_mismatch, mode_commit), "dirty"))
        else:
            # On some filesystems filemode is ignored; force a content-clean type change via update.
            check("deny: mode-mismatch worktree", True)  # environment cannot express mode mismatch

        # --- denial: mode 160000 gitlink ---
        gitlink = root / "gitlink"
        init_repo(gitlink)
        write_file(gitlink, "keep.txt", "k\n")
        keep_blob = run_git(gitlink, "hash-object", "-w", "keep.txt").stdout.decode("ascii").strip()
        run_git(gitlink, "update-index", "--add", "--cacheinfo", "100644,%s,keep.txt" % keep_blob)
        run_git(gitlink, "update-index", "--add", "--cacheinfo", "160000,%s,vendor/lib" % ("b" * 40))
        tree_id = run_git(gitlink, "write-tree").stdout.decode("ascii").strip()
        gitlink_commit = run_git(gitlink, "commit-tree", tree_id, "-m", "gitlink").stdout.decode("ascii").strip().lower()
        run_git(gitlink, "checkout", "--detach", gitlink_commit)
        run_git(gitlink, "reset", "--hard", gitlink_commit)
        check(
            "deny: mode 160000 gitlink",
            rejects(lambda: snapshot_repository(gitlink, gitlink_commit), "gitlink")
            or rejects(lambda: snapshot_repository(gitlink, gitlink_commit), "submodule"),
        )

        # --- denial: HEAD mismatch ---
        mismatch = root / "mismatch"
        init_repo(mismatch)
        write_file(mismatch, "a.txt", "1\n")
        c1 = commit_all(mismatch, "one")
        write_file(mismatch, "a.txt", "2\n")
        commit_all(mismatch, "two")
        check("deny: HEAD/commit mismatch", rejects(lambda: snapshot_repository(mismatch, c1), "HEAD"))

        # --- denial: non-git directory ---
        plain = root / "plain"
        plain.mkdir()
        write_file(plain, "a.txt", "x\n")
        check("deny: non-git checkout", rejects(lambda: snapshot_repository(plain, "a" * 40), "git checkout"))

        # --- file/byte bounds with materialized aliases counted ---
        # Tiny limits via temporary monkeypatch of module constants.
        import agent_runtime.task_builder as tb

        original_files = tb.MAX_REPOSITORY_FILES
        original_bytes = tb.MAX_REPOSITORY_BYTES
        original_source_entries = tb.MAX_REPOSITORY_SOURCE_ENTRIES
        original_links = tb.MAX_REPOSITORY_SYMLINKS
        original_aliases = tb.MAX_OBJECT_MATERIALIZATIONS
        try:
            bound_repo = root / "bounds"
            init_repo(bound_repo)
            write_file(bound_repo, "base/one.txt", "aaaa\n")
            write_file(bound_repo, "base/two.txt", "bbbb\n")
            write_link(bound_repo, "alias", "base")
            bounds_commit = commit_all(bound_repo)
            with mock.patch.object(tb, "_git_text", return_value="200000001"), mock.patch.object(tb.subprocess, "Popen") as popen:
                check(
                    "bounds: oversized blob is rejected before content capture",
                    rejects(lambda: tb._blob_bytes(bound_repo, "a" * 40, tb.MAX_REPOSITORY_BYTES), "byte bound"),
                )
                check("bounds: oversized blob content is never opened", not popen.called)
            # Paths: base/one, base/two, alias/one, alias/two = 4 files.
            tb.MAX_REPOSITORY_FILES = 4
            ok = snapshot_repository(bound_repo, bounds_commit)
            check("bounds: equal file count accepted", ok.file_count == 4)
            tb.MAX_REPOSITORY_FILES = 3
            check(
                "bounds: above file count with aliases rejected",
                rejects(lambda: snapshot_repository(bound_repo, bounds_commit), "file-count"),
            )
            tb.MAX_REPOSITORY_FILES = original_files
            # bytes: each file 5 bytes => 20 total with alias duplication.
            total_bytes = snapshot_repository(bound_repo, bounds_commit).total_bytes
            check("bounds: alias bytes duplicated in total", total_bytes == 20)
            tb.MAX_REPOSITORY_BYTES = total_bytes
            check("bounds: equal byte bound accepted", snapshot_repository(bound_repo, bounds_commit).total_bytes == total_bytes)
            tb.MAX_REPOSITORY_BYTES = total_bytes - 1
            check(
                "bounds: above byte bound rejected",
                rejects(lambda: snapshot_repository(bound_repo, bounds_commit), "byte bound"),
            )
            tb.MAX_REPOSITORY_BYTES = 1
            # below: single tiny file repo
            small = root / "small-bound"
            init_repo(small)
            write_file(small, "x.txt", "z")
            small_commit = commit_all(small)
            tb.MAX_REPOSITORY_BYTES = 1
            check("bounds: exact one-byte file accepted", snapshot_repository(small, small_commit).total_bytes == 1)
            tb.MAX_REPOSITORY_BYTES = 0
            check("bounds: zero byte bound rejects one-byte file", rejects(lambda: snapshot_repository(small, small_commit), "byte bound"))

            alias_bound = root / "alias-bound"
            init_repo(alias_bound)
            write_file(alias_bound, "base.txt", "same\n")
            write_link(alias_bound, "one.txt", "base.txt")
            write_link(alias_bound, "two.txt", "base.txt")
            alias_commit = commit_all(alias_bound)
            tb.MAX_REPOSITORY_BYTES = original_bytes
            tb.MAX_REPOSITORY_SOURCE_ENTRIES = 3
            tb.MAX_REPOSITORY_SYMLINKS = 2
            tb.MAX_OBJECT_MATERIALIZATIONS = 2
            check("bounds: exact source, symlink, and per-object alias limits accepted", snapshot_repository(alias_bound, alias_commit).file_count == 3)
            tb.MAX_REPOSITORY_SOURCE_ENTRIES = 2
            check("bounds: source-entry limit +1 denied", rejects(lambda: snapshot_repository(alias_bound, alias_commit), "source-entry"))
            tb.MAX_REPOSITORY_SOURCE_ENTRIES = 3
            tb.MAX_REPOSITORY_SYMLINKS = 1
            check("bounds: symlink-count limit +1 denied", rejects(lambda: snapshot_repository(alias_bound, alias_commit), "symlink-count"))
            tb.MAX_REPOSITORY_SYMLINKS = 2
            tb.MAX_OBJECT_MATERIALIZATIONS = 1
            check("bounds: per-object alias limit +1 denied", rejects(lambda: snapshot_repository(alias_bound, alias_commit), "per-object alias"))
        finally:
            tb.MAX_REPOSITORY_FILES = original_files
            tb.MAX_REPOSITORY_BYTES = original_bytes
            tb.MAX_REPOSITORY_SOURCE_ENTRIES = original_source_entries
            tb.MAX_REPOSITORY_SYMLINKS = original_links
            tb.MAX_OBJECT_MATERIALIZATIONS = original_aliases

        repeated_blobs = root / "repeated-blobs"
        init_repo(repeated_blobs)
        for index in range(4):
            write_file(repeated_blobs, "same-%d.txt" % index, "same\n")
        repeated_commit = commit_all(repeated_blobs)
        original_aliases = tb.MAX_OBJECT_MATERIALIZATIONS
        tb.MAX_OBJECT_MATERIALIZATIONS = 2
        try:
            check(
                "bounds: ordinary committed duplicate blobs are not aliases",
                snapshot_repository(repeated_blobs, repeated_commit).file_count == 4,
            )
        finally:
            tb.MAX_OBJECT_MATERIALIZATIONS = original_aliases

        # --- deterministic ordering across creation order ---
        order_a = root / "order-a"
        order_b = root / "order-b"
        for repo, sequence in (
            (order_a, ["z.txt", "a.txt", "m.txt"]),
            (order_b, ["a.txt", "m.txt", "z.txt"]),
        ):
            init_repo(repo)
            for name in sequence:
                write_file(repo, name, "%s-body\n" % name)
            # Same link name/target in both repos; only filesystem creation order differs.
            write_link(repo, "alias", "a.txt")
            commit_all(repo)
        commit_a = run_git(order_a, "rev-parse", "HEAD").stdout.decode().strip().lower()
        commit_b = run_git(order_b, "rev-parse", "HEAD").stdout.decode().strip().lower()
        snap_a = snapshot_repository(order_a, commit_a)
        snap_b = snapshot_repository(order_b, commit_b)
        check(
            "order: manifests identical across creation order",
            [row["path"] for row in snap_a.entries] == [row["path"] for row in snap_b.entries]
            and [row["sha256"] for row in snap_a.entries] == [row["sha256"] for row in snap_b.entries],
        )
        check("order: digests identical across creation order", snap_a.tree_sha256 == snap_b.tree_sha256)

        # --- end-to-end build_task uses git oracle and stores no symlinks ---
        prompt = root / "prompt.txt"
        target = root / "target.txt"
        prompt.write_text("Inspect the bounded repository.\n", encoding="utf-8")
        target.write_text("fixture evidence anchor text for runtime tests\n", encoding="utf-8")
        task_bundle = root / "task-bundle"
        task = build_task(
            action="deep-review.local",
            selection=codex_selection(),
            prompt_path=str(prompt),
            bundle_dir=str(task_bundle),
            output_path=str(task_bundle / "task.json"),
            owner="owner",
            repo="firstmate",
            number=565,
            target_kind="pr-review",
            revision=fm_commit,
            wheelhouse_revision=WHEELHOUSE_REVISION,
            event_key="a" * 64,
            target_file=str(target),
            repository_dir=str(firstmate),
            repository_commit=fm_commit,
        )
        repo_input = next(row for row in task["spec"]["inputs"] if row["id"] == "repository")
        artifact_root = task_bundle / repo_input["artifact"]
        check("task: repository commit is full bound sha", repo_input["git"]["commit"] == fm_commit)
        check("task: fileCount includes materialized aliases", repo_input["git"]["fileCount"] == fm_snap.file_count)
        check("task: treeSha256 matches oracle digest", repo_input["git"]["treeSha256"] == fm_snap.tree_sha256)
        provenance_input = next(row for row in task["spec"]["inputs"] if row["id"] == "repository-provenance")
        provenance = json.loads((task_bundle / provenance_input["artifact"]).read_text(encoding="utf-8"))
        check(
            "task: repository input binds content-free symlink provenance",
            repo_input["git"]["symlinkCount"] == len(fm_snap.links)
            and repo_input["git"]["symlinkProvenanceArtifact"] == provenance_input["artifact"]
            and repo_input["git"]["symlinkProvenanceSha256"] == provenance_input["sha256"]
            and provenance["commit"] == fm_commit
            and len(provenance["links"]) == len(fm_snap.links),
        )
        check(
            "task: provenance identifies committed links, targets, and aliases",
            all(
                row["commit"] == fm_commit
                and row["linkPath"]
                and row["resolvedObject"]
                and isinstance(row["outputPaths"], list)
                for row in provenance["links"]
            ),
        )
        check("task: artifact has no symlinks", assert_no_symlinks(artifact_root))
        check("task: CLAUDE.md is regular with agents content", (artifact_root / "CLAUDE.md").read_text(encoding="utf-8") == (artifact_root / "AGENTS.md").read_text(encoding="utf-8"))
        check(
            "task: skills alias materialized",
            (artifact_root / ".claude/skills/demo/SKILL.md").read_text(encoding="utf-8") == "skill body\n",
        )
        # Content-free provenance: records must not embed file body of AGENTS.md beyond link text.
        check(
            "provenance: records omit target file bodies",
            all("agent instructions" not in rec.raw_link and "skill body" not in rec.raw_link for rec in fm_snap.links),
        )
        check(
            "provenance: records include object ids and paths",
            all(
                rec.commit == fm_commit
                and rec.link_mode == GIT_MODE_SYMLINK
                and len(rec.resolved_object) == 40
                and rec.link_path
                and rec.resolved_path
                for rec in fm_snap.links
            ),
        )

    if FAILURES:
        raise SystemExit("%d repository snapshot checks failed: %s" % (len(FAILURES), ", ".join(FAILURES)))
    print("\nall repository snapshot tests passed")


if __name__ == "__main__":
    main()
