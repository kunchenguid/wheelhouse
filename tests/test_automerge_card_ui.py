#!/usr/bin/env python3
"""Offline end-to-end coverage for decision-card auto-merge criteria UI.

Run: python tests/test_automerge_card_ui.py
"""

import os
import sys
from contextlib import ExitStack
from unittest.mock import patch

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)
import auto_merge as am  # noqa: E402
import automerge_criteria as schema  # noqa: E402
import render_card  # noqa: E402
import wheelhouse_core as core  # noqa: E402

_failures = []
HEAD = "a" * 40
BASE = "b" * 40
VISION = "vsha"


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        _failures.append(name)


def item(**overrides):
    value = {
        "repo": "axi",
        "number": 96,
        "kind": "pr-review",
        "bucket": "merge-ready",
        "head_sha": HEAD,
        "updated_at": "2026-07-13T16:27:26Z",
        "title": "docs: add jj-axi to community catalog",
        "author": "aivv73",
        "comp": "pass",
        "tests": "green",
        "url": "https://github.com/kunchenguid/axi/pull/96",
        "summary": "compliance=pass tests=green",
        "recommendation": "Merge - compliance and tests are green.",
        "priority": "med",
        "auto_triage": True,
    }
    value.update(overrides)
    return value


def verdict(**overrides):
    value = {
        "behavior_class": "A",
        "aligns_with_vision": True,
        "changes_existing_or_default_behavior": False,
        "recommend_merge": True,
        "optin_default_off": False,
        "vision_sha": VISION,
        "base_sha": BASE,
    }
    value.update(overrides)
    return value


def card_entry(**state_overrides):
    state = {
        "repo": "axi",
        "number": 96,
        "kind": "pr-review",
        "head_sha": HEAD,
        "triaged_sha": HEAD,
        "triage_status": "succeeded",
        "triage_recommendation": {"action": "merge", "reason": ""},
        "automerge_verdict": verdict(),
    }
    state.update(state_overrides)
    return {
        "issue": 623,
        "state": state,
        "labels": {
            "needs-decision",
            "processing",
            am.AUTO_MERGE_CLAIM_LABEL,
            "repo:axi",
            "kind:pr-review",
            "priority:med",
            "target:axi-96",
        },
    }


def live_pr(**overrides):
    value = {
        "head": {"sha": HEAD},
        "base": {"sha": BASE},
        "mergeable": True,
        "mergeable_state": "clean",
        "additions": 20,
        "deletions": 0,
        "changed_files": 3,
        "user": {"login": "aivv73", "type": "User"},
        "labels": [],
        "merged": False,
        "state": "open",
    }
    value.update(overrides)
    return value


def evaluate(
    *,
    item_value=None,
    card_value="default",
    repo_cfg=None,
    token=True,
    vision=(True, VISION),
    pr_value=None,
    prior=True,
    files=None,
    maintainers=None,
    full=True,
    require_claim=True,
):
    item_value = item_value or item()
    card_value = card_entry() if card_value == "default" else card_value
    pr_value = live_pr() if pr_value is None else pr_value
    files = (["README.md", "catalog.yaml", "docs/index.html"], True, True) if files is None else files
    old_token = os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN")
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true" if token else "false"
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(am, "vision_on_default_branch", return_value=vision))
            stack.enter_context(patch.object(am, "live_pr", return_value=pr_value))
            stack.enter_context(patch.object(am, "has_prior_merged_pr", return_value=prior))
            stack.enter_context(patch.object(am, "immutable_compare_files", return_value=files))
            return am.evaluate_candidate(
                "kunchenguid",
                item_value,
                card_value,
                {"auto_merge": True} if repo_cfg is None else repo_cfg,
                True,
                {"kunchenguid"} if maintainers is None else maintainers,
                full_evaluation=full,
                require_claim=require_claim,
            )
    finally:
        if old_token is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = old_token


def rows(result):
    return {row["id"]: row for row in result["criteria"]}


def test_all_preflight_criteria_met_and_action_semantics_unchanged():
    action = evaluate(full=False)
    check("positive: authoritative action evaluator remains eligible", action["eligible"] is True)
    visible = rows(action)
    preflight = [key for key in schema.CRITERIA_IDS if key != "g7_immediate_recheck"]
    check(
        "positive: every G0-G6 and safety preflight criterion is MET",
        all(visible[key]["status"] == schema.STATUS_MET for key in preflight),
    )
    check(
        "positive: G7 is explicitly unavailable until the immediate act boundary",
        visible["g7_immediate_recheck"]["status"] == schema.STATUS_UNAVAILABLE,
    )


def test_each_guarded_criterion_has_a_fail_closed_negative():
    cases = [
        (
            "scope_candidate",
            {"item_value": item(bucket="review-needed", tests="pending")},
        ),
        ("g0_repo_enabled", {"repo_cfg": {"auto_merge": False}}),
        ("g0_vision_present", {"vision": (False, "")}),
        ("g1_card_identity", {"card_value": None}),
        ("g1_card_published", {"card_value": card_entry(held=True)}),
        (
            "g1_card_claim",
            {
                "card_value": dict(
                    card_entry(),
                    labels={
                        "needs-decision",
                        "repo:axi",
                        "kind:pr-review",
                        "priority:med",
                        "target:axi-96",
                    },
                )
            },
        ),
        ("g2_files_complete", {"files": ([], False, False)}),
        (
            "g2_exclusions_clear",
            {"files": ([".github/workflows/ci.yml"], True, True)},
        ),
        (
            "g3_author_identity",
            {"pr_value": live_pr(user={"login": "kunchenguid", "type": "User"})},
        ),
        ("g3_prior_merge", {"prior": False}),
        (
            "g4_checks_green",
            {"item_value": item(bucket="review-needed", comp="pass", tests="pending")},
        ),
        ("g4_mergeable", {"pr_value": live_pr(mergeable=None)}),
        ("g4_clean", {"pr_value": live_pr(mergeable_state="unstable")}),
        ("g5_file_limit", {"pr_value": live_pr(changed_files=21)}),
        ("g5_line_limit", {"pr_value": live_pr(additions=1001)}),
        ("g6_triage_available", {"token": False}),
        (
            "g6_triage_success",
            {"card_value": card_entry(triage_status="queued")},
        ),
        (
            "g6_merge_recommendation",
            {"card_value": card_entry(triage_recommendation={"action": "hold", "reason": ""})},
        ),
        (
            "g6_behavior_class",
            {"card_value": card_entry(automerge_verdict=verdict(behavior_class="D"))},
        ),
        (
            "g6_vision_alignment",
            {"card_value": card_entry(automerge_verdict=verdict(aligns_with_vision=False))},
        ),
        (
            "g6_default_behavior",
            {
                "card_value": card_entry(
                    automerge_verdict=verdict(changes_existing_or_default_behavior=True)
                )
            },
        ),
        (
            "g6_verdict_merge",
            {"card_value": card_entry(automerge_verdict=verdict(recommend_merge=False))},
        ),
        (
            "g6_class_c_mode",
            {
                "card_value": card_entry(
                    automerge_verdict=verdict(
                        behavior_class="C", optin_default_off=False
                    )
                )
            },
        ),
        (
            "g6_vision_revision",
            {"card_value": card_entry(automerge_verdict=verdict(vision_sha="old"))},
        ),
        (
            "g6_base_revision",
            {"card_value": card_entry(automerge_verdict=verdict(base_sha="c" * 40))},
        ),
        ("safety_target_open", {"pr_value": live_pr(state="closed")}),
        (
            "safety_escape_hatch",
            {
                "pr_value": live_pr(
                    labels=[{"name": core.NO_AUTO_MERGE_LABEL}]
                )
            },
        ),
        (
            "safety_head_current",
            {"pr_value": live_pr(head={"sha": "d" * 40})},
        ),
    ]
    for criterion, kwargs in cases:
        status = rows(evaluate(**kwargs))[criterion]["status"]
        check(
            "negative: %s is never displayed MET when its guard fails" % criterion,
            status in (schema.STATUS_UNMET, schema.STATUS_UNAVAILABLE),
        )


def test_owner_bot_and_security_exclusion_evidence_is_distinct():
    owner_result = rows(
        evaluate(pr_value=live_pr(user={"login": "kunchenguid", "type": "User"}))
    )["g3_author_identity"]
    bot_result = rows(
        evaluate(pr_value=live_pr(user={"login": "catalog-bot", "type": "Bot"}))
    )["g3_author_identity"]
    history_result = rows(evaluate(prior=False))["g3_prior_merge"]
    workflow_result = rows(
        evaluate(files=([".github/workflows/ci.yml"], True, True))
    )["g2_exclusions_clear"]
    security_result = rows(evaluate(files=(["src/auth/session.py"], True, True)))[
        "g2_exclusions_clear"
    ]
    check("identity: owner is explicitly ineligible", "bot/maintainer" in owner_result["evidence"])
    check("identity: Bot typename is explicitly ineligible", "bot/maintainer" in bot_result["evidence"])
    check("identity: missing contributor history has its own row", "no prior merged PR" in history_result["evidence"])
    check("exclusions: workflow path is evidenced", ".github/workflows/ci.yml" in workflow_result["evidence"])
    check("exclusions: security/auth path is evidenced", "authentication:src/auth/session.py" in security_result["evidence"])


def test_unknown_evidence_and_historical_cards_degrade_safely():
    normalized = schema.normalize_criteria([{"id": "g4_mergeable", "status": "maybe"}])
    normalized_rows = {row["id"]: row for row in normalized}
    check(
        "compat: malformed status becomes explicit UNAVAILABLE",
        normalized_rows["g4_mergeable"]["status"] == schema.STATUS_UNAVAILABLE,
    )
    old_card = render_card.render(item())
    check("compat: old item still renders the complete criteria section", old_card["body"].count("**UNAVAILABLE**") == len(schema.CRITERIA_IDS))
    old_state = core.parse_state_block(old_card["body"])
    check("compat: absent historical criteria never become trusted state", render_card.AUTOMERGE_CRITERIA_FIELD not in old_state)


def test_authoritative_scan_snapshot_flows_into_true_card_render():
    base_card = render_card.render(item())
    state = core.parse_state_block(base_card["body"])
    state.update(card_entry()["state"])
    raw_card = {
        "number": 623,
        "body": render_card._replace_state_block(base_card["body"], state),
        "labels": [
            {"name": name}
            for name in (
                "needs-decision",
                "repo:axi",
                "kind:pr-review",
                "priority:med",
                "target:axi-96",
            )
        ],
        "author": am.CARD_AUTOMATION_AUTHOR,
        "comments": 0,
    }
    scan = {
        "repos": {
            "axi": {
                "ok": True,
                "truncated": False,
                "indeterminate_pr_numbers": [],
            }
        },
        "items": [item()],
    }
    old_token = os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN")
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true"
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(core, "get_owner", return_value="kunchenguid"))
            stack.enter_context(patch.object(core, "maintainers", return_value={"kunchenguid"}))
            stack.enter_context(
                patch.object(
                    core,
                    "load_config",
                    return_value={"auto_merge": True, "repos": {"axi": {}}},
                )
            )
            stack.enter_context(patch.object(am, "vision_on_default_branch", return_value=(True, VISION)))
            stack.enter_context(patch.object(am, "live_pr", return_value=live_pr()))
            stack.enter_context(patch.object(am, "has_prior_merged_pr", return_value=True))
            stack.enter_context(
                patch.object(
                    am,
                    "immutable_compare_files",
                    return_value=(["README.md", "catalog.yaml", "docs/index.html"], True, True),
                )
            )
            handoff = am.collect_card_criteria(scan, [raw_card])
            scan["repos"]["axi"]["ok"] = False
            unhealthy_handoff = am.collect_card_criteria(scan, [raw_card])
    finally:
        if old_token is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = old_token
    rendered = render_card.render(
        item(automerge_criteria=handoff[0]["criteria"])
    )
    check("flow: scan evaluator emits one head-bound criterion record", len(handoff) == 1 and handoff[0]["head_sha"] == HEAD)
    check("flow: card render consumes the evaluator record", "✅ **MET** `G3 - prior merged contribution in this repo`" in rendered["body"])
    check("flow: pre-claim snapshot is explicit rather than silently eligible", "⚪ **UNAVAILABLE** `G1 - exclusive card claim`" in rendered["body"])
    unhealthy_rows = {
        row["id"]: row for row in unhealthy_handoff[0]["criteria"]
    }
    check(
        "flow: unhealthy scan evidence is explicitly UNAVAILABLE",
        unhealthy_rows["scan_complete"]["status"]
        == schema.STATUS_UNAVAILABLE,
    )


def test_true_card_render_path_shows_stable_human_visible_rows():
    result = evaluate()
    rendered = render_card.render(
        item(automerge_criteria=result["criteria"]),
        held=True,
    )
    body = rendered["body"]
    state = core.parse_state_block(body)
    check("render: card has the criteria heading", "### Auto-merge criteria" in body)
    check("render: stable MET label is visible", "✅ **MET** `G0 - repository auto-merge enabled`" in body)
    check("render: stable UNAVAILABLE label is visible", "⚪ **UNAVAILABLE** `G7 - immediate live recheck and manual merge gate`" in body)
    check("render: every stable criterion gets exactly one visible row", all(body.count("`%s`" % label) == 1 for _, label in schema.CRITERIA_SPECS))
    check("render: structured rows round-trip in non-material state", state.get(render_card.AUTOMERGE_CRITERIA_FIELD) == schema.normalize_criteria(result["criteria"]))
    check("render: criteria never enter material fields", render_card.AUTOMERGE_CRITERIA_FIELD not in render_card.MATERIAL_FIELDS)


def test_displayed_met_rows_cannot_grant_eligibility():
    forged = schema.normalize_criteria(
        [
            {"id": criterion_id, "status": schema.STATUS_MET, "evidence": "claimed met"}
            for criterion_id in schema.CRITERIA_IDS
        ]
    )
    actual = card_entry()
    actual["state"] = dict(actual["state"], automerge_verdict=None, automerge_criteria=forged)
    result = evaluate(card_value=actual, full=False)
    check("security: displayed MET rows do not grant eligibility", result["eligible"] is False)
    check("security: live persisted verdict guard still holds", result["hold_reason"].startswith("G6 no structured behavior verdict"))


def test_axi_pr96_shape_surfaces_after_ci_wait_with_honest_evidence():
    # Production #96 shape: safe 3-file docs/catalog PR, green checks, first-time
    # contributor, and no pre-existing card on the auto-approve scan.
    result = evaluate(card_value=None, prior=False, require_claim=False)
    criterion_rows = rows(result)
    check("axi#96: absent card is explicit UNMET, not a silent disappearance", criterion_rows["g1_card_identity"]["status"] == schema.STATUS_UNMET)
    check("axi#96: first-time contributor history is explicit UNMET", criterion_rows["g3_prior_merge"]["status"] == schema.STATUS_UNMET)
    check("axi#96: safe docs/catalog paths clear exclusions", criterion_rows["g2_exclusions_clear"]["status"] == schema.STATUS_MET)
    check("axi#96: 3 files and 20 lines clear both blast limits", criterion_rows["g5_file_limit"]["status"] == schema.STATUS_MET and criterion_rows["g5_line_limit"]["status"] == schema.STATUS_MET)
    rendered = render_card.render(item(automerge_criteria=result["criteria"]), held=True)
    check("axi#96: real card render carries target and criterion UI", "[axi#96]" in rendered["title"] and "G3 - prior merged contribution in this repo" in rendered["body"])


def test_criteria_changes_refresh_ui_without_becoming_material():
    first = evaluate()
    first_item = item(automerge_criteria=first["criteria"])
    state = core.parse_state_block(render_card.render(first_item)["body"])
    changed = schema.normalize_criteria(first["criteria"])
    for row in changed:
        if row["id"] == "g3_prior_merge":
            row["status"] = schema.STATUS_UNMET
            row["evidence"] = "history changed"
    next_item = item(automerge_criteria=changed)
    check("refresh: changed criterion evidence triggers display refresh", render_card.automerge_criteria_stale(next_item, state) is True)
    check("refresh: criterion-only change remains non-material", render_card.material_changed(next_item, state) is False)


def main():
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print()
    if _failures:
        print("FAILURES: %d" % len(_failures))
        for failure in _failures:
            print("  - " + failure)
        sys.exit(1)
    print("all auto-merge card UI tests passed")


if __name__ == "__main__":
    main()
