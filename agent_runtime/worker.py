"""Sandboxed adapter worker entrypoint.

This process has only bounded read-only inputs, exact typed tools, provider-only
network through a Unix-socket proxy, and the minimum model credential. It never
receives a GitHub acting or repository token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import shutil
import signal
import socket
import socketserver
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .adapters.claude import ClaudeProtocolError, ClaudeStreamParser
from .contract import ContractError, atomic_write_json, canonical_json_bytes, canonical_sha256, load_json_regular
from .redaction import redact_text, sanitize_message
from .tools import CanonicalTools, ToolError, dynamic_tool_spec


class WorkerFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        adapter_code: str | None = None,
        spend_started: bool = False,
        usage: dict[str, Any] | None = None,
        actual_model: str = "",
        actual_provider: str = "",
        actual_effort: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.adapter_code = adapter_code
        self.spend_started = spend_started
        self.usage = usage
        self.actual_model = actual_model
        self.actual_provider = actual_provider
        self.actual_effort = actual_effort


class RuntimeBudget:
    def __init__(self, limits: dict[str, Any]) -> None:
        self.max_turns = int(limits["maxTurns"])
        self.max_provider_requests = int(limits["maxProviderRequests"])
        self.max_input_tokens = int(limits["maxInputTokens"])
        self.max_output_tokens = int(limits["maxOutputTokens"])
        self.turns = 0
        self.provider_requests = 0

    def begin_provider_request(self, observed_usage: dict[str, Any] | None = None) -> None:
        if observed_usage is not None:
            normalized = _token_counts(observed_usage)
            if normalized["inputTokens"] is not None and normalized["inputTokens"] >= self.max_input_tokens:
                raise WorkerFailure("context.exceeded", "Agent exhausted the task input-token limit before continuation.", spend_started=self.provider_requests > 0)
            if normalized["outputTokens"] is not None and normalized["outputTokens"] >= self.max_output_tokens:
                raise WorkerFailure("context.exceeded", "Agent exhausted the task output-token limit before continuation.", spend_started=self.provider_requests > 0)
        if self.turns + 1 > self.max_turns:
            raise WorkerFailure("context.exceeded", "Agent exceeded the task turn limit.", spend_started=self.provider_requests > 0)
        if self.provider_requests + 1 > self.max_provider_requests:
            raise WorkerFailure("context.exceeded", "Agent exceeded the task provider-request limit.", spend_started=self.provider_requests > 0)
        self.turns += 1
        self.provider_requests += 1

    def observe_tokens(self, usage: dict[str, Any]) -> None:
        normalized = _token_counts(usage)
        input_tokens = normalized["inputTokens"]
        output_tokens = normalized["outputTokens"]
        if input_tokens is not None and input_tokens > self.max_input_tokens:
            raise WorkerFailure("context.exceeded", "Agent exceeded the task input-token limit.", spend_started=True)
        if output_tokens is not None and output_tokens > self.max_output_tokens:
            raise WorkerFailure("context.exceeded", "Agent exceeded the task output-token limit.", spend_started=True)


class InternalEvents:
    def __init__(self, path: Path, limit: int) -> None:
        self.handle = path.open("wb")
        self.limit = limit
        self.written = 0

    def emit(self, event_type: str, data: dict[str, Any]) -> None:
        line = canonical_json_bytes({"type": event_type, "data": data}) + b"\n"
        if self.written + len(line) > self.limit:
            raise WorkerFailure("harness.protocol", "Adapter event stream exceeded its byte bound.")
        self.handle.write(line)
        self.handle.flush()
        self.written += len(line)

    def close(self) -> None:
        self.handle.close()


class _ProxyBridge:
    def __init__(self, unix_path: str) -> None:
        bridge = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                remote = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                remote.connect(bridge.unix_path)
                threads = []

                def copy(source: socket.socket, destination: socket.socket) -> None:
                    try:
                        while True:
                            data = source.recv(65536)
                            if not data:
                                break
                            destination.sendall(data)
                    except OSError:
                        pass
                    finally:
                        try:
                            destination.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass

                for source, destination in ((self.request, remote), (remote, self.request)):
                    thread = threading.Thread(target=copy, args=(source, destination), daemon=True)
                    thread.start()
                    threads.append(thread)
                for thread in threads:
                    thread.join()
                remote.close()

        self.unix_path = unix_path
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class RpcClient:
    def __init__(self, command: list[str], environment: dict[str, str], events: InternalEvents, secret_values: list[str] | None = None) -> None:
        self.events = events
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            start_new_session=True,
            bufsize=1,
        )
        self.messages: queue.Queue[Any] = queue.Queue()
        self.stderr_secret_matches = 0
        self.secret_values = [value for value in (secret_values or []) if len(value) >= 8]
        self.next_id = 1
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.reader.start()
        self.stderr_reader.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                if len(line.encode("utf-8")) > 8 * 1024 * 1024:
                    self.messages.put(WorkerFailure("harness.protocol", "Codex app-server emitted an oversized protocol message."))
                    return
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    self.messages.put(WorkerFailure("harness.protocol", "Codex app-server emitted malformed JSON."))
                    return
                self.messages.put(value)
        finally:
            self.messages.put(None)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        total = 0
        for line in self.process.stderr:
            if total >= 32768:
                continue
            clean = line[:4096]
            exact_matches = 0
            for secret in self.secret_values:
                if secret in clean:
                    exact_matches += clean.count(secret)
                    clean = clean.replace(secret, "[REDACTED_SECRET]")
            clean, matches = redact_text(clean, max_chars=4096)
            self.stderr_secret_matches += matches + exact_matches
            total += len(clean)
        if self.stderr_secret_matches:
            self.events.emit("warning", {"code": "diagnostic.secret-redacted", "matches": self.stderr_secret_matches})

    def send(self, value: dict[str, Any]) -> None:
        if self.process.poll() is not None or self.process.stdin is None:
            raise WorkerFailure("harness.crash", "Codex app-server exited unexpectedly.")
        line = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> int:
        request_id = self.next_id
        self.next_id += 1
        self.send({"id": request_id, "method": method, "params": params})
        return request_id

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def respond(self, request_id: Any, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result or {}
        self.send(message)

    def get(self, timeout: float = 0.2) -> Any:
        try:
            value = self.messages.get(timeout=timeout)
        except queue.Empty:
            return ...
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise WorkerFailure("harness.crash", "Codex app-server closed its protocol stream.")
        return value

    def terminate(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.reader.join(timeout=2)
        self.stderr_reader.join(timeout=2)


def _wait_response(client: RpcClient, request_id: int, cancel_path: Path, timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel_path.exists():
            raise WorkerFailure("lifecycle.cancelled", "Agent runtime was cancelled before model spend.")
        message = client.get()
        if message is ...:
            continue
        if message.get("id") == request_id:
            if "error" in message:
                raise WorkerFailure("harness.protocol", "Codex app-server rejected a preflight request.", str((message.get("error") or {}).get("code")))
            result = message.get("result")
            if not isinstance(result, dict):
                raise WorkerFailure("harness.protocol", "Codex app-server returned an invalid preflight response.")
            return result
    raise WorkerFailure("transport.connection", "Codex app-server preflight timed out.")


def _auth_secret_values(value: Any) -> list[str]:
    sensitive = {"id_token", "access_token", "refresh_token", "account_id", "personal_access_token", "agent_private_key", "private_key"}
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in sensitive and isinstance(child, str) and child:
                found.append(child)
            else:
                found.extend(_auth_secret_values(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_auth_secret_values(child))
    return found


def _prepare_auth(candidate: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    source = Path("/auth-source/credential")
    if not source.is_file() or source.is_symlink():
        raise WorkerFailure("auth.missing", "codex-subscription credential handoff is missing.")
    mechanism = candidate.get("authMechanism")
    expected_workspace = str(candidate.get("expectedWorkspaceId") or "")
    if not expected_workspace:
        raise WorkerFailure("auth.invalid", "Expected ChatGPT workspace restriction is missing.")
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir(parents=True, mode=0o700)
    credential_environment: dict[str, str] = {}
    secret_values: list[str] = []
    if mechanism == "codex-access-token":
        token = source.read_text(encoding="utf-8").strip()
        if not token.startswith("at-") or len(token) < 16 or any(character.isspace() for character in token):
            raise WorkerFailure("auth.invalid", "Codex access-token credential handoff is invalid.")
        credential_environment["CODEX_ACCESS_TOKEN"] = token
        secret_values.append(token)
    elif mechanism == "managed-auth-json":
        try:
            auth = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkerFailure("auth.invalid", "Managed Codex auth store is invalid.") from error
        tokens = auth.get("tokens") if isinstance(auth, dict) else None
        if not isinstance(auth, dict) or auth.get("auth_mode") != "chatgpt" or not isinstance(tokens, dict) or not tokens.get("refresh_token"):
            raise WorkerFailure("auth.invalid", "Managed Codex auth store is not refreshable ChatGPT authentication.")
        secret_values.extend(_auth_secret_values(auth))
        shutil.copyfile(source, home / "auth.json")
        os.chmod(home / "auth.json", 0o600)
    else:
        raise WorkerFailure("auth.invalid", "Codex subscription auth mechanism is unsupported.")
    config = ("""cli_auth_credentials_store = "file"
forced_login_method = "chatgpt"
forced_chatgpt_workspace_id = %s
check_for_update_on_startup = false
web_search = "disabled"
include_apps_instructions = false
include_collaboration_mode_instructions = false
include_environment_context = false
include_permissions_instructions = false

[analytics]
enabled = false

[history]
persistence = "none"

[model_providers.openai]
request_max_retries = 0
stream_max_retries = 0

[features]
apps = false
browser_use = false
codex_hooks = false
collab = false
connectors = false
hooks = false
memories = false
memory_tool = false
multi_agent = false
multi_agent_mode = false
plugins = false
remote_control = false
responses_websockets = false
responses_websockets_v2 = false
search_tool = false
shell_snapshot = false
shell_tool = false
standalone_web_search = false
tool_search = false
web_search = false
web_search_request = false
""" % json.dumps(expected_workspace))
    (home / "config.toml").write_text(config, encoding="utf-8")
    os.chmod(home / "config.toml", 0o600)
    return credential_environment, secret_values


def _error_from_codex(info: Any) -> tuple[str, bool, bool]:
    if info == "usageLimitExceeded":
        return "provider.quota_exhausted", True, True
    if info == "serverOverloaded":
        return "provider.overloaded", True, True
    if info == "contextWindowExceeded":
        return "context.exceeded", False, False
    if info == "unauthorized":
        return "auth.invalid", False, False
    if info == "sandboxError":
        return "sandbox.violation", False, False
    if isinstance(info, dict):
        if "responseStreamDisconnected" in info:
            return "transport.stream_interrupted", True, True
        if "httpConnectionFailed" in info:
            return "transport.connection", True, True
    return "provider.unavailable", False, False


def _tool_response(value: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return {"success": True, "contentItems": [{"type": "inputText", "text": text}]}


def _item_text(item: dict[str, Any]) -> str:
    for key in ("text", "content", "message"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _bounded_output_schema(schema: dict[str, Any], max_output_tokens: int) -> dict[str, Any]:
    bounded = json.loads(json.dumps(schema))

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            value_type = value.get("type")
            if value_type == "string" or isinstance(value_type, list) and "string" in value_type:
                current = value.get("maxLength")
                value["maxLength"] = min(current, max_output_tokens) if isinstance(current, int) else max_output_tokens
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(bounded)
    return bounded


def _checkpoint(
    output: Path,
    *,
    plan: dict[str, Any],
    spend_started: bool,
    actual_model: str,
    actual_provider: str,
    actual_effort: str,
    usage: dict[str, Any],
) -> None:
    atomic_write_json(
        output / "worker-state.json",
        {
            "executionId": plan["executionId"],
            "requestSha256": plan["taskSha256"],
            "attemptId": plan["attemptId"],
            "spendStarted": spend_started,
            "actualModel": actual_model,
            "actualProvider": actual_provider,
            "actualEffort": actual_effort,
            "usage": usage,
        },
    )


def _run_claude(plan: dict[str, Any], output: Path, events: InternalEvents, cancel_path: Path) -> dict[str, Any]:
    candidate = plan["candidate"]
    actual_model = ""
    actual_provider = ""
    actual_effort = candidate["effort"]
    spend_started = False
    usage: dict[str, Any] = {
        "inputTokens": None,
        "outputTokens": None,
        "cacheReadTokens": None,
        "cacheWriteTokens": None,
        "providerRequests": 0,
        "toolCalls": 0,
        "turns": 0,
        "quota": {"available": False, "snapshotSha256": None, "primaryUsedPercent": None, "secondaryUsedPercent": None},
        "cost": {"amount": None, "currency": None, "quality": "unavailable"},
    }
    source = Path("/auth-source/credential")
    try:
        info = source.lstat()
    except OSError as error:
        raise WorkerFailure("auth.missing", "anthropic-subscription credential handoff is missing.") from error
    if source.is_symlink() or not source.is_file() or info.st_size < 16 or info.st_size > 65536:
        raise WorkerFailure("auth.invalid", "anthropic-subscription credential handoff is invalid.")
    try:
        token = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise WorkerFailure("auth.invalid", "anthropic-subscription credential handoff is invalid.") from error
    if token != token.strip() or "\x00" in token:
        raise WorkerFailure("auth.invalid", "anthropic-subscription credential handoff is invalid.")
    if plan["tools"]["tools"]:
        raise WorkerFailure("capability.unsatisfied", "Claude CLI canonical tool transport is not enabled by this offline profile.")
    claude_plan = plan.get("claude") if isinstance(plan.get("claude"), dict) else {}
    argv = claude_plan.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or argv[0] != "claude"
        or not all(isinstance(value, str) for value in argv)
        or "--fallback-model" in argv
        or claude_plan.get("structuredOutputMechanism") != "native-schema"
    ):
        raise WorkerFailure("harness.protocol", "Claude adapter plan is invalid.")
    prompt_path = Path(str(claude_plan.get("stdinArtifact") or ""))
    try:
        prompt_info = prompt_path.lstat()
        if prompt_path.is_symlink() or not prompt_path.is_file() or prompt_info.st_size > 262144:
            raise OSError("invalid prompt artifact")
        prompt = prompt_path.read_bytes()
        prompt.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as error:
        raise WorkerFailure("contract.invalid", "Claude prompt artifact is unavailable or invalid.") from error
    proxy_path = os.environ.get("WHEELHOUSE_PROVIDER_SOCKET", "")
    if not proxy_path:
        raise WorkerFailure("sandbox.violation", "Provider-only network proxy is unavailable.")
    bridge = _ProxyBridge(proxy_path)
    bridge.start()
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp/home"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "HTTP_PROXY": bridge.url,
        "HTTPS_PROXY": bridge.url,
        "ALL_PROXY": bridge.url,
        "NO_PROXY": "127.0.0.1,localhost",
        "TZ": "UTC",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "CLAUDE_CODE_OAUTH_TOKEN": token,
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
    }
    process: subprocess.Popen[bytes] | None = None
    stdout_messages: queue.Queue[bytes | None | Exception] = queue.Queue(maxsize=4)
    stderr_matches = 0

    def read_stdout() -> None:
        assert process is not None and process.stdout is not None
        try:
            while True:
                line = process.stdout.readline(1024 * 1024 + 1)
                if not line:
                    break
                stdout_messages.put(line)
        except Exception as error:
            stdout_messages.put(error)
        finally:
            stdout_messages.put(None)

    def read_stderr() -> None:
        nonlocal stderr_matches
        assert process is not None and process.stderr is not None
        total = 0
        while True:
            line = process.stderr.readline(4097)
            if not line:
                break
            if total >= 32768:
                continue
            clean = line.decode("utf-8", errors="replace")
            exact = clean.count(token)
            if exact:
                clean = clean.replace(token, "[REDACTED_SECRET]")
            _, pattern = redact_text(clean, max_chars=4096)
            stderr_matches += exact + pattern
            total += min(len(line), 4096)

    parser = ClaudeStreamParser(expected_model=candidate["model"], require_structured_output=True)
    stdout_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None
    cancelled = False
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        spend_started = True
        usage["providerRequests"] = 1
        usage["turns"] = 1
        _checkpoint(
            output,
            plan=plan,
            spend_started=True,
            actual_model="",
            actual_provider="",
            actual_effort=actual_effort,
            usage=usage,
        )
        events.emit("model.request.started", {"model": candidate["model"], "provider": candidate["provider"], "effort": candidate["effort"]})
        assert process.stdin is not None
        process.stdin.write(prompt)
        process.stdin.close()

        stream_closed = False
        while not stream_closed:
            if cancel_path.exists() and not cancelled and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                cancelled = True
                events.emit("cancellation.requested", {"mechanism": "sigterm+process-group"})
            try:
                message = stdout_messages.get(timeout=0.1)
            except queue.Empty:
                if process.poll() is not None and stdout_thread is not None and not stdout_thread.is_alive():
                    stream_closed = True
                continue
            if message is None:
                stream_closed = True
            elif isinstance(message, Exception):
                raise WorkerFailure("transport.stream_interrupted", "Claude stream reader failed.", spend_started=True)
            else:
                try:
                    parser.feed(message)
                except ClaudeProtocolError as error:
                    raise WorkerFailure("harness.protocol", str(error), spend_started=True) from error
        try:
            return_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired as error:
            raise WorkerFailure("harness.protocol", "Claude CLI did not exit after closing its terminal stream.", spend_started=True) from error
        if cancelled:
            raise WorkerFailure("lifecycle.cancelled", "Agent runtime was cancelled.", spend_started=True)
        if return_code != 0:
            raise WorkerFailure("provider.unavailable", "Claude CLI exited without a successful terminal result.", str(return_code), spend_started=True)
        try:
            outcome = parser.finish()
        except ClaudeProtocolError as error:
            raise WorkerFailure("harness.protocol", str(error), spend_started=True) from error
        actual_model = outcome.model
        actual_provider = candidate["provider"]
        usage.update(outcome.usage)
        usage["providerRequests"] = 1
        usage["toolCalls"] = 0
        if usage.get("turns") is None:
            usage["turns"] = 1
        final_bytes = canonical_json_bytes(outcome.structured_output)
        if len(final_bytes) > plan["limits"]["maxFinalBytes"]:
            raise WorkerFailure("output.schema_invalid", "Claude structured output exceeded its byte bound.", spend_started=True)
        final_text = final_bytes.decode("utf-8")
        if token in final_text:
            raise WorkerFailure("sandbox.violation", "Secret scanner rejected the delivered result.", spend_started=True)
        events.emit("adapter.claude.result.completed", {"model": actual_model, "bytes": len(final_bytes), "sha256": hashlib.sha256(final_bytes).hexdigest()})
        events.emit("usage.updated", {"usageSha256": canonical_sha256(usage)})
        _checkpoint(
            output,
            plan=plan,
            spend_started=True,
            actual_model=actual_model,
            actual_provider=actual_provider,
            actual_effort=actual_effort,
            usage=usage,
        )
        return {
            "status": "succeeded",
            "actualModel": actual_model,
            "actualProvider": actual_provider,
            "actualEffort": actual_effort,
            "final": outcome.structured_output,
            "usage": usage,
            "spendStarted": True,
        }
    except WorkerFailure as error:
        error.spend_started = error.spend_started or spend_started
        error.actual_model = actual_model
        error.actual_provider = actual_provider
        error.actual_effort = actual_effort
        error.usage = usage
        _checkpoint(
            output,
            plan=plan,
            spend_started=error.spend_started,
            actual_model=actual_model,
            actual_provider=actual_provider,
            actual_effort=actual_effort,
            usage=usage,
        )
        raise
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if stdout_thread is not None:
            stdout_thread.join(timeout=2)
        if stderr_thread is not None:
            stderr_thread.join(timeout=2)
        if stderr_matches:
            events.emit("warning", {"code": "diagnostic.secret-redacted", "matches": stderr_matches})
        bridge.close()


def _run_codex(plan: dict[str, Any], output: Path, events: InternalEvents, cancel_path: Path) -> dict[str, Any]:
    actual_model = ""
    actual_provider = ""
    actual_effort = ""
    spend_started = False
    usage: dict[str, Any] = {}
    quota: dict[str, Any] = {}
    tools: CanonicalTools | None = None
    budget = RuntimeBudget(plan["limits"])
    credential_environment, secret_values = _prepare_auth(plan["candidate"])
    proxy_path = os.environ.get("WHEELHOUSE_PROVIDER_SOCKET", "")
    if not proxy_path:
        raise WorkerFailure("sandbox.violation", "Provider-only network proxy is unavailable.")
    bridge = _ProxyBridge(proxy_path)
    bridge.start()
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ["CODEX_HOME"],
        "CODEX_HOME": os.environ["CODEX_HOME"],
        "HTTP_PROXY": bridge.url,
        "HTTPS_PROXY": bridge.url,
        "ALL_PROXY": bridge.url,
        "NO_PROXY": "127.0.0.1,localhost",
        "TZ": "UTC",
        "LC_ALL": "C.UTF-8",
        "RUST_LOG": "warn",
        "LOG_FORMAT": "json",
    }
    environment.update(credential_environment)
    command = [
        "codex",
        "app-server",
        "--stdio",
        "--strict-config",
        "--disable", "shell_tool",
        "--disable", "apply_patch_freeform",
        "--disable", "apply_patch_streaming_events",
        "--disable", "code_mode",
        "--disable", "code_mode_host",
        "--disable", "code_mode_only",
        "--disable", "computer_use",
        "--disable", "image_generation",
        "--disable", "js_repl",
        "--disable", "network_proxy",
        "--disable", "request_permissions",
        "--disable", "request_permissions_tool",
        "--disable", "unified_exec",
        "--disable", "unified_exec_zsh_fork",
        "--disable", "web_search",
        "--disable", "web_search_request",
        "--disable", "standalone_web_search",
        "--disable", "apps",
        "--disable", "connectors",
        "--disable", "memories",
        "--disable", "memory_tool",
        "--disable", "multi_agent",
        "--disable", "plugins",
        "--disable", "hooks",
    ]
    client = RpcClient(command, environment, events, secret_values=secret_values)
    try:
        initialize = client.request(
            "initialize",
            {
                "clientInfo": {"name": "wheelhouse_agent_runtime", "title": "Wheelhouse Agent Runtime", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True, "mcpServerOpenaiFormElicitation": False, "requestAttestation": False},
            },
        )
        _wait_response(client, initialize, cancel_path)
        client.notify("initialized")

        account_id = client.request("account/read", {"refreshToken": False})
        account = _wait_response(client, account_id, cancel_path)
        if not isinstance(account.get("account"), dict) or account["account"].get("type") != "chatgpt":
            raise WorkerFailure("auth.invalid", "Observed Codex account is not ChatGPT subscription authentication.")
        if plan["candidate"].get("authMechanism") == "codex-access-token" and account["account"].get("planType") not in ("business", "enterprise", "enterprise_cbp_usage_based"):
            raise WorkerFailure("auth.invalid", "Observed Codex access token is not attached to an eligible workspace plan.")

        models_id = client.request("model/list", {"includeHidden": True, "limit": 100})
        models = _wait_response(client, models_id, cancel_path)
        candidate = plan["candidate"]
        model_rows = models.get("data") or []
        model_row = next((row for row in model_rows if isinstance(row, dict) and (row.get("id") == candidate["model"] or row.get("model") == candidate["model"])), None)
        if model_row is None:
            raise WorkerFailure("capability.unsatisfied", "Requested exact Codex model is unavailable.")
        efforts = [row.get("reasoningEffort") for row in model_row.get("supportedReasoningEfforts") or [] if isinstance(row, dict)]
        if candidate["effort"] not in efforts:
            raise WorkerFailure("capability.unsatisfied", "Requested exact Codex effort is unavailable.")
        if "text" not in (model_row.get("inputModalities") or ["text"]):
            raise WorkerFailure("capability.unsatisfied", "Requested Codex model lacks text input.")

        provider_id = client.request("modelProvider/capabilities/read", {})
        provider_capabilities = _wait_response(client, provider_id, cancel_path)
        quota_available = False
        try:
            quota_id = client.request("account/rateLimits/read", {})
            quota = _wait_response(client, quota_id, cancel_path)
            quota_available = isinstance(quota.get("rateLimits"), dict)
        except WorkerFailure:
            quota_available = False
            quota = {}
        events.emit("capabilities.probed", {"accountType": "chatgpt", "model": candidate["model"], "effort": candidate["effort"], "quotaSnapshot": quota_available, "providerCapabilitiesSha256": canonical_sha256(provider_capabilities)})

        tool_names = [row["name"] for row in plan["tools"]["tools"]]
        max_results = {row["name"]: row["maxResultBytes"] for row in plan["tools"]["tools"]}
        tools = CanonicalTools(
            os.environ.get("WHEELHOUSE_WORK_ROOT", "/work"),
            tool_names,
            max_results,
            search_socket=os.environ.get("WHEELHOUSE_SEARCH_SOCKET", ""),
        )
        thread_id = client.request(
            "thread/start",
            {
                "model": candidate["model"],
                "modelProvider": candidate["provider"],
                "allowProviderModelFallback": False,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "cwd": os.environ.get("WHEELHOUSE_WORK_ROOT", "/work"),
                "ephemeral": True,
                "environments": [],
                "experimentalRawEvents": False,
                "config": {
                    "model_reasoning_effort": candidate["effort"],
                    "model_context_window": plan["limits"]["maxInputTokens"],
                    "model_auto_compact_token_limit": plan["limits"]["maxInputTokens"],
                    "web_search": "disabled",
                    "include_apps_instructions": False,
                    "include_collaboration_mode_instructions": False,
                    "include_environment_context": False,
                    "include_permissions_instructions": False,
                },
                "dynamicTools": [dynamic_tool_spec(name) for name in tool_names],
            },
        )
        thread = _wait_response(client, thread_id, cancel_path)
        actual_model = str(thread.get("model") or "")
        actual_provider = str(thread.get("modelProvider") or "")
        actual_effort = str(thread.get("reasoningEffort") or "")
        if actual_model != candidate["model"]:
            raise WorkerFailure("model.mismatch", "Observed Codex model does not match the selected model.")
        if actual_provider != candidate["provider"]:
            raise WorkerFailure("model.mismatch", "Observed Codex provider does not match the selected provider.")
        if actual_effort and actual_effort != candidate["effort"]:
            raise WorkerFailure("effort.mismatch", "Observed Codex effort does not match the selected effort.")

        prompt = Path("/run/wheelhouse/prompt.txt")
        if not prompt.exists():
            prompt = Path(os.environ["WHEELHOUSE_BUNDLE_ROOT"]) / plan["prompt"]["userArtifact"]
        prompt_text = prompt.read_text(encoding="utf-8")
        schema_path = Path("/run/wheelhouse/output-schema.json")
        if not schema_path.exists():
            schema_path = Path(os.environ["WHEELHOUSE_BUNDLE_ROOT"]) / plan["output"]["schemaArtifact"]
        output_schema = _bounded_output_schema(load_json_regular(schema_path, max_bytes=65536), plan["limits"]["maxOutputTokens"])
        events.emit("model.request.started", {"model": actual_model, "provider": actual_provider, "effort": candidate["effort"]})
        budget.begin_provider_request()
        spend_started = True
        _checkpoint(
            output,
            plan=plan,
            spend_started=True,
            actual_model=actual_model,
            actual_provider=actual_provider,
            actual_effort=actual_effort or candidate["effort"],
            usage=_normalize_usage(usage, tools.calls, quota, budget.provider_requests, budget.turns),
        )
        turn_request = client.request(
            "turn/start",
            {
                "threadId": thread["thread"]["id"],
                "input": [{"type": "text", "text": prompt_text, "text_elements": []}],
                "model": candidate["model"],
                "effort": candidate["effort"],
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "restricted"},
                "environments": [],
                "outputSchema": output_schema,
            },
        )
        started = _wait_response(client, turn_request, Path("/nonexistent"))
        turn_id = str((started.get("turn") or {}).get("id") or "")
        if not turn_id:
            raise WorkerFailure("harness.protocol", "Codex app-server did not return a turn id.", spend_started=True)
        events.emit("adapter.codex.turn.started", {"turnIdSha256": hashlib.sha256(turn_id.encode()).hexdigest()})

        final_text = ""
        interrupted = False
        cancel_sent = False
        terminal_status = ""

        def continue_after_tool(request_id: Any, *, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> None:
            budget.begin_provider_request(usage)
            _checkpoint(
                output,
                plan=plan,
                spend_started=True,
                actual_model=actual_model,
                actual_provider=actual_provider,
                actual_effort=actual_effort or candidate["effort"],
                usage=_normalize_usage(usage, tools.calls, quota, budget.provider_requests, budget.turns),
            )
            client.respond(request_id, result=result, error=error)

        while True:
            if cancel_path.exists() and not cancel_sent:
                client.request("turn/interrupt", {"threadId": thread["thread"]["id"], "turnId": turn_id})
                cancel_sent = True
                events.emit("cancellation.requested", {"mechanism": "turn/interrupt"})
            message = client.get()
            if message is ...:
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if "id" in message and method:
                if method != "item/tool/call":
                    client.request("turn/interrupt", {"threadId": thread["thread"]["id"], "turnId": turn_id})
                    raise WorkerFailure("tool.denied", "Codex requested an unregistered host operation.", spend_started=True)
                name = str(params.get("tool") or "")
                arguments = params.get("arguments")
                call_id = str(params.get("callId") or "")
                events.emit("tool.started", {"callId": call_id, "tool": name, "argumentsSha256": canonical_sha256(arguments)})
                if tools.calls >= plan["limits"]["maxToolCalls"]:
                    client.request("turn/interrupt", {"threadId": thread["thread"]["id"], "turnId": turn_id})
                    raise WorkerFailure("tool.denied", "Agent exceeded the task tool-call limit.", spend_started=True)
                before = time.monotonic()
                try:
                    value = tools.call(name, arguments)
                    try:
                        continue_after_tool(message["id"], result=_tool_response(value))
                    except WorkerFailure:
                        client.request("turn/interrupt", {"threadId": thread["thread"]["id"], "turnId": turn_id})
                        raise
                    encoded = canonical_json_bytes(value)
                    events.emit("tool.completed", {"callId": call_id, "tool": name, "status": "success", "resultSha256": hashlib.sha256(encoded).hexdigest(), "resultBytes": len(encoded), "truncated": bool(value.get("truncated")), "durationMs": int((time.monotonic() - before) * 1000)})
                except ToolError:
                    try:
                        continue_after_tool(message["id"], error={"code": -32000, "message": "canonical tool request rejected"})
                    except WorkerFailure:
                        client.request("turn/interrupt", {"threadId": thread["thread"]["id"], "turnId": turn_id})
                        raise
                    events.emit("tool.completed", {"callId": call_id, "tool": name, "status": "failed", "resultSha256": canonical_sha256({"failed": True}), "resultBytes": 0, "truncated": False, "durationMs": int((time.monotonic() - before) * 1000)})
                continue
            if method == "model/rerouted":
                if not cancel_sent:
                    client.request("turn/interrupt", {"threadId": thread["thread"]["id"], "turnId": turn_id})
                raise WorkerFailure("model.mismatch", "Codex attempted an undeclared model reroute.", spend_started=True)
            if method == "item/started":
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                forbidden_item_types = {
                    "commandExecution",
                    "fileChange",
                    "mcpToolCall",
                    "collabAgentToolCall",
                    "webSearch",
                    "imageGeneration",
                }
                if item.get("type") in forbidden_item_types:
                    if not cancel_sent:
                        client.request("turn/interrupt", {"threadId": thread["thread"]["id"], "turnId": turn_id})
                    raise WorkerFailure("tool.denied", "Codex exposed or invoked a tool outside the negotiated inventory.", spend_started=True)
            if method == "thread/tokenUsage/updated":
                token_usage = params.get("tokenUsage") or params.get("usage") or {}
                if isinstance(token_usage, dict):
                    usage = token_usage
                    events.emit("usage.updated", {"usageSha256": canonical_sha256(token_usage)})
                    _checkpoint(
                        output,
                        plan=plan,
                        spend_started=True,
                        actual_model=actual_model,
                        actual_provider=actual_provider,
                        actual_effort=actual_effort or candidate["effort"],
                        usage=_normalize_usage(usage, tools.calls, quota, budget.provider_requests, budget.turns),
                    )
                    try:
                        budget.observe_tokens(usage)
                    except WorkerFailure:
                        if not cancel_sent:
                            client.request("turn/interrupt", {"threadId": thread["thread"]["id"], "turnId": turn_id})
                            cancel_sent = True
                        raise
            elif method == "item/agentMessage/delta":
                delta = str(params.get("delta") or "")
                events.emit("message.delta", {"bytes": len(delta.encode("utf-8")), "sha256": hashlib.sha256(delta.encode("utf-8")).hexdigest()})
            elif method == "item/completed":
                item = params.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    text = _item_text(item)
                    if text:
                        final_text = text
                        events.emit("message.completed", {"bytes": len(text.encode("utf-8")), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
            elif method == "error":
                turn_error = params.get("error") if isinstance(params.get("error"), dict) else {}
                info = turn_error.get("codexErrorInfo")
                code, _, _ = _error_from_codex(info)
                raise WorkerFailure(code, "Codex provider returned a classified error.", adapter_code=str(info)[:120], spend_started=True)
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                terminal_status = str(turn.get("status") or "")
                if terminal_status == "interrupted":
                    interrupted = True
                events.emit("adapter.codex.turn.completed", {"status": terminal_status})
                break

        if interrupted or cancel_sent:
            raise WorkerFailure("lifecycle.cancelled", "Agent runtime was cancelled.", spend_started=True)
        if terminal_status not in ("completed", "success"):
            raise WorkerFailure("provider.unavailable", "Codex turn did not complete successfully.", terminal_status, spend_started=True)
        if not final_text:
            raise WorkerFailure("output.missing", "Codex completed without a final result.", spend_started=True)
        if len(final_text.encode("utf-8")) > plan["limits"]["maxFinalBytes"]:
            raise WorkerFailure("output.schema_invalid", "Codex final result exceeded its byte bound.", spend_started=True)
        if any(secret in final_text for secret in secret_values):
            raise WorkerFailure("sandbox.violation", "Secret scanner rejected the delivered result.", spend_started=True)
        try:
            final = json.loads(final_text)
        except json.JSONDecodeError:
            return {
                "status": "failed",
                "actualModel": actual_model,
                "actualProvider": actual_provider,
                "actualEffort": actual_effort or candidate["effort"],
                "delivered": final_text,
                "usage": _normalize_usage(usage, tools.calls, quota, budget.provider_requests, budget.turns),
                "spendStarted": spend_started,
                "error": {
                    "code": "output.schema_invalid",
                    "message": "Codex delivered a result that was not valid JSON.",
                    "adapterCode": "invalid-json",
                },
            }
        return {
            "status": "succeeded",
            "actualModel": actual_model,
            "actualProvider": actual_provider,
            "actualEffort": actual_effort or candidate["effort"],
            "final": final,
            "usage": _normalize_usage(usage, tools.calls, quota, budget.provider_requests, budget.turns),
            "spendStarted": spend_started,
        }
    except WorkerFailure as error:
        if spend_started:
            error.spend_started = True
        error.actual_model = actual_model
        error.actual_provider = actual_provider
        error.actual_effort = actual_effort or plan["candidate"]["effort"]
        error.usage = _normalize_usage(
            usage,
            tools.calls if tools is not None else 0,
            quota,
            budget.provider_requests,
            budget.turns,
        )
        _checkpoint(
            output,
            plan=plan,
            spend_started=error.spend_started,
            actual_model=error.actual_model,
            actual_provider=error.actual_provider,
            actual_effort=error.actual_effort,
            usage=error.usage,
        )
        raise
    except Exception as error:
        failure = WorkerFailure(
            "internal.error",
            "Sandboxed adapter worker failed internally.",
            spend_started=spend_started,
            usage=_normalize_usage(
                usage,
                tools.calls if tools is not None else 0,
                quota,
                budget.provider_requests,
                budget.turns,
            ),
            actual_model=actual_model,
            actual_provider=actual_provider,
            actual_effort=actual_effort,
        )
        _checkpoint(
            output,
            plan=plan,
            spend_started=failure.spend_started,
            actual_model=failure.actual_model,
            actual_provider=failure.actual_provider,
            actual_effort=failure.actual_effort,
            usage=failure.usage or {},
        )
        raise failure from error
    finally:
        client.terminate()
        bridge.close()


def _token_counts(usage: dict[str, Any]) -> dict[str, int | None]:
    last = usage.get("last") if isinstance(usage.get("last"), dict) else usage
    total = usage.get("total") if isinstance(usage.get("total"), dict) else last

    def pick(*names: str) -> int | None:
        for source in (total, last):
            for name in names:
                value = source.get(name) if isinstance(source, dict) else None
                if isinstance(value, int) and value >= 0:
                    return value
        return None

    return {
        "inputTokens": pick("inputTokens", "input_tokens"),
        "outputTokens": pick("outputTokens", "output_tokens"),
        "cacheReadTokens": pick("cachedInputTokens", "cacheReadTokens", "cached_input_tokens"),
        "cacheWriteTokens": pick("cacheWriteTokens", "cache_write_tokens"),
    }


def _normalize_usage(usage: dict[str, Any], tool_calls: int, quota: dict[str, Any], provider_requests: int, turns: int) -> dict[str, Any]:
    tokens = _token_counts(usage)
    snapshot = quota.get("rateLimits") if isinstance(quota.get("rateLimits"), dict) else {}
    primary = snapshot.get("primary") if isinstance(snapshot.get("primary"), dict) else {}
    secondary = snapshot.get("secondary") if isinstance(snapshot.get("secondary"), dict) else {}
    quota_summary = {
        "available": bool(snapshot),
        "snapshotSha256": canonical_sha256(quota) if snapshot else None,
        "primaryUsedPercent": primary.get("usedPercent") if isinstance(primary.get("usedPercent"), int) else None,
        "secondaryUsedPercent": secondary.get("usedPercent") if isinstance(secondary.get("usedPercent"), int) else None,
    }
    return {
        **tokens,
        "providerRequests": provider_requests,
        "toolCalls": tool_calls,
        "turns": turns,
        "quota": quota_summary,
        "cost": {"amount": None, "currency": None, "quality": "unavailable"},
    }


def _run_fake(plan: dict[str, Any], output: Path, events: InternalEvents, cancel_path: Path) -> dict[str, Any]:
    script = plan.get("fakeScript") or {}
    if script.get("ignoreTerm"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    tools = CanonicalTools(
        os.environ.get("WHEELHOUSE_WORK_ROOT", str(Path(os.environ["WHEELHOUSE_BUNDLE_ROOT"]) / "work")),
        [row["name"] for row in plan["tools"]["tools"]],
        {row["name"]: row["maxResultBytes"] for row in plan["tools"]["tools"]},
        search_socket=os.environ.get("WHEELHOUSE_SEARCH_SOCKET", ""),
    )
    events.emit("capabilities.probed", {"fake": True})
    if script.get("truncatedEvents"):
        events.handle.write(b"\xff")
        events.handle.flush()
    for call in script.get("toolCalls") or []:
        name = call.get("name")
        args = call.get("arguments")
        events.emit("tool.started", {"callId": str(tools.calls + 1), "tool": name, "argumentsSha256": canonical_sha256(args)})
        value = tools.call(name, args)
        events.emit("tool.completed", {"callId": str(tools.calls), "tool": name, "status": "success", "resultSha256": canonical_sha256(value), "resultBytes": len(canonical_json_bytes(value)), "truncated": bool(value.get("truncated")), "durationMs": 0})
    sleep_ms = int(script.get("sleepMs") or 0)
    elapsed = 0
    while script.get("hang") or elapsed < sleep_ms:
        if cancel_path.exists() and not script.get("ignoreCancel"):
            events.emit("cancellation.requested", {"mechanism": "fake-cancel"})
            raise WorkerFailure("lifecycle.cancelled", "Agent runtime was cancelled.", spend_started=bool(script.get("spendStarted")))
        time.sleep(0.01)
        elapsed += 10
    if script.get("crash"):
        os._exit(17)
    if script.get("malformedResult"):
        (output / "worker-result.json").write_text("{bad", encoding="utf-8")
        return {"skipWrite": True}
    if script.get("nonObjectResult"):
        _checkpoint(
            output,
            plan=plan,
            spend_started=True,
            actual_model=str(script.get("actualModel") or plan["candidate"]["model"]),
            actual_provider=str(script.get("actualProvider") or plan["candidate"]["provider"]),
            actual_effort=str(script.get("actualEffort") or plan["candidate"]["effort"]),
            usage={
                "inputTokens": 12,
                "outputTokens": 6,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
                "providerRequests": 2,
                "toolCalls": tools.calls,
                "turns": 2,
                "cost": {"amount": 0, "currency": "USD", "quality": "estimated"},
            },
        )
        (output / "worker-result.json").write_text("[]", encoding="utf-8")
        return {"skipWrite": True}
    if script.get("error"):
        error = script["error"]
        raise WorkerFailure(str(error.get("code") or "internal.error"), str(error.get("message") or "Fake adapter failed."), str(error.get("adapterCode") or "fake"), bool(error.get("spendStarted")))
    final = script.get("final")
    if final is None:
        raise WorkerFailure("output.missing", "Fake adapter produced no final result.", spend_started=bool(script.get("spendStarted")))
    if script.get("nonCanonicalFinal") and isinstance(final, dict):
        final = dict(final, score=1.5)
    try:
        final_bytes = canonical_json_bytes(final)
    except ContractError:
        final_bytes = json.dumps(final, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    events.emit("message.completed", {"bytes": len(final_bytes), "sha256": hashlib.sha256(final_bytes).hexdigest()})
    return {
        "status": "succeeded",
        "actualModel": str(script.get("actualModel") or plan["candidate"]["model"]),
        "actualProvider": str(script.get("actualProvider") or plan["candidate"]["provider"]),
        "actualEffort": str(script.get("actualEffort") or plan["candidate"]["effort"]),
        "final": final,
        "usage": {
            "inputTokens": 10,
            "outputTokens": 5,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
            "providerRequests": 1,
            "toolCalls": tools.calls,
            "turns": 1,
            "cost": {"amount": 0, "currency": "USD", "quality": "estimated"},
        },
        "spendStarted": bool(script.get("spendStarted", True)),
    }


def _canonical_worker_result(result: dict[str, Any], max_final_bytes: int) -> dict[str, Any]:
    try:
        canonical_json_bytes(result)
        return result
    except ContractError:
        candidate = result.get("final") if "final" in result else result.get("delivered")
        try:
            raw = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            encoded = canonical_json_bytes(raw)
        except (ContractError, TypeError, ValueError):
            raw = ""
            encoded = b""
        normalized = dict(result)
        normalized.pop("final", None)
        normalized.pop("delivered", None)
        normalized["status"] = "failed"
        normalized["error"] = {
            "code": "output.schema_invalid",
            "message": "Adapter delivered a result outside canonical contract JSON.",
            "adapterCode": "non-canonical-json",
        }
        if raw and len(encoded) <= max_final_bytes:
            normalized["delivered"] = raw
        canonical_json_bytes(normalized)
        return normalized


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    plan = load_json_regular(args.plan, max_bytes=16 * 1024 * 1024)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    cancel_path = output / "cancel.request"
    events = InternalEvents(output / "adapter-events.ndjson", max(1024, int(plan["limits"]["maxEventBytes"]) // 2))
    started = time.monotonic()
    result: dict[str, Any]
    try:
        if plan["descriptor"]["adapter"] == "claude-cli":
            result = _run_claude(plan, output, events, cancel_path)
        elif plan["descriptor"]["adapter"] == "codex-app-server":
            result = _run_codex(plan, output, events, cancel_path)
        elif plan["descriptor"]["adapter"] == "fake":
            result = _run_fake(plan, output, events, cancel_path)
        else:
            raise WorkerFailure("selection.no_candidate", "Adapter is not allowlisted.")
    except WorkerFailure as error:
        result = {
            "status": "cancelled" if error.code == "lifecycle.cancelled" else "failed",
            "actualModel": error.actual_model,
            "actualProvider": error.actual_provider,
            "actualEffort": error.actual_effort,
            "usage": error.usage or {
                "inputTokens": None,
                "outputTokens": None,
                "cacheReadTokens": None,
                "cacheWriteTokens": None,
                "providerRequests": 0,
                "toolCalls": 0,
                "turns": 0,
                "cost": {"amount": None, "currency": None, "quality": "unavailable"},
            },
            "spendStarted": error.spend_started,
            "error": {"code": error.code, "message": sanitize_message(error.message), "adapterCode": error.adapter_code},
        }
    except Exception:
        result = {
            "status": "failed",
            "actualModel": "",
            "actualProvider": "",
            "actualEffort": "",
            "usage": {"inputTokens": None, "outputTokens": None, "cacheReadTokens": None, "cacheWriteTokens": None, "providerRequests": 0, "toolCalls": 0, "turns": 0, "cost": {"amount": None, "currency": None, "quality": "unavailable"}},
            "spendStarted": False,
            "error": {"code": "internal.error", "message": "Sandboxed adapter worker failed internally.", "adapterCode": None},
        }
    result["durationMs"] = int((time.monotonic() - started) * 1000)
    events.close()
    if not result.pop("skipWrite", False):
        result = _canonical_worker_result(result, int(plan["limits"]["maxFinalBytes"]))
        atomic_write_json(output / "worker-result.json", result)


if __name__ == "__main__":
    main()
