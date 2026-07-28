"""Shared normalized-output parsing and evidence validation."""

from __future__ import annotations

import json
import re
from typing import Any

_EVIDENCE_SEGMENT_RE = re.compile(r"(?:\r?\n|\s+\|\s+)")
_EVIDENCE_ELLIPSIS_RE = re.compile(r"(?:\u2026|\.{3})")

# Captain-fixed evidence-quote byte policy: prompts ask for at most 1024 UTF-8
# bytes per quote, trusted validation accepts through this inclusive hard
# ceiling, and anything above it is invalid (correction-eligible). JSON Schema
# `maxLength` counts CHARACTERS, so the schemas keep a 2048-character bound as
# secondary defense only (a string over 2048 characters is necessarily over
# 2048 UTF-8 bytes, so the character bound can never reject a byte-valid
# quote); this explicit byte count is the primary rule.
EVIDENCE_QUOTE_MAX_UTF8_BYTES = 2048
# Matching span bound for anchor extraction so a byte-valid long quote can
# still anchor to the target text.
EVIDENCE_SPAN_MAX_LEN = EVIDENCE_QUOTE_MAX_UTF8_BYTES
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


def _quote_byte_violation(path: str, quote: Any, max_bytes: int) -> str | None:
    if not isinstance(quote, str):
        return None
    size = len(quote.encode("utf-8"))
    if size <= max_bytes:
        return None
    return "%s exceeds %d UTF-8 bytes (%d)" % (path, max_bytes, size)


def evidence_quote_utf8_byte_violations(
    value: Any, max_bytes: int = EVIDENCE_QUOTE_MAX_UTF8_BYTES
) -> list[str]:
    """Explicit UTF-8 byte policy for every evidence-quote surface.

    Counts bytes, not characters, because JSON Schema ``maxLength`` counts
    characters and multibyte quotes diverge. Returns purely structural
    violation strings (field path plus byte count) that are safe to persist
    and display - never quote content. Non-string or absent fields are the
    bound schema's job and yield no violation here.
    """

    violations: list[str] = []
    if not isinstance(value, dict):
        return violations

    def check(path: str, quote: Any) -> None:
        violation = _quote_byte_violation(path, quote, max_bytes)
        if violation:
            violations.append(violation)

    evidence = value.get("evidence")
    if isinstance(evidence, str):
        for index, segment in enumerate(_EVIDENCE_SEGMENT_RE.split(evidence)):
            check("$.evidence segment %d" % index, segment)
    elif isinstance(evidence, list):
        for index, item in enumerate(evidence):
            check("$.evidence[%d]" % index, item)

    vision = value.get("vision_evidence")
    if isinstance(vision, dict) and isinstance(vision.get("applicable_criteria"), list):
        for index, criterion in enumerate(vision["applicable_criteria"]):
            if isinstance(criterion, dict):
                check(
                    "$.vision_evidence.applicable_criteria[%d].quote" % index,
                    criterion.get("quote"),
                )

    automerge = value.get("automerge")
    if isinstance(automerge, dict):
        if isinstance(automerge.get("behavior_assertions"), list):
            for index, assertion in enumerate(automerge["behavior_assertions"]):
                if isinstance(assertion, dict) and isinstance(
                    assertion.get("evidence"), dict
                ):
                    check(
                        "$.automerge.behavior_assertions[%d].evidence.quote" % index,
                        assertion["evidence"].get("quote"),
                    )
        restoration = automerge.get("class_b_restoration")
        if isinstance(restoration, dict):
            for field in (
                "corrected_defect_evidence",
                "intended_behavior_restored_evidence",
            ):
                if isinstance(restoration.get(field), dict):
                    check(
                        "$.automerge.class_b_restoration.%s.quote" % field,
                        restoration[field].get("quote"),
                    )
    return violations


def collect_trusted_validation_errors(
    value: Any, schema: dict[str, Any], target_text: str
) -> list[str]:
    """Every trusted-validation error for a delivered triage candidate.

    The bound-schema validator reports its first failure, so after a whole-value
    failure each present top-level field is revalidated in isolation to surface
    independent defects (the card #1746 class carried two). Byte-policy and
    evidence-anchor results are appended so the one correction turn receives the
    complete trusted error list. Messages are structural (path plus defect),
    never candidate content.
    """

    from .contract import ContractError, validate_schema

    errors: list[str] = []
    try:
        validate_schema(value, schema)
    except ContractError as error:
        errors.append(str(error))
    if errors and isinstance(value, dict):
        for name in sorted(schema.get("properties") or {}):
            if name not in value:
                continue
            try:
                validate_schema(
                    value[name],
                    schema["properties"][name],
                    path="$.%s" % name,
                    root=schema,
                )
            except ContractError as error:
                if str(error) not in errors:
                    errors.append(str(error))
    for violation in evidence_quote_utf8_byte_violations(value):
        if violation not in errors:
            errors.append(violation)
    if (
        isinstance(value, dict)
        and target_text
        and not evidence_anchor_ok(value.get("evidence"), target_text)
    ):
        errors.append("$.evidence did not anchor to the immutable target input")
    return errors


def normalize_evidence_text(text: Any) -> str:
    value = re.sub(r"[`*]", "", str(text or ""))
    return re.sub(r"\s+", " ", value).strip().lower()


def _is_quote_opener(text: str, index: int) -> bool:
    before = text[index - 1] if index else ""
    return not before or before.isspace() or before in ":([{=,`"


def _is_escaped_quote_opener(text: str, index: int) -> bool:
    run_start = index
    while run_start and text[run_start - 1] == "\\":
        run_start -= 1
    if run_start == index:
        return False
    before = text[run_start - 1] if run_start else ""
    return not before or before.isspace() or before in ":([{=,`"


def _scan_quoted_evidence(
    text: str, max_span_len: int = EVIDENCE_SPAN_MAX_LEN
) -> tuple[list[str], bool]:
    """Extract quote-delimited spans without mistaking an escaped delimiter.

    Evidence is already decoded from JSON when it reaches this boundary. Models
    sometimes still use prose-style ``\\'`` or ``\\\"`` to represent the quote
    character inside a matching quote-delimited source span. Remove exactly the
    one escape slash that quotes the matching delimiter. Every other slash and
    every other Unicode character remains significant, so the decoded span must
    still occur verbatim after the existing whitespace/case/Markdown cleanup.
    """

    spans = []
    malformed = False
    index = 0
    while index < len(text):
        delimiter = text[index]
        if delimiter not in {"'", '"'}:
            index += 1
            continue
        if _is_escaped_quote_opener(text, index):
            malformed = True
            index += 1
            continue
        if not _is_quote_opener(text, index):
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
            malformed = True
            index += 1
            continue
        if cursor >= len(text) or text[cursor] != delimiter:
            malformed = True
            index += 1
    return spans, malformed


def _quoted_evidence_spans(
    text: str, max_span_len: int = EVIDENCE_SPAN_MAX_LEN
) -> list[str]:
    return _scan_quoted_evidence(text, max_span_len)[0]


def evidence_candidates(evidence: Any) -> tuple[list[str], list[str]]:
    """Return quoted spans and conservative unquoted evidence fragments."""

    text = flatten_evidence(evidence)
    if text is None:
        return [], []
    quoted = []
    fallback = []
    for segment in _EVIDENCE_SEGMENT_RE.split(text):
        segment_quotes, malformed = _scan_quoted_evidence(segment)
        quoted.extend(segment_quotes)
        if malformed:
            continue
        segment = _EVIDENCE_LIST_PREFIX_RE.sub("", segment, count=1)
        segment = _EVIDENCE_PATH_PREFIX_RE.sub("", segment, count=1)
        for fragment in _EVIDENCE_ELLIPSIS_RE.split(segment):
            fragment = fragment.strip()
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
