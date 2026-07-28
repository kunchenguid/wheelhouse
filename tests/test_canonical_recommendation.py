#!/usr/bin/env python3
"""One canonical Wheelhouse recommendation surface (card #1746).

Every check here is offline: no network call, no live card or target is read or
mutated, and no model runs. The fixtures reproduce the exact production shape
recorded for decision card #1746 / firstmate#1089 (head `91be95d3...`, delivered
candidate `valueSha256 1c947887...`):

- bucket `merge-ready`, compliance pass, configured checks green,
- an advisory `### Triage` result whose prose recommends merge,
- `recommendation_basis.kind = "configured-tests"` (a kind that does not exist),
- `automerge.optin_default_off` omitted,
- primary `output.schema_invalid`, admission `basis.missing_or_invalid`,
- no admitted assessment and therefore no authority-bearing recommendation.

The product rule proven here: a recommendation is presented ONLY when it comes
from a current admitted structured agent-triage result, in exactly one place.
There is no deterministic check-derived recommendation, and a delivered but
invalid or non-admitted candidate never has its advisory action presented as the
agent's recommendation.
"""

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

import assessment_admission as admission  # noqa: E402
import auto_merge  # noqa: E402
import build_item  # noqa: E402
import card_projection  # noqa: E402
import automerge_criteria as criteria_schema  # noqa: E402
import render_card as rc  # noqa: E402
import target_reconcile  # noqa: E402
import wheelhouse_core as core  # noqa: E402
import test_option_b_architecture as ob  # noqa: E402
from agent_runtime import contract  # noqa: E402

FAILURES = []

CARD_1746_HEAD = "91be95d3584cbcfe3322d0f7827e1224ccb999cc"
CARD_1746_BASE = "a5fe1bcc0f1e4d2b8c37a9d0e6b45f1723c8d904"
CARD_1746_CHECKS = [
    {
        "name": "PR must be raised via no-mistakes",
        "role": "compliance",
        "outcome": "pass",
    },
    {"name": "Ubuntu", "role": "test", "outcome": "pass"},
    {"name": "macOS", "role": "test", "outcome": "pass"},
    {"name": "Windows", "role": "test", "outcome": "pass"},
    {"name": "E2E", "role": "test", "outcome": "pass"},
]
TRIAGE_WORKFLOW = ROOT / ".github" / "workflows" / "triage.yml"
WHEELHOUSE_CONFIG = ROOT / "wheelhouse.config.yml"
TRIAGE_REPLAY = ROOT / "scripts" / "triage_replay.py"
PR_SCHEMA = json.loads(
    (
        ROOT / "agent_runtime" / "schemas" / "actions" / "triage-pr-v1.schema.json"
    ).read_text(encoding="utf-8")
)


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def schema_error(value):
    """The first bound-schema violation for one candidate, or ''."""
    try:
        contract.validate_schema(value, PR_SCHEMA)
    except contract.ContractError as error:
        return str(error)
    return ""


def card_1746_world():
    """The exact card-1746 production shape: rendered card plus its inputs."""
    obs = ob.observation(
        number=1089,
        head=CARD_1746_HEAD,
        base=CARD_1746_BASE,
        checks=CARD_1746_CHECKS,
        paths=[".github/workflows/pin.yml"],
    )
    context = ob.context_for(obs, rows=[])
    item = ob.item_for(obs, context)
    item["title"] = "chore(ci): bump pinned Treehouse to v2.1.0"
    item["author"] = "baksoy"
    delivered = {
        "summary": "Bumps the pinned Treehouse version to v2.1.0.",
        "product_implications": "Straight chore; the CI pin is the only change.",
        "recommended_action": "merge",
        "recommended_reason": (
            "Straight chore bump with all configured checks green."
        ),
        "evidence": "target.txt: \"chore(ci): bump pinned Treehouse to v2.1.0\"",
        "recommendation_basis": {
            "kind": "configured-tests",
            "observation_id": obs["observation_id"],
            "context_id": context["context_id"],
            "check_names": ["Ubuntu", "macOS", "Windows", "E2E"],
        },
        "automerge": {
            "behavior_class": "A",
            "changes_existing_or_default_behavior": False,
        },
    }
    body = rc.body_with_triage_result(
        rc.render(item, owner="kunchenguid")["body"],
        CARD_1746_HEAD,
        triage=delivered,
        owner="kunchenguid",
        base_sha=item["base_sha"],
        primary_error_code="output.schema_invalid",
    )
    return obs, context, item, delivered, body


# --------------------------------------------------------------------------- #
# The generation defect stays a defect
# --------------------------------------------------------------------------- #
def test_card_1746_candidate_remains_schema_invalid():
    obs, context, _item, delivered, _body = card_1746_world()

    check(
        "card-1746: the delivered candidate fails the bound schema",
        bool(schema_error(delivered)),
    )
    optin_only = json.loads(json.dumps(delivered))
    optin_only["automerge"]["optin_default_off"] = False
    check(
        "card-1746: the unsupported `configured-tests` kind alone is invalid",
        "recommendation_basis" in schema_error(optin_only),
    )
    basis_only = json.loads(json.dumps(delivered))
    basis_only["recommendation_basis"] = {
        "kind": "other",
        "observation_id": obs["observation_id"],
        "context_id": context["context_id"],
    }
    check(
        "card-1746: the omitted `optin_default_off` alone is invalid",
        "optin_default_off" in schema_error(basis_only),
    )
    both = json.loads(json.dumps(basis_only))
    both["automerge"]["optin_default_off"] = False
    check(
        "card-1746: fixing exactly those two fields validates",
        schema_error(both) == "",
    )
    check(
        "card-1746: no positive green-checks kind was added to the union",
        admission.BASIS_KINDS
        == frozenset(
            {"other", "configured-tests-not-run", "configured-tests-not-green"}
        )
        and "configured-tests-green" not in admission.BASIS_KINDS,
    )
    check(
        "card-1746: admission still refuses the unsupported kind",
        admission.normalize_basis(delivered["recommendation_basis"]) is None
        and admission.admit_assessment(delivered, obs, context) is None,
    )


# --------------------------------------------------------------------------- #
# The exact captain-visible card
# --------------------------------------------------------------------------- #
def test_card_1746_render_has_no_conflicting_recommendation():
    _obs, _context, _item, _delivered, body = card_1746_world()
    state = core.parse_state_block(body)

    check(
        "card-1746: the failing state is reproduced exactly",
        state.get("triage_status") == "succeeded"
        and state.get("triaged_sha") == CARD_1746_HEAD
        and state.get(rc.TRIAGE_PRIMARY_STATUS_FIELD) == "failed"
        and state.get(rc.TRIAGE_PRIMARY_ERROR_FIELD) == "output.schema_invalid"
        and state.get(rc.TRIAGE_CONSUMPTION_FIELD) == "advisory"
        and state.get("assessment_admission")
        == {"status": "unavailable", "reason": "basis.missing_or_invalid"}
        and rc.ASSESSMENT_FIELD not in state
        and "triage_recommendation" not in state,
    )
    check(
        "card-1746: no deterministic recommendation section is rendered",
        "### Recommended action" not in body,
    )
    check(
        "card-1746: no deterministic check-derived recommendation copy remains",
        "compliance and tests are green" not in body,
    )
    check(
        "card-1746: the advisory action is not presented as a recommendation",
        "Recommended next step" not in body
        and "Agent recommendation" not in body,
    )
    check(
        "card-1746: no Accept shortcut is offered",
        "<!-- opt:accept-recommendation -->" not in body,
    )
    check(
        "card-1746: useful non-recommendation analysis is preserved",
        "- **Summary:** Bumps the pinned Treehouse version to v2.1.0." in body
        and "- **Product implications:** Straight chore; the CI pin is the "
        "only change." in body,
    )
    check(
        "card-1746: the honest primary-failure warning is preserved",
        "Primary model validation failed (`output.schema_invalid`), but the "
        "delivered candidate was consumed for advisory triage." in body,
    )
    check(
        "card-1746: the honest admission warning is preserved",
        "The advisory assessment was not admitted (`basis.missing_or_invalid`)."
        in body,
    )
    check(
        "card-1746: the deterministic decision controls are untouched",
        "<!-- opt:merge -->" in body
        and "<!-- opt:close -->" in body
        and "<!-- opt:hold -->" in body,
    )
    check(
        "card-1746: check facts remain facts in Situation",
        "- Compliance: `pass`" in body and "- Tests: `green`" in body,
    )


def test_card_1746_g6_evidence_is_truthful():
    _obs, _context, _item, _delivered, body = card_1746_world()
    state = core.parse_state_block(body)
    facts, _behavior_class = auto_merge.fresh_verdict_facts(state, CARD_1746_HEAD)
    row = facts["g6_merge_recommendation"]

    check(
        "card-1746 G6: the recommendation row is still UNMET",
        row["status"] == criteria_schema.STATUS_UNMET,
    )
    check(
        "card-1746 G6: evidence says no valid agent recommendation was established",
        row["evidence"]
        == "no valid agent recommendation was established: the advisory "
        "assessment was not admitted"
        and row["evidence"] == row["reason"],
    )
    check(
        "card-1746 G6: evidence never implies the model recommended something else",
        "not an explicit merge" not in row["evidence"],
    )
    check(
        "card-1746 G6: the triage row keeps its identical authority semantics",
        facts["g6_triage_success"]["status"] == criteria_schema.STATUS_UNMET
        and facts["g6_triage_success"]["evidence"]
        == "current assessment is not admitted for its observation/head",
    )
    check(
        "card-1746 G6: the candidate is still ineligible for auto-merge",
        auto_merge._fresh_verdict_for_head(state, CARD_1746_HEAD)[0] is False,
    )


# --------------------------------------------------------------------------- #
# The generation contract in the real PR-triage prompt
# --------------------------------------------------------------------------- #
def _triage_prompt_block():
    workflow = yaml.safe_load(TRIAGE_WORKFLOW.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        for step in job.get("steps") or []:
            run = step.get("run") or ""
            if "prompt.txt" in run and "recommendation_basis" in run:
                return run
    return ""


def test_pr_triage_prompt_states_the_exact_basis_contract():
    block = _triage_prompt_block()
    check("prompt: the PR-triage prompt block was found", bool(block))
    for kind in ("other", "configured-tests-not-run", "configured-tests-not-green"):
        check(
            "prompt: names the exact valid basis kind %r" % kind,
            '"%s"' % kind in block,
        )
    check(
        "prompt: never offers a positive green-checks kind",
        "configured-tests-green" not in block,
    )
    check(
        "prompt: drops the ambiguous bare `configured-tests basis` shorthand",
        "configured-tests basis" not in block,
    )
    check(
        "prompt: states the union is closed",
        "No other kind exists." in block,
    )
    check(
        "prompt: routes a green-checks rationale to `other` without check names",
        "There is no positive green-checks kind" in block
        and "omits check_names entirely" in block,
    )
    check(
        "prompt: the basis contract is on the shared pr-review branch",
        block.count("No other kind exists.") == 1
        and re.search(
            r'if \[ "\$KIND" = "pr-review" \]; then\n(?:.*\n)*?'
            r".*recommendation_basis\.kind must be EXACTLY one of",
            block,
        )
        is not None,
    )


def test_pr_triage_prompt_requires_optin_default_off_everywhere():
    block = _triage_prompt_block()
    optin_lines = [
        line.strip()
        for line in block.splitlines()
        if '"optin_default_off"' in line and line.strip().startswith("echo")
    ]
    check(
        "prompt: every automerge branch emits an optin_default_off field line",
        len(optin_lines) == 2,
    )
    check(
        "prompt: every optin_default_off line says it is always required",
        all("always required" in line for line in optin_lines),
    )
    check(
        "prompt: the class-C-only framing that invited omission is gone",
        "(class C only:" not in block,
    )
    check(
        "prompt: class-C significance is still explained, not the requirement",
        all("class C treats true as meaningful" in line for line in optin_lines),
    )
    check(
        "prompt: the behavior instructions repeat the always-required rule",
        "Always emit automerge.optin_default_off; it is required for EVERY class."
        in block,
    )


# --------------------------------------------------------------------------- #
# Proven controls
# --------------------------------------------------------------------------- #
def _pr_card(head="aaaa1111bbbb2222cccc3333dddd4444eeee5555", number=42):
    obs = ob.observation(number=number, head=head)
    context = ob.context_for(obs, rows=[])
    item = ob.item_for(obs, context)
    return obs, context, item, rc.render(item, owner="kunchenguid")["body"]


def _admitted_payload(obs, context, action, reason):
    return {
        "summary": "Adds one bounded helper.",
        "product_implications": "Routine internal change.",
        "recommended_action": action,
        "recommended_reason": reason,
        "evidence": "target.txt: \"Adds one bounded helper\"",
        "recommendation_basis": {
            "kind": "other",
            "observation_id": obs["observation_id"],
            "context_id": context["context_id"],
        },
    }


def test_admitted_recommendation_is_the_one_canonical_surface():
    head = "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
    obs, context, _item, body = _pr_card(head=head)

    merged = rc.body_with_triage_result(
        body,
        head,
        triage=_admitted_payload(
            obs, context, "merge", "Green checks and a bounded chore change."
        ),
        owner="kunchenguid",
    )
    merged_state = core.parse_state_block(merged)
    check(
        "control: an admitted merge recommendation renders exactly once",
        merged.count("### Recommended action") == 1
        and merged.count("- **Agent recommendation:**") == 1,
    )
    check(
        "control: the canonical section carries the action and its reason",
        "- **Agent recommendation:** `merge`" in merged
        and "- **Reason:** Green checks and a bounded chore change." in merged,
    )
    check(
        "control: the canonical section is not duplicated inside Triage",
        "Recommended next step" not in merged,
    )
    check(
        "control: the admitted recommendation drives the Accept shortcut",
        "<!-- opt:accept-recommendation -->" in merged
        and merged_state.get("triage_recommendation", {}).get("action") == "merge"
        and rc.accept_recommendation_available(merged_state),
    )
    check(
        "control: the canonical section sits between Triage and the controls",
        merged.find(rc.TRIAGE_END)
        < merged.find("### Recommended action")
        < merged.find(rc.DECISION_START),
    )
    check(
        "control: an admitted merge recommendation satisfies G6",
        auto_merge.fresh_verdict_facts(merged_state, head)[0][
            "g6_merge_recommendation"
        ]["status"]
        == criteria_schema.STATUS_MET,
    )

    changes = rc.body_with_triage_result(
        body,
        head,
        triage=_admitted_payload(
            obs, context, "request-changes", "Please add a regression test for #7."
        ),
        owner="kunchenguid",
    )
    changes_state = core.parse_state_block(changes)
    check(
        "control: an admitted non-merge recommendation renders correctly",
        changes.count("### Recommended action") == 1
        and "- **Agent recommendation:** `request-changes`" in changes
        and "<!-- opt:accept-recommendation -->" in changes,
    )
    check(
        "control: the canonical reason is cross-repo qualified deterministically",
        "kunchenguid/firstmate#7" in changes,
    )
    check(
        "control: a non-merge recommendation leaves G6 UNMET on its own evidence",
        auto_merge.fresh_verdict_facts(changes_state, head)[0][
            "g6_merge_recommendation"
        ]
        == {
            "status": criteria_schema.STATUS_UNMET,
            "evidence": "top-level triage recommendation is not an explicit merge",
            "reason": "top-level triage recommendation is not an explicit merge",
        },
    )


def test_non_admitted_and_pre_triage_cards_present_no_recommendation():
    head = "bbbb1111cccc2222dddd3333eeee4444ffff5555"
    obs, context, item, body = _pr_card(head=head, number=43)

    check(
        "control: a pre-triage card has no recommendation section",
        "### Recommended action" not in body,
    )
    queued = rc.body_with_triage_queued(body, item)
    check(
        "control: a queued card does not regain a deterministic recommendation",
        "### Recommended action" not in queued
        and "Automatic triage queued for this exact revision." in queued,
    )
    failed = rc.body_with_triage_result(
        queued, head, error="triage timed out", owner="kunchenguid"
    )
    check(
        "control: a no-result card has no recommendation section",
        "### Recommended action" not in failed
        and core.parse_state_block(failed).get("triage_status") == "error",
    )

    stale_basis = _admitted_payload(obs, context, "merge", "Looks fine.")
    stale_basis["recommendation_basis"]["observation_id"] = "sha256:" + "0" * 64
    denied = rc.body_with_triage_result(
        queued, head, triage=stale_basis, owner="kunchenguid"
    )
    denied_state = core.parse_state_block(denied)
    check(
        "control: an observation-mismatched assessment is not admitted",
        denied_state.get("assessment_admission")
        == {"status": "stale", "reason": "binding.mismatch"}
        and "triage_recommendation" not in denied_state
        and not rc.assessment_current_admitted(denied_state),
    )
    check(
        "control: a non-admitted advisory keeps analysis without action copy",
        "- **Summary:** Adds one bounded helper." in denied
        and "### Recommended action" not in denied
        and "Recommended next step" not in denied
        and "<!-- opt:accept-recommendation -->" not in denied,
    )


def test_issue_and_pr_paths_stay_coherent():
    issue_item = {
        "repo": "firstmate",
        "number": 771,
        "kind": "issue-triage",
        "head_sha": "",
        "updated_at": "2026-07-24T09:00:00Z",
        "title": "Bulk export request",
        "author": "contributor",
        "bucket": "issue-triage",
        "comp": "n/a",
        "tests": "n/a",
        "priority": "low",
        "url": "https://github.com/kunchenguid/firstmate/issues/771",
        "summary": "open issue, no linked PR",
    }
    plain = rc.render(issue_item, owner="kunchenguid")["body"]
    check(
        "issue: a pre-triage issue card has no recommendation section",
        "### Recommended action" not in plain,
    )
    triaged = rc.render(
        dict(
            issue_item,
            triage={
                "summary": "Wants a bulk export option.",
                "product_implications": "Modest ask.",
                "evidence": "target.txt: quoted a line from the issue",
                "recommended_action": "close",
                "recommended_reason": "Duplicate of kunchenguid/firstmate#12.",
            },
        ),
        owner="kunchenguid",
    )["body"]
    check(
        "issue: a structured issue recommendation renders once, canonically",
        triaged.count("### Recommended action") == 1
        and "- **Agent recommendation:** `close`" in triaged
        and "<!-- opt:accept-recommendation -->" in triaged
        and "Recommended next step" not in triaged,
    )
    legacy = rc.render(
        dict(
            issue_item,
            triage={
                "summary": "Wants a bulk export option.",
                "product_implications": "Modest ask.",
                "evidence": "target.txt: quoted a line from the issue",
                "recommended_next_step": "discuss - worth a maintainer opinion.",
            },
        ),
        owner="kunchenguid",
    )["body"]
    check(
        "issue: legacy free-text next-step is never presented as a recommendation",
        "### Recommended action" not in legacy
        and "Recommended next step" not in legacy
        and "- **Summary:** Wants a bulk export option." in legacy,
    )
    check(
        "producers: the deterministic recommendation generators are gone",
        not hasattr(core, "_recommendation")
        and not hasattr(target_reconcile, "_TERMINAL_RECOMMENDATIONS"),
    )
    check(
        "producers: a normalized ingest item carries no recommendation field",
        "recommendation"
        not in build_item.normalize(
            {"repo": "firstmate", "number": 771, "kind": "issue-triage"}
        ),
    )
    check(
        "producers: lifecycle and replay items carry no recommendation field",
        '"recommendation": "Await the next qualifying scheduled observation."'
        not in (ROOT / "scripts" / "render_card.py").read_text(encoding="utf-8")
        and '"recommendation": "Needs your call."'
        not in TRIAGE_REPLAY.read_text(encoding="utf-8"),
    )
    config = WHEELHOUSE_CONFIG.read_text(encoding="utf-8")
    check(
        "config: operator guidance names only the canonical recommendation surface",
        "Recommended next step" not in config
        and "single Recommended action section" in config
        and "conditional admitted Recommended action section" in config,
    )


def test_render_version_migration_heals_an_existing_card():
    """A card-1746-shaped v11 card heals on the ordinary render-version refresh.

    No target write, no model spend, and no authority state changes: the
    assessment admission, triage cache, and decision options are identical
    before and after; only the presentation is corrected.
    """
    _obs, _context, item, _delivered, body = card_1746_world()
    state = core.parse_state_block(body)

    # Reconstruct the v11 presentation this card was written with.
    legacy_state = dict(state)
    legacy_state["render_version"] = 11
    legacy = body.replace(
        "- **Product implications:** Straight chore; the CI pin is the only "
        "change.",
        "- **Product implications:** Straight chore; the CI pin is the only "
        "change.\n- **Recommended next step:** merge - Straight chore bump "
        "with all configured checks green.",
    )
    legacy = legacy.replace(
        "\n" + rc.DECISION_START,
        "\n### Recommended action\nMerge - compliance and tests are green.\n\n"
        + rc.DECISION_START,
    )
    legacy = rc._replace_state_block(legacy, legacy_state)
    legacy_parsed = core.parse_state_block(legacy)
    check(
        "migration: the v11 fixture reproduces both legacy surfaces",
        "- **Recommended next step:** merge -" in legacy
        and "Merge - compliance and tests are green." in legacy
        and legacy_parsed["render_version"] == 11,
    )
    check(
        "migration: the stale card is refreshable and render-stale",
        rc.render_stale(legacy_parsed)
        and rc.is_refreshable(["needs-decision"])
        and rc.refresh_needed(item, legacy_parsed, ["needs-decision"]),
    )
    check(
        "migration: the stale card is materially unchanged (display-only)",
        not rc.material_changed(item, legacy_parsed),
    )

    fresh = rc.render(item, owner="kunchenguid")["body"]
    healed = rc._preserve_same_revision_triage(
        fresh, legacy, item, legacy_parsed, owner="kunchenguid"
    )
    healed_state = core.parse_state_block(healed)
    check(
        "migration: the deterministic recommendation section is gone",
        "### Recommended action" not in healed
        and "Merge - compliance and tests are green." not in healed,
    )
    check(
        "migration: the cached advisory action bullet is gone",
        "Recommended next step" not in healed,
    )
    check(
        "migration: cached analysis and honest warnings survive",
        "- **Summary:** Bumps the pinned Treehouse version to v2.1.0." in healed
        and "Primary model validation failed (`output.schema_invalid`)" in healed
        and "The advisory assessment was not admitted "
        "(`basis.missing_or_invalid`)." in healed,
    )
    check(
        "migration: authority state is byte-identical",
        {
            key: healed_state.get(key)
            for key in (
                "triage_status",
                "triaged_sha",
                "triage_recommendation",
                "assessment_admission",
                rc.ASSESSMENT_FIELD,
                rc.TRIAGE_PRIMARY_STATUS_FIELD,
                rc.TRIAGE_PRIMARY_ERROR_FIELD,
                rc.TRIAGE_CONSUMPTION_FIELD,
                "options",
            )
        }
        == {
            key: legacy_parsed.get(key)
            for key in (
                "triage_status",
                "triaged_sha",
                "triage_recommendation",
                "assessment_admission",
                rc.ASSESSMENT_FIELD,
                rc.TRIAGE_PRIMARY_STATUS_FIELD,
                rc.TRIAGE_PRIMARY_ERROR_FIELD,
                rc.TRIAGE_CONSUMPTION_FIELD,
                "options",
            )
        },
    )
    check(
        "migration: G6 rows are unchanged by the presentation correction",
        auto_merge.fresh_verdict_facts(healed_state, CARD_1746_HEAD)[0]
        == auto_merge.fresh_verdict_facts(legacy_parsed, CARD_1746_HEAD)[0],
    )
    check(
        "migration: the card is stamped with the current render version",
        healed_state["render_version"] == rc.CARD_RENDER_VERSION
        and rc.CARD_RENDER_VERSION == 15,
    )
    check(
        "migration: no fresh triage is queued for the same revision (no spend)",
        not rc.should_auto_triage(item, healed_state, ["needs-decision"], True)
        and healed_state["triaged_sha"] == CARD_1746_HEAD,
    )
    check(
        "migration: the healed card is a no-op on the next scan",
        not rc.render_stale(healed_state)
        and not rc.refresh_needed(item, healed_state, ["needs-decision"]),
    )


# --------------------------------------------------------------------------- #
# Backfill: the sanctioned migration must cover the complete active cohort
# --------------------------------------------------------------------------- #
def _v11(body, extra_state=None):
    """Re-stamp a rendered body with the pre-migration presentation."""
    state = dict(core.parse_state_block(body))
    state["render_version"] = 11
    state.update(extra_state or {})
    return rc._replace_state_block(body, state)


def test_projection_refresh_migrates_an_admitted_card_without_losing_telemetry():
    """The other half of the cohort: cards WITH an admitted assessment.

    `card_projection.plan_card_projection` re-renders `### Triage` from the
    bound assessment instead of lifting the cached section, so this path must
    carry the prior card's honest primary/advisory telemetry across or the
    render-version migration would silently delete it fleet-wide.
    """
    head = "eeee1111ffff2222aaaa3333bbbb4444cccc5555"
    obs = ob.observation(number=555, head=head, base="f" * 40)
    context = ob.context_for(obs, rows=[])
    item = ob.item_for(obs, context)
    written = rc.body_with_triage_result(
        rc.render(item, owner="kunchenguid")["body"],
        head,
        triage=_admitted_payload(obs, context, "merge", "Green checks."),
        owner="kunchenguid",
        base_sha=item["base_sha"],
        primary_error_code="output.schema_invalid",
    )
    state = core.parse_state_block(written)
    legacy = _v11(written).replace(
        "- **Product implications:** Routine internal change.",
        "- **Product implications:** Routine internal change.\n"
        "- **Recommended next step:** merge - Green checks.",
    )
    legacy = legacy.replace(
        "### Recommended action\n\n- **Agent recommendation:** `merge`",
        "### Recommended action\nMerge - compliance and tests are green.\n\n"
        "<!-- legacy -->\n- **Agent recommendation:** `merge`",
    )
    prior = ob.issue_from_projection(
        {"title": "t", "body": legacy, "managed_labels": ["needs-decision"]},
        number=99,
    )
    admitted = rc.assessment_admission.normalize_assessment(
        state.get(rc.ASSESSMENT_FIELD)
    )
    check("backfill: the admitted-assessment fixture is affected", bool(admitted))
    projection = card_projection.plan_card_projection(
        ob.item_for(obs, context, admitted),
        prior=prior,
        cause="migration-current",
        preserve_same_revision=True,
    )
    healed = projection["body"]
    healed_state = core.parse_state_block(healed)
    check(
        "backfill: the admitted card renders exactly one canonical recommendation",
        healed.count("### Recommended action") == 1
        and "- **Agent recommendation:** `merge`" in healed
        and "Merge - compliance and tests are green." not in healed
        and "Recommended next step" not in healed,
    )
    check(
        "backfill: honest primary/advisory telemetry survives the refresh",
        healed_state.get(rc.TRIAGE_PRIMARY_STATUS_FIELD) == "failed"
        and healed_state.get(rc.TRIAGE_PRIMARY_ERROR_FIELD)
        == "output.schema_invalid"
        and healed_state.get(rc.TRIAGE_CONSUMPTION_FIELD) == "advisory",
    )
    check(
        "backfill: current admitted authority does not present historical advisory failure",
        "consumed for advisory triage" not in healed
        and "Tick **Accept recommendation**" in healed
        and "<!-- opt:accept-recommendation -->" in healed,
    )
    check(
        "backfill: admitted authority is unchanged by the migration",
        healed_state[rc.ASSESSMENT_FIELD] == state[rc.ASSESSMENT_FIELD]
        and healed_state.get("triage_recommendation")
        == state.get("triage_recommendation")
        and healed_state["triaged_sha"] == head
        and rc.assessment_current_admitted(healed_state),
    )
    check(
        "backfill: the migration is a real projection write, not a noop",
        projection["cause"] == "migration-current"
        and "body" in projection["changed_sections"],
    )
    check(
        "backfill: a second pass over the healed card is a noop",
        card_projection.plan_card_projection(
            ob.item_for(obs, context, admitted),
            prior=ob.issue_from_projection(
                {
                    "title": projection["title"],
                    "body": healed,
                    "managed_labels": projection["managed_labels"],
                },
                number=99,
            ),
            preserve_same_revision=True,
        )["cause"]
        == "noop",
    )


def test_recommendation_census_classifies_the_complete_cohort():
    _obs, _context, _item, _delivered, current = card_1746_world()
    legacy_both = _v11(current).replace(
        "- **Product implications:** Straight chore; the CI pin is the only "
        "change.",
        "- **Product implications:** Straight chore; the CI pin is the only "
        "change.\n- **Recommended next step:** merge - green.",
    ).replace(
        "\n" + rc.DECISION_START,
        "\n### Recommended action\nMerge - compliance and tests are green.\n\n"
        + rc.DECISION_START,
    )

    head = "cccc1111dddd2222eeee3333ffff4444aaaa5555"
    obs, context, _item2, base = _pr_card(head=head, number=44)
    canonical = rc.body_with_triage_result(
        base,
        head,
        triage=_admitted_payload(obs, context, "merge", "Green checks."),
        owner="kunchenguid",
    )

    check(
        "census: a card-1746-shaped body reports both retired surfaces",
        rc.legacy_recommendation_presentation(legacy_both)
        == (rc.LEGACY_ADVISORY_NEXT_STEP, rc.LEGACY_DETERMINISTIC_RECOMMENDATION),
    )
    check(
        "census: the canonical admitted section is never reported as legacy",
        rc.legacy_recommendation_presentation(canonical) == (),
    )
    check(
        "census: a already-migrated basis-denied card reports nothing",
        rc.legacy_recommendation_presentation(current) == (),
    )

    cards = [
        {
            "number": 1746,
            "url": "https://github.com/kunchenguid/wheelhouse/issues/1746",
            "body": legacy_both,
            "labels": [{"name": "needs-decision"}],
        },
        {
            "number": 1747,
            "url": "https://github.com/kunchenguid/wheelhouse/issues/1747",
            "body": canonical,
            "labels": [{"name": "needs-decision"}],
        },
        {
            "number": 1748,
            "url": "https://github.com/kunchenguid/wheelhouse/issues/1748",
            "body": legacy_both,
            "labels": [{"name": "needs-decision"}, {"name": "blocked"}],
        },
        {
            "number": 1749,
            "url": "https://github.com/kunchenguid/wheelhouse/issues/1749",
            "body": "no state block here",
            "labels": [{"name": "needs-decision"}],
        },
    ]
    report = rc.recommendation_census(cards)
    check(
        "census: every row lands in exactly one bucket over the full list",
        report["total"] == 4
        and len(report["affected"]) + report["clean"] + len(report["skipped"]) == 4,
    )
    check(
        "census: only the refreshable affected card is queued for migration",
        [row["number"] for row in report["affected"]] == [1746]
        and report["affected"][0]["migrates_on_refresh"] is True
        and report["affected"][0]["render_version"] == 11
        and report["affected"][0]["url"].endswith("/1746"),
    )
    check(
        "census: an in-flight decision is reported with a reason, never rewritten",
        any(
            row["number"] == 1748 and "not refreshable" in row["reason"]
            for row in report["skipped"]
        ),
    )
    check(
        "census: a non-card issue is reported, not counted as clean",
        report["clean"] == 1
        and any(
            row["number"] == 1749
            and row["reason"] == "not a pr-review decision card"
            for row in report["skipped"]
        ),
    )
    check(
        "census: the helper performs no GitHub call and no write",
        "gh" not in rc.recommendation_census.__code__.co_names
        and "_edit_issue_body_and_labels"
        not in rc.recommendation_census.__code__.co_names,
    )


def main():
    prior_owner = os.environ.get("GITHUB_REPOSITORY_OWNER")
    os.environ["GITHUB_REPOSITORY_OWNER"] = "kunchenguid"
    try:
        test_card_1746_candidate_remains_schema_invalid()
        test_card_1746_render_has_no_conflicting_recommendation()
        test_card_1746_g6_evidence_is_truthful()
        test_pr_triage_prompt_states_the_exact_basis_contract()
        test_pr_triage_prompt_requires_optin_default_off_everywhere()
        test_admitted_recommendation_is_the_one_canonical_surface()
        test_non_admitted_and_pre_triage_cards_present_no_recommendation()
        test_issue_and_pr_paths_stay_coherent()
        test_render_version_migration_heals_an_existing_card()
        test_projection_refresh_migrates_an_admitted_card_without_losing_telemetry()
        test_recommendation_census_classifies_the_complete_cohort()
    finally:
        if prior_owner is None:
            os.environ.pop("GITHUB_REPOSITORY_OWNER", None)
        else:
            os.environ["GITHUB_REPOSITORY_OWNER"] = prior_owner
    if FAILURES:
        print("\n%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        sys.exit(1)
    print("\nall canonical-recommendation tests passed")


if __name__ == "__main__":
    main()
