"""Build immutable, content-addressed AgentTask v1 requests."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import API_VERSION
from .admission import DIGEST
from .contract import (
    ArtifactError,
    atomic_write_json,
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    load_json_regular,
    sha256_bytes,
    validate_contract,
)
from .tools import TOOL_SCHEMAS, tool_schema_sha256

ROOT = Path(__file__).resolve().parent
ACTION_SCHEMAS = ROOT / "schemas" / "actions"
MAX_REPOSITORY_BYTES = 200_000_000
MAX_REPOSITORY_FILES = 30_000
MAX_REPOSITORY_SOURCE_ENTRIES = 30_000
MAX_SYMLINK_HOPS = 32
MAX_REPOSITORY_SYMLINKS = 4_096
MAX_OBJECT_MATERIALIZATIONS = 256
MAX_SYMLINK_TARGET_BYTES = 4_096
MAX_REPOSITORY_TREE_ROW_BYTES = 65_536
MAX_REPOSITORY_PROVENANCE_BYTES = 8 * 1024 * 1024
GIT_MODE_FILE = "100644"
GIT_MODE_EXEC = "100755"
GIT_MODE_SYMLINK = "120000"
GIT_MODE_GITLINK = "160000"
GIT_MODE_TREE = "040000"

ACTION_LIMITS = {
    "triage.issue.local": (240_000, 270_000, 32, 80, 65_536),
    "triage.issue.search": (240_000, 270_000, 32, 80, 65_536),
    "triage.pr.local": (300_000, 330_000, 32, 80, 65_536),
    "triage.pr.search": (300_000, 330_000, 32, 80, 65_536),
    "triage.schema-repair": (60_000, 75_000, 1, 0, 65_536),
    "deep-review.local": (540_000, 600_000, 64, 160, 131_072),
    "deep-review.search": (540_000, 600_000, 64, 160, 131_072),
    "nl-decision.local": (240_000, 270_000, 32, 80, 65_536),
    "nl-decision.search": (240_000, 270_000, 32, 80, 65_536),
}


def _artifact_path(bundle: Path, digest: str) -> Path:
    return bundle / "artifacts" / "sha256" / digest


def _copy_file(source: Path, bundle: Path, max_bytes: int) -> tuple[str, int, str]:
    info = source.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ArtifactError("input artifact must be a regular file")
    if info.st_size > max_bytes:
        raise ArtifactError("input artifact exceeds its action bound")
    digest = file_sha256(source, max_bytes=max_bytes)
    destination = _artifact_path(bundle, digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o400)
    return digest, info.st_size, "artifacts/sha256/%s" % digest


@dataclass(frozen=True)
class MaterializedLinkRecord:
    """Content-free proof that a mode-120000 path was bound to committed objects."""

    commit: str
    link_path: str
    link_mode: str
    raw_link: str
    resolved_path: str
    resolved_mode: str
    resolved_object: str
    output_paths: tuple[str, ...]
    file_count: int
    byte_count: int


@dataclass
class RepositorySnapshot:
    """Immutable repository snapshot derived only from a bound Git commit."""

    commit: str
    entries: list[dict[str, Any]]
    total_bytes: int
    blob_by_path: dict[str, bytes]
    links: list[MaterializedLinkRecord] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.entries)

    @property
    def tree_sha256(self) -> str:
        return canonical_sha256(self.entries)


def _git(repo: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise ArtifactError("repository snapshot cannot invoke git") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ArtifactError("repository snapshot git command failed: %s" % (detail or args[0]))
    return completed.stdout


def _git_text(repo: Path, *args: str) -> str:
    try:
        return _git(repo, *args).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ArtifactError("repository snapshot git output is not valid UTF-8") from error


def _require_bound_clean_repository(repo: Path, commit: str) -> str:
    info = repo.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ArtifactError("repository input must be a directory")
    # Worktrees use a .git file; plain directories without git metadata fail closed.
    if not (repo / ".git").exists():
        raise ArtifactError("repository input is not a git checkout")
    try:
        full = _git_text(repo, "rev-parse", "--verify", "%s^{commit}" % commit).strip().lower()
    except ArtifactError as error:
        raise ArtifactError("repository commit binding is not a readable commit") from error
    if not full or any(character not in "0123456789abcdef" for character in full):
        raise ArtifactError("repository commit binding is not a readable commit")
    head = _git_text(repo, "rev-parse", "HEAD").strip().lower()
    if head != full:
        raise ArtifactError("repository HEAD does not match the bound commit")
    # The source checkout may be branch-attached. actions/checkout@v4 checks an
    # external repository's default branch out with `git checkout -B`, which is
    # the production issue-triage shape. Exact HEAD equality plus clean index and
    # worktree bind the source; AgentTask git.detached describes the emitted
    # content-addressed snapshot, not the source checkout's branch attachment.
    # Fail closed on any dirty, staged, untracked, intent-to-add, or mode mismatch.
    status = _git(repo, "status", "--porcelain=v1", "-uall", "--ignore-submodules=none")
    if status.strip():
        raise ArtifactError("repository worktree is dirty, untracked, or mode-mismatched")
    if _git(repo, "diff", "--raw", full).strip() or _git(repo, "diff", "--cached", "--raw", full).strip():
        raise ArtifactError("repository worktree is dirty, untracked, or mode-mismatched")
    return full


def _parse_ls_tree(payload: bytes) -> list[tuple[str, str, str, str]]:
    """Return (mode, objtype, object, path) rows from nul-delimited ls-tree output."""

    rows: list[tuple[str, str, str, str]] = []
    if not payload:
        return rows
    for item in payload.split(b"\0"):
        if not item:
            continue
        rows.append(_parse_ls_tree_item(item))
    return rows


def _parse_ls_tree_item(item: bytes) -> tuple[str, str, str, str]:
    try:
        meta, path_bytes = item.split(b"\t", 1)
    except ValueError as error:
        raise ArtifactError("repository tree listing is malformed") from error
    parts = meta.split(b" ")
    if len(parts) != 3:
        raise ArtifactError("repository tree listing is malformed")
    try:
        mode = parts[0].decode("ascii", errors="strict")
        objtype = parts[1].decode("ascii", errors="strict")
        object_id = parts[2].decode("ascii", errors="strict").lower()
        path = path_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ArtifactError("repository tree listing is not valid UTF-8") from error
    if not path or path == ".git" or path.startswith(".git/") or "\0" in path:
        raise ArtifactError("repository tree contains a forbidden path")
    return mode, objtype, object_id, path


def _load_committed_index(repo: Path, commit: str) -> dict[str, tuple[str, str]]:
    """Map committed paths to (mode, object_id) using the object database only."""

    try:
        process = subprocess.Popen(
            ["git", "-C", str(repo), "ls-tree", "-r", "-z", commit],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ArtifactError("repository snapshot cannot invoke git") from error
    assert process.stdout is not None
    assert process.stderr is not None
    index: dict[str, tuple[str, str]] = {}
    buffer = bytearray()
    source_entries = 0
    try:
        while True:
            chunk = process.stdout.read(65_536)
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                separator = buffer.find(0)
                if separator < 0:
                    break
                item = bytes(buffer[:separator])
                del buffer[: separator + 1]
                if not item:
                    continue
                if len(item) > MAX_REPOSITORY_TREE_ROW_BYTES:
                    raise ArtifactError("repository tree listing row exceeds its bound")
                source_entries += 1
                if source_entries > MAX_REPOSITORY_SOURCE_ENTRIES:
                    raise ArtifactError("repository snapshot exceeds its source-entry bound")
                mode, objtype, object_id, path = _parse_ls_tree_item(item)
                if mode == GIT_MODE_GITLINK or objtype == "commit":
                    raise ArtifactError("repository snapshot rejects gitlinks/submodules")
                if mode not in (GIT_MODE_FILE, GIT_MODE_EXEC, GIT_MODE_SYMLINK):
                    raise ArtifactError("repository snapshot contains an unsupported git mode")
                if objtype != "blob":
                    raise ArtifactError("repository snapshot contains a non-blob entry")
                if path in index:
                    raise ArtifactError("repository tree contains a duplicate path")
                index[path] = (mode, object_id)
            if len(buffer) > MAX_REPOSITORY_TREE_ROW_BYTES:
                raise ArtifactError("repository tree listing row exceeds its bound")
        if buffer:
            raise ArtifactError("repository tree listing is malformed")
    except BaseException:
        process.kill()
        process.communicate()
        raise
    stderr = process.stderr.read()
    returncode = process.wait()
    if returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise ArtifactError("repository snapshot git command failed: %s" % (detail or "ls-tree"))
    return index


def _blob_bytes(repo: Path, object_id: str, max_bytes: int) -> bytes:
    if max_bytes < 0:
        raise ArtifactError("repository snapshot exceeds its byte bound")
    try:
        size_text = _git_text(repo, "cat-file", "-s", object_id).strip()
        size = int(size_text)
    except (ArtifactError, ValueError) as error:
        raise ArtifactError("repository blob size is invalid") from error
    if size < 0 or size > max_bytes:
        raise ArtifactError("repository snapshot exceeds its byte bound")
    try:
        process = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "blob", object_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ArtifactError("repository snapshot cannot invoke git") from error
    assert process.stdout is not None
    assert process.stderr is not None
    payload = process.stdout.read(size + 1)
    oversized = len(payload) > size
    if oversized:
        process.kill()
    stderr = process.stderr.read()
    returncode = process.wait()
    if returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise ArtifactError("repository snapshot git command failed: %s" % (detail or "cat-file"))
    if oversized or len(payload) != size:
        raise ArtifactError("repository blob size changed while reading")
    return payload


def _is_git_path_safe(path: str) -> bool:
    if path == "":
        return True
    if path == ".git" or path.startswith(".git/"):
        return False
    if path.startswith("/") or path.endswith("/") or "//" in path:
        return False
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    return True


def _decode_link_target(raw: bytes) -> str:
    if not raw:
        raise ArtifactError("repository symlink target is empty")
    if b"\0" in raw:
        raise ArtifactError("repository symlink target has invalid encoding")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ArtifactError("repository symlink target has invalid encoding") from error
    if text != text.strip("\n") or "\n" in text or "\r" in text:
        # Git stores the raw link body without a trailing newline; reject control breaks.
        if "\n" in text or "\r" in text:
            raise ArtifactError("repository symlink target has invalid encoding")
    return text


def _resolve_link_path(link_path: str, raw_target: str) -> str:
    if raw_target.startswith("/") or raw_target.startswith("\\"):
        raise ArtifactError("repository symlink target is absolute")
    if len(raw_target) >= 2 and raw_target[1] == ":" and raw_target[0].isalpha():
        raise ArtifactError("repository symlink target is absolute")
    if not raw_target or raw_target == ".":
        raise ArtifactError("repository symlink target is invalid")
    base = PurePosixPath(link_path).parent
    combined = PurePosixPath(base) / raw_target if str(base) != "." else PurePosixPath(raw_target)
    parts: list[str] = []
    for part in combined.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ArtifactError("repository symlink escapes the repository root")
            parts.pop()
            continue
        parts.append(part)
    resolved = "/".join(parts)
    if not _is_git_path_safe(resolved):
        raise ArtifactError("repository symlink target is forbidden")
    if resolved == ".git" or resolved.startswith(".git/"):
        raise ArtifactError("repository symlink targets .git")
    return resolved


def _tree_object_id(repo: Path, commit: str, path: str) -> str | None:
    if path == "":
        return _git_text(repo, "rev-parse", "%s^{tree}" % commit).strip().lower()
    payload = _git(repo, "ls-tree", "-z", commit, "--", path)
    rows = _parse_ls_tree(payload)
    if not rows:
        return None
    if len(rows) != 1:
        raise ArtifactError("repository tree lookup is ambiguous")
    mode, objtype, object_id, found = rows[0]
    if found != path:
        return None
    if mode == GIT_MODE_TREE or objtype == "tree":
        return object_id
    return None


def _path_kind(
    repo: Path,
    commit: str,
    index: dict[str, tuple[str, str]],
    path: str,
) -> tuple[str, str, str] | None:
    """Return (kind, mode, object_id) for a committed path, or None if absent."""

    if path in index:
        mode, object_id = index[path]
        if mode == GIT_MODE_SYMLINK:
            return ("symlink", mode, object_id)
        return ("file", mode, object_id)
    tree_id = _tree_object_id(repo, commit, path)
    if tree_id is not None:
        return ("tree", GIT_MODE_TREE, tree_id)
    # Prefix presence without a tree object should not happen for a well-formed commit.
    prefix = path + "/"
    if any(candidate.startswith(prefix) for candidate in index):
        raise ArtifactError("repository tree metadata is inconsistent")
    return None


def _descendants(index: dict[str, tuple[str, str]], tree_path: str) -> list[str]:
    if tree_path == "":
        return sorted(index)
    prefix = tree_path + "/"
    return sorted(path for path in index if path.startswith(prefix))


def _account(entries: dict[str, dict[str, Any]], path: str, data: bytes, blob_by_path: dict[str, bytes]) -> None:
    if path in entries:
        raise ArtifactError("repository snapshot path collision")
    size = len(data)
    if len(entries) + 1 > MAX_REPOSITORY_FILES:
        raise ArtifactError("repository snapshot exceeds its file-count bound")
    entries[path] = {"path": path, "bytes": size, "sha256": sha256_bytes(data)}
    blob_by_path[path] = data


def _materialize_file(
    repo: Path,
    entries: dict[str, dict[str, Any]],
    blob_by_path: dict[str, bytes],
    output_path: str,
    object_id: str,
    total: list[int],
    object_materializations: dict[str, int],
    *,
    count_alias: bool,
) -> None:
    if count_alias:
        count = object_materializations.get(object_id, 0) + 1
        if count > MAX_OBJECT_MATERIALIZATIONS:
            raise ArtifactError("repository snapshot exceeds its per-object alias bound")
        object_materializations[object_id] = count
    data = _blob_bytes(repo, object_id, MAX_REPOSITORY_BYTES - total[0])
    total[0] += len(data)
    if total[0] > MAX_REPOSITORY_BYTES:
        raise ArtifactError("repository snapshot exceeds its byte bound")
    _account(entries, output_path, data, blob_by_path)


def _resolve_terminal(
    repo: Path,
    commit: str,
    index: dict[str, tuple[str, str]],
    start_path: str,
    *,
    hops: int = 0,
    chain: set[str] | None = None,
) -> tuple[str, str, str, str, str, int]:
    """Resolve start_path to a terminal committed file or tree.

    Returns (resolved_path, resolved_kind, resolved_mode, resolved_object, raw_link_or_empty, hops).
    Mutates chain in place with every symlink hop path so nested expansion can
    propagate cycle detection. hops counts every link hop across nested aliases.
    """

    chain = set() if chain is None else chain
    path = start_path
    first_raw = ""
    while True:
        if hops > MAX_SYMLINK_HOPS:
            raise ArtifactError("repository symlink hop limit exceeded")
        if path in chain:
            raise ArtifactError("repository symlink cycle detected")
        kind_row = _path_kind(repo, commit, index, path)
        if kind_row is None:
            raise ArtifactError("repository symlink target is broken")
        kind, mode, object_id = kind_row
        if kind == "file":
            return path, kind, mode, object_id, first_raw, hops
        if kind == "tree":
            return path, kind, mode, object_id, first_raw, hops
        if kind != "symlink":
            raise ArtifactError("repository symlink target is unsupported")
        chain.add(path)
        raw = _blob_bytes(repo, object_id, MAX_SYMLINK_TARGET_BYTES)
        text = _decode_link_target(raw)
        if not first_raw:
            first_raw = text
        path = _resolve_link_path(path, text)
        hops += 1


def _materialize_tree_alias(
    repo: Path,
    commit: str,
    index: dict[str, tuple[str, str]],
    entries: dict[str, dict[str, Any]],
    blob_by_path: dict[str, bytes],
    alias_prefix: str,
    tree_path: str,
    total: list[int],
    outer_chain: set[str],
    hops_used: int,
    object_materializations: dict[str, int],
) -> tuple[list[str], int, int]:
    """Materialize committed descendants of tree_path under alias_prefix.

    Nested mode-120000 entries continue the outer symlink chain and hop budget so
    cycles and hop limits cannot be reset by directory-link expansion.
    """

    outputs: list[str] = []
    added_files = 0
    added_bytes = 0
    for source_path in _descendants(index, tree_path):
        rel = source_path if tree_path == "" else source_path[len(tree_path) + 1 :]
        output_path = rel if alias_prefix == "" else "%s/%s" % (alias_prefix, rel)
        if not _is_git_path_safe(output_path):
            raise ArtifactError("repository snapshot path is forbidden")
        mode, object_id = index[source_path]
        if mode in (GIT_MODE_FILE, GIT_MODE_EXEC):
            before = total[0]
            _materialize_file(repo, entries, blob_by_path, output_path, object_id, total, object_materializations, count_alias=True)
            outputs.append(output_path)
            added_files += 1
            added_bytes += total[0] - before
            continue
        if mode == GIT_MODE_SYMLINK:
            # Propagate chain + hop budget into nested resolution (never reset).
            nested_chain = set(outer_chain)
            resolved_path, resolved_kind, resolved_mode, resolved_object, _, nested_hops = _resolve_terminal(
                repo,
                commit,
                index,
                source_path,
                hops=hops_used,
                chain=nested_chain,
            )
            if resolved_kind == "file":
                before = total[0]
                _materialize_file(repo, entries, blob_by_path, output_path, resolved_object, total, object_materializations, count_alias=True)
                outputs.append(output_path)
                added_files += 1
                added_bytes += total[0] - before
                continue
            # Nested directory link: expand with the same chain/hop continuity.
            sub_outputs, sub_files, sub_bytes = _materialize_tree_alias(
                repo,
                commit,
                index,
                entries,
                blob_by_path,
                output_path,
                resolved_path,
                total,
                nested_chain,
                nested_hops,
                object_materializations,
            )
            outputs.extend(sub_outputs)
            added_files += sub_files
            added_bytes += sub_bytes
            continue
        raise ArtifactError("repository snapshot contains an unsupported git mode")
    return outputs, added_files, added_bytes


def _manifest_path_order(paths: set[str], prefix: str = "") -> list[str]:
    """Match the verifier's sorted os.walk order without reading live files."""

    relative = {
        path[len(prefix) + 1 :] if prefix else path
        for path in paths
        if not prefix or path.startswith(prefix + "/")
    }
    files = sorted(path for path in relative if "/" not in path)
    directories = sorted({path.split("/", 1)[0] for path in relative if "/" in path})
    ordered = ["%s/%s" % (prefix, name) if prefix else name for name in files]
    for directory in directories:
        child = "%s/%s" % (prefix, directory) if prefix else directory
        ordered.extend(_manifest_path_order(paths, child))
    return ordered


def snapshot_repository(source: Path, commit: str) -> RepositorySnapshot:
    """Build a regular-file snapshot from the exact committed tree at commit.

    Live worktree symlinks never appear in the result. Supported relative mode
    120000 links are materialized as regular bounded content. Mode 160000 is
    rejected. Provenance records are content-free except for the raw link text
    needed to prove binding.
    """

    bound = _require_bound_clean_repository(source, commit)
    index = _load_committed_index(source, bound)
    if sum(1 for mode, _object_id in index.values() if mode == GIT_MODE_SYMLINK) > MAX_REPOSITORY_SYMLINKS:
        raise ArtifactError("repository snapshot exceeds its symlink-count bound")
    entries: dict[str, dict[str, Any]] = {}
    blob_by_path: dict[str, bytes] = {}
    links: list[MaterializedLinkRecord] = []
    total = [0]
    object_materializations: dict[str, int] = {}

    for path in sorted(index):
        mode, object_id = index[path]
        if mode in (GIT_MODE_FILE, GIT_MODE_EXEC):
            if path in entries:
                # A prior directory-link materialization already claimed this path.
                raise ArtifactError("repository snapshot path collision")
            _materialize_file(source, entries, blob_by_path, path, object_id, total, object_materializations, count_alias=False)
            continue
        if mode != GIT_MODE_SYMLINK:
            raise ArtifactError("repository snapshot contains an unsupported git mode")
        raw = _blob_bytes(source, object_id, MAX_SYMLINK_TARGET_BYTES)
        raw_text = _decode_link_target(raw)
        # Validate the first hop explicitly so absolute/traversal fail before deeper resolution.
        _resolve_link_path(path, raw_text)
        resolve_chain: set[str] = set()
        resolved_path, resolved_kind, resolved_mode, resolved_object, _, hops_used = _resolve_terminal(
            source,
            bound,
            index,
            path,
            hops=0,
            chain=resolve_chain,
        )
        if resolved_kind == "file":
            if path in entries:
                raise ArtifactError("repository snapshot path collision")
            before = total[0]
            _materialize_file(source, entries, blob_by_path, path, resolved_object, total, object_materializations, count_alias=True)
            links.append(
                MaterializedLinkRecord(
                    commit=bound,
                    link_path=path,
                    link_mode=GIT_MODE_SYMLINK,
                    raw_link=raw_text,
                    resolved_path=resolved_path,
                    resolved_mode=resolved_mode,
                    resolved_object=resolved_object,
                    output_paths=(path,),
                    file_count=1,
                    byte_count=total[0] - before,
                )
            )
            continue
        if resolved_kind == "tree":
            before_files = len(entries)
            before_bytes = total[0]
            outputs, file_count, byte_count = _materialize_tree_alias(
                source,
                bound,
                index,
                entries,
                blob_by_path,
                path,
                resolved_path,
                total,
                resolve_chain,
                hops_used,
                object_materializations,
            )
            if file_count == 0 and before_files == len(entries):
                # Empty tree alias contributes no regular files; still record the bind.
                outputs = []
            links.append(
                MaterializedLinkRecord(
                    commit=bound,
                    link_path=path,
                    link_mode=GIT_MODE_SYMLINK,
                    raw_link=raw_text,
                    resolved_path=resolved_path,
                    resolved_mode=resolved_mode,
                    resolved_object=resolved_object,
                    output_paths=tuple(outputs),
                    file_count=file_count,
                    byte_count=byte_count if byte_count else total[0] - before_bytes,
                )
            )
            continue
        raise ArtifactError("repository symlink target is unsupported")

    # Detect worktree/index changes that raced object compilation. Materialized
    # bytes came only from exact objects either way, but a dirty postcondition is
    # rejected so callers never mistake a concurrently changing checkout for a
    # clean source binding.
    _require_bound_clean_repository(source, bound)
    ordered = [entries[path] for path in _manifest_path_order(set(entries))]
    return RepositorySnapshot(
        commit=bound,
        entries=ordered,
        total_bytes=total[0],
        blob_by_path={row["path"]: blob_by_path[row["path"]] for row in ordered},
        links=links,
    )


def _copy_directory(source: Path, bundle: Path, commit: str) -> tuple[str, int, int, str, str, list[MaterializedLinkRecord]]:
    snapshot = snapshot_repository(source, commit)
    digest = snapshot.tree_sha256
    destination = _artifact_path(bundle, digest)
    if not destination.exists():
        destination.mkdir(parents=True)
        for row in snapshot.entries:
            data = snapshot.blob_by_path[row["path"]]
            dst = destination / row["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            os.chmod(dst, 0o400)
            # Post-materialization hard guarantee: no live symlink may enter the handoff.
            if dst.is_symlink() or not dst.is_file():
                raise ArtifactError("repository snapshot materialization produced a non-regular file")
        for base, dirs, names in os.walk(destination, topdown=False, followlinks=False):
            for name in names:
                child = Path(base) / name
                if child.is_symlink() or not child.is_file():
                    raise ArtifactError("repository snapshot materialization produced a non-regular file")
            for name in dirs:
                child = Path(base) / name
                if child.is_symlink():
                    raise ArtifactError("repository snapshot materialization produced a symlink")
                os.chmod(child, 0o500)
        os.chmod(destination, 0o500)
    return digest, snapshot.total_bytes, snapshot.file_count, "artifacts/sha256/%s" % digest, snapshot.commit, snapshot.links


def _link_provenance_value(commit: str, links: list[MaterializedLinkRecord]) -> dict[str, Any]:
    return {
        "version": 1,
        "commit": commit,
        "links": [
            {
                "commit": row.commit,
                "linkPath": row.link_path,
                "linkMode": row.link_mode,
                "rawLink": row.raw_link,
                "resolvedPath": row.resolved_path,
                "resolvedMode": row.resolved_mode,
                "resolvedObject": row.resolved_object,
                "outputPaths": list(row.output_paths),
                "fileCount": row.file_count,
                "byteCount": row.byte_count,
            }
            for row in links
        ],
    }


def _schema_for(action: str, repair_kind: str) -> tuple[Path, str]:
    if action.startswith("triage.issue") or (action == "triage.schema-repair" and repair_kind == "issue"):
        return ACTION_SCHEMAS / "triage-issue-v1.schema.json", "wheelhouse/triage-issue/v1"
    if action.startswith("triage.pr") or action == "triage.schema-repair":
        return ACTION_SCHEMAS / "triage-pr-v1.schema.json", "wheelhouse/triage-pr/v1"
    if action.startswith("deep-review"):
        return ACTION_SCHEMAS / "deep-review-text-v1.schema.json", "wheelhouse/deep-review-text/v1"
    if action.startswith("nl-decision"):
        return ACTION_SCHEMAS / "nl-decision-v1.schema.json", "wheelhouse/nl-decision/v1"
    raise ArtifactError("unsupported action output schema")


def claude_declared_tools(action: str) -> list[str]:
    if action == "triage.schema-repair":
        return []
    tools = ["Read", "Grep", "Glob"]
    if action.startswith("nl-decision") or action.endswith(".search"):
        tools.append("Write")
    if action.endswith(".search"):
        tools.append("Bash(wheelhouse-search)")
    return tools


def claude_declared_outputs(action: str) -> list[str]:
    outputs = ["search-request.json"] if action.endswith(".search") else []
    if action.startswith("nl-decision"):
        outputs.append("decision.json")
    return outputs


def claude_limit_enforcement() -> dict[str, str]:
    return {
        "softDeadlineMs": "unavailable",
        "hardDeadlineMs": "unavailable",
        "dispatchDeadlineMs": "externally-enforced",
        "childExecutionTimeoutMs": "externally-enforced",
        "cancelGraceMs": "unavailable",
        "maxTurns": "unavailable",
        "maxToolCalls": "unavailable",
        "maxFinalBytes": "adapter-enforced",
        "maxEventBytes": "adapter-enforced",
        "maxProviderRequests": "unavailable",
        "maxInputTokens": "unavailable",
        "maxOutputTokens": "unavailable",
    }


def claude_isolation(action: str) -> dict[str, Any]:
    return {
        "profile": "claude-artifact-bridge-v1",
        "worker": "separate-read-only-github-job",
        "rootFilesystem": "verified-artifact-workspace",
        "writableRoots": ["/github/workspace", "/tmp"],
        "modelNetwork": {"mode": "runner-default", "allowedHosts": []},
        "toolNetwork": {"mode": "broker-only" if action.endswith(".search") else "none"},
        "inheritEnvironment": True,
        "dropCapabilities": False,
        "noNewPrivileges": False,
        "denyHostHome": False,
    }


def claude_capabilities(action: str, schema_digest: str) -> dict[str, Any]:
    required = [
        {"name": "input.text", "constraints": {"handoff": "content-addressed-bounded", "mount": "read-only"}},
        {"name": "output.structured", "constraints": {"schemaSha256": schema_digest, "strict": True, "mechanismAnyOf": ["trusted-post-action-bridge"]}},
        {"name": "lifecycle.cancel", "constraints": {"mechanism": "parent-workflow-cancel"}},
        {"name": "provenance.actual-model", "constraints": {}},
        {"name": "provenance.actual-provider", "constraints": {}},
        {"name": "isolation.external", "constraints": {"worker": "separate-read-only-github-job", "profile": "claude-artifact-bridge-v1"}},
        {"name": "github.permissions", "constraints": {"actions": "read", "contents": "read", "issues": "none", "actingToken": False}},
        {"name": "credentials.isolated", "constraints": {"fleetToken": "absent", "readonlyToken": "broker-only" if action.endswith(".search") else "absent"}},
        {"name": "tools.declared", "constraints": {"exact": claude_declared_tools(action)}},
        {"name": "target.inputs", "constraints": {"mount": "read-only", "writes": False, "declaredOutputPaths": claude_declared_outputs(action)}},
        {"name": "transcript.bounded", "constraints": {"maxBytes": 8388608, "reduced": True}},
    ]
    return {
        "required": required,
        "optional": [
            {"name": "usage.tokens", "constraints": {}},
            {"name": "usage.cost", "constraints": {}},
            {"name": "quota.snapshot", "constraints": {}},
            {"name": "event.reasoning-summary", "constraints": {"retained": False}},
        ],
    }


def _capabilities(action: str, schema_digest: str, adapter: str) -> dict[str, Any]:
    if adapter == "claude-action-compat":
        return claude_capabilities(action, schema_digest)
    required = [
        {"name": "input.text", "constraints": {}},
        {"name": "process.exec", "constraints": {"mode": "none"}},
        {"name": "tool.network", "constraints": {"mode": "broker-only" if action.endswith(".search") else "none"}},
        {"name": "output.structured", "constraints": {"schemaSha256": schema_digest, "strict": True, "mechanismAnyOf": ["native-schema", "typed-terminating-tool"]}},
        {"name": "lifecycle.cancel", "constraints": {"ackMs": 10000, "mechanism": "adapter-interrupt"}},
        {"name": "provenance.actual-model", "constraints": {}},
        {"name": "provenance.actual-provider", "constraints": {}},
        {"name": "isolation.external", "constraints": {"worker": "sandboxed-adapter-worker"}},
    ]
    if action != "triage.schema-repair":
        required.extend(
            [
                {"name": "fs.read", "constraints": {"roots": ["target.txt", "target-src"], "writes": False}},
                {"name": "fs.grep", "constraints": {"roots": ["target.txt", "target-src"]}},
                {"name": "fs.glob", "constraints": {"roots": ["target-src"]}},
            ]
        )
    if action.endswith(".search"):
        required.append({"name": "github.search.readonly", "constraints": {"broker": True}})
    return {
        "required": required,
        "optional": [
            {"name": "usage.tokens", "constraints": {}},
            {"name": "usage.cost", "constraints": {}},
            {"name": "quota.snapshot", "constraints": {}},
            {"name": "event.reasoning-summary", "constraints": {"retained": False}},
        ],
    }


def _tools(action: str, adapter: str) -> dict[str, Any]:
    if adapter == "claude-action-compat":
        return {"default": "deny", "parallel": False, "tools": []}
    names: list[str] = []
    if action != "triage.schema-repair":
        names = ["fs.read", "fs.grep", "fs.glob"]
    if action.endswith(".search"):
        names.append("github.search.readonly")
    bounds = {"fs.read": 65536, "fs.grep": 65536, "fs.glob": 32768, "github.search.readonly": 65536}
    return {
        "default": "deny",
        "parallel": False,
        "tools": [
            {
                "name": name,
                "version": 1,
                "maxResultBytes": bounds[name],
                "inputSchemaSha256": tool_schema_sha256(name),
            }
            for name in names
        ],
    }


def _limit_enforcement(adapter: str) -> dict[str, str]:
    if adapter == "claude-action-compat":
        return claude_limit_enforcement()
    return {
        "softDeadlineMs": "externally-enforced",
        "hardDeadlineMs": "externally-enforced",
        "dispatchDeadlineMs": "unavailable",
        "childExecutionTimeoutMs": "unavailable",
        "cancelGraceMs": "externally-enforced",
        "maxTurns": "adapter-enforced",
        "maxToolCalls": "adapter-enforced",
        "maxFinalBytes": "externally-enforced",
        "maxEventBytes": "externally-enforced",
        "maxProviderRequests": "adapter-enforced",
        "maxInputTokens": "adapter-enforced",
        "maxOutputTokens": "adapter-enforced",
    }


def _trust_segments(action: str, prompt_digest: str, prompt_bytes: int, inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments = [
        {
            "name": "runtime-instructions",
            "trust": "trusted",
            "origin": "wheelhouse",
            "sha256": prompt_digest,
            "bytes": prompt_bytes,
        }
    ]
    if action.startswith("deep-review"):
        segments.append({"name": "decision-card", "trust": "untrusted", "origin": "card-inline-bounded", "sha256": prompt_digest, "bytes": prompt_bytes})
    elif action.startswith("nl-decision"):
        segments.append({"name": "maintainer-context", "trust": "trusted", "origin": "authorized-card-thread-inline-bounded", "sha256": prompt_digest, "bytes": prompt_bytes})
    elif action == "triage.schema-repair":
        segments.append({"name": "prior-candidate", "trust": "untrusted", "origin": "delivered-model-result-inline-bounded", "sha256": prompt_digest, "bytes": prompt_bytes})
    for item in inputs:
        segments.append({"name": item["id"], "trust": item["trust"], "origin": "prepared-artifact", "sha256": item["sha256"], "bytes": item["bytes"], "artifact": item["artifact"]})
    return segments


def build_task(
    *,
    action: str,
    selection: dict[str, Any],
    prompt_path: str,
    bundle_dir: str,
    output_path: str,
    owner: str,
    repo: str,
    number: int,
    target_kind: str,
    revision: str,
    wheelhouse_revision: str,
    event_key: str,
    target_file: str = "",
    repository_dir: str = "",
    repository_commit: str = "",
    vision_file: str = "",
    repair_kind: str = "pr",
) -> dict[str, Any]:
    if action not in ACTION_LIMITS:
        raise ArtifactError("unsupported agent runtime action")
    if not DIGEST.fullmatch(event_key):
        raise ArtifactError("agent event key binding is invalid")
    adapter = (selection.get("profile") or {}).get("adapter")
    if (selection.get("mode"), adapter) not in (("claude", "claude-action-compat"), ("codex", "codex-app-server")):
        raise ArtifactError("the selected adapter is not supported by the task compiler")
    bundle = Path(bundle_dir).resolve()
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True, mode=0o700)

    source_prompt = Path(prompt_path)
    try:
        prompt_text = source_prompt.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ArtifactError("prompt artifact must be bounded UTF-8 text") from error
    tool_instruction = (
        "This schema-repair task exposes no tools. Work only from the bounded candidate in the prompt."
        if action == "triage.schema-repair"
        else "The sandboxed adapter worker exposes only fs.read, fs.grep, fs.glob and, on search profiles, github.search.readonly. Use those typed tools directly."
    )
    shim_lines = [
        "",
        "<wheelhouse-adapter-shim trust=\"trusted\" version=\"codex-app-server/v1\">",
        tool_instruction,
        "Any earlier request-file, Write, Bash, wheelhouse-search command, or",
        "decision.json instruction describes the direct Claude production path and",
        "is superseded for this adapter task. You have no shell or file-write tool.",
        "Submit the final value through the native strict output schema.",
        "Do not add Markdown fences or prose outside that schema.",
        "</wheelhouse-adapter-shim>",
    ]
    compiled_prompt = prompt_text if adapter == "claude-action-compat" else prompt_text.rstrip() + "\n" + "\n".join(shim_lines) + "\n"
    compiled_path = bundle / ".compiled-prompt"
    compiled_path.write_text(compiled_prompt, encoding="utf-8")
    os.chmod(compiled_path, 0o600)
    prompt_digest, prompt_bytes, prompt_artifact = _copy_file(compiled_path, bundle, 262144)
    compiled_path.unlink()
    inputs: list[dict[str, Any]] = []
    if target_file:
        digest, size, artifact = _copy_file(Path(target_file), bundle, 1_500_000 + 8192)
        inputs.append({"id": "target", "artifact": artifact, "logicalPath": "target.txt", "sha256": digest, "mediaType": "text/plain; charset=utf-8", "trust": "untrusted", "mount": "read-only", "maxBytes": 1_508_192, "bytes": size})
    if repository_dir:
        if not repository_commit:
            raise ArtifactError("repository commit binding is required")
        digest, size, count, artifact, bound_commit, links = _copy_directory(Path(repository_dir), bundle, repository_commit)
        provenance_source = bundle / ".repository-symlink-provenance.json"
        atomic_write_json(provenance_source, _link_provenance_value(bound_commit, links))
        try:
            provenance_digest, provenance_size, provenance_artifact = _copy_file(
                provenance_source,
                bundle,
                MAX_REPOSITORY_PROVENANCE_BYTES,
            )
        finally:
            provenance_source.unlink(missing_ok=True)
        inputs.append({"id": "repository", "artifact": artifact, "logicalPath": "target-src", "sha256": digest, "mediaType": "application/vnd.wheelhouse.repo-snapshot", "trust": "untrusted", "mount": "read-only", "maxBytes": MAX_REPOSITORY_BYTES, "bytes": size, "git": {"commit": bound_commit, "detached": True, "fileCount": count, "treeSha256": digest, "symlinkCount": len(links), "symlinkProvenanceArtifact": provenance_artifact, "symlinkProvenanceSha256": provenance_digest}})
        inputs.append({"id": "repository-provenance", "artifact": provenance_artifact, "logicalPath": "repository-provenance.json", "sha256": provenance_digest, "mediaType": "application/vnd.wheelhouse.repo-symlink-provenance+json", "trust": "untrusted", "mount": "read-only", "maxBytes": MAX_REPOSITORY_PROVENANCE_BYTES, "bytes": provenance_size})
    if vision_file:
        digest, size, artifact = _copy_file(Path(vision_file), bundle, 40000)
        inputs.append({"id": "vision", "artifact": artifact, "logicalPath": "vision.md", "sha256": digest, "mediaType": "text/markdown; charset=utf-8", "trust": "trusted", "mount": "read-only", "maxBytes": 40000, "bytes": size})

    schema_path, schema_id = _schema_for(action, repair_kind)
    schema = load_json_regular(schema_path, max_bytes=65536)
    schema_digest, _, schema_artifact = _copy_file(schema_path, bundle, 65536)
    soft, hard, turns, tool_calls, final_bytes = ACTION_LIMITS[action]
    profile = selection["profile"]
    shim_version = "claude-action-compat/v1" if adapter == "claude-action-compat" else "codex-app-server/v1"
    shim = {
        "adapter": adapter,
        "version": shim_version,
        "promptRole": "user",
        "nativeDefault": "pinned-claude-code-2.1.197" if adapter == "claude-action-compat" else "pinned-codex-0.144.0",
        "tools": "claude-action-mapped" if adapter == "claude-action-compat" else "dynamic-only",
        "output": "trusted-post-action-bridge" if adapter == "claude-action-compat" else "turn/start.outputSchema",
    }
    execution_id = str(uuid.uuid4())
    candidate = {
        "harness": profile["harness"],
        "adapter": profile["adapter"],
        "provider": profile["provider"],
        "authProfile": profile["auth_profile"],
        "authMechanism": profile["auth_mechanism"],
        "expectedWorkspaceId": profile["expected_workspace_id"] or None,
        "model": profile["model"],
        "effort": profile["effort"],
        "costClass": profile["cost_class"],
        "dataBoundary": profile["data_boundary"],
        "allowModelAlias": profile["allow_model_alias"],
    }
    task = {
        "apiVersion": API_VERSION,
        "kind": "AgentTask",
        "metadata": {
            "executionId": execution_id,
            "action": action,
            "idempotencyKey": event_key,
            "wheelhouseRevision": wheelhouse_revision,
            "target": {"owner": owner, "repo": repo, "number": int(number), "kind": target_kind, "revision": revision},
        },
        "spec": {
            "selection": {"profile": selection["profileName"], "candidates": [candidate], "fallback": {"mode": "none"}},
            "prompt": {
                "system": {"mode": "native-default-plus-core", "adapterShimVersion": shim_version, "adapterShimSha256": canonical_sha256(shim)},
                "userArtifact": prompt_artifact,
                "segments": _trust_segments(action, prompt_digest, prompt_bytes, inputs),
            },
            "inputs": inputs,
            "capabilities": _capabilities(action, schema_digest, adapter),
            "tools": _tools(action, adapter),
            "isolation": claude_isolation(action) if adapter == "claude-action-compat" else {
                "profile": "sandboxed-worker-v1",
                "worker": "sandboxed-adapter-worker",
                "rootFilesystem": "read-only",
                "writableRoots": ["/run/wheelhouse/output", "/tmp"],
                "modelNetwork": {"mode": "provider-only", "allowedHosts": list(profile["provider_hosts"])},
                "toolNetwork": {"mode": "broker-only" if action.endswith(".search") else "none"},
                "inheritEnvironment": False,
                "dropCapabilities": True,
                "noNewPrivileges": True,
                "denyHostHome": True,
            },
            "limits": {
                "softDeadlineMs": None if adapter == "claude-action-compat" else soft,
                "hardDeadlineMs": None if adapter == "claude-action-compat" else hard,
                "dispatchDeadlineMs": 60_000 if adapter == "claude-action-compat" else None,
                "childExecutionTimeoutMs": ((hard + 59_999) // 60_000) * 60_000 if adapter == "claude-action-compat" else None,
                "cancelGraceMs": None if adapter == "claude-action-compat" else 10000,
                "maxTurns": None if adapter == "claude-action-compat" else turns,
                "maxToolCalls": None if adapter == "claude-action-compat" else tool_calls,
                "maxFinalBytes": final_bytes,
                "maxEventBytes": 8388608,
                "maxProviderRequests": None if adapter == "claude-action-compat" else (64 if turns > 32 else 40),
                "maxInputTokens": None if adapter == "claude-action-compat" else 180000,
                "maxOutputTokens": None if adapter == "claude-action-compat" else (16000 if action.startswith("deep-review") else 8000),
                "enforcement": _limit_enforcement(adapter),
            },
            "output": {
                "schemaArtifact": schema_artifact,
                "schemaId": schema_id,
                "schemaSha256": schema_digest,
                "evidencePolicy": "target-anchor/v1" if action.startswith("triage.") and action != "triage.schema-repair" else "none",
                "allowProseFallback": False,
            },
            "retry": {"sameCandidateMaxAttempts": 1, "retryable": [], "repairTask": "triage.schema-repair/v1" if action.startswith("triage.") and action != "triage.schema-repair" else None},
            "session": {"mode": "ephemeral", "resume": "forbidden"},
            "retention": {"normalizedEventsDays": 3, "rawTranscript": "transient-cross-job" if adapter == "claude-action-compat" else "discard", "finalResult": "consumer-owned", "redactionPolicy": "wheelhouse-agent/v1"},
        },
    }
    validate_contract(task, "AgentTask")
    atomic_write_json(output_path, task)
    return task
