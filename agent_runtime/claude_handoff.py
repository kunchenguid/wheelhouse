"""Bounded content-addressed handoff for the read-only Claude model workflow."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import re
from pathlib import Path
from typing import Any

from .contract import ContractError, atomic_write_json, canonical_sha256, file_sha256, load_json_regular, validate_contract

MAX_HANDOFF_BYTES = 220_000_000
MAX_HANDOFF_FILES = 32_000
ROOT = Path(__file__).resolve().parent
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _files(root: Path, excluded: set[str] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total = 0
    excluded = excluded or set()
    for base, dirs, names in os.walk(root, topdown=True, followlinks=False):
        dirs[:] = sorted(dirs)
        for name in dirs:
            if (Path(base) / name).is_symlink():
                raise ContractError("handoff contains a directory symlink")
        for name in sorted(names):
            path = Path(base) / name
            relative = path.relative_to(root).as_posix()
            if relative in excluded:
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ContractError("handoff contains a non-regular file")
            total += info.st_size
            if total > MAX_HANDOFF_BYTES or len(rows) >= MAX_HANDOFF_FILES:
                raise ContractError("handoff exceeds its declared bound")
            rows.append({"path": relative, "bytes": info.st_size, "sha256": file_sha256(path)})
    return rows


def _copy_runtime(destination: Path) -> None:
    package = destination / "runtime" / "agent_runtime"
    package.mkdir(parents=True)
    for name in ("__init__.py", "contract.py", "claude_handoff.py"):
        shutil.copyfile(ROOT / name, package / name)
    shutil.copytree(ROOT / "schemas", package / "schemas")
    shutil.copyfile(ROOT.parent / "scripts" / "nl_readonly_search.py", destination / "runtime" / "nl_readonly_search.py")


def _verify_bundle(task: dict[str, Any], bundle: Path) -> None:
    references = [task["spec"]["prompt"]["userArtifact"], task["spec"]["output"]["schemaArtifact"]]
    references.extend(item["artifact"] for item in task["spec"]["inputs"])
    for reference in references:
        path = bundle / reference
        expected = reference.rsplit("/", 1)[-1]
        if path.is_symlink() or not path.exists():
            raise ContractError("handoff artifact is missing or unsafe")
        if path.is_file():
            if file_sha256(path) != expected:
                raise ContractError("handoff artifact digest mismatch")
            item = next((row for row in task["spec"]["inputs"] if row["artifact"] == reference), None)
            if item is not None and (item["sha256"] != expected or path.stat().st_size != item["bytes"] or item["bytes"] > item["maxBytes"]):
                raise ContractError("handoff input binding mismatch")
            continue
        item = next((row for row in task["spec"]["inputs"] if row["artifact"] == reference), None)
        if item is None or not path.is_dir():
            raise ContractError("handoff directory artifact is invalid")
        rows = _files(path)
        if (
            canonical_sha256(rows) != expected
            or sum(row["bytes"] for row in rows) != item["bytes"]
            or len(rows) != item["git"]["fileCount"]
        ):
            raise ContractError("handoff directory artifact digest mismatch")


def pack(task_path: str, bundle_dir: str, output_dir: str, allowed_repos: str) -> dict[str, Any]:
    task = load_json_regular(task_path, max_bytes=16 * 1024 * 1024)
    validate_contract(task, "AgentTask")
    try:
        repos = json.loads(allowed_repos or "[]")
    except json.JSONDecodeError as error:
        raise ContractError("search repository scope is invalid") from error
    if (
        not isinstance(repos, list)
        or len(repos) > 256
        or any(not isinstance(repo, str) or not REPOSITORY.fullmatch(repo) for repo in repos)
    ):
        raise ContractError("search repository scope is invalid")
    repos = sorted(set(repos))
    source = Path(bundle_dir).resolve()
    destination = Path(output_dir).resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ContractError("handoff paths must be disjoint")
    _verify_bundle(task, source)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, mode=0o700)
    shutil.copytree(source, destination / "bundle")
    _copy_runtime(destination)
    metadata = {
        "version": 1,
        "taskSha256": canonical_sha256(task),
        "action": task["metadata"]["action"],
        "allowedRepos": repos,
    }
    atomic_write_json(destination / "handoff.json", metadata)
    manifest = _files(destination, {"manifest.json"})
    manifest_sha = canonical_sha256(manifest)
    atomic_write_json(destination / "manifest.json", {"version": 1, "files": manifest, "manifestSha256": manifest_sha})
    return {**metadata, "manifestSha256": manifest_sha}


def verify(handoff_dir: str) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(handoff_dir).resolve()
    manifest = load_json_regular(root / "manifest.json", max_bytes=8 * 1024 * 1024)
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("files"), list):
        raise ContractError("handoff manifest is invalid")
    observed = _files(root, {"manifest.json"})
    if observed != manifest["files"] or canonical_sha256(observed) != manifest.get("manifestSha256"):
        raise ContractError("handoff manifest verification failed")
    metadata = load_json_regular(root / "handoff.json", max_bytes=65536)
    task = load_json_regular(root / "bundle" / "task.json", max_bytes=16 * 1024 * 1024)
    validate_contract(task, "AgentTask")
    if metadata.get("taskSha256") != canonical_sha256(task) or metadata.get("action") != task["metadata"]["action"]:
        raise ContractError("handoff task binding failed")
    if not isinstance(metadata.get("allowedRepos"), list) or any(not isinstance(repo, str) or not REPOSITORY.fullmatch(repo) for repo in metadata["allowedRepos"]):
        raise ContractError("handoff search scope is invalid")
    _verify_bundle(task, root / "bundle")
    return metadata, task


def _copy_artifact(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination)
        for base, dirs, names in os.walk(destination):
            for name in dirs:
                os.chmod(Path(base) / name, 0o500)
            for name in names:
                os.chmod(Path(base) / name, 0o400)
        os.chmod(destination, 0o500)
    else:
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o400)


def hydrate(handoff_dir: str, workspace_dir: str) -> dict[str, Any]:
    metadata, task = verify(handoff_dir)
    root = Path(handoff_dir).resolve()
    workspace = Path(workspace_dir).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if any(workspace.iterdir()):
        raise ContractError("model workspace must start empty")
    prompt = root / "bundle" / task["spec"]["prompt"]["userArtifact"]
    if file_sha256(prompt) != task["spec"]["prompt"]["userArtifact"].rsplit("/", 1)[-1]:
        raise ContractError("prompt artifact digest mismatch")
    for item in task["spec"]["inputs"]:
        source = root / "bundle" / item["artifact"]
        destination = workspace / item["logicalPath"]
        _copy_artifact(source, destination)
    return {
        "action": task["metadata"]["action"],
        "prompt": prompt.read_text(encoding="utf-8"),
        "taskSha256": canonical_sha256(task),
        "allowedRepos": metadata["allowedRepos"],
        "dispatchDeadlineMs": task["spec"]["limits"]["dispatchDeadlineMs"],
        "childExecutionTimeoutMs": task["spec"]["limits"]["childExecutionTimeoutMs"],
    }


def declared_output_paths(task: dict[str, Any]) -> list[str]:
    matches = [
        row.get("constraints", {}).get("declaredOutputPaths")
        for row in task["spec"]["capabilities"]["required"]
        if row.get("name") == "target.inputs"
    ]
    if len(matches) != 1 or not isinstance(matches[0], list) or any(not isinstance(path, str) or not path for path in matches[0]):
        raise ContractError("declared output paths are invalid")
    return matches[0]


def workspace_input_observation(task: dict[str, Any], workspace_dir: str) -> str:
    validate_contract(task, "AgentTask")
    workspace = Path(workspace_dir).resolve()
    output_paths = declared_output_paths(task)
    allowed = [Path(item["logicalPath"]) for item in task["spec"]["inputs"]] + [Path(path) for path in output_paths] + [Path(".git")]
    for path in workspace.rglob("*"):
        relative = path.relative_to(workspace)
        if not any(relative == root or root in relative.parents or relative in root.parents for root in allowed):
            raise ContractError("model workspace contains an undeclared output path")
    observed: list[dict[str, Any]] = []
    for item in task["spec"]["inputs"]:
        path = workspace / item["logicalPath"]
        try:
            info = path.lstat()
        except OSError as error:
            raise ContractError("hydrated input is missing") from error
        if stat.S_ISLNK(info.st_mode):
            raise ContractError("hydrated input is a symlink")
        if path.is_file():
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o400 or info.st_size != item["bytes"] or file_sha256(path) != item["sha256"]:
                raise ContractError("hydrated file input changed")
            observed.append({"logicalPath": item["logicalPath"], "kind": "file", "bytes": info.st_size, "sha256": item["sha256"], "mode": "0400"})
            continue
        if not path.is_dir() or stat.S_IMODE(info.st_mode) != 0o500:
            raise ContractError("hydrated directory input changed")
        for directory in [path, *(candidate for candidate in path.rglob("*") if candidate.is_dir())]:
            directory_info = directory.lstat()
            if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode) or stat.S_IMODE(directory_info.st_mode) != 0o500:
                raise ContractError("hydrated directory permissions changed")
        rows = _files(path)
        if any(stat.S_IMODE((path / row["path"]).lstat().st_mode) != 0o400 for row in rows):
            raise ContractError("hydrated file permissions changed")
        if canonical_sha256(rows) != item["sha256"] or sum(row["bytes"] for row in rows) != item["bytes"] or len(rows) != item["git"]["fileCount"]:
            raise ContractError("hydrated directory input changed")
        observed.append({"logicalPath": item["logicalPath"], "kind": "directory", "bytes": item["bytes"], "sha256": item["sha256"], "fileCount": len(rows), "mode": "0500"})
    return canonical_sha256({"inputs": observed, "declaredOutputPaths": output_paths})


def _output(name: str, value: Any) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    text = json.dumps(value, separators=(",", ":")) if not isinstance(value, str) else value
    if not path:
        print(text)
        return
    delimiter = "WHEELHOUSE_HANDOFF_%s" % os.urandom(16).hex()
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("%s<<%s\n%s\n%s\n" % (name, delimiter, text, delimiter))


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    pack_parser = commands.add_parser("pack")
    pack_parser.add_argument("--task", required=True)
    pack_parser.add_argument("--bundle", required=True)
    pack_parser.add_argument("--out", required=True)
    pack_parser.add_argument("--allowed-repos", default="[]")
    hydrate_parser = commands.add_parser("hydrate")
    hydrate_parser.add_argument("--handoff", required=True)
    hydrate_parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    if args.command == "pack":
        value = pack(args.task, args.bundle, args.out, args.allowed_repos)
    else:
        value = hydrate(args.handoff, args.workspace)
    for key, item in value.items():
        _output(key, item)
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":
    main()
