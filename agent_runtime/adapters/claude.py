"""Pinned Claude CLI stream-JSON adapter.

This module owns only Claude-specific preflight, invocation, protocol parsing,
and cancellation declarations. The runtime core remains responsible for
sandboxing, deadlines, trusted output validation, evidence, and AgentResult.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from ..capabilities import claude_descriptor
from ..contract import canonical_json_bytes, canonical_sha256, file_sha256
from .base import AdapterDescriptor, AdapterProbe, AgentAdapterV1

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "runtime.lock.json"
PROTOCOL_FIXTURE = (
    ROOT / "vendor" / "claude-stream-json-2.1.215" / "protocol-fixture.ndjson"
)
EXPECTED_MODEL = "claude-sonnet-4-6"
AUTH_ENVIRONMENT = "CLAUDE_CODE_OAUTH_TOKEN"
CREDENTIAL_FILE_ENVIRONMENT = "WHEELHOUSE_CLAUDE_CREDENTIAL_FILE"
MAX_SCHEMA_BYTES = 65536
MAX_STREAM_LINE_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 8 * 1024 * 1024


class ClaudeProbeError(ValueError):
    """A content-free Claude preflight failure."""


class ClaudeProtocolError(ValueError):
    """A content-free Claude stream protocol failure."""


@dataclass(frozen=True)
class ClaudeStreamOutcome:
    structured_output: Any
    model: str
    usage: dict[str, int | None]
    terminal_subtype: str


def _load_lock() -> dict[str, Any]:
    with LOCK_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _platform_key() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return "linux-x64"
    if system == "Linux" and machine in ("aarch64", "arm64"):
        return "linux-arm64"
    if system == "Darwin" and machine == "arm64":
        return "darwin-arm64"
    raise ClaudeProbeError("Claude CLI platform is not pinned by the runtime lock")


def _protocol_fixture_digest(lock: dict[str, Any]) -> str:
    if (
        lock["claude"].get("protocolFixture")
        != "vendor/claude-stream-json-2.1.215/protocol-fixture.ndjson"
    ):
        raise ClaudeProbeError("pinned Claude protocol fixture identity mismatch")
    expected = str(lock["claude"]["protocolFixtureSha256"])
    try:
        info = PROTOCOL_FIXTURE.lstat()
    except OSError as error:
        raise ClaudeProbeError(
            "pinned Claude protocol fixture is unavailable"
        ) from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ClaudeProbeError("pinned Claude protocol fixture is invalid")
    actual = file_sha256(PROTOCOL_FIXTURE, max_bytes=MAX_STREAM_BYTES)
    if not hmac.compare_digest(actual, expected):
        raise ClaudeProbeError("pinned Claude protocol fixture digest mismatch")
    with PROTOCOL_FIXTURE.open("rb") as handle:
        outcome = parse_stream(
            handle, expected_model=EXPECTED_MODEL, require_structured_output=True
        )
    if outcome.structured_output != {"fixture": True}:
        raise ClaudeProbeError("pinned Claude protocol fixture is unsupported")
    return actual


def _require_int(value: Any, name: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ClaudeProbeError("Claude output schema uses an invalid %s" % name)
    return value


def _strict_json(text: str) -> Any:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in values:
            if name in result:
                raise ValueError("duplicate object key")
            result[name] = value
        return result

    def constant(_value: str) -> Any:
        raise ValueError("non-finite number")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def validate_schema_subset(
    schema_bytes: bytes, expected_sha256: str = ""
) -> tuple[dict[str, Any], str]:
    """Validate the exact small schema subset used by Wheelhouse actions."""

    if (
        not isinstance(schema_bytes, bytes)
        or not schema_bytes
        or len(schema_bytes) > MAX_SCHEMA_BYTES
    ):
        raise ClaudeProbeError("Claude output schema exceeds its supported byte bound")
    observed = hashlib.sha256(schema_bytes).hexdigest()
    if expected_sha256 and not hmac.compare_digest(observed, expected_sha256):
        raise ClaudeProbeError(
            "Claude output schema digest does not match the bound task"
        )
    try:
        text = schema_bytes.decode("utf-8", errors="strict")
        schema = _strict_json(text)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise ClaudeProbeError(
            "Claude output schema is not valid UTF-8 JSON"
        ) from error
    if not isinstance(schema, dict):
        raise ClaudeProbeError("Claude output schema must be an object")
    if schema.get("$schema") not in {
        "https://json-schema.org/draft/2020-12/schema",
        "http://json-schema.org/draft-07/schema#",
    }:
        raise ClaudeProbeError("Claude output schema dialect is unsupported")

    allowed = {
        "$schema",
        "$id",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "pattern",
        "items",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "$ref",
        "definitions",
    }

    node_count = 0
    definitions = schema.get("definitions", {})

    def visit(node: Any, root: bool = False, depth: int = 0) -> None:
        nonlocal node_count
        node_count += 1
        if depth > 8 or node_count > 256:
            raise ClaudeProbeError("Claude output schema exceeds its structural bound")
        if not isinstance(node, dict):
            raise ClaudeProbeError(
                "Claude output schema contains a non-object schema node"
            )
        unknown = set(node) - allowed
        if unknown:
            raise ClaudeProbeError("Claude output schema uses an unsupported keyword")
        if "$ref" in node:
            reference = node["$ref"]
            if (
                set(node) != {"$ref"}
                or not isinstance(reference, str)
                or not reference.startswith("#/definitions/")
                or reference.removeprefix("#/definitions/") not in definitions
            ):
                raise ClaudeProbeError("Claude output schema reference is invalid")
            return
        if not root and any(name in node for name in ("$schema", "$id")):
            raise ClaudeProbeError(
                "Claude output schema metadata is allowed only at the root"
            )
        if root and (
            not isinstance(node.get("$id"), str)
            or not node["$id"]
            or len(node["$id"]) > 256
        ):
            raise ClaudeProbeError("Claude output schema identity is invalid")
        node_type = node.get("type")
        if node_type not in ("object", "array", "string", "boolean", "integer"):
            raise ClaudeProbeError("Claude output schema uses an unsupported type")
        if "const" in node and "enum" in node:
            raise ClaudeProbeError("Claude output schema cannot combine const and enum")
        if "enum" in node:
            values = node["enum"]
            if not isinstance(values, list) or not values or len(values) > 64:
                raise ClaudeProbeError("Claude output schema enum is invalid")
            try:
                if len({canonical_json_bytes(value) for value in values}) != len(
                    values
                ):
                    raise ClaudeProbeError(
                        "Claude output schema enum contains duplicates"
                    )
            except (TypeError, ValueError) as error:
                raise ClaudeProbeError(
                    "Claude output schema enum is invalid"
                ) from error
        scalar_values = list(node.get("enum") or [])
        if "const" in node:
            scalar_values.append(node["const"])
        if node_type == "string" and any(
            not isinstance(value, str) for value in scalar_values
        ):
            raise ClaudeProbeError(
                "Claude string schema uses a non-string value constraint"
            )
        if node_type == "boolean" and any(
            not isinstance(value, bool) for value in scalar_values
        ):
            raise ClaudeProbeError(
                "Claude boolean schema uses a non-boolean value constraint"
            )
        if node_type == "integer" and any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in scalar_values
        ):
            raise ClaudeProbeError(
                "Claude integer schema uses a non-integer value constraint"
            )
        if node_type == "object":
            if node.get("additionalProperties") is not False:
                raise ClaudeProbeError(
                    "Claude object schemas must deny additional properties"
                )
            properties = node.get("properties")
            required = node.get("required")
            if not isinstance(properties, dict) or not properties:
                raise ClaudeProbeError("Claude object schema properties are invalid")
            if (
                not isinstance(required, list)
                or len(required) != len(set(required))
                or not all(isinstance(name, str) for name in required)
            ):
                raise ClaudeProbeError(
                    "Claude object schema required fields are invalid"
                )
            if not set(required).issubset(properties):
                raise ClaudeProbeError(
                    "Claude object schema requires an unknown property"
                )
            if any(
                not isinstance(name, str) or not name or len(name) > 128
                for name in properties
            ):
                raise ClaudeProbeError("Claude object schema property name is invalid")
            for child in properties.values():
                visit(child, depth=depth + 1)
            if root and "definitions" in node:
                if (
                    not isinstance(definitions, dict)
                    or not definitions
                    or any(
                        not isinstance(name, str)
                        or not name
                        or not isinstance(child, dict)
                        for name, child in definitions.items()
                    )
                ):
                    raise ClaudeProbeError(
                        "Claude output schema definitions are invalid"
                    )
                for child in definitions.values():
                    visit(child, depth=depth + 1)
            elif "definitions" in node:
                raise ClaudeProbeError(
                    "Claude output schema definitions are allowed only at the root"
                )
            if any(
                name in node
                for name in (
                    "minLength",
                    "maxLength",
                    "pattern",
                    "items",
                    "minItems",
                    "maxItems",
                    "enum",
                    "const",
                    "minimum",
                    "maximum",
                )
            ):
                raise ClaudeProbeError("Claude object schema uses a scalar constraint")
        elif node_type == "array":
            if any(
                name in node
                for name in (
                    "properties",
                    "required",
                    "additionalProperties",
                    "minLength",
                    "maxLength",
                    "pattern",
                    "enum",
                    "const",
                    "minimum",
                    "maximum",
                )
            ):
                raise ClaudeProbeError("Claude array schema uses an incompatible constraint")
            minimum = _require_int(node.get("minItems", 0), "minItems")
            maximum = _require_int(node.get("maxItems", 256), "maxItems")
            if maximum > 256 or minimum > maximum:
                raise ClaudeProbeError("Claude array schema item bounds are invalid")
            items = node.get("items")
            if not isinstance(items, dict):
                raise ClaudeProbeError("Claude array schema items are invalid")
            visit(items, depth=depth + 1)
        else:
            if any(
                name in node
                for name in (
                    "properties",
                    "required",
                    "additionalProperties",
                    "items",
                    "minItems",
                    "maxItems",
                )
            ):
                raise ClaudeProbeError("Claude scalar schema uses an object constraint")
            if node_type == "string":
                if any(name in node for name in ("minimum", "maximum")):
                    raise ClaudeProbeError(
                        "Claude string schema uses an integer constraint"
                    )
                minimum = _require_int(node.get("minLength", 0), "minLength")
                maximum = _require_int(node.get("maxLength", 131072), "maxLength")
                if maximum > 131072 or minimum > maximum:
                    raise ClaudeProbeError(
                        "Claude string schema length bounds are invalid"
                    )
                if any(not minimum <= len(value) <= maximum for value in scalar_values):
                    raise ClaudeProbeError(
                        "Claude string schema value violates its length bounds"
                    )
                pattern = node.get("pattern")
                if pattern is not None:
                    if not isinstance(pattern, str) or len(pattern) > 256:
                        raise ClaudeProbeError("Claude string schema pattern is invalid")
                    try:
                        re.compile(pattern)
                    except re.error as error:
                        raise ClaudeProbeError(
                            "Claude string schema pattern is invalid"
                        ) from error
            elif node_type == "integer":
                minimum = _require_int(node.get("minimum", -(2**63)), "minimum")
                maximum = _require_int(node.get("maximum", 2**63 - 1), "maximum")
                if minimum > maximum:
                    raise ClaudeProbeError(
                        "Claude integer schema bounds are invalid"
                    )
            elif any(
                name in node
                for name in (
                    "minLength",
                    "maxLength",
                    "pattern",
                    "minimum",
                    "maximum",
                )
            ):
                raise ClaudeProbeError(
                    "Claude non-string schema uses a string constraint"
                )

    visit(schema, root=True)
    return schema, text


def _usage(terminal: dict[str, Any]) -> dict[str, int | None]:
    raw = terminal.get("usage") if isinstance(terminal.get("usage"), dict) else {}

    def count(*names: str) -> int | None:
        for name in names:
            value = raw.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    turns = terminal.get("num_turns")
    return {
        "inputTokens": count("input_tokens", "inputTokens"),
        "outputTokens": count("output_tokens", "outputTokens"),
        "cacheReadTokens": count("cache_read_input_tokens", "cacheReadTokens"),
        "cacheWriteTokens": count("cache_creation_input_tokens", "cacheWriteTokens"),
        "turns": max(1, turns)
        if isinstance(turns, int) and not isinstance(turns, bool) and turns >= 0
        else 1,
    }


class ClaudeStreamParser:
    """Online, bounded parser for Claude CLI stream-JSON output."""

    def __init__(
        self,
        *,
        expected_model: str,
        require_structured_output: bool,
        max_line_bytes: int = MAX_STREAM_LINE_BYTES,
        max_total_bytes: int = MAX_STREAM_BYTES,
    ) -> None:
        if not expected_model or max_line_bytes < 1 or max_total_bytes < max_line_bytes:
            raise ValueError("invalid Claude stream parser bounds")
        self.expected_model = expected_model
        self.require_structured_output = require_structured_output
        self.max_line_bytes = max_line_bytes
        self.max_total_bytes = max_total_bytes
        self.total_bytes = 0
        self.model: str | None = None
        self.terminal: dict[str, Any] | None = None
        self.api_error_status: int | None = None

    def feed(self, line: bytes) -> None:
        if not isinstance(line, bytes):
            raise ClaudeProtocolError("Claude stream frame must be bytes")
        self.total_bytes += len(line)
        if len(line) > self.max_line_bytes:
            raise ClaudeProtocolError("Claude stream frame exceeded its byte bound")
        if self.total_bytes > self.max_total_bytes:
            raise ClaudeProtocolError("Claude stream exceeded its total byte bound")
        if not line.endswith(b"\n"):
            raise ClaudeProtocolError("Claude stream ended with an unterminated frame")
        if self.terminal is not None:
            raise ClaudeProtocolError(
                "Claude stream emitted an event after its terminal result"
            )
        try:
            text = line.decode("utf-8", errors="strict")
            event = _strict_json(text)
        except UnicodeDecodeError as error:
            raise ClaudeProtocolError(
                "Claude stream contained invalid UTF-8"
            ) from error
        except (json.JSONDecodeError, ValueError, RecursionError) as error:
            raise ClaudeProtocolError(
                "Claude stream contained malformed JSON"
            ) from error
        if not isinstance(event, dict):
            raise ClaudeProtocolError("Claude stream event must be an object")
        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "init":
            if self.model is not None:
                raise ClaudeProtocolError(
                    "Claude stream emitted duplicate initialization"
                )
            model = event.get("model")
            if not isinstance(model, str) or not hmac.compare_digest(
                model, self.expected_model
            ):
                raise ClaudeProtocolError(
                    "Claude stream model did not match the immutable selection"
                )
            self.model = model
        elif event_type == "system" and event.get("subtype") == "api_retry":
            status = event.get("error_status")
            if isinstance(status, int) and not isinstance(status, bool):
                self.api_error_status = status
        elif event_type == "result":
            if self.model is None:
                raise ClaudeProtocolError(
                    "Claude stream terminated before initialization"
                )
            self.terminal = event

    def finish(self) -> ClaudeStreamOutcome:
        if self.model is None:
            raise ClaudeProtocolError("Claude stream omitted initialization")
        if self.terminal is None:
            raise ClaudeProtocolError("Claude stream omitted its terminal result")
        subtype = self.terminal.get("subtype")
        if subtype != "success":
            raise ClaudeProtocolError(
                "Claude stream terminal result was not successful"
            )
        if self.require_structured_output and "structured_output" not in self.terminal:
            raise ClaudeProtocolError(
                "Claude native schema result omitted structured output"
            )
        structured = self.terminal.get("structured_output")
        if self.require_structured_output and structured is None:
            raise ClaudeProtocolError(
                "Claude native schema result omitted structured output"
            )
        return ClaudeStreamOutcome(
            structured_output=structured,
            model=self.model,
            usage=_usage(self.terminal),
            terminal_subtype=subtype,
        )

    def terminal_failure(self) -> tuple[str, str] | None:
        """Classify a bounded unsuccessful Claude API status without its text."""

        if self.terminal is not None and self.terminal.get("is_error"):
            status = self.terminal.get("api_error_status")
        elif self.terminal is None:
            status = self.api_error_status
        else:
            return None
        if status is None:
            return None
        if status in (401, 403):
            return (
                "auth.invalid",
                "Claude rejected the anthropic-subscription credential.",
            )
        if status == 429:
            return ("provider.rate_limited", "Claude rate-limited the model request.")
        if status in (502, 503, 529):
            return (
                "provider.overloaded",
                "Claude was unavailable for the model request.",
            )
        return ("provider.unavailable", "Claude rejected the model request.")


def parse_stream(
    source: BinaryIO | Iterable[bytes],
    *,
    expected_model: str,
    require_structured_output: bool,
    max_line_bytes: int = MAX_STREAM_LINE_BYTES,
    max_total_bytes: int = MAX_STREAM_BYTES,
) -> ClaudeStreamOutcome:
    parser = ClaudeStreamParser(
        expected_model=expected_model,
        require_structured_output=require_structured_output,
        max_line_bytes=max_line_bytes,
        max_total_bytes=max_total_bytes,
    )
    for line in source:
        parser.feed(line)
    return parser.finish()


def _reject_ambient_credentials() -> None:
    forbidden = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "CLAUDE_CODE_MODEL",
        "CLAUDE_CODE_FALLBACK_MODEL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_ENABLE_TELEMETRY",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_API_KEY",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_TENANT_ID",
        "ARM_CLIENT_ID",
        "ARM_CLIENT_SECRET",
        "ARM_TENANT_ID",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "FLEET_TOKEN",
        "READONLY_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        AUTH_ENVIRONMENT,
    }
    if any(os.environ.get(name) for name in forbidden):
        raise ClaudeProbeError(
            "ambient provider, cloud, or GitHub credentials are forbidden"
        )


def _oauth_file() -> Path:
    raw = os.environ.get(CREDENTIAL_FILE_ENVIRONMENT, "").strip()
    if not raw:
        raise ClaudeProbeError(
            "anthropic-subscription credential handoff is unavailable"
        )
    path = Path(raw)
    try:
        info = path.lstat()
    except OSError as error:
        raise ClaudeProbeError(
            "anthropic-subscription credential handoff is missing"
        ) from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size < 16
        or info.st_size > 65536
        or info.st_mode & 0o077
    ):
        raise ClaudeProbeError("anthropic-subscription credential handoff is invalid")
    return path


class ClaudeCliAdapter(AgentAdapterV1):
    id = "claude-cli"
    adapter_version = "1.0.0"

    def probe(
        self, task: dict[str, Any], schema_bytes: bytes | None = None
    ) -> AdapterProbe:
        candidate = task["spec"]["selection"]["candidates"][0]
        if (
            candidate.get("authProfile") != "anthropic-subscription"
            or candidate.get("authMechanism") != "claude-code-oauth-token"
        ):
            raise ClaudeProbeError(
                "Claude adapter requires the anthropic-subscription OAuth binding"
            )
        if (
            candidate.get("provider") != "anthropic"
            or candidate.get("costClass") != "subscription"
        ):
            raise ClaudeProbeError(
                "Claude adapter refuses provider or billing substitution"
            )
        if candidate.get("expectedWorkspaceId") not in (None, ""):
            raise ClaudeProbeError(
                "Claude adapter refuses an alternate workspace binding"
            )
        if (
            candidate.get("model") != EXPECTED_MODEL
            or candidate.get("allowModelAlias") is not False
        ):
            raise ClaudeProbeError(
                "Claude adapter requires the immutable model selection"
            )
        selection = task["spec"]["selection"]
        if (
            len(selection.get("candidates") or []) != 1
            or (selection.get("fallback") or {}).get("mode") != "none"
        ):
            raise ClaudeProbeError("Claude adapter refuses model or provider fallback")
        _reject_ambient_credentials()
        credential = _oauth_file()
        if schema_bytes is None:
            raise ClaudeProbeError("Claude output schema probe is unavailable")
        _, schema_text = validate_schema_subset(
            schema_bytes, str(task["spec"]["output"]["schemaSha256"])
        )

        binary = shutil.which("claude")
        if not binary:
            raise ClaudeProbeError("pinned Claude CLI is unavailable")
        candidate_path = Path(binary)
        try:
            info = candidate_path.lstat()
        except OSError as error:
            raise ClaudeProbeError("pinned Claude CLI is unavailable") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ClaudeProbeError("pinned Claude CLI must be a regular file")
        resolved = str(candidate_path.resolve())
        lock = _load_lock()
        locked = lock["claude"]
        expected_version = str(locked["binaryVersion"])
        platform_key = _platform_key()
        artifact = locked["platforms"].get(platform_key)
        if not isinstance(artifact, dict):
            raise ClaudeProbeError(
                "Claude CLI platform is not pinned by the runtime lock"
            )
        digest = file_sha256(resolved, max_bytes=512 * 1024 * 1024)
        if not hmac.compare_digest(digest, str(artifact.get("sha256") or "")):
            raise ClaudeProbeError("Claude CLI digest does not match the runtime pin")
        version_env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": "/nonexistent",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
        }
        try:
            check = subprocess.run(
                [resolved, "--version"],
                capture_output=True,
                text=True,
                env=version_env,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ClaudeProbeError("Claude CLI version probe failed") from error
        version_text = (check.stdout + check.stderr).strip()
        if (
            check.returncode != 0
            or version_text != "%s (Claude Code)" % expected_version
        ):
            raise ClaudeProbeError("Claude CLI version does not match the runtime pin")
        fixture_digest = _protocol_fixture_digest(lock)
        descriptor = claude_descriptor(expected_version, digest, fixture_digest)
        return AdapterProbe(
            descriptor=AdapterDescriptor(descriptor),
            binary_path=resolved,
            auth_source=str(credential),
            supplemental={
                "binaryResolved": resolved,
                "binaryDigest": digest,
                "sourceCommit": locked["sourceCommit"],
                "platform": platform_key,
                "downloadUrl": artifact["url"],
                "protocolFixtureSha256": fixture_digest,
                "schemaText": schema_text,
                "schemaSha256": hashlib.sha256(schema_bytes).hexdigest(),
            },
        )

    def compile(
        self, task: dict[str, Any], proof: dict[str, Any], probe: AdapterProbe
    ) -> dict[str, Any]:
        candidate = task["spec"]["selection"]["candidates"][0]
        schema_text = probe.supplemental.get("schemaText")
        if not isinstance(schema_text, str):
            raise ClaudeProbeError("Claude output schema probe is unavailable")
        max_turns = task["spec"]["limits"].get("maxTurns")
        if (
            not isinstance(max_turns, int)
            or isinstance(max_turns, bool)
            or max_turns < 1
        ):
            raise ClaudeProbeError("Claude max-turns bound is invalid")
        argv = [
            "claude",
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "StructuredOutput",
            "--model",
            EXPECTED_MODEL,
            "--max-turns",
            str(max_turns),
            "--json-schema",
            schema_text,
        ]
        requested_tools = [row["name"] for row in task["spec"]["tools"]["tools"]]
        if not requested_tools:
            # Safe mode disables all MCP servers, including the exact explicit
            # configuration. Tool-free actions retain that extra defense.
            argv.insert(5, "--safe-mode")
        if requested_tools:
            mcp_names = {
                "fs.read": "fs_read",
                "fs.grep": "fs_grep",
                "fs.glob": "fs_glob",
                "public.search": "public_search",
                "public.fetch": "public_fetch",
                "public.git_snapshot": "public_git_snapshot",
                "public.artifact": "public_artifact",
                "exercise.run": "exercise_run",
            }
            if any(name not in mcp_names for name in requested_tools):
                raise ClaudeProbeError("Claude task requested an unsupported typed tool")
            argv.extend(
                [
                    "--mcp-config",
                    "/tmp/wheelhouse-mcp.json",
                    "--allowedTools",
                    ",".join(
                        ["StructuredOutput"]
                        + [
                            "mcp__wheelhouse__%s" % mcp_names[name]
                            for name in requested_tools
                        ]
                    ),
                ]
            )
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
            "claude": {
                "binaryVersion": probe.descriptor.value["harnessVersion"],
                "protocol": probe.descriptor.value["protocol"],
                "argv": argv,
                "stdinArtifact": "/run/wheelhouse/prompt.txt",
                "environment": {"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"},
                "secretBindings": [
                    {
                        "authRef": "anthropic-subscription",
                        "target": "env:CLAUDE_CODE_OAUTH_TOKEN",
                    }
                ],
                "allowProviderModelFallback": False,
                "structuredOutputMechanism": proof.get("structuredOutputMechanism"),
                "schemaSha256": probe.supplemental["schemaSha256"],
            },
        }

    def worker_command(self, plan_path: str, output_dir: str) -> list[str]:
        return [
            "python3",
            "-m",
            "agent_runtime.worker",
            "--plan",
            plan_path,
            "--output-dir",
            output_dir,
        ]

    def cancel_protocol(self) -> str:
        return "sigterm+process-group"


__all__ = [
    "AUTH_ENVIRONMENT",
    "CREDENTIAL_FILE_ENVIRONMENT",
    "ClaudeCliAdapter",
    "ClaudeProbeError",
    "ClaudeProtocolError",
    "ClaudeStreamOutcome",
    "ClaudeStreamParser",
    "parse_stream",
    "validate_schema_subset",
]
