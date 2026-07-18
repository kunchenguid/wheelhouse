"""Pinned Codex CLI app-server adapter host implementation."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from ..capabilities import codex_descriptor
from ..contract import canonical_sha256, file_sha256
from .base import AdapterDescriptor, AdapterProbe, AgentAdapterV1

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "runtime.lock.json"
VENDOR = ROOT / "vendor" / "codex-app-server-0.144.0"


class CodexProbeError(ValueError):
    pass


def _load_lock() -> dict[str, Any]:
    with LOCK_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _protocol_digest(lock: dict[str, Any]) -> str:
    expected = lock["codex"]["protocolSchemas"]
    observed = {}
    for name, digest in sorted(expected.items()):
        path = VENDOR / name
        if not path.is_file() or path.is_symlink():
            raise CodexProbeError("pinned Codex protocol schema is unavailable")
        actual = file_sha256(path)
        if actual != digest:
            raise CodexProbeError("pinned Codex protocol schema digest mismatch")
        observed[name] = actual
    return canonical_sha256(observed)


def _auth_file(mechanism: str) -> Path:
    """Resolve only a named private-boundary credential handoff.

    The public workflow never sets this path. A future captain-approved private
    credential boundary may materialize one mode-0600 file for the selected
    worker. Ambient CODEX_HOME, CODEX_ACCESS_TOKEN, and auth blobs in repository
    secrets are deliberately not accepted.
    """
    raw = os.environ.get("WHEELHOUSE_CODEX_CREDENTIAL_FILE", "").strip()
    if not raw:
        raise CodexProbeError("codex-subscription credential handoff is unavailable")
    path = Path(raw)
    try:
        info = path.lstat()
    except OSError as error:
        raise CodexProbeError("codex-subscription credential handoff is missing") from error
    maximum = 4096 if mechanism == "codex-access-token" else 1024 * 1024
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > maximum:
        raise CodexProbeError("codex-subscription credential handoff is invalid")
    if info.st_mode & 0o077:
        raise CodexProbeError("codex-subscription credential handoff permissions are too broad")
    return path


class CodexAppServerAdapter(AgentAdapterV1):
    id = "codex-app-server"
    adapter_version = "1.0.0"

    def probe(self, task: dict[str, Any], schema_bytes: bytes | None = None) -> AdapterProbe:
        candidate = task["spec"]["selection"]["candidates"][0]
        if candidate.get("authProfile") != "codex-subscription":
            raise CodexProbeError("Codex adapter requires the codex-subscription auth profile")
        if candidate.get("provider") != "openai" or candidate.get("costClass") != "subscription":
            raise CodexProbeError("Codex adapter refuses provider or billing substitution")
        mechanism = str(candidate.get("authMechanism") or "")
        if mechanism not in ("codex-access-token", "managed-auth-json"):
            raise CodexProbeError("codex-subscription auth mechanism is not approved")
        if not candidate.get("expectedWorkspaceId"):
            raise CodexProbeError("codex-subscription expected workspace restriction is missing")
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "ANTHROPIC_API_KEY"):
            if os.environ.get(name):
                raise CodexProbeError("ambient model credentials are forbidden")
        auth = _auth_file(mechanism)
        binary = shutil.which("codex")
        if not binary:
            raise CodexProbeError("pinned Codex CLI is unavailable")
        lock = _load_lock()
        expected_version = lock["codex"]["binaryVersion"]
        version_env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": "/nonexistent",
            "CODEX_HOME": "/nonexistent",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
        }
        check = subprocess.run([binary, "--version"], capture_output=True, text=True, env=version_env, timeout=20)
        version_text = (check.stdout + check.stderr).strip()
        if check.returncode != 0 or expected_version not in version_text.split():
            raise CodexProbeError("Codex CLI version does not match the runtime pin")
        resolved = str(Path(binary).resolve())
        digest = file_sha256(resolved, max_bytes=512 * 1024 * 1024)
        protocol_digest = _protocol_digest(lock)
        descriptor = codex_descriptor(expected_version, digest, protocol_digest)
        return AdapterProbe(
            descriptor=AdapterDescriptor(descriptor),
            binary_path=binary,
            auth_source=str(auth),
            supplemental={
                "binaryResolved": resolved,
                "binaryDigest": digest,
                "protocolSchemaSha256": protocol_digest,
                "packageIntegrity": lock["codex"]["npmPackageIntegrity"],
                "sourceCommit": lock["codex"]["sourceCommit"],
                "authMode": "chatgpt",
                "authMechanism": mechanism,
            },
        )

    def compile(self, task: dict[str, Any], proof: dict[str, Any], probe: AdapterProbe) -> dict[str, Any]:
        candidate = task["spec"]["selection"]["candidates"][0]
        return {
            "planVersion": "wheelhouse.agent-runtime/adapter-plan-v1",
            "executionId": task["metadata"]["executionId"],
            "action": task["metadata"]["action"],
            "taskSha256": canonical_sha256(task),
            "candidate": candidate,
            "descriptor": probe.descriptor.value,
            "negotiation": proof,
            "prompt": task["spec"]["prompt"],
            "inputs": task["spec"]["inputs"],
            "tools": task["spec"]["tools"],
            "limits": task["spec"]["limits"],
            "output": task["spec"]["output"],
            "retention": task["spec"]["retention"],
            "isolation": task["spec"]["isolation"],
            "codex": {
                "binaryVersion": probe.descriptor.value["harnessVersion"],
                "protocol": probe.descriptor.value["protocol"],
                "strictConfig": True,
                "allowProviderModelFallback": False,
                "ephemeral": True,
                "historyPersistence": "none",
                "analytics": False,
                "shell": False,
                "webSearch": False,
                "apps": False,
                "memories": False,
                "multiAgent": False,
            },
        }

    def worker_command(self, plan_path: str, output_dir: str) -> list[str]:
        return ["python3", "-m", "agent_runtime.worker", "--plan", plan_path, "--output-dir", output_dir]

    def cancel_protocol(self) -> str:
        return "turn/interrupt"
