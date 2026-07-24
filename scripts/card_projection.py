#!/usr/bin/env python3
"""Pure byte-deterministic complete PR-review card projection planner."""

import hashlib
import json
import re

import assessment_admission
import decision_context
import target_observation

PROJECTION_SCHEMA = "wheelhouse.card-projection/v2"
CAUSE_CODES = frozenset(
    {
        "projection-current",
        "target-revision",
        "context-current",
        "assessment-result",
        "lifecycle-transition",
        "agent-status",
        "target-activity-reflection",
        "automerge-release",
        "decision-or-action",
        "migration-current",
        "noop",
    }
)
MANAGED_PREFIXES = ("repo:", "kind:", "priority:", "target:")
MANAGED_EXACT = frozenset(
    {
        "needs-decision",
        "pending-triage",
        "wheelhouse:manual-merge-required",
        "wheelhouse:confirming-target-state",
    }
)

QUEUE_EFFECT_BY_CAUSE = {
    "projection-current": "promote",
    "target-revision": "promote",
    "context-current": "promote",
    "assessment-result": "promote",
    "lifecycle-transition": "promote",
    "agent-status": "promote",
    "target-activity-reflection": "promote",
    "automerge-release": "promote",
    "decision-or-action": "promote",
    "migration-current": "promote",
    "noop": "none",
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value):
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _label_names(value):
    return sorted(
        {
            label if isinstance(label, str) else (label or {}).get("name", "")
            for label in (value or [])
            if (label if isinstance(label, str) else (label or {}).get("name", ""))
        }
    )


def _managed_label_names(value):
    return [
        label
        for label in _label_names(value)
        if label.startswith(MANAGED_PREFIXES) or label in MANAGED_EXACT
    ]


def projection_from_values(
    *,
    title,
    body,
    labels,
    cause,
    observation_id="",
    context_id="",
    prior=None,
):
    """Plan one complete already-composed projection without side effects."""
    if cause not in CAUSE_CODES:
        raise ValueError("unsupported card projection cause")
    labels = _label_names(labels)
    prior = prior or {}
    changed = []
    if prior.get("title") != title:
        changed.append("title")
    if prior.get("body") != body:
        changed.append("body")
    if _managed_label_names(prior.get("labels")) != labels:
        changed.append("managed-labels")
    if not changed:
        cause = "noop"
    payload = {
        "schema": PROJECTION_SCHEMA,
        "title": str(title or ""),
        "body": str(body or ""),
        "managed_labels": labels,
        "cause": cause,
        "queue_effect": QUEUE_EFFECT_BY_CAUSE[cause],
        "changed_sections": changed,
        "observation_id": str(observation_id or ""),
        "context_id": str(context_id or ""),
    }
    payload["projection_id"] = _digest(_canonical(payload))
    return normalize_card_projection(payload)


def plan_card_projection(
    item,
    *,
    prior=None,
    cause="projection-current",
    held=False,
    workflow_hold=None,
    preserve_same_revision=True,
):
    """Compose a complete PR-review card from normalized facts and advisory input.

    This function performs no reads, writes, provider calls, or target actions.
    """
    if not isinstance(item, dict) or item.get("kind", "pr-review") != "pr-review":
        raise ValueError("CardProjection currently owns pr-review only")
    import render_card

    projected_item = dict(item)
    observation = target_observation.normalize_review_observation(
        item.get("target_observation") or item.get("review_observation")
    )
    if observation is None:
        raise ValueError("pr-review projection requires ReviewObservation")
    target = observation["target"]
    if (
        target.get("repo") != item.get("repo")
        or target.get("number") != int(item.get("number") or 0)
        or observation["revision"].get("head_sha") != str(item.get("head_sha") or "")
    ):
        raise ValueError("projection item and observation do not match")
    facts = observation["facts"]
    if observation["completeness"]["complete"]:
        bucket = facts.get("bucket") or "ci-state-unknown"
        comp = facts.get("comp") or "unknown"
        tests = facts.get("tests") or "unknown"
        freshness = (
            "pending"
            if bucket == "ci-running" or facts.get("check_phase") == "pending"
            else "current"
        )
    else:
        # Partial observations may retain raw diagnostic rows, but they never
        # produce current green, approval-needed, or classifier assertions.
        bucket = "ci-state-unknown"
        comp = "unknown"
        tests = "unknown"
        freshness = "unknown"
    projected_item.update(
        {
            "head_sha": observation["revision"]["head_sha"],
            "base_sha": observation["revision"]["base_sha"],
            "title": facts.get("title") or item.get("title") or "(no title)",
            "author": facts.get("author") or item.get("author") or "?",
            "updated_at": facts.get("updated_at") or item.get("updated_at", ""),
            "bucket": bucket,
            "comp": comp,
            "tests": tests,
            "target_observation": observation,
            "review_observation": observation,
            "projection_ref": target_observation.make_projection_ref(
                observation, freshness, bucket
            ),
        }
    )
    context = decision_context.normalize_decision_context(item.get("decision_context"))
    if context is None:
        context = decision_context.unavailable_context(
            observation, "context.unavailable"
        )
    if context["target"]["observation_id"] != observation["observation_id"]:
        context = decision_context.unavailable_context(
            observation, "context.observation_mismatch"
        )
    projected_item["decision_context"] = context

    assessment = assessment_admission.normalize_assessment(item.get("assessment"))
    if assessment and assessment["target"]["observation_id"] == observation["observation_id"] and assessment["target"]["context_id"] == context["context_id"]:
        projected_item["assessment"] = assessment
        projected_item["triage"] = {
            "summary": assessment["summary"],
            "product_implications": assessment["product_implications"],
            "recommended_action": assessment["recommendation"]["action"],
            "recommended_reason": assessment["recommendation"]["reason"],
            "recommended_next_step": "%s%s"
            % (
                assessment["recommendation"]["action"],
                (
                    " - " + assessment["recommendation"]["reason"]
                    if assessment["recommendation"]["reason"]
                    else ""
                ),
            ),
            "evidence": "bound assessment",
        }
    card = render_card.render(
        projected_item,
        held=held,
        workflow_hold=workflow_hold,
        owner=target["owner"],
    )
    prior = prior or {}
    if preserve_same_revision and prior.get("body") and assessment is None:
        old_state = render_card.parse_state_block(prior.get("body", "")) or {}
        card["body"] = render_card._preserve_same_revision_triage(
            card["body"],
            prior["body"],
            projected_item,
            old_state,
            owner=target["owner"],
        )
    return projection_from_values(
        title=card["title"],
        body=card["body"],
        labels=card["labels"],
        cause=cause,
        observation_id=observation["observation_id"],
        context_id=context["context_id"],
        prior=prior,
    )


def normalize_card_projection(value):
    if not isinstance(value, dict) or set(value) != {
        "schema", "projection_id", "title", "body", "managed_labels", "cause",
        "queue_effect", "changed_sections", "observation_id", "context_id"
    }:
        return None
    if (
        value.get("schema") != PROJECTION_SCHEMA
        or not isinstance(value.get("title"), str)
        or not value["title"]
        or len(value["title"]) > 256
        or not isinstance(value.get("body"), str)
        or not value["body"]
        or len(value["body"].encode("utf-8")) > 60000
        or value.get("cause") not in CAUSE_CODES
        or value.get("queue_effect") != QUEUE_EFFECT_BY_CAUSE[value["cause"]]
        or not isinstance(value.get("managed_labels"), list)
        or value["managed_labels"] != sorted(set(value["managed_labels"]))
        or any(
            not isinstance(label, str)
            or not label
            or len(label) > 100
            or (
                not label.startswith(MANAGED_PREFIXES)
                and label not in MANAGED_EXACT
            )
            for label in value["managed_labels"]
        )
        or not isinstance(value.get("changed_sections"), list)
        or any(
            section not in {"title", "body", "managed-labels"}
            for section in value["changed_sections"]
        )
        or value["changed_sections"]
        != sorted(
            value["changed_sections"],
            key=("title", "body", "managed-labels").index,
        )
        or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(value.get("observation_id") or "")
        )
        or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(value.get("context_id") or "")
        )
    ):
        return None
    if value["cause"] == "noop" and value["changed_sections"]:
        return None
    if value["cause"] != "noop" and not value["changed_sections"]:
        return None
    without_id = dict(value)
    claimed = without_id.pop("projection_id", None)
    if claimed != _digest(_canonical(without_id)):
        return None
    return json.loads(_canonical(value))
