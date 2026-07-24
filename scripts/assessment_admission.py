#!/usr/bin/env python3
"""Pure typed admission for advisory PR triage assessments."""

import hashlib
import json
import re

import decision_context
import target_observation

ASSESSMENT_SCHEMA = "wheelhouse.triage-assessment/v1"
ADMISSION_SCHEMA = "wheelhouse.assessment-admission/v1"
ADMISSION_STATUSES = frozenset({"admitted", "rejected", "stale", "unavailable"})
BASIS_KINDS = frozenset(
    {"other", "configured-tests-not-run", "configured-tests-not-green"}
)
TEST_BASIS_KINDS = frozenset(
    {"configured-tests-not-run", "configured-tests-not-green"}
)
MAX_CHECK_NAMES = target_observation.MAX_CHECK_ROWS
MAX_PERSISTED_PROSE = 700
ALLOWED_RECOMMENDATIONS = frozenset(
    {"merge", "request-changes", "decline", "close", "hold", "investigate", "comment"}
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _identity(prefix, value):
    return prefix + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def normalize_basis(value):
    if not isinstance(value, dict) or set(value) != {
        "kind", "observation_id", "context_id", "check_names"
    }:
        return None
    names = value.get("check_names")
    if (
        value.get("kind") not in BASIS_KINDS
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("observation_id") or ""))
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("context_id") or ""))
        or not isinstance(names, list)
        or len(names) > MAX_CHECK_NAMES
        or len(names) != len(set(names))
        or any(not isinstance(name, str) or not name or len(name) > 300 for name in names)
    ):
        return None
    if value["kind"] == "other" and names:
        return None
    normalized = dict(value)
    normalized["check_names"] = sorted(names)
    return json.loads(_canonical(normalized))


def _admission(status, reason):
    value = {"schema": ADMISSION_SCHEMA, "status": status, "reason": reason}
    return value


def admit_assessment(data, observation, context):
    """Return a typed bounded assessment with a content-free admission result.

    Prose remains display-only. Only an ``admitted`` result may create an Accept
    shortcut or supply a G6 recommendation/verdict.
    """
    observation = target_observation.normalize_review_observation(observation)
    context = decision_context.normalize_decision_context(context)
    if not isinstance(data, dict):
        return None
    required = {
        "summary", "product_implications", "recommended_action",
        "recommended_reason", "recommendation_basis"
    }
    if not required.issubset(data):
        return None
    summary = data.get("summary")
    implications = data.get("product_implications")
    action = data.get("recommended_action")
    reason = data.get("recommended_reason")
    basis = normalize_basis(data.get("recommendation_basis"))
    if (
        not isinstance(summary, str) or not summary.strip() or len(summary) > 4000
        or not isinstance(implications, str) or not implications.strip() or len(implications) > 4000
        or action not in ALLOWED_RECOMMENDATIONS
        or not isinstance(reason, str) or len(reason) > 4000
        or basis is None
    ):
        return None

    status = "admitted"
    code = "admission.ok"
    if observation is None or context is None:
        status, code = "unavailable", "binding.unavailable"
    elif basis["observation_id"] != observation["observation_id"] or basis["context_id"] != context["context_id"]:
        status, code = "stale", "binding.mismatch"
    elif observation["compatibility"] != "native-v2" or not observation["completeness"]["complete"]:
        status, code = "unavailable", "observation.incomplete"
    elif context["status"] != "complete":
        status, code = "unavailable", "context.%s" % context["status"]
    else:
        test_rows = [
            row for row in observation["facts"]["configured_checks"]
            if row["role"] == "test"
        ]
        by_name = {row["name"]: row for row in test_rows}
        named = basis["check_names"]
        if basis["kind"] in TEST_BASIS_KINDS and (
            not observation["completeness"]["configured_checks"]
            or any(name not in by_name for name in named)
        ):
            status, code = "unavailable", "basis.checks_unavailable"
        elif basis["kind"] == "configured-tests-not-run":
            if test_rows:
                status, code = "rejected", "basis.checks_contradict"
        elif basis["kind"] == "configured-tests-not-green":
            rows = [by_name[name] for name in named] if named else test_rows
            if rows and all(row["outcome"] == "pass" for row in rows):
                status, code = "rejected", "basis.checks_contradict"
            elif not rows:
                status, code = "unavailable", "basis.checks_unavailable"

    target = (
        {
            "owner": observation["target"]["owner"],
            "repo": observation["target"]["repo"],
            "number": observation["target"]["number"],
            "head_sha": observation["revision"]["head_sha"],
            "observation_id": observation["observation_id"],
            "context_id": context["context_id"],
        }
        if observation is not None and context is not None
        else {
            "owner": "unknown", "repo": "unknown", "number": 1,
            "head_sha": "unknown", "observation_id": basis["observation_id"],
            "context_id": basis["context_id"],
        }
    )
    payload = {
        "schema": ASSESSMENT_SCHEMA,
        "target": target,
        "summary": summary.strip()[:MAX_PERSISTED_PROSE],
        "product_implications": implications.strip()[:MAX_PERSISTED_PROSE],
        "recommendation": {
            "action": action,
            "reason": reason.strip()[:MAX_PERSISTED_PROSE],
            "basis": basis,
        },
        "admission": _admission(status, code),
    }
    payload["assessment_id"] = _identity("sha256:", payload)
    return normalize_assessment(payload)


def normalize_assessment(value):
    if not isinstance(value, dict) or set(value) != {
        "schema", "assessment_id", "target", "summary", "product_implications",
        "recommendation", "admission"
    }:
        return None
    if value.get("schema") != ASSESSMENT_SCHEMA:
        return None
    target = value.get("target")
    if (
        not isinstance(target, dict)
        or set(target) != {"owner", "repo", "number", "head_sha", "observation_id", "context_id"}
        or not isinstance(target.get("owner"), str)
        or not target["owner"]
        or len(target["owner"]) > 100
        or not isinstance(target.get("repo"), str)
        or not target["repo"]
        or len(target["repo"]) > 100
        or isinstance(target.get("number"), bool)
        or not isinstance(target.get("number"), int)
        or target["number"] < 1
        or not isinstance(target.get("head_sha"), str)
        or not target["head_sha"]
        or len(target["head_sha"]) > 100
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(target.get("observation_id") or ""))
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(target.get("context_id") or ""))
    ):
        return None
    if any(
        not isinstance(value.get(key), str)
        or not value[key]
        or len(value[key]) > MAX_PERSISTED_PROSE
        for key in ("summary", "product_implications")
    ):
        return None
    recommendation = value.get("recommendation")
    if not isinstance(recommendation, dict) or set(recommendation) != {"action", "reason", "basis"}:
        return None
    if (
        recommendation.get("action") not in ALLOWED_RECOMMENDATIONS
        or not isinstance(recommendation.get("reason"), str)
        or len(recommendation["reason"]) > MAX_PERSISTED_PROSE
        or normalize_basis(recommendation.get("basis")) != recommendation.get("basis")
    ):
        return None
    admission = value.get("admission")
    if (
        not isinstance(admission, dict)
        or set(admission) != {"schema", "status", "reason"}
        or admission.get("schema") != ADMISSION_SCHEMA
        or admission.get("status") not in ADMISSION_STATUSES
        or not isinstance(admission.get("reason"), str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,119}", admission["reason"])
    ):
        return None
    without_id = dict(value)
    claimed = without_id.pop("assessment_id", None)
    if claimed != _identity("sha256:", without_id):
        return None
    return json.loads(_canonical(value))


def admitted(value):
    value = normalize_assessment(value)
    return bool(value and value["admission"]["status"] == "admitted")
