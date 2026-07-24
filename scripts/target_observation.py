#!/usr/bin/env python3
"""Pure revision-bound target observation and target action contracts.

``wheelhouse.review-observation/v2`` is the current PR-review fact boundary.
The concrete persisted ``wheelhouse.target-observation/v1`` shape remains
readable only to migrate an old open or reusable card. A v1 record is always
migrated with configured-check and changed-path completeness set to false, so
legacy evidence can delay work but can never become new current or eligible
facts. Remove the v1 reader after no trusted open/reusable card contains v1.
"""

import hashlib
import json
import re
from datetime import datetime, timezone

OBSERVATION_SCHEMA_V1 = "wheelhouse.target-observation/v1"
REVIEW_OBSERVATION_SCHEMA = "wheelhouse.review-observation/v2"
OBSERVATION_SCHEMA = REVIEW_OBSERVATION_SCHEMA
ACTION_RECEIPT_SCHEMA = "wheelhouse.target-action-receipt/v1"
PROJECTION_REF_SCHEMA = "wheelhouse.card-projection-ref/v1"
OBSERVATION_SOURCES = frozenset({"bulk-scan", "exact-reread"})
OBSERVATION_COMPATIBILITY = frozenset({"native-v2", "persisted-v1"})
CHECK_ROLES = frozenset({"compliance", "test", "informational"})
CHECK_OUTCOMES = frozenset({"pass", "fail", "pending"})
MAX_CHECK_ROWS = 16
MAX_CHECK_NAME_LENGTH = 200
MAX_CHANGED_PATHS = 12
MAX_CHANGED_PATH_LENGTH = 512
APPROVAL_INVALIDATES = (
    "approval_phase",
    "check_phase",
    "comp",
    "tests",
    "bucket",
)
_RECEIPT_STATUSES = frozenset({"approved", "noop", "hold", "error", "partial"})
_RECEIPT_EFFECTS = frozenset({"changed", "unchanged", "unknown"})
_FRESHNESS = frozenset({"current", "pending", "unknown", "last-known"})


def utc_now():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _identity(prefix, value):
    digest = hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()
    return "%s%s" % (prefix, digest)


def _review_identity(value):
    semantic = dict(value)
    semantic.pop("observation_id", None)
    semantic.pop("observed_at", None)
    return _identity("sha256:", semantic)


def _bounded_text(value, maximum=300):
    if not isinstance(value, str):
        return None
    if not value or value != value.strip() or len(value) > maximum:
        return None
    return value


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _target(owner, repo, number):
    owner = _bounded_text(str(owner or ""), 100)
    repo = _bounded_text(str(repo or ""), 100)
    if isinstance(number, bool):
        number = 0
    try:
        number = int(number)
    except (TypeError, ValueError):
        number = 0
    if not owner or not repo or number < 1:
        raise ValueError("target identity is incomplete")
    return {"owner": owner, "repo": repo, "number": number, "type": "pull-request"}


def _safe_path(value):
    return bool(
        isinstance(value, str)
        and 1 <= len(value) <= MAX_CHANGED_PATH_LENGTH
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def changed_path_facts(paths=None, *, complete=False, count=None):
    """Build bounded immutable changed-path facts from one complete file list."""
    if not complete:
        return {
            "complete": False,
            "count": 0,
            "digest": "",
            "paths": [],
            "paths_truncated": False,
        }
    if not isinstance(paths, list) or any(not _safe_path(path) for path in paths):
        raise ValueError("changed paths are malformed")
    normalized = sorted(set(paths))
    if count is None:
        count = len(normalized)
    if isinstance(count, bool) or not isinstance(count, int) or count < len(normalized):
        raise ValueError("changed path count is malformed")
    digest = hashlib.sha256(_canonical(normalized).encode("utf-8")).hexdigest()
    return {
        "complete": True,
        "count": count,
        "digest": "sha256:" + digest,
        "paths": normalized[:MAX_CHANGED_PATHS],
        "paths_truncated": len(normalized) > MAX_CHANGED_PATHS,
    }


def normalize_check_rows(value):
    if not isinstance(value, list) or len(value) > MAX_CHECK_ROWS:
        return None
    rows = []
    identities = set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"name", "role", "outcome"}:
            return None
        name = raw.get("name")
        role = raw.get("role")
        outcome = raw.get("outcome")
        identity = (name, role)
        if (
            not _bounded_text(name, MAX_CHECK_NAME_LENGTH)
            or role not in CHECK_ROLES
            or outcome not in CHECK_OUTCOMES
            or identity in identities
        ):
            return None
        identities.add(identity)
        rows.append({"name": name, "role": role, "outcome": outcome})
    rows.sort(key=lambda row: (row["role"], row["name"]))
    return rows


def _base_completeness(value, *, v2):
    if not isinstance(value, dict):
        return None
    base_keys = {
        "complete",
        "target",
        "checks",
        "action_required_runs",
        "head_matches_expected",
        "check_contexts_seen",
        "check_contexts_total",
        "mergeability",
    }
    keys = base_keys | ({"configured_checks", "changed_paths"} if v2 else set())
    if set(value) != keys:
        return None
    bool_keys = {
        "complete",
        "target",
        "checks",
        "action_required_runs",
        "head_matches_expected",
    } | ({"configured_checks", "changed_paths"} if v2 else set())
    if any(not isinstance(value.get(key), bool) for key in bool_keys):
        return None
    seen = value.get("check_contexts_seen")
    total = value.get("check_contexts_total")
    if (
        isinstance(seen, bool)
        or isinstance(total, bool)
        or not isinstance(seen, int)
        or not isinstance(total, int)
        or seen < 0
        or total < 0
        or seen > total
        or value.get("mergeability")
        not in {"conclusive", "not-required", "unknown"}
    ):
        return None
    return dict(value)


def _base_facts(value, *, v2):
    if not isinstance(value, dict):
        return None
    scalar_keys = {
        "open",
        "title",
        "author",
        "updated_at",
        "draft",
        "cross_repo",
        "head_ref",
        "mergeable",
        "ci",
        "comp",
        "tests",
        "bucket",
        "approval_phase",
        "check_phase",
    }
    keys = scalar_keys | ({"configured_checks"} if v2 else set())
    if set(value) != keys:
        return None
    if not isinstance(value.get("open"), bool) or not isinstance(value.get("draft"), bool):
        return None
    if value.get("cross_repo") not in (True, False, None) or not isinstance(value.get("ci"), bool):
        return None
    if any(
        not isinstance(value.get(key), str) or len(value.get(key)) > 500
        for key in scalar_keys
        if key not in {"open", "draft", "cross_repo", "ci"}
    ):
        return None
    if v2 and normalize_check_rows(value.get("configured_checks")) is None:
        return None
    out = dict(value)
    if v2:
        out["configured_checks"] = normalize_check_rows(value["configured_checks"])
    return out


def make_observation(
    owner,
    repo,
    number,
    *,
    head_sha,
    base_sha="",
    expected_head_sha="",
    observed_at=None,
    source,
    completeness,
    facts,
    changed_paths=None,
    error="",
):
    """Build one strict content-free ReviewObservation v2 contract."""
    if source not in OBSERVATION_SOURCES:
        raise ValueError("unsupported observation source")
    complete = dict(completeness or {})
    fact_values = dict(facts or {})
    check_rows = normalize_check_rows(fact_values.get("configured_checks", []))
    if check_rows is None:
        raise ValueError("configured check rows are malformed")
    fact_values["configured_checks"] = check_rows
    complete.setdefault("configured_checks", bool(complete.get("checks")))
    path_values = changed_paths or changed_path_facts()
    complete.setdefault("changed_paths", bool(path_values.get("complete")))
    complete["complete"] = bool(
        complete.get("complete")
        and complete.get("configured_checks")
        and complete.get("changed_paths")
    )
    payload = {
        "schema": REVIEW_OBSERVATION_SCHEMA,
        "target": _target(owner, repo, number),
        "revision": {
            "head_sha": str(head_sha or ""),
            "base_sha": str(base_sha or ""),
            "expected_head_sha": str(expected_head_sha or ""),
        },
        "observed_at": observed_at or utc_now(),
        "source": source,
        "compatibility": "native-v2",
        "completeness": complete,
        "facts": fact_values,
        "changed_paths": path_values,
    }
    if error:
        payload["error"] = str(error)[:300]
    payload["observation_id"] = _review_identity(payload)
    normalized = normalize_observation(payload)
    if normalized is None:
        raise ValueError("invalid review observation")
    return normalized


def incomplete_observation(
    owner,
    repo,
    number,
    *,
    expected_head_sha="",
    observed_head_sha="",
    observed_at=None,
    source="exact-reread",
    error="target observation incomplete",
):
    return make_observation(
        owner,
        repo,
        number,
        head_sha=observed_head_sha or expected_head_sha,
        expected_head_sha=expected_head_sha,
        observed_at=observed_at,
        source=source,
        completeness={
            "complete": False,
            "target": False,
            "checks": False,
            "configured_checks": False,
            "changed_paths": False,
            "action_required_runs": False,
            "head_matches_expected": bool(observed_head_sha)
            and (not expected_head_sha or observed_head_sha == expected_head_sha),
            "check_contexts_seen": 0,
            "check_contexts_total": 0,
            "mergeability": "unknown",
        },
        facts={
            "open": True,
            "title": "",
            "author": "",
            "updated_at": "",
            "draft": False,
            "cross_repo": None,
            "head_ref": "",
            "mergeable": "UNKNOWN",
            "ci": False,
            "comp": "unknown",
            "tests": "unknown",
            "bucket": "ci-state-unknown",
            "approval_phase": "unknown",
            "check_phase": "unknown",
            "configured_checks": [],
        },
        changed_paths=changed_path_facts(),
        error=error,
    )


def _normalize_common(value, *, v2):
    if not isinstance(value, dict):
        return None
    expected = {
        "schema",
        "observation_id",
        "target",
        "revision",
        "observed_at",
        "source",
        "completeness",
        "facts",
    }
    if v2:
        expected |= {"compatibility", "changed_paths"}
    if "error" in value:
        expected.add("error")
    schema = REVIEW_OBSERVATION_SCHEMA if v2 else OBSERVATION_SCHEMA_V1
    if set(value) != expected or value.get("schema") != schema:
        return None
    target = value.get("target")
    try:
        if target != _target(target.get("owner"), target.get("repo"), target.get("number")):
            return None
    except (AttributeError, ValueError):
        return None
    revision = value.get("revision")
    if not isinstance(revision, dict) or set(revision) != {"head_sha", "base_sha", "expected_head_sha"}:
        return None
    if any(not isinstance(revision.get(key), str) or len(revision.get(key)) > 100 for key in revision):
        return None
    if not _timestamp(value.get("observed_at")) or value.get("source") not in OBSERVATION_SOURCES:
        return None
    if v2 and value.get("compatibility") not in OBSERVATION_COMPATIBILITY:
        return None
    completeness = _base_completeness(value.get("completeness"), v2=v2)
    facts = _base_facts(value.get("facts"), v2=v2)
    if completeness is None or facts is None:
        return None
    if "error" in value and (not isinstance(value.get("error"), str) or len(value.get("error")) > 300):
        return None
    if v2:
        paths = value.get("changed_paths")
        if not isinstance(paths, dict) or set(paths) != {"complete", "count", "digest", "paths", "paths_truncated"}:
            return None
        path_list = paths.get("paths")
        count = paths.get("count")
        if (
            not isinstance(paths.get("complete"), bool)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or not isinstance(path_list, list)
            or len(path_list) > MAX_CHANGED_PATHS
            or path_list != sorted(set(path_list))
            or any(not _safe_path(path) for path in path_list)
            or not isinstance(paths.get("paths_truncated"), bool)
        ):
            return None
        if paths["complete"]:
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(paths.get("digest") or "")):
                return None
            if count < len(path_list) or paths["paths_truncated"] != (count > len(path_list)):
                return None
        elif paths != changed_path_facts():
            return None
        if completeness["changed_paths"] != paths["complete"]:
            return None
        if completeness["configured_checks"] and not completeness["checks"]:
            return None
        required_complete = all(
            completeness[key]
            for key in (
                "target",
                "checks",
                "configured_checks",
                "changed_paths",
                "action_required_runs",
                "head_matches_expected",
            )
        ) and completeness["mergeability"] != "unknown"
        if completeness["complete"] != required_complete:
            return None
        expected_head = revision["expected_head_sha"]
        if completeness["target"] and completeness[
            "head_matches_expected"
        ] != bool(not expected_head or expected_head == revision["head_sha"]):
            return None
        if completeness["target"] and (
            not revision["head_sha"] or not revision["base_sha"]
        ):
            return None
        mergeable = facts["mergeable"].upper()
        if completeness["mergeability"] == "conclusive" and mergeable not in {
            "MERGEABLE", "CONFLICTING"
        }:
            return None
        if completeness["mergeability"] == "unknown" and completeness["complete"]:
            return None
        if completeness["configured_checks"]:
            tests = [
                row["outcome"]
                for row in facts["configured_checks"]
                if row["role"] == "test"
            ]
            reduced_tests = (
                "none"
                if not tests
                else "fail"
                if "fail" in tests
                else "pending"
                if "pending" in tests
                else "green"
            )
            if facts["tests"] != reduced_tests:
                return None
        if completeness["complete"] and (
            facts["comp"] == "unknown"
            or facts["tests"] == "unknown"
            or facts["bucket"] in {"ci-state-unknown", "mergeability-pending"}
        ):
            return None
    claimed_id = value.get("observation_id")
    expected_id = (
        _review_identity(value)
        if v2
        else _identity(
            "sha256:",
            {key: field for key, field in value.items() if key != "observation_id"},
        )
    )
    if claimed_id != expected_id:
        return None
    return json.loads(_canonical(value))


def normalize_observation(value):
    """Normalize a concrete v2 or persisted v1 observation without guessing."""
    if isinstance(value, dict) and value.get("schema") == REVIEW_OBSERVATION_SCHEMA:
        return _normalize_common(value, v2=True)
    if isinstance(value, dict) and value.get("schema") == OBSERVATION_SCHEMA_V1:
        return _normalize_common(value, v2=False)
    return None


def normalize_review_observation(value):
    """Return v2, migrating concrete v1 evidence to strict unknown dimensions."""
    normalized = normalize_observation(value)
    if normalized is None:
        return None
    if normalized["schema"] == REVIEW_OBSERVATION_SCHEMA:
        return normalized
    completeness = dict(normalized["completeness"])
    completeness.update(
        {"complete": False, "configured_checks": False, "changed_paths": False}
    )
    facts = dict(normalized["facts"])
    facts["configured_checks"] = []
    payload = {
        "schema": REVIEW_OBSERVATION_SCHEMA,
        "target": normalized["target"],
        "revision": normalized["revision"],
        "observed_at": normalized["observed_at"],
        "source": normalized["source"],
        "compatibility": "persisted-v1",
        "completeness": completeness,
        "facts": facts,
        "changed_paths": changed_path_facts(),
        "error": "persisted v1 observation lacks configured checks and changed paths",
    }
    payload["observation_id"] = _review_identity(payload)
    return _normalize_common(payload, v2=True)


def make_approval_receipt(
    owner,
    repo,
    number,
    *,
    expected_head_sha,
    initial_observation_id,
    status,
    completed_at=None,
):
    if status not in _RECEIPT_STATUSES:
        status = "error"
    effect = "changed" if status == "approved" else ("unchanged" if status in {"noop", "hold"} else "unknown")
    invalidates = list(APPROVAL_INVALIDATES) if effect != "unchanged" else []
    payload = {
        "schema": ACTION_RECEIPT_SCHEMA,
        "target": _target(owner, repo, number),
        "initial_observation_id": str(initial_observation_id or ""),
        "expected_head_sha": str(expected_head_sha or ""),
        "action": "approve-fork-ci",
        "status": status,
        "effect": effect,
        "completed_at": completed_at or utc_now(),
        "invalidates": invalidates,
        "requires_reobservation": effect != "unchanged",
    }
    payload["receipt_id"] = _identity("sha256:", payload)
    normalized = normalize_action_receipt(payload)
    if normalized is None:
        raise ValueError("invalid target action receipt")
    return normalized


def normalize_action_receipt(value):
    if not isinstance(value, dict) or set(value) != {
        "schema", "receipt_id", "target", "initial_observation_id",
        "expected_head_sha", "action", "status", "effect", "completed_at",
        "invalidates", "requires_reobservation",
    }:
        return None
    if value.get("schema") != ACTION_RECEIPT_SCHEMA:
        return None
    target = value.get("target")
    try:
        if target != _target(target.get("owner"), target.get("repo"), target.get("number")):
            return None
    except (AttributeError, ValueError):
        return None
    if (
        value.get("action") != "approve-fork-ci"
        or value.get("status") not in _RECEIPT_STATUSES
        or value.get("effect") not in _RECEIPT_EFFECTS
        or not _timestamp(value.get("completed_at"))
        or not isinstance(value.get("initial_observation_id"), str)
        or not value.get("initial_observation_id").startswith("sha256:")
        or len(value.get("initial_observation_id")) != 71
        or not _bounded_text(value.get("expected_head_sha"), 100)
        or not isinstance(value.get("requires_reobservation"), bool)
    ):
        return None
    invalidates = value.get("invalidates")
    if not isinstance(invalidates, list) or invalidates not in ([], list(APPROVAL_INVALIDATES)):
        return None
    if value.get("requires_reobservation") != (value.get("effect") != "unchanged") or bool(invalidates) != value.get("requires_reobservation"):
        return None
    without_id = dict(value)
    claimed_id = without_id.pop("receipt_id", None)
    if claimed_id != _identity("sha256:", without_id):
        return None
    return json.loads(_canonical(value))


def make_projection_ref(observation, freshness, bucket):
    observation = normalize_observation(observation)
    if observation is None or freshness not in _FRESHNESS:
        raise ValueError("invalid projection reference")
    return {
        "schema": PROJECTION_REF_SCHEMA,
        "observation_id": observation["observation_id"],
        "observed_at": observation["observed_at"],
        "source": observation["source"],
        "freshness": freshness,
        "complete": bool(observation["completeness"]["complete"]),
        "target": dict(observation["target"]),
        "revision": {"head_sha": observation["revision"]["head_sha"]},
        "bucket": str(bucket or ""),
    }


def normalize_projection_ref(value):
    if not isinstance(value, dict) or set(value) != {
        "schema", "observation_id", "observed_at", "source", "freshness",
        "complete", "target", "revision", "bucket",
    }:
        return None
    if (
        value.get("schema") != PROJECTION_REF_SCHEMA
        or not isinstance(value.get("observation_id"), str)
        or not value["observation_id"].startswith("sha256:")
        or not _timestamp(value.get("observed_at"))
        or value.get("source") not in OBSERVATION_SOURCES
        or value.get("freshness") not in _FRESHNESS
        or not isinstance(value.get("complete"), bool)
        or not isinstance(value.get("bucket"), str)
        or len(value.get("bucket")) > 100
    ):
        return None
    target = value.get("target")
    revision = value.get("revision")
    try:
        if target != _target(target.get("owner"), target.get("repo"), target.get("number")):
            return None
    except (AttributeError, ValueError):
        return None
    if not isinstance(revision, dict) or set(revision) != {"head_sha"} or not isinstance(revision.get("head_sha"), str) or len(revision.get("head_sha")) > 100:
        return None
    return json.loads(_canonical(value))
