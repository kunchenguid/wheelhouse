"""Trusted Agent Runtime Contract bridge for the pinned Claude Action."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import API_VERSION
from .contract import (
    ContractError,
    atomic_write_json,
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    load_json_regular,
    validate_contract,
    validate_schema,
)
from .events import EventWriter
from .supervisor import _anchor_ok, _error, _verify_artifacts

ACTION_COMMIT = "fad22eb3fa582b7357fc0ea48af6645851b884fd"
ACTION_VERSION = "1.0.161"
CLAUDE_CODE_VERSION = "2.1.197"
IMMUTABLE_MODEL = "claude-sonnet-4-6"
PROTOCOL = "claude-agent-sdk-json-v1"


def _bounded_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            text = item["text"].strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _transcript(path: str) -> list[dict[str, Any]]:
    candidate = Path(path)
    if not path or candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > 8 * 1024 * 1024:
        return []
    value = load_json_regular(candidate, max_bytes=8 * 1024 * 1024)
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _spend_started(path: str) -> bool:
    candidate = Path(path)
    try:
        return bool(path) and not candidate.is_symlink() and candidate.is_file() and candidate.stat().st_size > 0
    except OSError:
        return False


def _observed_model(rows: list[dict[str, Any]]) -> str:
    models = {
        row.get("model")
        for row in rows
        if row.get("type") == "system"
        and row.get("subtype") == "init"
        and isinstance(row.get("model"), str)
        and row.get("model")
    }
    return next(iter(models)) if len(models) == 1 else ""


def _result_text(rows: list[dict[str, Any]]) -> str:
    for row in reversed(rows):
        if row.get("type") == "result" and not row.get("is_error") and isinstance(row.get("result"), str) and row["result"].strip():
            return row["result"].strip()
    for row in reversed(rows):
        if row.get("type") == "assistant" and isinstance(row.get("message"), dict):
            text = _bounded_text(row["message"].get("content"))
            if text:
                return text
    return ""


def _delivered(action: str, rows: list[dict[str, Any]], delivered_file: str) -> Any:
    if delivered_file:
        return load_json_regular(delivered_file, max_bytes=131072)
    text = _result_text(rows)
    if not text:
        raise ContractError("Claude action delivered no final result")
    if action.startswith("deep-review"):
        return {"text": text}
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ContractError("Claude action result was not an object")
    return value


def _usage(rows: list[dict[str, Any]], duration_ms: int) -> dict[str, Any]:
    turns = 0
    token_usage: dict[str, Any] = {}
    tool_calls = 0
    for row in rows:
        if row.get("type") == "assistant" and isinstance(row.get("message"), dict):
            content = row["message"].get("content")
            if isinstance(content, list):
                tool_calls += sum(1 for item in content if isinstance(item, dict) and item.get("type") == "tool_use")
    for row in reversed(rows):
        value = row.get("num_turns")
        if row.get("type") == "result" and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            turns = value
        usage = row.get("usage")
        if row.get("type") == "result" and isinstance(usage, dict):
            token_usage = usage
        if turns or token_usage:
            break

    def token(name: str) -> int | None:
        value = token_usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    return {
        "inputTokens": token("input_tokens"),
        "outputTokens": token("output_tokens"),
        "cacheReadTokens": token("cache_read_input_tokens"),
        "cacheWriteTokens": token("cache_creation_input_tokens"),
        "providerRequests": None,
        "toolCalls": tool_calls,
        "turns": turns,
        "durationMs": max(0, duration_ms),
        "quota": {"available": False, "snapshotSha256": None, "observedAt": None, "primaryUsedPercent": None, "secondaryUsedPercent": None},
        "cost": {"amount": None, "currency": None, "quality": "unavailable"},
    }


def bridge(task_path: str, bundle_dir: str, execution_file: str, delivered_file: str, result_path: str, events_path: str) -> dict[str, Any]:
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    started = time.monotonic()
    task = load_json_regular(task_path, max_bytes=16 * 1024 * 1024)
    validate_contract(task, "AgentTask")
    bundle = Path(bundle_dir).resolve()
    _verify_artifacts(task, bundle)
    candidate = task["spec"]["selection"]["candidates"][0]
    if candidate["adapter"] != "claude-action-compat" or candidate["provider"] != "anthropic" or candidate["model"] != IMMUTABLE_MODEL or candidate["allowModelAlias"]:
        raise ContractError("Claude production selection violates the pinned adapter contract")
    transcript_error = False
    try:
        rows = _transcript(execution_file)
    except (ContractError, OSError, RecursionError, UnicodeError, ValueError):
        rows = []
        transcript_error = True
    actual_model = _observed_model(rows)
    spend_started = _spend_started(execution_file)
    error = None
    delivered = None
    final = None
    action_failed = any(row.get("type") == "result" and row.get("is_error") is True for row in rows)
    if transcript_error:
        error = _error("harness.protocol", "Claude action execution data failed bounded protocol validation.", spend_started=spend_started)
    elif not rows:
        error = _error("output.missing", "Claude action delivered no execution data.", spend_started=False)
    elif actual_model != candidate["model"]:
        error = _error("model.mismatch", "Observed Claude model did not match the immutable requested model.", spend_started=spend_started)
    elif action_failed:
        error = _error("harness.crash", "Claude action reported an unsuccessful execution.", spend_started=spend_started)
    else:
        try:
            value = _delivered(task["metadata"]["action"], rows, delivered_file)
            encoded = canonical_json_bytes(value)
            if len(encoded) > task["spec"]["limits"]["maxFinalBytes"]:
                raise ContractError("Claude action result exceeded its byte bound")
            delivered = {"value": value, "valueSha256": canonical_sha256(value), "bytes": len(encoded)}
            schema = load_json_regular(bundle / task["spec"]["output"]["schemaArtifact"], max_bytes=65536)
            validate_schema(value, schema)
            if not _anchor_ok(value, task, bundle):
                error = _error("output.evidence_invalid", "Delivered evidence did not anchor to the immutable target input.", spend_started=True)
            else:
                final = {
                    "schemaId": task["spec"]["output"]["schemaId"],
                    "value": value,
                    "valueSha256": delivered["valueSha256"],
                    "bytes": delivered["bytes"],
                    "validation": [
                        {"name": "json-schema", "status": "passed"},
                        {"name": task["spec"]["output"]["evidencePolicy"], "status": "passed" if task["spec"]["output"]["evidencePolicy"] != "none" else "not-applicable"},
                        {"name": "observed-provenance", "status": "passed"},
                    ],
                }
        except (ContractError, json.JSONDecodeError, OSError, RecursionError, UnicodeError, ValueError):
            raw = _result_text(rows)
            if raw:
                encoded = canonical_json_bytes(raw)
                if len(encoded) <= task["spec"]["limits"]["maxFinalBytes"]:
                    delivered = {"value": raw, "valueSha256": canonical_sha256(raw), "bytes": len(encoded)}
            error = _error("output.schema_invalid" if rows or delivered_file else "output.missing", "Claude action output failed trusted contract validation.", spend_started=spend_started)
    duration = int((time.monotonic() - started) * 1000)
    status = "succeeded" if final is not None else "failed"
    selection = {
        "candidateIndex": 0,
        "harness": candidate["harness"],
        "adapter": candidate["adapter"],
        "adapterVersion": "1.0.0",
        "adapterDigest": file_sha256(Path(__file__)),
        "harnessVersion": CLAUDE_CODE_VERSION,
        "harnessDigest": canonical_sha256({"actionCommit": ACTION_COMMIT, "actionVersion": ACTION_VERSION, "claudeCodeVersion": CLAUDE_CODE_VERSION}),
        "protocol": PROTOCOL,
        "protocolSchemaSha256": canonical_sha256({"protocol": PROTOCOL, "systemInitModel": True}),
        "provider": candidate["provider"],
        "actualProvider": candidate["provider"] if actual_model else "",
        "authProfile": candidate["authProfile"],
        "authMechanism": candidate["authMechanism"],
        "expectedWorkspaceIdSha256": canonical_sha256(candidate["expectedWorkspaceId"]) if candidate.get("expectedWorkspaceId") else None,
        "requestedModel": candidate["model"],
        "actualModel": actual_model,
        "requestedEffort": candidate["effort"],
        "actualEffort": candidate["effort"] if actual_model else "",
        "costClass": candidate["costClass"],
        "dataBoundary": candidate["dataBoundary"],
        "fallbackUsed": False,
        "fallbackReason": None,
    }
    result = {
        "apiVersion": API_VERSION,
        "kind": "AgentResult",
        "executionId": task["metadata"]["executionId"],
        "requestSha256": canonical_sha256(task),
        "status": status,
        "selection": selection,
        "proof": {
            "contractMajor": 1,
            "capabilitySnapshotSha256": canonical_sha256(task["spec"]["capabilities"]),
            "negotiationSha256": canonical_sha256({"candidate": candidate, "tools": task["spec"]["tools"], "fallback": "none"}),
            "policySha256": canonical_sha256({name: task["spec"][name] for name in ("isolation", "limits", "retention", "retry")}),
            "compiledPromptSha256": task["spec"]["prompt"]["segments"][0]["sha256"],
            "inputManifestSha256": canonical_sha256(task["spec"]["inputs"]),
            "outputSchemaSha256": task["spec"]["output"]["schemaSha256"],
            "sandboxPolicySha256": canonical_sha256(task["spec"]["isolation"]),
        },
        "usage": _usage(rows, duration),
        "artifacts": [],
        "startedAt": started_at,
        "completedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if delivered is not None:
        result["delivered"] = delivered
    if final is not None:
        result["final"] = final
    else:
        result["error"] = error or _error("output.missing", "Claude action delivered no result.", spend_started=spend_started)
    with EventWriter(events_path, result["executionId"], task["spec"]["limits"]["maxEventBytes"]) as events:
        events.emit("execution.accepted", {"requestSha256": result["requestSha256"]})
        events.emit("selection.resolved", {"candidateIndex": 0, "adapter": candidate["adapter"], "harness": candidate["harness"], "provider": candidate["provider"], "model": candidate["model"], "effort": candidate["effort"], "fallback": "none"})
        events.emit("capabilities.negotiated", {"proofSha256": result["proof"]["negotiationSha256"], "exactTools": [row["name"] for row in task["spec"]["tools"]["tools"]]})
        events.emit("validation.completed", {"status": "passed" if final else "failed", "errorCode": None if final else result["error"]["code"], "spendStarted": spend_started})
        events.emit("execution.completed", {"status": status, "resultSha256": canonical_sha256(result)})
    event_file = Path(events_path)
    result["artifacts"].append({"role": "normalized-events", "sha256": file_sha256(event_file), "mediaType": "application/x-ndjson", "bytes": event_file.stat().st_size, "retentionDays": task["spec"]["retention"]["normalizedEventsDays"], "redaction": "wheelhouse-agent/v1"})
    validate_contract(result, "AgentResult")
    atomic_write_json(result_path, result)
    return result
