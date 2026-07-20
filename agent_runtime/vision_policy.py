"""Generic, target-owned VISION planning and advisory projection.

This module intentionally knows nothing about any repository-specific policy.
It turns every bounded VISION unit into a stable
record, derives generic evidence operations from normative language, and makes
positive projection fail closed when coverage or required evidence is missing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .contract import canonical_sha256, file_sha256, load_json_regular

PLAN_VERSION = "wheelhouse/evidence-plan/v1"
REVIEW_KIND = "AdvisoryReview"

_NORMATIVE = re.compile(
    r"(?i)\b(?:must|required|requires?|shall|should|ought\s+to|can|may|only\s+after|cannot"
    r"\s+(?:a\s+)?positive\b|insufficient\s+evidence|remain\s+inconclusive|"
    r"never|contingent\s+upon|prerequisite)\b"
)
_GENERIC_OPERATIONS = {
    "public.git_snapshot",
    "public.fetch",
    "public.artifact",
    "digest.verify",
    "exercise.run",
    "policy.assess",
}

_LOCAL_POLICY_RULE = re.compile(
    r"(?i)(?:\b(?:should|may|can)\b|\b(?:welcome|belong|faithful|similar|"
    r"common[- ]denominator|scope|principle|default|opt[- ]?in|owner|maintain|"
    r"clarity|meaning|behavior|interface|output|error|discoverab|ergonomic|"
    r"assertion|claim|screenshot|evidence)\w*\b|"
    r"\b(?:open|closed)\s+to\b)"
)
_AMBIGUOUS_CONDITION = re.compile(
    r"(?i)\b(?:unless|except\s+when|provided\s+that|either\b[^.]{0,160}\bor|"
    r"and\s*/\s*or)\b"
)
_DIGEST_LANGUAGE = re.compile(
    r"(?i)\b(?:checksum|digest|integrity|hash|sha-?256)\b"
)
_SHA256_VALUE = re.compile(
    r"(?i)\bsha-?256\s*[:=]?\s*([0-9a-f]{64})\b"
)
_VISION_TOKEN = re.compile(r"https?://\S+|[0-9a-fA-F]{64}|[A-Za-z]+(?:-[A-Za-z]+)?|[,;:.()]" )
_MODALS = {"must", "shall", "should", "may", "can", "cannot"}
_CONDITIONS = {"if", "when", "after", "before", "with", "without"}
_PREDICATES = {
    "attribute", "attributing", "audit", "auditing", "avoid", "complete",
    "completed", "establish", "established", "evolve", "execute", "executed", "executes",
    "execution", "exercise", "exercised", "exist", "exists", "fetch", "fetched",
    "fetches", "fetching", "identify", "inspect", "inspected", "inspection", "inspects",
    "maintain", "provide", "receive", "recommend", "remain", "representative",
    "request", "review", "satisfy", "verify", "verification", "verifies",
    "verifying", "validate", "validation", "distinguish",
}
_OPERAND_QUALIFIERS = {
    "across", "against", "as", "at", "by", "for", "from", "in", "including", "into",
    "of", "on", "rather", "such", "than", "through", "to", "under", "using",
}
_LIST_INTRODUCERS = {"across", "including", "representative", "through"}
_COORDINATED_OBJECT_PREDICATES = {"identify"}
_COORDINATING_PREPOSITIONS = {"against", "at", "by", "from", "of", "to", "under"}


class VisionPolicyError(ValueError):
    pass


def vision_units(text: str) -> list[dict[str, Any]]:
    """Split every non-empty heading/paragraph/list row into stable units."""
    if not isinstance(text, str) or not text.strip():
        raise VisionPolicyError("VISION is missing or empty")
    lines = text.splitlines()
    units: list[dict[str, Any]] = []
    pending: list[str] = []
    start = 0

    def flush(end_line: int) -> None:
        nonlocal pending, start
        value = "\n".join(pending).strip()
        if value:
            units.append(
                {
                    "unit_id": "P%03d" % (len(units) + 1),
                    "start_line": start,
                    "end_line": end_line,
                    "text": value,
                    "sha256": canonical_sha256(value),
                }
            )
        pending = []
        start = 0

    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        starts_unit = stripped.startswith("#") or bool(
            re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)", stripped)
        )
        if not stripped:
            flush(number - 1)
            continue
        if starts_unit:
            flush(number - 1)
            start = number
            pending = [stripped]
            flush(number)
            continue
        if not pending:
            start = number
        pending.append(stripped)
    flush(len(lines))
    if not units:
        raise VisionPolicyError("VISION contains no reviewable units")
    return units


def _expected_sha256(text: str) -> str | None:
    if not _DIGEST_LANGUAGE.search(text):
        return None
    matches = {match.group(1).casefold() for match in _SHA256_VALUE.finditer(text)}
    return next(iter(matches)) if len(matches) == 1 else None


def _operations(text: str) -> list[str]:
    lowered = text.casefold()
    operations: list[str] = []
    artifact_language = any(
        word in lowered for word in ("release", "artifact", "package", "distribution")
    )

    def add(name: str) -> None:
        if name not in operations:
            operations.append(name)

    if (
        any(word in lowered for word in ("source", "repository", "revision", "commit"))
        and any(word in lowered for word in ("inspect", "review", "verify", "fetch"))
    ):
        add("public.git_snapshot")
    if any(word in lowered for word in ("dataset", "manifest", "public url")) or (
        "https" in lowered and not artifact_language
    ):
        add("public.fetch")
    if artifact_language:
        add("public.artifact")
    if _DIGEST_LANGUAGE.search(text):
        add("public.artifact" if _expected_sha256(text) else "digest.verify")
    if any(word in lowered for word in ("execute", "run ", "running", "verifier", "exercise")):
        add("exercise.run")
    if "validat" in lowered and "representative" in lowered:
        add("exercise.run")
    return operations


def _normative_productions(text: str) -> list[str]:
    productions = []
    prose = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if _NORMATIVE.search(heading):
                productions.append(heading)
        else:
            prose.append(stripped)
    for sentence in re.split(r"(?<=[.!?])\s+", " ".join(prose)):
        normalized = re.sub(r"\s+", " ", sentence).strip(" .")
        if normalized and _NORMATIVE.search(normalized):
            productions.append(normalized)
    return productions


def _predicate_operand(tokens: list[str], index: int) -> list[str]:
    end = len(tokens)
    for offset in range(index + 1, len(tokens)):
        if tokens[offset] in {";", "."}:
            end = offset
            break
        if tokens[offset] in {"and", "or"}:
            cursor = offset + 1
            while cursor < len(tokens) and tokens[cursor] in {",", "("}:
                cursor += 1
            if (
                tokens[cursor : cursor + 1] == ["when"]
                or (cursor < len(tokens) and tokens[cursor] in _PREDICATES)
            ):
                end = offset
                break
    return tokens[index + 1 : end]


def _predicate_operations(
    predicate: str, tokens: list[str], index: int
) -> list[str] | None:
    operand_tokens = _predicate_operand(tokens, index)
    operand = " ".join(operand_tokens)
    if predicate in {"inspect", "inspected", "inspection", "inspects"}:
        return ["public.git_snapshot"]
    if predicate in {"fetch", "fetched", "fetches", "fetching"}:
        return ["public.fetch"]
    if predicate in {"execute", "executed", "executes", "execution", "exercise", "exercised"}:
        return ["public.artifact", "exercise.run"]
    if predicate == "review":
        if re.search(r"\b(?:package|artifact|release)\b", operand):
            return ["public.artifact"]
        if re.search(r"\b(?:source|repository|revision|commit)\b", operand):
            return ["public.git_snapshot"]
        return ["policy.assess"]
    if predicate in {"verify", "verification", "verifies", "verifying"}:
        if (
            any(re.fullmatch(r"[0-9a-f]{64}", token) for token in tokens)
            and re.search(r"\b(?:artifact|package|release)\b", operand)
        ):
            return ["public.artifact"]
        if _DIGEST_LANGUAGE.search(operand):
            return ["public.artifact"] if _expected_sha256(" ".join(tokens)) else ["digest.verify"]
        if re.search(r"\b(?:manifest|dataset|public\s+url)\b", operand):
            return ["public.fetch"]
        if re.search(r"\b(?:source|repository|revision|commit)\b", operand):
            return ["public.git_snapshot"]
        return ["policy.assess"]
    if predicate == "identify":
        operations = []
        if re.search(r"\b(?:source|repository|revision|commit)\b", operand):
            operations.append("public.git_snapshot")
        if re.search(r"\b(?:package|artifact|release)\b", operand):
            operations.append("public.artifact")
        if re.search(r"\b(?:execute|execution|exercise|exercised)\b", operand):
            operations.append("exercise.run")
        return operations or ["policy.assess"]
    if predicate == "receive":
        suffix = " ".join(tokens[index : index + 8])
        return ["policy.assess"] if re.match(
            r"receive\s+(?:a\s+)?positive\s+(?:[a-z-]+\s+){0,3}(?:review|verdict)",
            suffix,
        ) else None
    if predicate == "remain":
        return ["policy.assess"] if tokens[index + 1 : index + 2] == ["inconclusive"] else None
    if predicate in {"complete", "completed", "establish", "established"}:
        if re.search(r"\b(?:source|inspection|revision|commit)\b", operand):
            return ["public.git_snapshot"]
        if re.search(r"\b(?:execute|execution|package|artifact|release)\b", operand):
            return ["public.artifact", "exercise.run"]
        if re.search(r"\b(?:manifest|dataset|ordering|fetched)\b", operand):
            return ["public.fetch"]
        return None
    return ["policy.assess"]


def _leading_guard_end(tokens: list[str]) -> int:
    if tokens[:1] != ["if"]:
        return -1
    for index, token in enumerate(tokens):
        if token != ",":
            continue
        stop = next(
            (
                offset
                for offset in range(index + 1, len(tokens))
                if tokens[offset] in {",", ";", "."}
            ),
            len(tokens),
        )
        modal = next(
            (
                offset
                for offset in range(index + 1, stop)
                if tokens[offset] in _MODALS - {"cannot"}
            ),
            stop,
        )
        predicate = next(
            (
                offset
                for offset in range(modal + 1, stop)
                if tokens[offset] in _PREDICATES
            ),
            stop,
        )
        if modal < predicate:
            return index
    return -1


def _operand_list_connector(
    tokens: list[str], index: int, source: int
) -> bool:
    if source < 0:
        return False
    span_start = max(
        [
            offset + 1
            for offset in range(source + 1, index)
            if tokens[offset] in {";", "."}
        ],
        default=source + 1,
    )
    prefix = tokens[span_start:index]
    explicitly_listed = tokens[source] in (
        _LIST_INTRODUCERS | _COORDINATED_OBJECT_PREDICATES
    ) or any(
        token in _LIST_INTRODUCERS for token in prefix
    ) or any(
        prefix[offset : offset + 2] == ["such", "as"]
        for offset in range(len(prefix) - 1)
    )
    return explicitly_listed or any(
        token in _COORDINATING_PREPOSITIONS for token in prefix
    )


def _guard_or_is_negative(
    tokens: list[str], source: int, target: int, guard_end: int, selected: dict[int, str]
) -> bool:
    if selected.get(source) == selected.get(target) == "negative":
        return True
    nominal_branches = all(
        tokens[index].endswith(("ing", "ion")) for index in (source, target)
    )
    shared_negative = any(
        target < index < guard_end and polarity == "negative"
        for index, polarity in selected.items()
    )
    return nominal_branches and shared_negative


def _fail_closed_guard(tokens: list[str], guard_end: int, selected: dict[int, str]) -> bool:
    if guard_end < 0 or not any(
        index < guard_end and polarity == "negative"
        for index, polarity in selected.items()
    ):
        return False
    remain = next(
        (
            index
            for index in selected
            if index > guard_end
            and tokens[index] == "remain"
            and tokens[index + 1 : index + 2] == ["inconclusive"]
        ),
        -1,
    )
    request = next(
        (
            index
            for index in selected
            if index > remain and tokens[index] == "request"
        ),
        -1,
    )
    return remain >= 0 and request >= 0 and "evidence" in tokens[request + 1 :]


def _parse_production(production: str) -> tuple[dict[str, str], str]:
    if ";" in production:
        operation_statuses: dict[str, str] = {}
        for clause in production.split(";"):
            clause = clause.strip()
            if not clause or not _NORMATIVE.search(clause):
                return {
                    operation: "unknown" for operation in operation_statuses
                }, "unknown"
            resolved, status = _parse_production(clause)
            operation_statuses.update(resolved)
            if status in {"unknown", "ambiguous"}:
                return {
                    operation: status for operation in operation_statuses
                }, status
        return operation_statuses, (
            "recognized-local"
            if set(operation_statuses) == {"policy.assess"}
            else "recognized"
        )
    tokens = [match.group(0).casefold() for match in _VISION_TOKEN.finditer(production)]
    if not tokens or len(tokens) > 160:
        return {}, "unknown"
    ambiguous = bool(_AMBIGUOUS_CONDITION.search(production))
    selected: dict[int, str] = {}
    connector_edges: list[tuple[str, int, int]] = []
    policy_marker = bool(
        re.search(r"\b(?:is|are)\s+insufficient\s+evidence\b", production, re.I)
    )

    for index, token in enumerate(tokens):
        modal_end = index
        if token == "ought" and tokens[index + 1 : index + 2] == ["to"]:
            modal_end = index + 1
        elif token not in _MODALS:
            continue
        cursor = modal_end + 1
        polarity = "negative" if token == "cannot" else "positive"
        if tokens[cursor : cursor + 1] == ["not"]:
            polarity = "negative"
            cursor += 1
        if tokens[cursor : cursor + 1] == ["be"]:
            cursor += 1
        if cursor >= len(tokens) or tokens[cursor] not in _PREDICATES:
            return {}, "unknown"
        selected[cursor] = polarity

    for index, token in enumerate(tokens):
        if token in {"require", "requires"}:
            candidates = [
                offset
                for offset in range(index + 1, min(len(tokens), index + 7))
                if tokens[offset] in _PREDICATES
            ]
            if not candidates:
                return {}, "unknown"
            selected[candidates[0]] = "positive"
        elif token == "required":
            if tokens[max(0, index - 1) : index] == ["not"]:
                policy_marker = True
                candidates = [
                    offset
                    for offset in range(max(0, index - 10), index)
                    if tokens[offset] in _PREDICATES
                ]
                if candidates:
                    selected[candidates[-1]] = "negative"
                continue
            candidates = [
                offset
                for offset in range(max(0, index - 6), min(len(tokens), index + 7))
                if tokens[offset] in _PREDICATES and offset != index
            ]
            if not candidates:
                return {}, "unknown"
            selected[candidates[0]] = "positive"

    for index, token in enumerate(tokens):
        if token == "not" and tokens[index + 1 : index + 2]:
            candidate = index + 1
            if tokens[candidate] in _PREDICATES:
                selected[candidate] = "negative"

    for index, token in enumerate(tokens):
        if token not in _CONDITIONS | {"contingent", "prerequisite"}:
            continue
        candidates = [
            offset
            for offset in range(index + 1, min(len(tokens), index + 14))
            if tokens[offset] in _PREDICATES or tokens[offset] == "required"
        ]
        if not candidates:
            return {}, "unknown"
        if tokens[candidates[0]] in _PREDICATES:
            selected.setdefault(
                candidates[0], "negative" if token == "without" else "positive"
            )
        comma = next(
            (
                offset
                for offset in range(candidates[0] + 1, min(len(tokens), index + 18))
                if tokens[offset] == ","
            ),
            -1,
        )
        if comma >= 0:
            modal = next(
                (
                    offset
                    for offset in range(comma + 1, min(len(tokens), comma + 8))
                    if tokens[offset] in _MODALS
                ),
                -1,
            )
            consequences = [
                offset
                for offset in range(modal + 1, min(len(tokens), comma + 10))
                if tokens[offset] in _PREDICATES
            ] if modal >= 0 else []
            if consequences:
                selected.setdefault(consequences[0], "positive")

    for index, token in enumerate(tokens):
        if token not in {"and", "or", "then"}:
            continue
        source = max((offset for offset in selected if offset < index), default=-1)
        cursor = index + 1
        while cursor < len(tokens) and tokens[cursor] in {",", "("}:
            cursor += 1
        polarity = "positive"
        if tokens[cursor : cursor + 1] == ["when"]:
            condition_candidates = [
                offset
                for offset in range(cursor + 1, min(len(tokens), cursor + 7))
                if tokens[offset] in _PREDICATES
            ][:1]
            if condition_candidates:
                selected.setdefault(condition_candidates[0], "positive")
            comma = next(
                (
                    offset
                    for offset in range(cursor + 1, min(len(tokens), cursor + 10))
                    if tokens[offset] == ","
                ),
                -1,
            )
            candidates = [
                offset
                for offset in range(comma + 1, min(len(tokens), comma + 7))
                if tokens[offset] in _PREDICATES
            ][:1] if comma >= 0 else condition_candidates
        else:
            modal = -1 if cursor < len(tokens) and tokens[cursor] in _PREDICATES else next(
                (
                    offset
                    for offset in range(cursor, min(len(tokens), cursor + 7))
                    if tokens[offset] in _MODALS
                ),
                -1,
            )
            if modal >= 0:
                cursor = modal
            if cursor < len(tokens) and tokens[cursor] in _MODALS:
                polarity = "negative" if tokens[cursor] == "cannot" else "positive"
                cursor += 1
                if tokens[cursor : cursor + 1] == ["not"]:
                    polarity = "negative"
                    cursor += 1
                if tokens[cursor : cursor + 1] == ["be"]:
                    cursor += 1
            candidates = [
                cursor
            ] if cursor < len(tokens) and tokens[cursor] in _PREDICATES else []
        if candidates:
            selected.setdefault(candidates[0], polarity)
            connector_edges.append((token, source, candidates[0]))
        elif source >= 0 and (
            token == "then" or not _operand_list_connector(tokens, index, source)
        ):
            return {}, "unknown"

    for index in list(selected):
        if tokens[index] == "avoid" and tokens[index + 1 : index + 2]:
            candidate = index + 1
            if tokens[candidate] in _PREDICATES:
                selected[candidate] = "negative"

    semantic_start = min(
        (
            index
            for index, token in enumerate(tokens)
            if token in _MODALS | {"ought", "require", "requires", "required"}
        ),
        default=len(tokens),
    )
    for index, token in enumerate(tokens):
        if index < semantic_start:
            continue
        if token in _PREDICATES and index not in selected:
            if policy_marker and not selected:
                continue
            if any(
                tokens[offset] in _MODALS
                for offset in range(index + 1, min(len(tokens), index + 4))
            ):
                continue
            if token == "review" and any(
                tokens[offset] == "receive" and 0 < index - offset <= 5
                for offset in selected
            ):
                continue
            previous = tokens[index - 1] if index else ""
            if previous not in _OPERAND_QUALIFIERS and not token.endswith("ed"):
                return {}, "unknown"

    for index in selected:
        if tokens[index] == "review":
            operand = _predicate_operand(tokens, index)
            opaque = [
                token
                for token in operand
                if token not in _OPERAND_QUALIFIERS
                | _CONDITIONS
                | {"a", "an", "the", "that", "which", "whose", ",", "(", ")"}
            ]
            if len(opaque) > 2:
                return {}, "unknown"
        if tokens[index] != "inspect":
            continue
        operand = _predicate_operand(tokens, index)
        for anchor in ("source", "repository", "revision", "commit"):
            if anchor not in operand:
                continue
            anchor_index = index + 1 + operand.index(anchor)
            trailing = tokens[anchor_index + 1 :]
            if trailing and trailing[0] not in (
                _OPERAND_QUALIFIERS
                | _CONDITIONS
                | {"and", "or", ",", ";", ".", ")"}
            ):
                return {}, "unknown"
            break

    ambiguous = ambiguous or "never" in tokens or "unless" in tokens or "and/or" in tokens
    operation_statuses = {"policy.assess": "recognized-local"} if policy_marker else {}
    node_operations: dict[int, list[str]] = {}
    for index in sorted(selected):
        end = next(
            (
                offset
                for offset in range(index + 1, len(tokens))
                if tokens[offset] in {"and", "or", ";", "."}
            ),
            len(tokens),
        )
        if end - index > 48:
            return {}, "unknown"
        resolved = (
            ["policy.assess"]
            if selected[index] == "negative"
            else _predicate_operations(tokens[index], tokens, index)
        )
        if resolved is None:
            return {}, "unknown"
        node_operations[index] = resolved
        for operation in resolved:
            operation_statuses[operation] = (
                "recognized-local" if operation == "policy.assess" else "recognized"
            )

    guard_end = _leading_guard_end(tokens)
    fail_closed_guard = _fail_closed_guard(tokens, guard_end, selected)
    for connector, source, target in connector_edges:
        if connector != "or":
            continue
        in_guard = (
            fail_closed_guard
            and source >= 0
            and source < guard_end
            and target < guard_end
            and _guard_or_is_negative(
                tokens, source, target, guard_end, selected
            )
        )
        fail_closed_outcome = (
            fail_closed_guard
            and source > guard_end
            and tokens[source] == "remain"
            and tokens[source + 1 : source + 2] == ["inconclusive"]
            and tokens[target] == "request"
            and "evidence" in _predicate_operand(tokens, target)
        )
        if not in_guard and not fail_closed_outcome:
            ambiguous = True

    if not operation_statuses:
        return {}, "unknown"
    preserves_local_policy = policy_marker or any(
        polarity == "negative" for polarity in selected.values()
    )
    if (
        len(operation_statuses) > 1
        and "policy.assess" in operation_statuses
        and not preserves_local_policy
    ):
        operation_statuses.pop("policy.assess")
    if ambiguous:
        return {operation: "ambiguous" for operation in operation_statuses}, "ambiguous"
    if "digest.verify" in operation_statuses:
        return {operation: "unknown" for operation in operation_statuses}, "unknown"
    return operation_statuses, (
        "recognized-local"
        if set(operation_statuses) == {"policy.assess"}
        else "recognized"
    )


def _parse_normative(text: str) -> tuple[dict[str, str], str]:
    operation_statuses: dict[str, str] = {}
    productions = _normative_productions(text)
    if not productions:
        return {}, "unknown"
    for production in productions:
        resolved, status = _parse_production(production)
        if status in {"unknown", "ambiguous"}:
            return resolved, status
        operation_statuses.update(resolved)
    if "digest.verify" in operation_statuses:
        return operation_statuses, "unknown"
    return operation_statuses, (
        "recognized-local"
        if set(operation_statuses) == {"policy.assess"}
        else "recognized"
    )


def _semantic_status(text: str, operations: list[str], normative: bool) -> str:
    """Conservatively prove that every normative sentence was understood.

    Known operation words in one part of a paragraph must not hide an unknown or
    alternative condition elsewhere in that paragraph. Unknown and ambiguous
    language remains structurally mapped, but its evidence can never become a
    complete positive observation.
    """
    if normative:
        return _parse_normative(text)[1]
    if "digest.verify" in operations:
        return "unknown"
    if not operations:
        return "recognized-local" if not normative or _LOCAL_POLICY_RULE.search(text) else "unknown"
    return "recognized"


def _coverage_audit(
    units: list[dict[str, Any]],
    planned_units: list[dict[str, Any]],
    obligations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Independently re-audit clause coverage without target or fetched data."""
    unit_index = {row["unit_id"]: row for row in units}
    planned_index = {
        row.get("unit_id"): row for row in planned_units if isinstance(row, dict)
    }
    mapped: dict[str, list[dict[str, Any]]] = {}
    obligation_ids: set[str] = set()
    obligations_valid = True
    for row in obligations:
        if not isinstance(row, dict):
            obligations_valid = False
            continue
        unit_id = row.get("unit_id")
        obligation_id = row.get("obligation_id")
        source = unit_index.get(unit_id)
        if (
            source is None
            or not isinstance(obligation_id, str)
            or obligation_id in obligation_ids
            or row.get("required") is not True
            or row.get("operation") not in _GENERIC_OPERATIONS
            or row.get("requirement") != source["text"]
            or row.get("source_location")
            != "VISION.md:L%d-L%d"
            % (source["start_line"], source["end_line"])
            or (
                row.get("expected_sha256")
                != (
                    _expected_sha256(source["text"])
                    if row.get("operation") == "public.artifact"
                    else None
                )
            )
        ):
            obligations_valid = False
        if isinstance(obligation_id, str):
            obligation_ids.add(obligation_id)
        if isinstance(unit_id, str):
            mapped.setdefault(unit_id, []).append(row)

    required_unit_ids = []
    classification_valid = set(planned_index) == set(unit_index)
    for unit_id, source in unit_index.items():
        planned = planned_index.get(unit_id) or {}
        normative = bool(_NORMATIVE.search(source["text"]))
        heading_only = source["text"].lstrip().startswith("#")
        requires_check = not heading_only or normative
        if requires_check:
            required_unit_ids.append(unit_id)
        if (
            planned.get("text") != source["text"]
            or planned.get("start_line") != source["start_line"]
            or planned.get("end_line") != source["end_line"]
            or planned.get("sha256") != source["sha256"]
            or planned.get("normative") is not normative
            or planned.get("classification")
            != ("evidence-obligation" if requires_check else "context-only")
            or (requires_check and not mapped.get(unit_id))
            or (not requires_check and mapped.get(unit_id))
        ):
            classification_valid = False

    value = {
        "version": "wheelhouse/evidence-plan-coverage/v1",
        "auditor": "deterministic-independent-pass/v1",
        "unit_count": len(units),
        "classified_unit_count": len(planned_units),
        "required_unit_ids": required_unit_ids,
        "mapped_unit_ids": sorted(mapped),
        "obligation_count": len(obligations),
        "complete": bool(
            classification_valid
            and obligations_valid
            and set(required_unit_ids) == set(mapped)
        ),
    }
    value["sha256"] = canonical_sha256(value)
    return value


def derive_evidence_plan(text: str) -> dict[str, Any]:
    units = vision_units(text)
    planned_units = []
    obligations = []
    for unit in units:
        normative = bool(_NORMATIVE.search(unit["text"]))
        heading_only = unit["text"].lstrip().startswith("#")
        if normative:
            operation_statuses, semantic_status = _parse_normative(unit["text"])
            operations = list(operation_statuses)
        else:
            operations = _operations(unit["text"]) if not heading_only else []
            semantic_status = _semantic_status(unit["text"], operations, normative)
            operation_statuses = {
                operation: semantic_status for operation in operations
            }
        if not operations and (not heading_only or normative):
            operations = ["policy.assess"]
            operation_statuses = {"policy.assess": semantic_status}
        classification = (
            "evidence-obligation"
            if operations
            else "decision-criterion"
            if normative
            else "context-only"
        )
        planned_units.append(
            {
                **unit,
                "classification": classification,
                "normative": normative,
                "semantic_status": semantic_status,
            }
        )
        for operation in operations:
            obligation = {
                "obligation_id": "O%03d" % (len(obligations) + 1),
                "unit_id": unit["unit_id"],
                "operation": operation,
                "required": True,
                "source_location": "VISION.md:L%d-L%d"
                % (unit["start_line"], unit["end_line"]),
                "requirement": unit["text"],
                "semantic_status": operation_statuses[operation],
            }
            expected_sha256 = (
                _expected_sha256(unit["text"])
                if operation == "public.artifact"
                else None
            )
            if expected_sha256:
                obligation["expected_sha256"] = expected_sha256
            obligations.append(obligation)
    if not obligations:
        raise VisionPolicyError("VISION contains no policy clauses")
    coverage_audit = _coverage_audit(units, planned_units, obligations)
    value = {
        "version": PLAN_VERSION,
        "vision_sha256": canonical_sha256(text),
        "units": planned_units,
        "obligations": obligations,
        "coverage_audit": coverage_audit,
        "coverage_complete": coverage_audit["complete"],
    }
    value["plan_sha256"] = canonical_sha256(value)
    return value


def write_evidence_plan(vision_path: Path, destination: Path) -> dict[str, Any]:
    try:
        text = vision_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise VisionPolicyError("VISION is unreadable") from error
    plan = derive_evidence_plan(text)
    destination.write_text(
        json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return plan


def _receipt_index(
    receipt_dirs: Iterable[Path], execution_id: str, task_sha256: str
) -> dict[str, dict[str, Any]]:
    receipts: dict[str, dict[str, Any]] = {}
    paths = []
    for receipt_dir in receipt_dirs:
        if receipt_dir.is_dir() and not receipt_dir.is_symlink():
            paths.extend(receipt_dir.glob("*.json"))
    for path in sorted(paths):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
            continue
        try:
            row = load_json_regular(path, max_bytes=262144)
        except Exception:
            continue
        evidence_id = row.get("evidence_id") if isinstance(row, dict) else None
        unsigned = dict(row) if isinstance(row, dict) else {}
        receipt_sha256 = unsigned.pop("receipt_sha256", None)
        identity = dict(unsigned)
        identity.pop("evidence_id", None)
        if (
            isinstance(evidence_id, str)
            and path.name == evidence_id + ".json"
            and row.get("execution_id") == execution_id
            and row.get("task_sha256") == task_sha256
            and receipt_sha256 == canonical_sha256(unsigned)
            and evidence_id == canonical_sha256({"receipt": identity})
        ):
            receipts[evidence_id] = row
    return receipts


def project_advisory_review(
    value: dict[str, Any],
    *,
    task: dict[str, Any],
    bundle: Path,
    receipt_dir: Path | tuple[Path, ...],
    task_sha256: str | None = None,
) -> dict[str, Any]:
    """Return trusted advisory projection or raise on structural substitution."""
    if not isinstance(value, dict) or value.get("result_kind") != REVIEW_KIND:
        raise VisionPolicyError("result is not an AdvisoryReview")
    vision_input = next(
        (item for item in task["spec"]["inputs"] if item.get("id") == "vision"),
        None,
    )
    if not vision_input:
        raise VisionPolicyError("trusted VISION input is unavailable")
    vision_path = bundle / vision_input["artifact"]
    if file_sha256(vision_path) != vision_input["sha256"]:
        raise VisionPolicyError("trusted VISION input changed")
    try:
        plan = derive_evidence_plan(vision_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise VisionPolicyError("trusted VISION input is unreadable") from error
    if value.get("plan_sha256") != plan["plan_sha256"]:
        raise VisionPolicyError("review does not bind to the trusted evidence plan")

    expected = {row["obligation_id"]: row for row in plan["obligations"]}
    rows = value.get("obligation_results")
    if not isinstance(rows, list):
        raise VisionPolicyError("advisory obligation results are unavailable")
    observed: dict[str, dict[str, Any]] = {}
    for row in rows:
        obligation_id = row.get("obligation_id") if isinstance(row, dict) else None
        if not isinstance(obligation_id, str) or obligation_id in observed:
            raise VisionPolicyError("advisory obligation identity is invalid")
        observed[obligation_id] = row
    if set(observed) != set(expected):
        raise VisionPolicyError("advisory review omitted or invented a VISION obligation")

    receipts = _receipt_index(
        (receipt_dir,) if isinstance(receipt_dir, Path) else receipt_dir,
        task["metadata"]["executionId"],
        task_sha256 or canonical_sha256(task),
    )
    bound_local_evidence = any(
        item.get("id") in {"target", "repository"}
        and isinstance(item.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        for item in task["spec"]["inputs"]
        if isinstance(item, dict)
    )
    citation_rows = value.get("citations")
    if not isinstance(citation_rows, list):
        raise VisionPolicyError("advisory citations are unavailable")
    cited_ids = {
        row.get("evidence_id")
        for row in citation_rows
        if isinstance(row, dict) and isinstance(row.get("evidence_id"), str)
    }
    projected_results = []
    all_required_pass = plan["coverage_complete"]
    all_evidence_complete = plan["coverage_complete"]
    for obligation_id, obligation in expected.items():
        row = observed[obligation_id]
        assessment = row.get("assessment")
        row_citations = row.get("citation_ids")
        citation_ids = row_citations if isinstance(row_citations, list) else []
        expected_sha256 = obligation.get("expected_sha256")
        receipts_complete = (
            bound_local_evidence
            and obligation.get("semantic_status") == "recognized-local"
            and not citation_ids
            if obligation["operation"] == "policy.assess"
            else obligation.get("semantic_status") == "recognized"
            and bool(citation_ids)
            and all(
                evidence_id in cited_ids
                and evidence_id in receipts
                and receipts[evidence_id].get("operation")
                == obligation["operation"]
                and receipts[evidence_id].get("status") == "complete"
                and receipts[evidence_id].get("truncated") is False
                and (
                    expected_sha256 is None
                    or (
                        receipts[evidence_id].get("artifact_sha256")
                        == expected_sha256
                        and receipts[evidence_id].get("sha256")
                        == expected_sha256
                        and receipts[evidence_id].get("staged") is True
                    )
                )
                for evidence_id in citation_ids
            )
        )
        if assessment == "pass" and receipts_complete:
            trusted_status = "complete-pass"
        elif assessment == "fail" and receipts_complete:
            trusted_status = "complete-fail"
        elif assessment == "not_applicable":
            trusted_status = "not-applicable"
        else:
            trusted_status = "unavailable"
        if obligation["required"] and trusted_status != "complete-pass":
            all_required_pass = False
        if obligation["required"] and trusted_status not in {
            "complete-pass",
            "complete-fail",
        }:
            all_evidence_complete = False
        projected_results.append({**row, "trusted_status": trusted_status})

    verdict = value.get("verdict")
    limitations = list(value.get("limitations") or [])
    requested = list(value.get("requested_evidence") or [])
    if verdict == "positive" and not all_required_pass:
        verdict = "inconclusive"
        limitations.append(
            "Trusted projection blocked a positive verdict because required VISION evidence was unavailable, incomplete, or failing."
        )
    eligibility = value.get("eligibility_facts")
    eligibility_complete = bool(
        isinstance(eligibility, dict)
        and eligibility.get("behavior_class") in {"A", "B", "C"}
        and eligibility.get("changes_existing_or_default_behavior") is False
        and eligibility.get("aligns_with_vision") is True
        and eligibility.get("recommendation") == "eligible"
        and (
            eligibility.get("behavior_class") != "C"
            or eligibility.get("optin_default_off") is True
        )
    )
    auto_merge_eligible = bool(
        verdict == "positive" and all_required_pass and eligibility_complete
    )
    return {
        **value,
        "verdict": verdict,
        "obligation_results": projected_results,
        "limitations": limitations,
        "requested_evidence": requested,
        "policy_coverage_complete": plan["coverage_complete"],
        "public_evidence_influenced": True,
        "acting_authority": False,
        "trusted_projection": True,
        "projection_complete": all_evidence_complete,
        "auto_merge_eligible": auto_merge_eligible,
    }


__all__ = [
    "PLAN_VERSION",
    "REVIEW_KIND",
    "VisionPolicyError",
    "derive_evidence_plan",
    "project_advisory_review",
    "vision_units",
    "write_evidence_plan",
]
