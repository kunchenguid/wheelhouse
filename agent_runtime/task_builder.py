"""Build immutable, content-addressed AgentTask v1 requests."""

from __future__ import annotations

import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any

from . import API_VERSION
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


def _directory_manifest(source: Path) -> tuple[list[dict[str, Any]], int]:
    entries: list[dict[str, Any]] = []
    total = 0
    for base, dirs, names in os.walk(source, topdown=True, followlinks=False):
        base_path = Path(base)
        dirs[:] = sorted(name for name in dirs if name != ".git")
        for name in list(dirs):
            child = base_path / name
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ArtifactError("repository snapshot contains a symlink")
            if not stat.S_ISDIR(info.st_mode):
                raise ArtifactError("repository snapshot contains a special directory entry")
        for name in sorted(names):
            child = base_path / name
            relative = str(child.relative_to(source))
            if relative == ".git" or relative.startswith(".git/"):
                continue
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ArtifactError("repository snapshot contains a non-regular file")
            total += info.st_size
            if total > MAX_REPOSITORY_BYTES:
                raise ArtifactError("repository snapshot exceeds its byte bound")
            entries.append({"path": relative, "bytes": info.st_size, "sha256": file_sha256(child)})
            if len(entries) > MAX_REPOSITORY_FILES:
                raise ArtifactError("repository snapshot exceeds its file-count bound")
    return entries, total


def _copy_directory(source: Path, bundle: Path) -> tuple[str, int, int, str]:
    info = source.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ArtifactError("repository input must be a directory")
    manifest, total = _directory_manifest(source)
    digest = canonical_sha256(manifest)
    destination = _artifact_path(bundle, digest)
    if not destination.exists():
        destination.mkdir(parents=True)
        for row in manifest:
            src = source / row["path"]
            dst = destination / row["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o400)
        for base, dirs, _ in os.walk(destination, topdown=False):
            for name in dirs:
                os.chmod(Path(base) / name, 0o500)
        os.chmod(destination, 0o500)
    return digest, total, len(manifest), "artifacts/sha256/%s" % digest


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
        {"name": "lifecycle.cancel", "constraints": {"ackMs": 10000, "mechanism": "parent-workflow-cancel"}},
        {"name": "provenance.actual-model", "constraints": {}},
        {"name": "provenance.actual-provider", "constraints": {}},
        {"name": "isolation.external", "constraints": {"worker": "separate-read-only-github-job", "profile": "claude-artifact-bridge-v1"}},
        {"name": "github.permissions", "constraints": {"actions": "read", "contents": "read", "issues": "none", "actingToken": False}},
        {"name": "credentials.isolated", "constraints": {"fleetToken": "absent", "readonlyToken": "broker-only" if action.endswith(".search") else "absent"}},
        {"name": "tools.declared", "constraints": {"exact": claude_declared_tools(action)}},
        {"name": "target.inputs", "constraints": {"mount": "read-only", "writes": False}},
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
    target_file: str = "",
    repository_dir: str = "",
    repository_commit: str = "",
    vision_file: str = "",
    repair_kind: str = "pr",
) -> dict[str, Any]:
    if action not in ACTION_LIMITS:
        raise ArtifactError("unsupported agent runtime action")
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
        digest, size, count, artifact = _copy_directory(Path(repository_dir), bundle)
        commit = repository_commit
        if not commit:
            raise ArtifactError("repository commit binding is required")
        inputs.append({"id": "repository", "artifact": artifact, "logicalPath": "target-src", "sha256": digest, "mediaType": "application/vnd.wheelhouse.repo-snapshot", "trust": "untrusted", "mount": "read-only", "maxBytes": MAX_REPOSITORY_BYTES, "bytes": size, "git": {"commit": commit, "detached": True, "fileCount": count, "treeSha256": digest}})
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
            "idempotencyKey": "%s:%s:%s:%s" % (action, repo, number, revision),
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
                "softDeadlineMs": soft,
                "hardDeadlineMs": hard,
                "cancelGraceMs": 10000,
                "maxTurns": turns,
                "maxToolCalls": tool_calls,
                "maxFinalBytes": final_bytes,
                "maxEventBytes": 8388608,
                "maxProviderRequests": 64 if turns > 32 else 40,
                "maxInputTokens": 180000,
                "maxOutputTokens": 16000 if action.startswith("deep-review") else 8000,
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
