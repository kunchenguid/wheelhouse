"""Trusted VISION unit binding and model-derived advisory projection."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .contract import canonical_sha256, file_sha256, load_json_regular

PLAN_VERSION = "wheelhouse/evidence-plan/v3"
AUDIT_VERSION = "wheelhouse/evidence-plan-audit/v2"
UNITS_VERSION = "wheelhouse/vision-units/v1"
REVIEW_KIND = "AdvisoryReview"
_OPERATIONS = {
    "public.git_snapshot",
    "public.fetch",
    "public.artifact",
    "digest.verify",
    "exercise.run",
    "policy.assess",
}
_CLASSIFICATIONS = {"context-only", "decision-criterion", "evidence-obligation"}
_SEMANTIC_STATUSES = {"recognized", "recognized-local", "unknown", "ambiguous"}
_CONDITION_STRENGTHS = {"none", "advisory", "required", "prohibited", "conditional"}


class VisionPolicyError(ValueError):
    pass


def vision_units(text: str) -> list[dict[str, Any]]:
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
        elif starts_unit:
            flush(number - 1)
            start = number
            pending = [stripped]
            flush(number)
        else:
            if not pending:
                start = number
            pending.append(stripped)
    flush(len(lines))
    if not units:
        raise VisionPolicyError("VISION contains no reviewable units")
    return units


def vision_unit_document(
    text: str,
    *,
    target_head_sha: str = "",
    target_base_sha: str = "",
    vision_blob_sha: str = "",
) -> dict[str, Any]:
    units = vision_units(text)
    value = {
        "version": UNITS_VERSION,
        "vision_sha256": canonical_sha256(text),
        "units": units,
    }
    if target_head_sha or target_base_sha or vision_blob_sha:
        value["target_head_sha"] = target_head_sha
        value["target_base_sha"] = target_base_sha
        value["vision_blob_sha"] = vision_blob_sha
    value["document_sha256"] = canonical_sha256(value)
    return value


def write_vision_units(
    vision_path: Path,
    destination: Path,
    *,
    target_head_sha: str = "",
    target_base_sha: str = "",
    vision_blob_sha: str = "",
) -> dict[str, Any]:
    try:
        text = vision_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise VisionPolicyError("VISION is unreadable") from error
    value = vision_unit_document(
        text,
        target_head_sha=target_head_sha,
        target_base_sha=target_base_sha,
        vision_blob_sha=vision_blob_sha,
    )
    destination.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return value


def _plan_digest(plan: dict[str, Any]) -> str:
    unsigned = dict(plan)
    unsigned.pop("plan_sha256", None)
    return canonical_sha256(unsigned)


def _semantic_fields(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("classification"),
        row.get("semantic_status"),
        row.get("normative"),
        row.get("decision_relevant"),
        row.get("condition_strength"),
        tuple(row.get("conditions") or ()),
    )


def obligation_semantic_status(unit_status: Any, operation: Any) -> Any:
    if unit_status in {"unknown", "ambiguous"}:
        return unit_status
    return "recognized-local" if operation == "policy.assess" else "recognized"


def validate_policy_artifacts(
    text: str,
    plan: Any,
    audit: Any,
    *,
    target_head_sha: str = "",
    target_base_sha: str = "",
    vision_blob_sha: str = "",
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    if not isinstance(plan, dict) or plan.get("version") != PLAN_VERSION:
        raise VisionPolicyError("policy derivation version is invalid")
    document = vision_unit_document(text)
    expected_binding = {
        "target_head_sha": target_head_sha,
        "target_base_sha": target_base_sha,
        "vision_blob_sha": vision_blob_sha,
    }
    if any(expected_binding.values()) and (
        not all(expected_binding.values())
        or any(plan.get(name) != value for name, value in expected_binding.items())
    ):
        raise VisionPolicyError("policy derivation revision binding is invalid")
    if plan.get("vision_sha256") != document["vision_sha256"]:
        raise VisionPolicyError("policy derivation is stale")
    if "plan_sha256" in plan:
        raise VisionPolicyError("policy derivation contains an untrusted digest")
    planned_units = plan.get("units")
    if not isinstance(planned_units, list) or len(planned_units) != len(document["units"]):
        raise VisionPolicyError("policy derivation omitted a VISION unit")
    unit_status: dict[str, dict[str, Any]] = {}
    for trusted, proposed in zip(document["units"], planned_units):
        if not isinstance(proposed, dict) or any(
            proposed.get(name) != trusted[name]
            for name in ("unit_id", "start_line", "end_line", "text", "sha256")
        ):
            raise VisionPolicyError("policy derivation changed a VISION unit")
        classification = proposed.get("classification")
        semantic_status = proposed.get("semantic_status")
        conditions = proposed.get("conditions")
        if (
            classification not in _CLASSIFICATIONS
            or semantic_status not in _SEMANTIC_STATUSES
            or not isinstance(proposed.get("normative"), bool)
            or not isinstance(proposed.get("decision_relevant"), bool)
            or proposed.get("condition_strength") not in _CONDITION_STRENGTHS
            or not isinstance(conditions, list)
            or any(not isinstance(row, str) or not row for row in conditions)
        ):
            raise VisionPolicyError("policy derivation classification is invalid")
        if classification == "context-only" and (
            proposed["normative"]
            or proposed["decision_relevant"]
            or proposed["condition_strength"] != "none"
            or conditions
        ):
            raise VisionPolicyError("policy derivation context classification is inconsistent")
        if classification != "context-only" and not (
            proposed["normative"] or proposed["decision_relevant"]
        ):
            raise VisionPolicyError("policy derivation omitted normative significance")
        unit_status[trusted["unit_id"]] = proposed
    obligations = plan.get("obligations")
    if not isinstance(obligations, list) or len(obligations) > 128:
        raise VisionPolicyError("policy derivation obligations are invalid")
    obligation_ids: set[str] = set()
    obligated_units: set[str] = set()
    for row in obligations:
        if not isinstance(row, dict):
            raise VisionPolicyError("policy derivation obligation is invalid")
        obligation_id = row.get("obligation_id")
        unit_id = row.get("unit_id")
        operation = row.get("operation")
        semantic_status = row.get("semantic_status")
        if (
            not isinstance(obligation_id, str)
            or obligation_id != "O%03d" % (len(obligation_ids) + 1)
            or obligation_id in obligation_ids
            or unit_id not in unit_status
            or operation not in _OPERATIONS
            or semantic_status not in _SEMANTIC_STATUSES
            or semantic_status
            != obligation_semantic_status(
                unit_status[unit_id]["semantic_status"], operation
            )
            or not isinstance(row.get("requirement"), str)
            or row.get("requirement") != next(
                unit["text"] for unit in document["units"] if unit["unit_id"] == unit_id
            )
        ):
            raise VisionPolicyError("policy derivation obligation binding is invalid")
        obligation_ids.add(obligation_id)
        obligated_units.add(unit_id)
        expected_sha256 = row.get("expected_sha256")
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise VisionPolicyError("policy derivation digest condition is invalid")
    if any(
        row["classification"] != "context-only" and unit_id not in obligated_units
        for unit_id, row in unit_status.items()
    ):
        raise VisionPolicyError("policy derivation omitted a gating obligation")
    if document["units"] and not obligated_units:
        raise VisionPolicyError("policy derivation classified all VISION units as context")

    if not isinstance(audit, dict) or audit.get("version") != AUDIT_VERSION:
        raise VisionPolicyError("coverage audit version is invalid")
    if any(expected_binding.values()) and any(
        audit.get(name) != value for name, value in expected_binding.items()
    ):
        raise VisionPolicyError("coverage audit revision binding is invalid")
    if (
        audit.get("vision_sha256") != document["vision_sha256"]
    ):
        raise VisionPolicyError("coverage audit binding is invalid")
    audit_units = audit.get("units")
    if not isinstance(audit_units, list) or len(audit_units) != len(planned_units):
        raise VisionPolicyError("coverage audit omitted a VISION unit")
    audit_status: dict[str, dict[str, Any]] = {}
    audit_complete = True
    for trusted, proposed, checked in zip(document["units"], planned_units, audit_units):
        if not isinstance(checked, dict) or any(
            checked.get(name) != trusted[name]
            for name in ("unit_id", "start_line", "end_line", "text", "sha256")
        ):
            raise VisionPolicyError("coverage auditor disagreed with unit identity")
        if (
            checked.get("classification") not in _CLASSIFICATIONS
            or checked.get("semantic_status") not in _SEMANTIC_STATUSES
            or not isinstance(checked.get("normative"), bool)
            or not isinstance(checked.get("decision_relevant"), bool)
            or checked.get("condition_strength") not in _CONDITION_STRENGTHS
            or not isinstance(checked.get("conditions"), list)
            or any(not isinstance(row, str) or not row for row in checked["conditions"])
        ):
            raise VisionPolicyError("coverage auditor classification is invalid")
        audit_status[trusted["unit_id"]] = checked
        audit_complete = audit_complete and _semantic_fields(checked) == _semantic_fields(proposed)
        audit_complete = audit_complete and checked["semantic_status"] in {"recognized", "recognized-local"}
    audit_obligations = audit.get("obligations")
    if not isinstance(audit_obligations, list) or len(audit_obligations) != len(obligations):
        raise VisionPolicyError("coverage audit omitted an obligation")
    for proposed, checked in zip(obligations, audit_obligations):
        if not isinstance(checked, dict) or any(
            checked.get(name) != proposed.get(name)
            for name in ("obligation_id", "unit_id", "operation", "requirement", "semantic_status", "expected_sha256")
        ):
            raise VisionPolicyError("coverage auditor disagreed with an obligation")
        audit_complete = audit_complete and proposed["semantic_status"] in {"recognized", "recognized-local"}
    disagreements = audit.get("disagreements")
    if not isinstance(disagreements, list) or any(not isinstance(row, str) for row in disagreements):
        raise VisionPolicyError("coverage audit disagreements are invalid")
    audit_complete = (
        audit_complete
        and not disagreements
        and all(
            row["semantic_status"] in {"recognized", "recognized-local"}
            for row in obligations
        )
    )
    if audit.get("complete") is not audit_complete:
        raise VisionPolicyError("coverage audit verdict is inconsistent")
    trusted_plan = {**plan, "coverage_complete": audit_complete}
    trusted_plan["plan_sha256"] = _plan_digest(trusted_plan)
    trusted_audit = dict(audit)
    trusted_audit["audit_sha256"] = canonical_sha256(audit)
    return trusted_plan, trusted_audit, audit_complete


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
    value: dict[str, Any], *, task: dict[str, Any], bundle: Path,
    receipt_dir: Path | tuple[Path, ...], task_sha256: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("result_kind") != REVIEW_KIND:
        raise VisionPolicyError("result is not an AdvisoryReview")
    vision_input = next(
        (item for item in task["spec"]["inputs"] if item.get("id") == "vision"), None
    )
    if not vision_input:
        raise VisionPolicyError("trusted VISION input is unavailable")
    binding_input = next(
        (item for item in task["spec"]["inputs"] if item.get("id") == "policy-binding"), None
    )
    if not binding_input:
        raise VisionPolicyError("trusted policy revision binding is unavailable")
    binding_path = bundle / binding_input["artifact"]
    if file_sha256(binding_path) != binding_input["sha256"]:
        raise VisionPolicyError("trusted policy revision binding changed")
    binding = load_json_regular(binding_path, max_bytes=4096)
    vision_path = bundle / vision_input["artifact"]
    if file_sha256(vision_path) != vision_input["sha256"]:
        raise VisionPolicyError("trusted VISION input changed")
    try:
        text = vision_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise VisionPolicyError("trusted VISION input is unreadable") from error
    plan_input = next(
        (item for item in task["spec"]["inputs"] if item.get("id") == "policy-derivation"), None
    )
    audit_input = next(
        (item for item in task["spec"]["inputs"] if item.get("id") == "policy-audit"), None
    )
    if not plan_input or not audit_input:
        raise VisionPolicyError("validated policy artifacts are unavailable")
    policy_values = []
    for item in (plan_input, audit_input):
        path = bundle / item["artifact"]
        if file_sha256(path) != item["sha256"]:
            raise VisionPolicyError("validated policy artifact changed")
        policy_values.append(load_json_regular(path, max_bytes=262144))
    if (
        binding.get("version") != "wheelhouse/policy-binding/v1"
        or binding.get("target_head_sha") != task["metadata"]["target"]["revision"]
        or binding.get("vision_sha256") != canonical_sha256(text)
    ):
        raise VisionPolicyError("trusted policy revision binding is inconsistent")
    plan, _, coverage_complete = validate_policy_artifacts(
        text,
        *policy_values,
        target_head_sha=binding.get("target_head_sha", ""),
        target_base_sha=binding.get("target_base_sha", ""),
        vision_blob_sha=binding.get("vision_blob_sha", ""),
    )
    expected = {row["obligation_id"]: row for row in plan["obligations"]}
    units_by_id = {row["unit_id"]: row for row in plan["units"]}
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
        task["metadata"]["executionId"], task_sha256 or canonical_sha256(task),
    )
    bound_local_evidence = any(
        item.get("id") in {"target", "repository"}
        and isinstance(item.get("sha256"), str) and len(item["sha256"]) == 64
        for item in task["spec"]["inputs"] if isinstance(item, dict)
    )
    citation_rows = value.get("citations")
    if not isinstance(citation_rows, list):
        raise VisionPolicyError("advisory citations are unavailable")
    cited_ids = {
        row.get("evidence_id") for row in citation_rows
        if isinstance(row, dict) and isinstance(row.get("evidence_id"), str)
    }
    projected_results = []
    all_required_pass = coverage_complete
    all_evidence_complete = coverage_complete
    for obligation_id, obligation in expected.items():
        row = observed[obligation_id]
        citation_ids = row.get("citation_ids") if isinstance(row.get("citation_ids"), list) else []
        expected_sha256 = obligation.get("expected_sha256")
        receipts_complete = (
            bound_local_evidence and obligation["semantic_status"] == "recognized-local" and not citation_ids
            if obligation["operation"] == "policy.assess"
            else obligation["semantic_status"] == "recognized" and bool(citation_ids) and all(
                evidence_id in cited_ids and evidence_id in receipts
                and receipts[evidence_id].get("operation") == obligation["operation"]
                and receipts[evidence_id].get("status") == "complete"
                and receipts[evidence_id].get("truncated") is False
                and (expected_sha256 is None or (
                    receipts[evidence_id].get("artifact_sha256") == expected_sha256
                    and receipts[evidence_id].get("sha256") == expected_sha256
                    and receipts[evidence_id].get("staged") is True
                )) for evidence_id in citation_ids
            )
        )
        assessment = row.get("assessment")
        scoped_not_applicable = bool(
            assessment == "not_applicable"
            and coverage_complete
            and obligation["semantic_status"]
            in {"recognized", "recognized-local"}
            and units_by_id[obligation["unit_id"]].get("conditions")
        )
        trusted_status = (
            "complete-pass" if assessment == "pass" and receipts_complete
            else "complete-fail" if assessment == "fail" and receipts_complete
            else "not-applicable" if scoped_not_applicable
            else "unavailable"
        )
        if trusted_status not in {"complete-pass", "not-applicable"}:
            all_required_pass = False
        if trusted_status not in {
            "complete-pass",
            "complete-fail",
            "not-applicable",
        }:
            all_evidence_complete = False
        projected_results.append({**row, "trusted_status": trusted_status})
    verdict = value.get("verdict")
    limitations = list(value.get("limitations") or [])
    if verdict == "positive" and not all_required_pass:
        verdict = "inconclusive"
        limitations.append("Trusted projection blocked a positive verdict because policy derivation, coverage, or required evidence was incomplete.")
    eligibility = value.get("eligibility_facts")
    eligibility_complete = bool(
        isinstance(eligibility, dict)
        and eligibility.get("behavior_class") in {"A", "B", "C"}
        and eligibility.get("changes_existing_or_default_behavior") is False
        and eligibility.get("aligns_with_vision") is True
        and eligibility.get("recommendation") == "eligible"
        and (eligibility.get("behavior_class") != "C" or eligibility.get("optin_default_off") is True)
    )
    return {
        **value,
        "plan_sha256": plan["plan_sha256"],
        "target_head_sha": binding["target_head_sha"],
        "target_base_sha": binding["target_base_sha"],
        "vision_blob_sha": binding["vision_blob_sha"],
        "vision_sha256": binding["vision_sha256"],
        "verdict": verdict,
        "obligation_results": projected_results,
        "limitations": limitations,
        "policy_coverage_complete": coverage_complete,
        "public_evidence_influenced": True,
        "acting_authority": False,
        "trusted_projection": True,
        "projection_complete": all_evidence_complete,
        "auto_merge_eligible": bool(verdict == "positive" and all_required_pass and eligibility_complete),
    }


__all__ = [
    "AUDIT_VERSION", "PLAN_VERSION", "REVIEW_KIND", "UNITS_VERSION",
    "VisionPolicyError", "obligation_semantic_status", "project_advisory_review",
    "validate_policy_artifacts", "vision_unit_document",
    "vision_units", "write_vision_units",
]
