#!/usr/bin/env python3
"""Versioned target-observation and target-action contracts.

This module is pure: it validates and hashes observations/receipts but performs
no GitHub reads or writes. ``wheelhouse_core`` owns the GitHub adapters and the
production check reducer/classifier that populate these contracts.
"""

import hashlib
import json
from datetime import datetime, timezone

OBSERVATION_SCHEMA = "wheelhouse.target-observation/v1"
ACTION_RECEIPT_SCHEMA = "wheelhouse.target-action-receipt/v1"
PROJECTION_REF_SCHEMA = "wheelhouse.card-projection-ref/v1"
OBSERVATION_SOURCES = frozenset({"bulk-scan", "exact-reread"})
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


def _bounded_text(value, maximum=300):
    if not isinstance(value, str):
        return None
    if not value or value != value.strip() or len(value) > maximum:
        return None
    return value


def _timestamp(value):
    if (
        not isinstance(value, str)
        or len(value) > 40
        or not value.endswith("Z")
    ):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return value


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
    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "type": "pull-request",
    }


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
    error="",
):
    """Build one content-free, identity-bound observation contract."""
    if source not in OBSERVATION_SOURCES:
        raise ValueError("unsupported observation source")
    payload = {
        "schema": OBSERVATION_SCHEMA,
        "target": _target(owner, repo, number),
        "revision": {
            "head_sha": str(head_sha or ""),
            "base_sha": str(base_sha or ""),
            "expected_head_sha": str(expected_head_sha or ""),
        },
        "observed_at": observed_at or utc_now(),
        "source": source,
        "completeness": dict(completeness or {}),
        "facts": dict(facts or {}),
    }
    if error:
        payload["error"] = str(error)[:300]
    payload["observation_id"] = _identity("sha256:", payload)
    normalized = normalize_observation(payload)
    if normalized is None:
        raise ValueError("invalid target observation")
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
    """Build a bounded unknown observation without inventing target facts."""
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
            "target": bool(observed_head_sha),
            "checks": False,
            "action_required_runs": False,
            "head_matches_expected": bool(observed_head_sha)
            and (
                not expected_head_sha or observed_head_sha == expected_head_sha
            ),
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
        },
        error=error,
    )


def normalize_observation(value):
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
    if "error" in value:
        expected.add("error")
    if set(value) != expected or value.get("schema") != OBSERVATION_SCHEMA:
        return None
    target = value.get("target")
    if not isinstance(target, dict) or set(target) != {
        "owner",
        "repo",
        "number",
        "type",
    }:
        return None
    if (
        not _bounded_text(target.get("owner"), 100)
        or not _bounded_text(target.get("repo"), 100)
        or isinstance(target.get("number"), bool)
        or not isinstance(target.get("number"), int)
        or target["number"] < 1
        or target.get("type") != "pull-request"
    ):
        return None
    revision = value.get("revision")
    if not isinstance(revision, dict) or set(revision) != {
        "head_sha",
        "base_sha",
        "expected_head_sha",
    }:
        return None
    if any(
        not isinstance(revision.get(key), str) or len(revision.get(key)) > 100
        for key in revision
    ):
        return None
    if not _timestamp(value.get("observed_at")):
        return None
    if value.get("source") not in OBSERVATION_SOURCES:
        return None
    completeness = value.get("completeness")
    completeness_keys = {
        "complete",
        "target",
        "checks",
        "action_required_runs",
        "head_matches_expected",
        "check_contexts_seen",
        "check_contexts_total",
        "mergeability",
    }
    if not isinstance(completeness, dict) or set(completeness) != completeness_keys:
        return None
    if any(
        not isinstance(completeness.get(key), bool)
        for key in (
            "complete",
            "target",
            "checks",
            "action_required_runs",
            "head_matches_expected",
        )
    ):
        return None
    seen = completeness.get("check_contexts_seen")
    total = completeness.get("check_contexts_total")
    if (
        isinstance(seen, bool)
        or isinstance(total, bool)
        or not isinstance(seen, int)
        or not isinstance(total, int)
        or seen < 0
        or total < 0
        or seen > total
        or completeness.get("mergeability")
        not in {"conclusive", "not-required", "unknown"}
    ):
        return None
    facts = value.get("facts")
    facts_keys = {
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
    if not isinstance(facts, dict) or set(facts) != facts_keys:
        return None
    if not isinstance(facts.get("open"), bool) or not isinstance(
        facts.get("draft"), bool
    ):
        return None
    if facts.get("cross_repo") not in (True, False, None) or not isinstance(
        facts.get("ci"), bool
    ):
        return None
    if any(
        not isinstance(facts.get(key), str) or len(facts.get(key)) > 500
        for key in facts_keys
        if key not in {"open", "draft", "cross_repo", "ci"}
    ):
        return None
    if "error" in value and (
        not isinstance(value.get("error"), str) or len(value.get("error")) > 300
    ):
        return None
    claimed_id = value.get("observation_id")
    without_id = dict(value)
    without_id.pop("observation_id", None)
    if claimed_id != _identity("sha256:", without_id):
        return None
    return json.loads(_canonical(value))


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
    if status == "approved":
        effect = "changed"
    elif status in {"noop", "hold"}:
        effect = "unchanged"
    else:
        effect = "unknown"
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
        "schema",
        "receipt_id",
        "target",
        "initial_observation_id",
        "expected_head_sha",
        "action",
        "status",
        "effect",
        "completed_at",
        "invalidates",
        "requires_reobservation",
    }:
        return None
    if value.get("schema") != ACTION_RECEIPT_SCHEMA:
        return None
    target = value.get("target")
    try:
        normalized_target = _target(
            target.get("owner"), target.get("repo"), target.get("number")
        )
    except (AttributeError, ValueError):
        return None
    if target != normalized_target:
        return None
    if (
        value.get("action") != "approve-fork-ci"
        or value.get("status") not in _RECEIPT_STATUSES
        or value.get("effect") not in _RECEIPT_EFFECTS
        or not _timestamp(value.get("completed_at"))
        or not isinstance(value.get("initial_observation_id"), str)
        or not value.get("initial_observation_id").startswith("sha256:")
        or len(value.get("initial_observation_id")) != 71
        or not isinstance(value.get("expected_head_sha"), str)
        or not value.get("expected_head_sha")
        or len(value.get("expected_head_sha")) > 100
        or not isinstance(value.get("requires_reobservation"), bool)
    ):
        return None
    invalidates = value.get("invalidates")
    if not isinstance(invalidates, list) or invalidates not in (
        [],
        list(APPROVAL_INVALIDATES),
    ):
        return None
    if value.get("requires_reobservation") != (value.get("effect") != "unchanged"):
        return None
    if bool(invalidates) != value.get("requires_reobservation"):
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
        "schema",
        "observation_id",
        "observed_at",
        "source",
        "freshness",
        "complete",
        "target",
        "revision",
        "bucket",
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
    if (
        not isinstance(revision, dict)
        or set(revision) != {"head_sha"}
        or not isinstance(revision.get("head_sha"), str)
        or len(revision.get("head_sha")) > 100
    ):
        return None
    return json.loads(_canonical(value))
