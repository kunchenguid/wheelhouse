#!/usr/bin/env python3
"""Pure target observation/action receipt -> card projection planning."""

import target_observation as contracts

_UNKNOWN_BUCKET = "ci-state-unknown"
_TERMINAL_RECOMMENDATIONS = {
    "merge-ready": "Merge - compliance and tests are green.",
    "review-needed": "Review before merge - the test signal is missing or unclear.",
    "needs-reraise": "Wait for the contributor to satisfy the compliance gate.",
    "fix-tests": "Wait for the contributor to fix the failing tests.",
    "needs-rebase": "Wait for the contributor to resolve the merge conflict.",
}


def _same_target(target, owner, item):
    return bool(
        isinstance(target, dict)
        and target.get("owner") == owner
        and target.get("repo") == item.get("repo")
        and target.get("number") == int(item.get("number") or 0)
    )


def _base_item(item, observation):
    facts = observation["facts"]
    revision = observation["revision"]
    out = dict(item)
    out.pop("target_observation", None)
    out.pop("action_receipt", None)
    # Criteria built from the pre-action scan would create a mixed-time card.
    # Absence renders the normal explicit UNAVAILABLE rows; authorization never
    # consumes displayed criteria.
    out.pop("automerge_criteria", None)
    out.update(
        {
            "kind": "pr-review",
            "target_observation": observation,
            "head_sha": revision.get("head_sha") or item.get("head_sha", ""),
            "base_sha": revision.get("base_sha") or item.get("base_sha", ""),
            "updated_at": facts.get("updated_at") or item.get("updated_at", ""),
            "title": facts.get("title") or item.get("title") or "(no title)",
            "author": facts.get("author") or item.get("author") or "?",
        }
    )
    return out


def _unknown_projection(item, observation, reason):
    out = _base_item(item, observation)
    observed_at = observation["observed_at"]
    out.update(
        {
            "bucket": _UNKNOWN_BUCKET,
            "comp": "unknown",
            "tests": "unknown",
            "priority": "low",
            "summary": (
                "Current CI state could not be completely verified as of %s. "
                "Approval-needed, green, and prior-head values are not being "
                "presented as current. %s"
                % (observed_at, str(reason or "Observation incomplete.")[:240])
            ),
            "recommendation": (
                "Wait for the next complete target observation before acting on "
                "CI state. Existing head-bound action guards remain authoritative."
            ),
        }
    )
    out["projection_ref"] = contracts.make_projection_ref(
        observation, "unknown", out["bucket"]
    )
    return out


def plan_ci_wait_projection(owner, item, observation, receipt=None):
    """Return an observation-bound projection for one existing CI-wait card.

    A malformed/mismatched/incomplete contract never produces current-tense
    approval or green claims. Successful same-head approvals cannot project
    ``needs-ci-approval`` even when GitHub briefly returns contradictory data.
    """
    raw_observation = observation
    observation = contracts.normalize_observation(observation)
    if observation is None:
        raw_revision = (
            raw_observation.get("revision")
            if isinstance(raw_observation, dict)
            and isinstance(raw_observation.get("revision"), dict)
            else {}
        )
        observation = contracts.incomplete_observation(
            owner,
            item.get("repo"),
            item.get("number"),
            expected_head_sha=str(item.get("head_sha") or ""),
            observed_head_sha=str(raw_revision.get("head_sha") or ""),
            error="exact target observation contract is malformed",
        )
        return _unknown_projection(
            item, observation, "Exact target observation contract is malformed."
        )
    if not _same_target(observation.get("target"), owner, item):
        observation = contracts.incomplete_observation(
            owner,
            item.get("repo"),
            item.get("number"),
            expected_head_sha=str(item.get("head_sha") or ""),
            observed_head_sha=str(
                observation.get("revision", {}).get("head_sha") or ""
            ),
            observed_at=observation.get("observed_at"),
            error="target identity mismatch",
        )
        return _unknown_projection(item, observation, "Target identity mismatch.")

    normalized_receipt = None
    if receipt is not None:
        normalized_receipt = contracts.normalize_action_receipt(receipt)
        initial_observation = contracts.normalize_observation(
            item.get("target_observation")
        )
        if (
            normalized_receipt is None
            or initial_observation is None
            or not _same_target(normalized_receipt.get("target"), owner, item)
            or not _same_target(initial_observation.get("target"), owner, item)
            or normalized_receipt.get("initial_observation_id")
            != initial_observation.get("observation_id")
            or normalized_receipt.get("expected_head_sha")
            != initial_observation.get("revision", {}).get("head_sha")
        ):
            return _unknown_projection(item, observation, "Action receipt mismatch.")

    complete = observation["completeness"]["complete"]
    if not complete:
        reason = observation.get("error") or "Exact observation was incomplete."
        return _unknown_projection(item, observation, reason)

    facts = observation["facts"]
    if not facts.get("open"):
        return _unknown_projection(item, observation, "Target is no longer open.")

    head_sha = observation["revision"].get("head_sha") or ""
    approved_same_head = bool(
        normalized_receipt
        and normalized_receipt.get("status") == "approved"
        and normalized_receipt.get("effect") == "changed"
        and normalized_receipt.get("expected_head_sha") == head_sha
    )
    bucket = facts.get("bucket") or _UNKNOWN_BUCKET
    if approved_same_head and bucket == "needs-ci-approval":
        return _unknown_projection(
            item,
            observation,
            "Approval succeeded for this head, but the exact read still reported "
            "approval-required state.",
        )
    if bucket in {"needs-ci-approval", "mergeability-pending"}:
        return _unknown_projection(
            item, observation, "Exact classification remained conservative or ambiguous."
        )

    out = _base_item(item, observation)
    out.update(
        {
            "bucket": bucket,
            "comp": facts.get("comp", "unknown"),
            "tests": facts.get("tests", "unknown"),
        }
    )
    observed_at = observation["observed_at"]
    if bucket == "ci-running" or facts.get("check_phase") == "pending":
        freshness = "pending"
        out.update(
            {
                "bucket": "ci-running",
                "priority": "low",
                "summary": (
                    "CI approval is complete and current-head checks were pending "
                    "as of %s. Automatic triage for this head remains deferred "
                    "until checks finish; prior-head triage does not apply."
                    % observed_at
                ),
                "recommendation": (
                    "Wait for checks to finish, then re-review. The timestamp is "
                    "the exact target observation boundary."
                ),
            }
        )
    else:
        freshness = "current"
        out.update(
            {
                "priority": {
                    "merge-ready": "med",
                    "review-needed": "low",
                }.get(bucket, "low"),
                "summary": "compliance=%s tests=%s; exact target observation %s"
                % (out["comp"], out["tests"], observed_at),
                "recommendation": _TERMINAL_RECOMMENDATIONS.get(
                    bucket, "Wait for the target to return to the maintainer worklist."
                ),
            }
        )
    out["projection_ref"] = contracts.make_projection_ref(
        observation, freshness, out["bucket"]
    )
    return out
