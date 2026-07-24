#!/usr/bin/env python3
"""Pure bounded advisory related-work context for PR-review projections.

DecisionContext is neutral evidence. It can be rendered and supplied to triage,
but no auto-merge or manual action gate may consume it.
"""

import hashlib
import json
import re

import target_observation as observations

CONTEXT_SCHEMA_V1 = "wheelhouse.decision-context/v1"
CONTEXT_SCHEMA = "wheelhouse.decision-context/v2"
CONTEXT_STATUSES = frozenset({"complete", "truncated", "unavailable"})
RELATION_KINDS = frozenset(
    {"same-closing-issue", "explicit-reference", "exact-shared-path"}
)
MAX_CONTEXT_CANDIDATES = 10
LEGACY_MAX_CONTEXT_CANDIDATES = 8
MAX_RELATIONS_PER_CANDIDATE = 3
MAX_SHARED_PATHS = 3
MAX_SHARED_ISSUES = 3
MAX_CANDIDATE_TITLE = 100
MAX_GITHUB_URL = 250


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _identity(prefix, value):
    return prefix + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _snapshot_identity(value):
    semantic = dict(value)
    semantic.pop("snapshot_id", None)
    semantic.pop("observed_at", None)
    return _identity("sha256:", semantic)


def _context_identity(value):
    semantic = json.loads(_canonical(value))
    semantic.pop("context_id", None)
    snapshot = semantic.get("repository_snapshot")
    if isinstance(snapshot, dict):
        snapshot.pop("observed_at", None)
    return _identity("sha256:", semantic)


def _target_key(value):
    if not isinstance(value, dict):
        return None
    owner = value.get("owner")
    repo = value.get("repo")
    number = value.get("number")
    if (
        not isinstance(owner, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner)
        or not isinstance(repo, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo)
        or isinstance(number, bool)
        or not isinstance(number, int)
        or number < 1
    ):
        return None
    return owner, repo, number


def _safe_head(value):
    return isinstance(value, str) and 1 <= len(value) <= 100


def _normalized_title(value):
    if not isinstance(value, str):
        return None
    title = re.sub(r"\s+", " ", value).strip()
    if not title:
        return None
    return title[:MAX_CANDIDATE_TITLE]


def _safe_url(value):
    return isinstance(value, str) and len(value) <= MAX_GITHUB_URL and (
        not value or value.startswith("https://github.com/")
    )


def _candidate_source(value):
    key = _target_key(value)
    title = _normalized_title(value.get("title") if isinstance(value, dict) else None)
    if key is None or not _safe_head(value.get("head_sha")) or title is None:
        return None
    paths = value.get("paths")
    closing = value.get("closing_issues")
    references = value.get("references")
    if (
        not isinstance(value.get("paths_complete"), bool)
        or not isinstance(paths, list)
        or paths != sorted(set(paths))
        or any(not observations._safe_path(path) for path in paths)
        or not isinstance(value.get("closing_complete"), bool)
        or not isinstance(closing, list)
        or any(isinstance(number, bool) or not isinstance(number, int) or number < 1 for number in closing)
        or closing != sorted(set(closing))
        or not isinstance(value.get("references_complete"), bool)
        or not isinstance(references, list)
    ):
        return None
    normalized_refs = []
    for reference in references:
        ref_key = _target_key(reference)
        if ref_key is None:
            return None
        normalized_refs.append(
            {"owner": ref_key[0], "repo": ref_key[1], "number": ref_key[2]}
        )
    normalized_refs.sort(key=lambda row: (row["owner"], row["repo"], row["number"]))
    card_issue = value.get("card_issue", 0)
    if isinstance(card_issue, bool) or not isinstance(card_issue, int) or card_issue < 0:
        return None
    url = value.get("url", "")
    card_url = value.get("card_url", "")
    if not _safe_url(url) or not url or not _safe_url(card_url):
        return None
    return {
        "owner": key[0],
        "repo": key[1],
        "number": key[2],
        "head_sha": value["head_sha"],
        "title": title,
        "paths_complete": value["paths_complete"],
        "paths": list(paths),
        "closing_complete": value["closing_complete"],
        "closing_issues": list(closing),
        "references_complete": value["references_complete"],
        "references": normalized_refs,
        "card_issue": card_issue,
        "url": url,
        "card_url": card_url,
    }


def repository_snapshot(
    candidates,
    observed_at,
    *,
    complete=True,
    reason="",
    candidate_count=None,
):
    normalized = []
    for candidate in candidates or []:
        source = _candidate_source(candidate)
        if source is None:
            return None
        normalized.append(source)
    normalized.sort(key=lambda row: (row["owner"], row["repo"], row["number"]))
    identities = [(row["owner"], row["repo"], row["number"]) for row in normalized]
    if len(identities) != len(set(identities)):
        return None
    if not isinstance(complete, bool) or not isinstance(reason, str) or len(reason) > 120:
        return None
    if candidate_count is None:
        candidate_count = len(normalized)
    if (
        isinstance(candidate_count, bool)
        or not isinstance(candidate_count, int)
        or candidate_count < len(normalized)
    ):
        return None
    if complete and candidate_count != len(normalized):
        return None
    if complete and reason:
        return None
    if not complete and not reason:
        reason = "snapshot.incomplete"
    payload = {
        "observed_at": observed_at,
        "complete": complete,
        "reason": reason,
        "candidate_count": candidate_count,
        "candidates": normalized,
    }
    return {
        "snapshot_id": _snapshot_identity(payload),
        **payload,
    }


def _relation(kind, *, paths=None, issues=None, source=""):
    relation = {"kind": kind, "paths": [], "issues": [], "source": source}
    if paths:
        relation["paths"] = sorted(set(paths))[:MAX_SHARED_PATHS]
    if issues:
        relation["issues"] = sorted(set(issues))[:MAX_SHARED_ISSUES]
    return relation


def build_decision_context(target_observation, snapshot, candidate_cap=MAX_CONTEXT_CANDIDATES):
    """Match the full observed repository snapshot, then bound related results."""
    observation = observations.normalize_review_observation(target_observation)
    if observation is None:
        return unavailable_context(target_observation, "observation.invalid")
    if (
        not isinstance(snapshot, dict)
        or not isinstance(snapshot.get("snapshot_id"), str)
        or not snapshot["snapshot_id"].startswith("sha256:")
        or observations._timestamp(snapshot.get("observed_at")) is None
        or not isinstance(snapshot.get("complete"), bool)
        or not isinstance(snapshot.get("reason"), str)
        or not isinstance(snapshot.get("candidate_count"), int)
        or isinstance(snapshot.get("candidate_count"), bool)
        or not isinstance(snapshot.get("candidates"), list)
    ):
        return unavailable_context(observation, "snapshot.invalid")
    rebuilt = repository_snapshot(
        snapshot["candidates"],
        snapshot["observed_at"],
        complete=snapshot["complete"],
        reason=snapshot["reason"],
        candidate_count=snapshot["candidate_count"],
    )
    if rebuilt is None or rebuilt["snapshot_id"] != snapshot["snapshot_id"]:
        return unavailable_context(observation, "snapshot.identity_mismatch")
    if (
        isinstance(candidate_cap, bool)
        or not isinstance(candidate_cap, int)
        or candidate_cap < 1
        or candidate_cap > MAX_CONTEXT_CANDIDATES
    ):
        return unavailable_context(observation, "context.bound_invalid")

    target_key = (
        observation["target"]["owner"],
        observation["target"]["repo"],
        observation["target"]["number"],
    )
    target = next(
        (
            row
            for row in rebuilt["candidates"]
            if (row["owner"], row["repo"], row["number"]) == target_key
        ),
        None,
    )
    if target is None or target["head_sha"] != observation["revision"]["head_sha"]:
        return unavailable_context(observation, "target.snapshot_mismatch", rebuilt)

    related = []
    comparison_incomplete = False
    relation_truncated = False
    # The repository scan already paid to observe these rows. Match every row
    # before applying the small display/model-result cap so repository volume
    # cannot crowd out a deterministic relation.
    for candidate in rebuilt["candidates"]:
        key = (candidate["owner"], candidate["repo"], candidate["number"])
        if key == target_key:
            continue
        relations = []
        if (
            target["owner"] == candidate["owner"]
            and target["repo"] == candidate["repo"]
            and target["closing_complete"]
            and candidate["closing_complete"]
        ):
            common_issues = sorted(
                set(target["closing_issues"]).intersection(candidate["closing_issues"])
            )
            if common_issues:
                if len(common_issues) > MAX_SHARED_ISSUES:
                    relation_truncated = True
                relations.append(_relation("same-closing-issue", issues=common_issues))
        elif target["owner"] == candidate["owner"] and target["repo"] == candidate["repo"]:
            comparison_incomplete = True
        if target["references_complete"]:
            if {
                "owner": candidate["owner"],
                "repo": candidate["repo"],
                "number": candidate["number"],
            } in target["references"]:
                relations.append(
                    _relation("explicit-reference", source="target-metadata")
                )
        else:
            comparison_incomplete = True
        if target["paths_complete"] and candidate["paths_complete"]:
            shared = sorted(set(target["paths"]).intersection(candidate["paths"]))
            if shared:
                if len(shared) > MAX_SHARED_PATHS:
                    relation_truncated = True
                relations.append(_relation("exact-shared-path", paths=shared))
        else:
            comparison_incomplete = True
        if relations:
            relations.sort(key=lambda row: (row["kind"], row["paths"], row["issues"]))
            related.append(
                {
                    "target": {
                        "owner": candidate["owner"],
                        "repo": candidate["repo"],
                        "number": candidate["number"],
                        "head_sha": candidate["head_sha"],
                    },
                    "title": candidate["title"],
                    "url": candidate["url"],
                    "card_issue": candidate["card_issue"],
                    "relations": relations[:MAX_RELATIONS_PER_CANDIDATE],
                }
            )
    related.sort(
        key=lambda row: (
            row["target"]["owner"], row["target"]["repo"], row["target"]["number"]
        )
    )
    related_candidate_count = len(related)
    result_truncated = related_candidate_count > candidate_cap
    status = (
        "truncated"
        if not rebuilt["complete"]
        or result_truncated
        or comparison_incomplete
        or relation_truncated
        else "complete"
    )
    reason = (
        rebuilt["reason"]
        if not rebuilt["complete"]
        else (
            "related-candidate-bound"
            if result_truncated
            else (
                "comparison_incomplete"
                if comparison_incomplete
                else ("relation_bound" if relation_truncated else "")
            )
        )
    )
    payload = {
        "schema": CONTEXT_SCHEMA,
        "target": {
            "owner": target_key[0],
            "repo": target_key[1],
            "number": target_key[2],
            "head_sha": observation["revision"]["head_sha"],
            "observation_id": observation["observation_id"],
        },
        "repository_snapshot": {
            "snapshot_id": rebuilt["snapshot_id"],
            "observed_at": rebuilt["observed_at"],
            "candidate_count": rebuilt["candidate_count"],
            "complete": rebuilt["complete"],
            "reason": rebuilt["reason"],
        },
        "status": status,
        "reason": reason,
        "related_candidate_count": related_candidate_count,
        "candidates": related[:candidate_cap],
    }
    payload["context_id"] = _context_identity(payload)
    normalized = normalize_decision_context(payload)
    if normalized is None:
        raise ValueError("decision context construction produced invalid output")
    return normalized


def unavailable_context(observation, reason, snapshot=None):
    normalized = observations.normalize_review_observation(observation)
    target = (normalized or {}).get("target") or {
        "owner": "unknown", "repo": "unknown", "number": 1
    }
    revision = (normalized or {}).get("revision") or {"head_sha": "unknown"}
    observed_at = ((snapshot or {}).get("observed_at") or (normalized or {}).get("observed_at") or "1970-01-01T00:00:00Z")
    snapshot_id = (snapshot or {}).get("snapshot_id") or _snapshot_identity(
        {"unavailable": reason, "observed_at": observed_at}
    )
    payload = {
        "schema": CONTEXT_SCHEMA,
        "target": {
            "owner": target.get("owner", "unknown"),
            "repo": target.get("repo", "unknown"),
            "number": int(target.get("number") or 1),
            "head_sha": revision.get("head_sha") or "unknown",
            "observation_id": (normalized or {}).get("observation_id", "sha256:" + "0" * 64),
        },
        "repository_snapshot": {
            "snapshot_id": snapshot_id,
            "observed_at": observed_at,
            "candidate_count": int((snapshot or {}).get("candidate_count") or len((snapshot or {}).get("candidates") or [])),
            "complete": False,
            "reason": str((snapshot or {}).get("reason") or reason or "context.unavailable")[:120],
        },
        "status": "unavailable",
        "reason": str(reason or "context.unavailable")[:120],
        "related_candidate_count": 0,
        "candidates": [],
    }
    payload["context_id"] = _context_identity(payload)
    return normalize_decision_context(payload)


def normalize_decision_context(value):
    if not isinstance(value, dict):
        return None
    schema = value.get("schema")
    if schema == CONTEXT_SCHEMA:
        expected_fields = {
            "schema", "context_id", "target", "repository_snapshot", "status",
            "reason", "related_candidate_count", "candidates",
        }
        candidate_fields = {
            "target", "title", "url", "card_issue", "relations"
        }
        candidate_limit = MAX_CONTEXT_CANDIDATES
    elif schema == CONTEXT_SCHEMA_V1:
        # Persisted v1 cards remain readable until normal maintenance projects
        # v2. V1 is never a source for the compact title/URL model handoff.
        expected_fields = {
            "schema", "context_id", "target", "repository_snapshot", "status",
            "reason", "candidates",
        }
        candidate_fields = {
            "target", "url", "card_issue", "card_url", "relations"
        }
        candidate_limit = LEGACY_MAX_CONTEXT_CANDIDATES
    else:
        return None
    if set(value) != expected_fields or value.get("status") not in CONTEXT_STATUSES:
        return None
    target = value.get("target")
    if (
        _target_key(target) is None
        or not _safe_head(target.get("head_sha"))
        or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(target.get("observation_id") or "")
        )
    ):
        return None
    snapshot = value.get("repository_snapshot")
    count = snapshot.get("candidate_count") if isinstance(snapshot, dict) else None
    if (
        not isinstance(snapshot, dict)
        or set(snapshot)
        != {"snapshot_id", "observed_at", "candidate_count", "complete", "reason"}
        or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(snapshot.get("snapshot_id") or "")
        )
        or observations._timestamp(snapshot.get("observed_at")) is None
        or not isinstance(snapshot.get("complete"), bool)
        or not isinstance(snapshot.get("reason"), str)
        or len(snapshot.get("reason")) > 120
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or not isinstance(value.get("reason"), str)
        or len(value["reason"]) > 120
    ):
        return None
    candidates = value.get("candidates")
    status = value["status"]
    related_count = (
        value.get("related_candidate_count")
        if schema == CONTEXT_SCHEMA
        else len(candidates) if isinstance(candidates, list) else None
    )
    if (
        not isinstance(candidates, list)
        or len(candidates) > candidate_limit
        or isinstance(related_count, bool)
        or not isinstance(related_count, int)
        or related_count < len(candidates)
        or count < related_count
        or (
            status == "complete"
            and (
                not snapshot["complete"]
                or value["reason"]
                or related_count != len(candidates)
            )
        )
        or (status == "truncated" and not value["reason"])
        or (
            status == "unavailable"
            and (
                snapshot["complete"]
                or not value["reason"]
                or candidates
                or related_count
            )
        )
    ):
        return None
    normalized_candidates = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != candidate_fields:
            return None
        ctarget = candidate.get("target")
        key = _target_key(ctarget)
        if key is None or key in seen or not _safe_head(ctarget.get("head_sha")):
            return None
        seen.add(key)
        title = candidate.get("title") if schema == CONTEXT_SCHEMA else None
        if schema == CONTEXT_SCHEMA and _normalized_title(title) != title:
            return None
        if (
            not _safe_url(candidate.get("url"))
            or (schema == CONTEXT_SCHEMA and not candidate.get("url"))
            or (
                schema == CONTEXT_SCHEMA_V1
                and not _safe_url(candidate.get("card_url"))
            )
        ):
            return None
        card_issue = candidate.get("card_issue")
        if isinstance(card_issue, bool) or not isinstance(card_issue, int) or card_issue < 0:
            return None
        relations = candidate.get("relations")
        if (
            not isinstance(relations, list)
            or not relations
            or len(relations) > MAX_RELATIONS_PER_CANDIDATE
        ):
            return None
        normalized_relations = []
        for relation in relations:
            if (
                not isinstance(relation, dict)
                or set(relation) != {"kind", "paths", "issues", "source"}
            ):
                return None
            kind = relation.get("kind")
            paths = relation.get("paths")
            issues = relation.get("issues")
            source = relation.get("source")
            if (
                kind not in RELATION_KINDS
                or not isinstance(paths, list)
                or len(paths) > MAX_SHARED_PATHS
                or paths != sorted(set(paths))
                or any(not observations._safe_path(path) for path in paths)
                or not isinstance(issues, list)
                or len(issues) > MAX_SHARED_ISSUES
                or issues != sorted(set(issues))
                or any(
                    isinstance(number, bool)
                    or not isinstance(number, int)
                    or number < 1
                    for number in issues
                )
                or not isinstance(source, str)
                or len(source) > 120
                or (kind == "exact-shared-path" and (not paths or issues or source))
                or (kind == "same-closing-issue" and (paths or not issues or source))
                or (
                    kind == "explicit-reference"
                    and (paths or issues or source != "target-metadata")
                )
            ):
                return None
            normalized_relations.append(dict(relation))
        normalized_relations.sort(
            key=lambda row: (row["kind"], row["paths"], row["issues"])
        )
        if relations != normalized_relations:
            return None
        normalized_candidates.append({**candidate, "relations": normalized_relations})
    normalized_candidates.sort(
        key=lambda row: (
            row["target"]["owner"],
            row["target"]["repo"],
            row["target"]["number"],
        )
    )
    if candidates != normalized_candidates:
        return None
    claimed = value.get("context_id")
    if claimed != _context_identity(value):
        return None
    return json.loads(_canonical(value))


def compact_model_context(value):
    """Return the sole model-visible related-work projection.

    Immutable identities and relation evidence remain in DecisionContext for
    deterministic binding and card rendering. The model receives only each
    bounded related title and full URL, plus honest status/count metadata.
    """
    context = normalize_decision_context(value)
    if context is None or context.get("schema") != CONTEXT_SCHEMA:
        return None
    return {
        "status": context["status"],
        "reason": context["reason"],
        "total_matches": context["related_candidate_count"],
        "shown_matches": len(context["candidates"]),
        "items": [
            {"title": candidate["title"], "url": candidate["url"]}
            for candidate in context["candidates"]
        ],
    }
