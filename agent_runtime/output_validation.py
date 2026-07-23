"""Shared normalized-output parsing and evidence validation."""

from __future__ import annotations

import json
import re
from typing import Any

_EVIDENCE_SEGMENT_RE = re.compile(r"(?:\r?\n|\s+\|\s+)")
_EVIDENCE_ELLIPSIS_RE = re.compile(r"(?:\u2026|\.{3})")
_EVIDENCE_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-+*]\s+|[0-9]+[.)]\s+)")
_EVIDENCE_PATH_PREFIX_RE = re.compile(
    r"^\s*(?:target\.txt|target-src/[^\s:]+)(?::[0-9]+(?:-[0-9]+)?)?:\s*",
    re.IGNORECASE,
)


def extract_json_object(text: Any) -> tuple[dict[str, Any] | None, str]:
    """Extract the one compact JSON object accepted by triage consumers."""

    if not isinstance(text, str):
        return None, "result text was not a string"
    candidate = text.strip()
    if not candidate:
        return None, "no result text was delivered"
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        value = json.loads(candidate)
    except (TypeError, ValueError):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            return None, "result contained no JSON object"
        try:
            value = json.loads(candidate[start : end + 1])
        except (TypeError, ValueError):
            return None, "result was not parseable as JSON"
    if not isinstance(value, dict):
        return None, "result JSON was not an object"
    return value, ""


def flatten_evidence(evidence: Any) -> str | None:
    """Return one non-empty evidence string for either accepted JSON shape."""

    if isinstance(evidence, str):
        return evidence.strip() or None
    if not isinstance(evidence, list) or not evidence:
        return None
    flattened = []
    for value in evidence:
        if not isinstance(value, str) or not value.strip():
            return None
        flattened.append(value.strip())
    return " | ".join(flattened)


def normalize_evidence_text(text: Any) -> str:
    value = re.sub(r"[`*]", "", str(text or ""))
    return re.sub(r"\s+", " ", value).strip().lower()


def _quoted_evidence_spans(text: str, max_span_len: int = 240) -> list[str]:
    """Extract quote-delimited spans without mistaking an escaped delimiter.

    Evidence is already decoded from JSON when it reaches this boundary. Models
    sometimes still use prose-style ``\\'`` or ``\\\"`` to represent the quote
    character inside a matching quote-delimited source span. Remove exactly the
    one escape slash that quotes the matching delimiter. Every other slash and
    every other Unicode character remains significant, so the decoded span must
    still occur verbatim after the existing whitespace/case/Markdown cleanup.
    """

    spans = []
    index = 0
    while index < len(text):
        delimiter = text[index]
        if delimiter not in {"'", '"'}:
            index += 1
            continue
        cursor = index + 1
        decoded = []
        while cursor < len(text):
            char = text[cursor]
            if char in "\r\n":
                break
            if char == "\\":
                run_end = cursor
                while run_end < len(text) and text[run_end] == "\\":
                    run_end += 1
                if run_end < len(text) and text[run_end] == delimiter:
                    slash_count = run_end - cursor
                    if slash_count % 2:
                        # Preserve every literal slash and remove only the final
                        # slash whose sole role is escaping this delimiter.
                        decoded.append("\\" * (slash_count - 1))
                        decoded.append(delimiter)
                        cursor = run_end + 1
                        continue
                    decoded.append("\\" * slash_count)
                    cursor = run_end
                    continue
                decoded.append("\\" * (run_end - cursor))
                cursor = run_end
                continue
            if char == delimiter:
                raw_length = cursor - index - 1
                if 1 <= raw_length <= max_span_len:
                    spans.append("".join(decoded))
                index = cursor + 1
                break
            decoded.append(char)
            cursor += 1
        else:
            index += 1
            continue
        if cursor >= len(text) or text[cursor] != delimiter:
            index += 1
    return spans


def evidence_candidates(evidence: Any) -> tuple[list[str], list[str]]:
    """Return quoted spans and conservative unquoted evidence fragments."""

    text = flatten_evidence(evidence)
    if text is None:
        return [], []
    quoted = _quoted_evidence_spans(text)
    fallback = []
    for segment in _EVIDENCE_SEGMENT_RE.split(text):
        segment = _EVIDENCE_LIST_PREFIX_RE.sub("", segment, count=1)
        segment = _EVIDENCE_PATH_PREFIX_RE.sub("", segment, count=1)
        for fragment in _EVIDENCE_ELLIPSIS_RE.split(segment):
            fragment = fragment.strip().strip("'\" ")
            if fragment:
                fallback.append(fragment)
    return quoted, fallback


def evidence_anchor_ok(
    evidence: Any,
    target_text: str,
    min_quote_len: int = 12,
    min_fallback_len: int = 20,
) -> bool:
    """Require one meaningful candidate span in the immutable target text."""

    quotes, fallback = evidence_candidates(evidence)
    haystack = normalize_evidence_text(target_text)
    if not haystack:
        return False
    for quote in quotes:
        needle = normalize_evidence_text(quote)
        if len(needle) >= min_quote_len and needle in haystack:
            return True
    for fragment in fallback:
        needle = normalize_evidence_text(fragment)
        if len(needle) >= min_fallback_len and needle in haystack:
            return True
    return False
