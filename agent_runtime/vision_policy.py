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
from typing import Any

from .contract import canonical_sha256, file_sha256, load_json_regular

PLAN_VERSION = "wheelhouse/evidence-plan/v1"
REVIEW_KIND = "AdvisoryReview"

_NORMATIVE = re.compile(
    r"(?i)\b(?:must|required|requires?|shall|only\s+after|cannot|may\s+receive"
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


def _operations(text: str) -> list[str]:
    lowered = text.casefold()
    operations: list[str] = []

    def add(name: str) -> None:
        if name not in operations:
            operations.append(name)

    if (
        any(word in lowered for word in ("source", "repository", "revision", "commit"))
        and any(word in lowered for word in ("inspect", "review", "verify", "fetch"))
    ):
        add("public.git_snapshot")
    if any(word in lowered for word in ("dataset", "manifest", "public url", "https")):
        add("public.fetch")
    if any(word in lowered for word in ("release", "artifact", "package", "distribution")):
        add("public.artifact")
    if any(word in lowered for word in ("checksum", "digest", "integrity", "hash")):
        add("digest.verify")
    if any(word in lowered for word in ("execute", "run ", "running", "verifier", "exercise")):
        add("exercise.run")
    if "validat" in lowered and "representative" in lowered:
        add("exercise.run")
    return operations


def _semantic_status(text: str, operations: list[str], normative: bool) -> str:
    """Conservatively prove that every normative sentence was understood.

    Known operation words in one part of a paragraph must not hide an unknown or
    alternative condition elsewhere in that paragraph. Unknown and ambiguous
    language remains structurally mapped, but its evidence can never become a
    complete positive observation.
    """
    if _AMBIGUOUS_CONDITION.search(text):
        return "ambiguous"
    sentences = [
        value.strip()
        for value in re.split(r"(?<=[.!?])\s+", text)
        if value.strip() and not value.lstrip().startswith("#")
    ]
    for sentence in sentences or [text]:
        sentence_normative = bool(_NORMATIVE.search(sentence))
        if (
            sentence_normative
            and not _operations(sentence)
            and not _LOCAL_POLICY_RULE.search(sentence)
        ):
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
        operations = _operations(unit["text"]) if not heading_only or normative else []
        semantic_status = _semantic_status(unit["text"], operations, normative)
        if not operations and (not heading_only or normative):
            operations = ["policy.assess"]
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
            obligations.append(
                {
                    "obligation_id": "O%03d" % (len(obligations) + 1),
                    "unit_id": unit["unit_id"],
                    "operation": operation,
                    "required": True,
                    "source_location": "VISION.md:L%d-L%d"
                    % (unit["start_line"], unit["end_line"]),
                    "requirement": unit["text"],
                    "semantic_status": semantic_status,
                }
            )
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
    receipt_dir: Path, execution_id: str, task_sha256: str
) -> dict[str, dict[str, Any]]:
    receipts: dict[str, dict[str, Any]] = {}
    if not receipt_dir.is_dir() or receipt_dir.is_symlink():
        return receipts
    for path in sorted(receipt_dir.glob("*.json")):
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
    receipt_dir: Path,
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
        receipt_dir,
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
