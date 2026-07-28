"""Bounded, privacy-safe retention helpers for Claude action diagnostics."""

from __future__ import annotations

import json
import re
from typing import Any

from .redaction import REDACTED, redact_text

# These bounds are deliberately small. Denial diagnostics are for identifying a
# rejected invocation, not for retaining a model transcript.
MAX_DENIED_INVOCATIONS = 8
MAX_DENIED_EVIDENCE_BYTES = 8192
MAX_TOOL_NAME_CHARS = 96
MAX_SHAPE_VALUE_CHARS = 240

# Only request-shape fields that identify a command or lookup are retained.
# Bodies, file contents, prompts, headers, credentials, and environment values
# are never copied even when a tool invocation is denied.
_ALLOWED_SHAPE_KEYS = frozenset(
    {
        "args",
        "cmd",
        "command",
        "file_path",
        "op",
        "path",
        "pattern",
        "query",
        "ref",
        "request",
        "url",
    }
)
_SECRET_ARGUMENT = re.compile(
    r"(?i)((?:--?(?:access[-_]?token|api[-_]?key|authorization|password|secret|token)|"
    r"(?:api[-_]?key|access[-_]?token|password|secret|token))\s*(?:=|:)\s*)[^\s,;]+"
)


def _bounded_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value[:MAX_SHAPE_VALUE_CHARS]
    value, _ = redact_text(value, max_chars=MAX_SHAPE_VALUE_CHARS)
    value = _SECRET_ARGUMENT.sub(lambda match: match.group(1) + REDACTED, value)
    value = re.sub(r"[\r\n\t]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()[:MAX_SHAPE_VALUE_CHARS]


def _shape_value(value: Any) -> Any:
    if isinstance(value, str):
        return _bounded_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        values = []
        for item in value[:8]:
            if isinstance(item, (str, int, float, bool)) or item is None:
                values.append(_shape_value(item))
        return values
    return "[omitted]"


def request_shape(value: Any) -> dict[str, Any]:
    """Keep only a compact, allowlisted command/request shape."""
    if not isinstance(value, dict):
        return {}
    shape = {}
    for key in sorted(_ALLOWED_SHAPE_KEYS):
        if key in value:
            shaped = _shape_value(value[key])
            if shaped != "":
                shape[key] = shaped
    return shape


def _denial_marker(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return bool(
        re.search(
            r"(?i)(permission\s+denied|not\s+(?:permitted|allowed)|"
            r"disallowed|blocked\s+by\s+(?:permission|policy)|requires?\s+permission)",
            value[:4096],
        )
    )


def _tool_use_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    uses = []
    for sequence, row in enumerate(rows, 1):
        if row.get("type") != "assistant" or not isinstance(row.get("message"), dict):
            continue
        content = row["message"].get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                uses.append(
                    {
                        "sequence": sequence,
                        "id": item.get("id") if isinstance(item.get("id"), str) else "",
                        "tool": (_bounded_text(item.get("name", ""))[:MAX_TOOL_NAME_CHARS] or "unknown"),
                        "request": request_shape(item.get("input")),
                    }
                )
    return uses


def _tool_result_rows(rows: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    denied_ids: set[str] = set()
    result_ids: set[str] = set()
    for row in rows:
        if row.get("type") != "user" or not isinstance(row.get("message"), dict):
            continue
        content = row["message"].get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            tool_id = item.get("tool_use_id")
            if not isinstance(tool_id, str):
                tool_id = ""
            if tool_id:
                result_ids.add(tool_id)
            raw = item.get("content")
            pieces = []
            if isinstance(raw, str):
                pieces.append(raw)
            elif isinstance(raw, dict) and isinstance(raw.get("text"), str):
                pieces.append(raw["text"])
            elif isinstance(raw, list):
                pieces.extend(
                    child.get("text", "")
                    for child in raw
                    if isinstance(child, dict) and isinstance(child.get("text"), str)
                )
            if item.get("is_error") is True and any(_denial_marker(piece) for piece in pieces):
                denied_ids.add(tool_id)
    return denied_ids, result_ids


def denied_tool_evidence(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract only denied invocation identity and request shape.

    The raw event stream is inspected before retention reduction. A positive
    permission-denial marker is required, or an explicit terminal denial count
    may conservatively select tool calls that never received a successful
    tool-result. With no denials, this returns None and successful calls gain no
    retained payload.
    """
    uses = _tool_use_rows(rows)
    denied_ids, result_ids = _tool_result_rows(rows)
    terminal = next((row for row in reversed(rows) if row.get("type") == "result"), {})
    count = terminal.get("permission_denials_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        count = len([tool_id for tool_id in denied_ids if tool_id])

    selected = []
    for use in uses:
        if use["id"] and use["id"] in denied_ids:
            selected.append(use)
    remaining_count = count - len(selected)
    unmatched = [
        use
        for use in uses
        if use not in selected and use["id"] and use["id"] not in result_ids
    ]
    unmatched_ids = [use["id"] for use in unmatched]
    if (
        remaining_count > 0
        and remaining_count == len(unmatched)
        and len(unmatched_ids) == len(set(unmatched_ids))
    ):
        selected.extend(unmatched)

    if count <= 0 and not selected:
        return None
    invocations = [
        {"sequence": use["sequence"], "tool": use["tool"], "request": use["request"]}
        for use in selected[:MAX_DENIED_INVOCATIONS]
    ]
    evidence = {
        "version": 1,
        "denialCount": min(max(count, len(invocations)), MAX_DENIED_INVOCATIONS),
        "reason": "permission_denied",
        "invocations": invocations,
    }
    # Deterministically drop later records if a future shape change approaches
    # the artifact bound. The first records preserve the earliest divergence.
    while len(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()) > MAX_DENIED_EVIDENCE_BYTES and invocations:
        invocations.pop()
        evidence["invocations"] = invocations
    return evidence


def retained_tool_denials(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Read the already-sanitized retention marker from a bounded transcript."""
    matches = [row.get("data") for row in rows if row.get("type") == "wheelhouse.tool_denials"]
    if len(matches) != 1 or not isinstance(matches[0], dict):
        return None
    value = matches[0]
    if value.get("version") != 1 or value.get("reason") != "permission_denied":
        return None
    if not isinstance(value.get("denialCount"), int) or not isinstance(value.get("invocations"), list):
        return None
    try:
        if len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()) > MAX_DENIED_EVIDENCE_BYTES:
            return None
    except (TypeError, ValueError):
        return None
    return value


def reduce_execution(rows: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
    """Apply the Claude cross-job reduction without retaining model payloads."""
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("execution data was not an event array")
    bounded = []
    for row in rows:
        if row.get("type") == "system" and row.get("subtype") == "init":
            bounded.append({"type": "system", "subtype": "init", "model": row.get("model")})
        elif row.get("type") == "assistant" and isinstance(row.get("message"), dict):
            content = row["message"].get("content")
            if isinstance(content, list):
                bounded.append({"type": "assistant", "message": {"content": [{"type": "tool_use"} for item in content if isinstance(item, dict) and item.get("type") == "tool_use"]}})
        elif row.get("type") == "result":
            kept = {key: row.get(key) for key in ("type", "subtype", "is_error", "result", "duration_ms", "num_turns")}
            if (
                isinstance(row.get("permission_denials_count"), int)
                and not isinstance(row.get("permission_denials_count"), bool)
                and row["permission_denials_count"] > 0
            ):
                kept["permission_denials_count"] = row["permission_denials_count"]
            if action.startswith("nl-decision.") and action != "nl-decision.schema-repair" and "structured_output" in row:
                kept["structured_output"] = row["structured_output"]
            usage = row.get("usage")
            if isinstance(usage, dict):
                kept["usage"] = {key: usage.get(key) for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
            bounded.append(kept)
        else:
            bounded.append({})
    evidence = denied_tool_evidence(rows)
    if evidence:
        terminal_index = next((index for index, row in enumerate(bounded) if row.get("type") == "result"), len(bounded))
        bounded.insert(terminal_index, {"type": "wheelhouse.tool_denials", "data": evidence})
    return bounded
