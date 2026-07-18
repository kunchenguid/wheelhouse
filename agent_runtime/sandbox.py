"""Externally enforced sandboxed adapter worker launcher policy."""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from typing import Any


class SandboxError(ValueError):
    pass


def host_proof(adapter_id: str) -> dict[str, Any]:
    test_mode = os.environ.get("WHEELHOUSE_AGENT_TEST_SANDBOX") == "1"
    if test_mode:
        if adapter_id != "fake":
            raise SandboxError("test sandbox is permitted only for the fake adapter")
        return {
            "implementation": "fake-test-process-boundary",
            "externalSandbox": True,
            "networkProxy": True,
            "denyHostHome": True,
            "processGroupCleanup": True,
            "testOnly": True,
        }
    if platform.system() != "Linux":
        raise SandboxError("production agent runtime requires the Linux GitHub Actions runner")
    binary = shutil.which("bwrap")
    if not binary:
        raise SandboxError("bubblewrap external sandbox is unavailable")
    return {
        "implementation": "bubblewrap-network-namespace-v1",
        "binary": binary,
        "externalSandbox": True,
        "networkProxy": True,
        "denyHostHome": True,
        "processGroupCleanup": True,
        "testOnly": False,
    }


def _bind_if_exists(command: list[str], path: str) -> None:
    if os.path.exists(path):
        command.extend(["--ro-bind", path, path])


def build_command(
    *,
    task: dict[str, Any],
    bundle: str,
    plan_path: str,
    output_dir: str,
    auth_source: str,
    binary_path: str,
    provider_socket: str,
    search_socket: str,
    worker_command: list[str],
    proof: dict[str, Any],
) -> tuple[list[str], dict[str, str]]:
    if proof.get("testOnly"):
        environment = {
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(Path(output_dir) / "fake-home"),
            "TZ": "UTC",
            "LC_ALL": "C.UTF-8",
            "WHEELHOUSE_WORK_ROOT": str(Path(bundle) / "work"),
            "WHEELHOUSE_BUNDLE_ROOT": str(Path(bundle)),
            "WHEELHOUSE_PROVIDER_SOCKET": provider_socket,
            "WHEELHOUSE_SEARCH_SOCKET": search_socket,
        }
        return worker_command, environment

    root = str(Path(__file__).resolve().parents[1])
    command = [
        proof["binary"],
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--uid", str(os.getuid()),
        "--gid", str(os.getgid()),
        "--cap-drop", "ALL",
        "--clearenv",
        "--tmpfs", "/tmp",
        "--proc", "/proc",
        "--dev", "/dev",
        "--dir", "/runtime",
        "--dir", "/run",
        "--dir", "/run/wheelhouse",
        "--dir", "/work",
        "--dir", "/auth-source",
        "--ro-bind", root + "/agent_runtime", "/runtime/agent_runtime",
        "--ro-bind", plan_path, "/run/wheelhouse/plan.json",
        "--bind", output_dir, "/run/wheelhouse/output",
    ]
    for path in ("/usr", "/usr/local", "/bin", "/lib", "/lib64", "/opt", "/etc/ssl", "/etc/ca-certificates"):
        _bind_if_exists(command, path)
    worker_path = "/usr/local/bin:/usr/bin:/bin"
    binary = Path(binary_path)
    parts = binary.parts
    if "node_modules" in parts:
        index = parts.index("node_modules")
        prefix = Path(*parts[:index])
        if not str(prefix).startswith("/"):
            prefix = Path("/") / prefix
        command.extend(["--ro-bind", str(prefix), "/runtime/codex-install"])
        worker_path = "/runtime/codex-install/node_modules/.bin:" + worker_path
    elif binary_path:
        binary_name = "claude" if binary.name == "claude" else "codex"
        command.extend(["--ro-bind", str(binary.resolve()), "/runtime/" + binary_name])
        worker_path = "/runtime:" + worker_path
    node = shutil.which("node")
    if node:
        command.extend(["--ro-bind", str(Path(node).resolve()), "/runtime/node"])
        worker_path = "/runtime:" + worker_path
    if auth_source:
        command.extend(["--ro-bind", auth_source, "/auth-source/credential"])
    if provider_socket:
        command.extend(["--bind", provider_socket, "/run/wheelhouse/provider.sock"])
    if search_socket:
        command.extend(["--bind", search_socket, "/run/wheelhouse/search.sock"])

    bundle_path = Path(bundle)
    for item in task["spec"]["inputs"]:
        source = bundle_path / item["artifact"]
        destination = "/work/" + item["logicalPath"]
        command.extend(["--ro-bind", str(source), destination])
    prompt_source = bundle_path / task["spec"]["prompt"]["userArtifact"]
    schema_source = bundle_path / task["spec"]["output"]["schemaArtifact"]
    command.extend(["--ro-bind", str(prompt_source), "/run/wheelhouse/prompt.txt"])
    command.extend(["--ro-bind", str(schema_source), "/run/wheelhouse/output-schema.json"])
    command.extend(
        [
            "--setenv", "PYTHONPATH", "/runtime",
            "--setenv", "PATH", worker_path,
            "--setenv", "HOME", "/tmp/home",
            "--setenv", "CODEX_HOME", "/tmp/codex-home",
            "--setenv", "TMPDIR", "/tmp",
            "--setenv", "TZ", "UTC",
            "--setenv", "LC_ALL", "C.UTF-8",
            "--setenv", "LANG", "C.UTF-8",
            "--setenv", "WHEELHOUSE_WORK_ROOT", "/work",
            "--setenv", "WHEELHOUSE_BUNDLE_ROOT", "/run/wheelhouse",
            "--setenv", "WHEELHOUSE_PROVIDER_SOCKET", "/run/wheelhouse/provider.sock" if provider_socket else "",
            "--setenv", "WHEELHOUSE_SEARCH_SOCKET", "/run/wheelhouse/search.sock" if search_socket else "",
            "--chdir", "/work",
            "--",
        ]
    )
    translated = ["/run/wheelhouse/plan.json" if arg == plan_path else "/run/wheelhouse/output" if arg == output_dir else arg for arg in worker_command]
    command.extend(translated)
    return command, {}
