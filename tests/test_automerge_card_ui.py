#!/usr/bin/env python3
"""Offline end-to-end coverage for decision-card auto-merge criteria UI.

Run: python tests/test_automerge_card_ui.py
"""

import inspect
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


def behavior_admission(behavior_class="A", contradiction=False):
    value = {
        "version": 1,
        "contradicts_existing_contract": contradiction,
    }
    if behavior_class == "B":
        value.update(
            {
                "corrected_defect": "Daemon restart lost an open monitored run.",
                "intended_behavior_restored": (
                    "An open monitored run remains recoverable after restart."
                ),
            }
        )
    return value


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
        "same_closing_issue_overlap": "",
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
    if "behavior_admission" not in overrides:
        value["behavior_admission"] = behavior_admission(value["behavior_class"])
    return value


def independent_verdict(**overrides):
    value = {
        "behavior_class": "A",
        "changes_existing_or_default_behavior": False,
        "optin_default_off": False,
    }
    value.update(overrides)
    if "behavior_admission" not in overrides:
        value["behavior_admission"] = behavior_admission(value["behavior_class"])
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
    files = (
        (["README.md", "catalog.yaml", "docs/index.html"], True, True)
        if files is None
        else files
    )
    old_token = os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN")
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true" if token else "false"
    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(am, "vision_on_default_branch", return_value=vision)
            )
            stack.enter_context(patch.object(am, "live_pr", return_value=pr_value))
            stack.enter_context(
                patch.object(am, "has_prior_merged_pr", return_value=prior)
            )
            stack.enter_context(
                patch.object(am, "immutable_compare_files", return_value=files)
            )
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
    check(
        "positive: authoritative action evaluator remains eligible",
        action["eligible"] is True,
    )
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


def test_reconcile_absence_state_does_not_affect_criteria_or_eligibility():
    baseline = evaluate(full=False)
    with_absence = card_entry(
        reconcile_absence={"version": 1, "threshold": 2, "count": 1}
    )
    actual = evaluate(card_value=with_absence, full=False)
    check(
        "criteria: reconcile absence is non-authoritative",
        actual["eligible"] == baseline["eligible"] is True
        and actual["criteria"] == baseline["criteria"],
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
            {
                "card_value": card_entry(
                    triage_recommendation={"action": "hold", "reason": ""}
                )
            },
        ),
        (
            "g6_behavior_class",
            {"card_value": card_entry(automerge_verdict=verdict(behavior_class="D"))},
        ),
        (
            "g6_vision_alignment",
            {
                "card_value": card_entry(
                    automerge_verdict=verdict(aligns_with_vision=False)
                )
            },
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
            {
                "card_value": card_entry(
                    automerge_verdict=verdict(recommend_merge=False)
                )
            },
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
            {"pr_value": live_pr(labels=[{"name": core.NO_AUTO_MERGE_LABEL}])},
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


def test_wh_aud_05_semantic_denial_is_visible_without_masking_other_gates():
    contradictory = verdict(
        behavior_class="B",
        behavior_admission=behavior_admission("B", contradiction=True),
    )
    result = evaluate(
        card_value=card_entry(automerge_verdict=contradictory),
        full=False,
    )
    criterion_rows = rows(result)
    check(
        "WH-AUD-05 UI: contradictory class B is visibly UNMET and cannot act",
        result["eligible"] is False
        and criterion_rows["g6_behavior_class"]["status"]
        == schema.STATUS_UNMET
        and "contradicts" in criterion_rows["g6_behavior_class"]["evidence"],
    )

    masked = evaluate(
        card_value=card_entry(automerge_verdict=contradictory),
        vision=(False, ""),
        prior=False,
    )
    masked_rows = rows(masked)
    check(
        "WH-AUD-05 controls: G0 and G3 remain independent denials",
        masked["eligible"] is False
        and masked_rows["g0_vision_present"]["status"]
        == schema.STATUS_UNAVAILABLE
        and masked_rows["g3_prior_merge"]["status"] == schema.STATUS_UNMET
        and masked_rows["g6_behavior_class"]["status"] == schema.STATUS_UNMET,
    )

    control_evidence = {
        1622: (
            "Resolved decisions moved to the archive were undetectable.",
            "Durable verification finds retained decisions after archive retention.",
        ),
        1624: (
            "Lifecycle retries failed after normal decision retention.",
            "Complete, verify, and resolve retries work after retention.",
        ),
    }
    for card_number, evidence_pair in control_evidence.items():
        control = verdict(
            behavior_class="B",
            behavior_admission={
                "version": 1,
                "contradicts_existing_contract": False,
                "corrected_defect": evidence_pair[0],
                "intended_behavior_restored": evidence_pair[1],
            },
            changes_existing_or_default_behavior=True,
        )
        control_result = evaluate(
            card_value=card_entry(automerge_verdict=control),
            full=False,
        )
        control_rows = rows(control_result)
        check(
            "class B control %s: independent contract-change denial remains" % card_number,
            control_result["eligible"] is False
            and control_rows["g6_behavior_class"]["status"] == schema.STATUS_MET
            and control_rows["g6_default_behavior"]["status"]
            == schema.STATUS_UNMET,
        )

    historical = verdict(behavior_class="B")
    historical.pop("behavior_admission")
    historical_result = evaluate(
        card_value=card_entry(automerge_verdict=historical),
        full=False,
    )
    check(
        "WH-AUD-05 compatibility: incomplete historical class B is unavailable",
        historical_result["eligible"] is False
        and rows(historical_result)["g6_behavior_class"]["status"]
        == schema.STATUS_UNAVAILABLE,
    )


def test_owner_bot_and_security_exclusion_evidence_is_distinct():
    owner_result = rows(
        evaluate(pr_value=live_pr(user={"login": "kunchenguid", "type": "User"}))
    )["g3_author_identity"]
    bot_result = rows(
        evaluate(pr_value=live_pr(user={"login": "catalog-bot", "type": "Bot"}))
    )["g3_author_identity"]
    history_result = rows(evaluate(prior=False))["g3_prior_merge"]
    workflow_result = rows(evaluate(files=([".github/workflows/ci.yml"], True, True)))[
        "g2_exclusions_clear"
    ]
    security_result = rows(evaluate(files=(["src/auth/session.py"], True, True)))[
        "g2_exclusions_clear"
    ]
    check(
        "identity: owner is explicitly ineligible",
        "bot/maintainer" in owner_result["evidence"],
    )
    check(
        "identity: Bot typename is explicitly ineligible",
        "bot/maintainer" in bot_result["evidence"],
    )
    check(
        "identity: missing contributor history has its own row",
        "no prior merged PR" in history_result["evidence"],
    )
    check(
        "exclusions: workflow path is evidenced",
        ".github/workflows/ci.yml" in workflow_result["evidence"],
    )
    check(
        "exclusions: security/auth path is evidenced",
        "authentication:src/auth/session.py" in security_result["evidence"],
    )


def test_no_vision_complete_diff_keeps_independent_g6_facts_in_real_card_flow():
    issue_head = "f4e096532b994d7a37a1161bdeaf6214e9d6439e"
    issue_base = "b708731dc7840c088bcd8c79991b7f052f9a0096"
    issue_item = item(
        repo="firstmate",
        number=527,
        head_sha=issue_head,
        title="issue 621 regression",
        author="contributor",
        url="https://github.com/kunchenguid/firstmate/pull/527",
    )
    base_card = render_card.render(issue_item)
    state = core.parse_state_block(base_card["body"])
    state.update(
        {
            "repo": "firstmate",
            "number": 527,
            "kind": "pr-review",
            "head_sha": issue_head,
            "triaged_sha": issue_head,
            "triaged_base_sha": issue_base,
            "triage_status": "succeeded",
            "triage_recommendation": {"action": "merge", "reason": ""},
            "automerge_verdict": independent_verdict(),
        }
    )
    raw_card = {
        "number": 621,
        "body": render_card._replace_state_block(base_card["body"], state),
        "labels": [
            {"name": name}
            for name in (
                "needs-decision",
                "repo:firstmate",
                "kind:pr-review",
                "priority:med",
                "target:firstmate-527",
            )
        ],
        "author": am.CARD_AUTOMATION_AUTHOR,
        "comments": 0,
    }
    scan = {
        "repos": {
            "firstmate": {
                "ok": True,
                "truncated": False,
                "indeterminate_pr_numbers": [],
            }
        },
        "items": [issue_item],
    }
    issue_pr = live_pr(
        head={"sha": issue_head},
        base={"sha": issue_base},
        user={"login": "contributor", "type": "User"},
    )
    old_token = os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN")
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true"
    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(core, "get_owner", return_value="kunchenguid")
            )
            stack.enter_context(
                patch.object(core, "maintainers", return_value={"kunchenguid"})
            )
            stack.enter_context(
                patch.object(
                    core,
                    "load_config",
                    return_value={
                        "auto_merge": True,
                        "repos": {"firstmate": {}},
                    },
                )
            )
            stack.enter_context(
                patch.object(am, "vision_on_default_branch", return_value=(False, ""))
            )
            stack.enter_context(patch.object(am, "live_pr", return_value=issue_pr))
            stack.enter_context(
                patch.object(am, "has_prior_merged_pr", return_value=True)
            )
            stack.enter_context(
                patch.object(
                    am,
                    "immutable_compare_files",
                    return_value=(["src/fix.py"], True, True),
                )
            )
            handoff = am.collect_card_criteria(scan, [raw_card])
    finally:
        if old_token is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = old_token

    criterion_rows = {row["id"]: row for row in handoff[0]["criteria"]}
    met_rows = (
        "g6_triage_available",
        "g6_triage_success",
        "g6_merge_recommendation",
        "g6_behavior_class",
        "g6_default_behavior",
        "g6_class_c_mode",
    )
    vision_rows = (
        "g6_vision_alignment",
        "g6_verdict_merge",
        "g6_vision_revision",
        "g6_base_revision",
    )
    check(
        "no VISION: complete-diff triage facts are independently MET",
        all(criterion_rows[key]["status"] == schema.STATUS_MET for key in met_rows),
    )
    check(
        "no VISION: independent facts are not masked as unavailable",
        all(
            criterion_rows[key]["status"] != schema.STATUS_UNAVAILABLE
            for key in (
                "g6_behavior_class",
                "g6_default_behavior",
                "g6_class_c_mode",
            )
        ),
    )
    check(
        "no VISION: vision-bound rows clearly require VISION.md",
        all(
            criterion_rows[key]["status"] == schema.STATUS_UNAVAILABLE
            and "VISION.md" in criterion_rows[key]["evidence"]
            for key in vision_rows
        ),
    )
    rendered = render_card.render(
        item(
            repo="firstmate",
            number=527,
            head_sha=issue_head,
            automerge_criteria=handoff[0]["criteria"],
        )
    )
    body = rendered["body"]
    g6_lines = [line for line in body.splitlines() if "`G6 - " in line]
    check(
        "no VISION: renderer keeps independent facts at the G6 behavior level",
        all(
            next(
                line
                for line in g6_lines
                if "`%s`" % schema.CRITERIA_LABELS[key] in line
            ).startswith("- ")
            for key in (
                "g6_behavior_class",
                "g6_default_behavior",
                "g6_class_c_mode",
            )
        ),
    )
    check(
        "no VISION: renderer nests vision-bound rows under the needs-VISION root",
        "- **VISION.md-dependent checks** - _needs VISION.md_" in body
        and all(
            next(
                line
                for line in g6_lines
                if "`%s`" % schema.CRITERIA_LABELS[key] in line
            ).startswith("    - ⚪ **UNAVAILABLE**")
            for key in vision_rows
        ),
    )
    check(
        "no VISION: complete evaluation remains ineligible",
        am.verdict_eligible(independent_verdict())[0] is False
        and criterion_rows["g0_vision_present"]["status"] == schema.STATUS_UNAVAILABLE,
    )


def test_no_vision_independent_behavior_negatives_are_unmet_not_masked():
    cases = (
        ("g6_default_behavior", {"changes_existing_or_default_behavior": True}),
        (
            "g6_class_c_mode",
            {"behavior_class": "C", "optin_default_off": False},
        ),
    )
    for criterion, overrides in cases:
        result = evaluate(
            card_value=card_entry(automerge_verdict=independent_verdict(**overrides)),
            vision=(False, ""),
        )
        criterion_rows = rows(result)
        check(
            "no VISION: %s is evaluated as UNMET from real facts" % criterion,
            criterion_rows[criterion]["status"] == schema.STATUS_UNMET,
        )
        check(
            "no VISION: %s cannot grant eligibility" % criterion,
            result["eligible"] is False,
        )
        check(
            "no VISION: vision-bound rows remain UNAVAILABLE for %s" % criterion,
            all(
                criterion_rows[key]["status"] == schema.STATUS_UNAVAILABLE
                for key in (
                    "g6_vision_alignment",
                    "g6_verdict_merge",
                    "g6_vision_revision",
                    "g6_base_revision",
                )
            ),
        )


def test_absent_none_and_non_dictionary_whole_verdicts_are_unavailable_dependents():
    absent = object()
    dependent_rows = (
        "g6_vision_alignment",
        "g6_default_behavior",
        "g6_verdict_merge",
        "g6_class_c_mode",
        "g6_vision_revision",
        "g6_base_revision",
    )
    for label, whole_verdict in (
        ("absent", absent),
        ("none", None),
        ("string", "merge"),
        ("list", ["merge"]),
    ):
        card = card_entry()
        if whole_verdict is absent:
            card["state"].pop("automerge_verdict", None)
        else:
            card["state"]["automerge_verdict"] = whole_verdict
        result = evaluate(card_value=card, vision=(True, VISION))
        criterion_rows = rows(result)
        check(
            "whole verdict %s: behavior class remains the blocking UNMET row" % label,
            criterion_rows["g6_behavior_class"]["status"] == schema.STATUS_UNMET
            and result["hold_reason"] == "G6 no structured behavior verdict",
        )
        check(
            "whole verdict %s: dependent and binding rows are UNAVAILABLE" % label,
            all(
                criterion_rows[key]["status"] == schema.STATUS_UNAVAILABLE
                and "not evaluated" in criterion_rows[key]["evidence"]
                for key in dependent_rows
            ),
        )


def test_structured_verdict_field_failures_remain_unmet():
    cases = (
        ("behavior_class", "g6_behavior_class", "D", {}),
        ("aligns_with_vision", "g6_vision_alignment", "true", {}),
        (
            "changes_existing_or_default_behavior",
            "g6_default_behavior",
            "false",
            {},
        ),
        ("recommend_merge", "g6_verdict_merge", "true", {}),
        (
            "optin_default_off",
            "g6_class_c_mode",
            "true",
            {"behavior_class": "C"},
        ),
        ("vision_sha", "g6_vision_revision", None, {}),
        ("base_sha", "g6_base_revision", "not-a-sha", {}),
    )
    for field, criterion, malformed, overrides in cases:
        for shape in ("missing", "malformed"):
            candidate = verdict(**overrides)
            if shape == "missing":
                candidate.pop(field, None)
            else:
                candidate[field] = malformed
            result = evaluate(
                card_value=card_entry(automerge_verdict=candidate),
                vision=(True, VISION),
            )
            check(
                "structured verdict %s %s remains genuinely UNMET" % (field, shape),
                rows(result)[criterion]["status"] == schema.STATUS_UNMET,
            )


def test_real_verdict_negative_and_stale_facts_remain_unmet():
    negative_cases = (
        (
            "g6_vision_alignment",
            card_entry(automerge_verdict=verdict(aligns_with_vision=False)),
            (True, VISION),
        ),
        (
            "g6_default_behavior",
            card_entry(
                automerge_verdict=verdict(changes_existing_or_default_behavior=True)
            ),
            (True, VISION),
        ),
        (
            "g6_verdict_merge",
            card_entry(automerge_verdict=verdict(recommend_merge=False)),
            (True, VISION),
        ),
        (
            "g6_class_c_mode",
            card_entry(
                automerge_verdict=verdict(behavior_class="C", optin_default_off=False)
            ),
            (True, VISION),
        ),
        (
            "g6_vision_revision",
            card_entry(automerge_verdict=verdict(vision_sha="stale-vision")),
            (True, VISION),
        ),
        (
            "g6_base_revision",
            card_entry(automerge_verdict=verdict(base_sha="c" * 40)),
            (True, VISION),
        ),
        (
            "g6_triage_success",
            card_entry(triaged_sha="stale-head"),
            (True, VISION),
        ),
    )
    for criterion, card, vision_value in negative_cases:
        result = evaluate(card_value=card, vision=vision_value)
        check(
            "real verdict negative/stale fact %s remains UNMET" % criterion,
            rows(result)[criterion]["status"] == schema.STATUS_UNMET,
        )

    for behavior_class, optin in (("A", False), ("B", False), ("C", True)):
        candidate = verdict(
            behavior_class=behavior_class,
            optin_default_off=optin,
        )
        result = evaluate(
            card_value=card_entry(automerge_verdict=candidate),
            full=False,
        )
        check(
            "real eligible class %s retains authorization behavior" % behavior_class,
            result["eligible"] is True,
        )


def test_unknown_evidence_and_historical_cards_degrade_safely():
    normalized = schema.normalize_criteria([{"id": "g4_mergeable", "status": "maybe"}])
    normalized_rows = {row["id"]: row for row in normalized}
    check(
        "compat: malformed status becomes explicit UNAVAILABLE",
        normalized_rows["g4_mergeable"]["status"] == schema.STATUS_UNAVAILABLE,
    )
    old_card = render_card.render(item())
    check(
        "compat: old item still renders the complete criteria section",
        old_card["body"].count("**UNAVAILABLE**") == len(schema.CRITERIA_IDS),
    )
    old_state = core.parse_state_block(old_card["body"])
    check(
        "compat: absent historical criteria never become trusted state",
        render_card.AUTOMERGE_CRITERIA_FIELD not in old_state,
    )


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
            stack.enter_context(
                patch.object(core, "get_owner", return_value="kunchenguid")
            )
            stack.enter_context(
                patch.object(core, "maintainers", return_value={"kunchenguid"})
            )
            stack.enter_context(
                patch.object(
                    core,
                    "load_config",
                    return_value={"auto_merge": True, "repos": {"axi": {}}},
                )
            )
            stack.enter_context(
                patch.object(
                    am, "vision_on_default_branch", return_value=(True, VISION)
                )
            )
            stack.enter_context(patch.object(am, "live_pr", return_value=live_pr()))
            stack.enter_context(
                patch.object(am, "has_prior_merged_pr", return_value=True)
            )
            stack.enter_context(
                patch.object(
                    am,
                    "immutable_compare_files",
                    return_value=(
                        ["README.md", "catalog.yaml", "docs/index.html"],
                        True,
                        True,
                    ),
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
    rendered = render_card.render(item(automerge_criteria=handoff[0]["criteria"]))
    check(
        "flow: scan evaluator emits one head-bound criterion record",
        len(handoff) == 1 and handoff[0]["head_sha"] == HEAD,
    )
    check(
        "flow: card render consumes the evaluator record",
        "✅ **MET** `G3 - prior merged contribution in this repo`" in rendered["body"],
    )
    check(
        "flow: pre-claim snapshot is explicit rather than silently eligible",
        "⚪ **UNAVAILABLE** `G1 - exclusive card claim`" in rendered["body"],
    )
    unhealthy_rows = {row["id"]: row for row in unhealthy_handoff[0]["criteria"]}
    check(
        "flow: unhealthy scan evidence is explicitly UNAVAILABLE",
        unhealthy_rows["scan_complete"]["status"] == schema.STATUS_UNAVAILABLE,
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
    check(
        "render: stable MET label is visible",
        "✅ **MET** `G0 - repository auto-merge enabled`" in body,
    )
    check(
        "render: stable UNAVAILABLE label is visible",
        "⚪ **UNAVAILABLE** `G7 - immediate live recheck and manual merge gate`"
        in body,
    )
    check(
        "render: every stable criterion gets exactly one visible row",
        all(body.count("`%s`" % label) == 1 for _, label in schema.CRITERIA_SPECS),
    )
    check(
        "render: structured rows round-trip in non-material state",
        state.get(render_card.AUTOMERGE_CRITERIA_FIELD)
        == schema.normalize_criteria(result["criteria"]),
    )
    check(
        "render: criteria never enter material fields",
        render_card.AUTOMERGE_CRITERIA_FIELD not in render_card.MATERIAL_FIELDS,
    )


def test_render_groups_every_criterion_by_id_family_and_preserves_evidence():
    family_prefixes = (
        ("Scope", ("scope_",)),
        ("Safety", ("scan_", "safety_")),
        ("G0 (repo)", ("g0_",)),
        ("G1 (card)", ("g1_",)),
        ("G2 (files)", ("g2_",)),
        ("G3 (author)", ("g3_",)),
        ("G4 (checks)", ("g4_",)),
        ("G5 (size)", ("g5_",)),
        ("G6 (triage + behavior)", ("g6_",)),
        ("G7 (final gate)", ("g7_",)),
    )
    rows_with_evidence = [
        {
            "id": criterion_id,
            "status": schema.STATUS_MET,
            "evidence": "proof-%02d" % index,
        }
        for index, criterion_id in enumerate(schema.CRITERIA_IDS)
    ]
    body = "\n".join(render_card._automerge_criteria_section(rows_with_evidence))
    headings = ["#### %s" % family for family, _ in family_prefixes] + ["#### Other"]

    for index, (family, prefixes) in enumerate(family_prefixes):
        start = body.index("#### %s" % family)
        later = [
            position
            for position in (
                body.find(heading, start + 1) for heading in headings[index + 1 :]
            )
            if position >= 0
        ]
        end = min(later) if later else len(body)
        section = body[start:end]
        expected_ids = [
            criterion_id
            for criterion_id in schema.CRITERIA_IDS
            if criterion_id.startswith(prefixes)
        ]
        check(
            "group: %s contains exactly its ID-prefix criteria" % family,
            all(
                "`%s`" % schema.CRITERIA_LABELS[criterion_id] in section
                for criterion_id in expected_ids
            )
            and all(
                "`%s`" % schema.CRITERIA_LABELS[criterion_id] not in section
                for criterion_id in schema.CRITERIA_IDS
                if criterion_id not in expected_ids
            ),
        )

    check(
        "render: every criterion keeps its concise evidence",
        all("proof-%02d" % index in body for index in range(len(schema.CRITERIA_IDS))),
    )


def test_g6_producer_hierarchy_splits_independent_and_vision_bound_facts():
    result = evaluate(
        card_value=card_entry(automerge_verdict=independent_verdict()),
        vision=(False, ""),
    )
    body = "\n".join(render_card._automerge_criteria_section(result["criteria"]))
    independent_ids = (
        "g6_behavior_class",
        "g6_default_behavior",
        "g6_class_c_mode",
    )
    vision_ids = (
        "g6_vision_alignment",
        "g6_verdict_merge",
        "g6_vision_revision",
        "g6_base_revision",
    )
    check(
        "hierarchy: complete-diff behavior facts stay at the G6 group level",
        all(
            next(
                line
                for line in body.splitlines()
                if "`%s`" % schema.CRITERIA_LABELS[criterion_id] in line
            ).startswith("- ")
            for criterion_id in independent_ids
        ),
    )
    check(
        "hierarchy: vision-bound facts share one needs-VISION subtree",
        "- **VISION.md-dependent checks** - _needs VISION.md_" in body
        and all(
            next(
                line
                for line in body.splitlines()
                if "`%s`" % schema.CRITERIA_LABELS[criterion_id] in line
            ).startswith("    - ")
            for criterion_id in vision_ids
        ),
    )


def test_unmet_and_unavailable_have_distinct_markers():
    body = "\n".join(
        render_card._automerge_criteria_section(
            [
                {
                    "id": "g6_behavior_class",
                    "status": schema.STATUS_UNMET,
                    "evidence": "root cause",
                },
                {
                    "id": "g6_vision_alignment",
                    "status": schema.STATUS_UNAVAILABLE,
                    "evidence": "not evaluated after root",
                },
            ]
        )
    )
    check(
        "markers: root UNMET is visually distinct from dependent UNAVAILABLE",
        "❌ **UNMET** `G6 - eligible behavior class`" in body
        and "⚪ **UNAVAILABLE** `G6 - behavior aligns with VISION.md`" in body,
    )


def test_unknown_prefix_criterion_renders_in_other_without_being_dropped():
    future = {
        "id": "future_policy_guard",
        "label": "Future - policy guard",
        "status": schema.STATUS_UNMET,
        "evidence": "future evidence remains visible",
    }
    criteria = [future]
    normalized = schema.normalize_criteria(criteria)
    body = "\n".join(render_card._automerge_criteria_section(criteria))
    other = body[body.index("#### Other") :]
    check(
        "group: unknown-prefix criterion falls back to Other",
        "`Future - policy guard`" in other,
    )
    check(
        "group: unknown-prefix criterion keeps its status and evidence",
        "❌ **UNMET**" in other and "future evidence remains visible" in other,
    )
    rendered = render_card.render(item(automerge_criteria=criteria))
    state = core.parse_state_block(rendered["body"])
    stored = state[render_card.AUTOMERGE_CRITERIA_FIELD]
    check(
        "group: stable future criterion persists in card state",
        future in stored and stored == normalized,
    )
    check(
        "group: matching future criterion does not keep refreshing the card",
        render_card.automerge_criteria_stale(item(automerge_criteria=criteria), state)
        is False,
    )
    changed = [dict(future, evidence="future evidence changed")]
    check(
        "group: changed future criterion refreshes the existing card",
        render_card.automerge_criteria_stale(item(automerge_criteria=changed), state)
        is True,
    )


def test_render_version_bump_refreshes_existing_card_exactly_once():
    result = evaluate()
    current_item = item(automerge_criteria=result["criteria"])
    current_state = core.parse_state_block(render_card.render(current_item)["body"])
    previous_state = dict(current_state)
    previous_state["render_version"] = render_card.CARD_RENDER_VERSION - 1
    labels = {"needs-decision"}
    check(
        "refresh: previous criteria UI version triggers one display-only refresh",
        render_card.refresh_needed(current_item, previous_state, labels=labels) is True,
    )
    check(
        "refresh: regrouped current-version card is a no-op on the next pass",
        render_card.refresh_needed(current_item, current_state, labels=labels) is False,
    )
    check(
        "refresh: criteria hierarchy version remains non-material",
        render_card.material_changed(current_item, previous_state) is False,
    )


def test_state_block_escapes_html_comment_terminators():
    evidence = "excluded path: docs/close-->inject.md & <tag>"
    criteria = [{"id": "g2_exclusions_clear", "status": "unmet", "evidence": evidence}]
    rendered = render_card.render(item(automerge_criteria=criteria))
    state_line = rendered["body"].split("<!-- wheelhouse-state: ", 1)[1]
    state = core.parse_state_block(rendered["body"])
    stored = {
        row["id"]: row["evidence"]
        for row in state[render_card.AUTOMERGE_CRITERIA_FIELD]
    }
    check(
        "security: target evidence cannot terminate the hidden state comment",
        "-->inject.md" not in state_line,
    )
    check(
        "security: escaped state evidence round-trips without semantic changes",
        stored["g2_exclusions_clear"] == evidence,
    )
    updated = render_card.body_with_activity_reflected(
        rendered["body"],
        item(updated_at="2026-07-14T16:27:26Z"),
    )
    updated_state = core.parse_state_block(updated)
    check(
        "security: escaped state survives later state-block replacement",
        updated_state["activity_reflected_at"] == "2026-07-14T16:27:26Z",
    )
    check(
        "security: replacement preserves escaped evidence",
        {
            row["id"]: row["evidence"]
            for row in updated_state[render_card.AUTOMERGE_CRITERIA_FIELD]
        }["g2_exclusions_clear"]
        == evidence,
    )


def test_displayed_met_rows_cannot_grant_eligibility():
    forged = schema.normalize_criteria(
        [
            {"id": criterion_id, "status": schema.STATUS_MET, "evidence": "claimed met"}
            for criterion_id in schema.CRITERIA_IDS
        ]
        + [
            {
                "id": "future_merge_authorization",
                "label": "Future - merge authorization",
                "status": schema.STATUS_MET,
                "evidence": "forged future row",
            }
        ]
    )
    actual = card_entry()
    actual["state"] = dict(
        actual["state"], automerge_verdict=None, automerge_criteria=forged
    )
    result = evaluate(card_value=actual, full=False)
    check(
        "security: displayed MET rows do not grant eligibility",
        result["eligible"] is False,
    )
    check(
        "security: unknown displayed MET rows remain advisory only",
        any(row["id"] == "future_merge_authorization" for row in forged),
    )
    check(
        "security: live persisted verdict guard still holds",
        result["hold_reason"].startswith("G6 no structured behavior verdict"),
    )


def test_triage_write_atomically_re_evaluates_and_replaces_stale_criteria():
    """Reproduce #1493: triage used to leave the pre-card G6 rows frozen."""
    stale = evaluate(card_value=None, prior=False)
    held = render_card.render(item(automerge_criteria=stale["criteria"]), held=True)
    held["body"] = render_card.body_with_triage_queued(held["body"], item())
    card = {
        "number": 623,
        "body": held["body"],
        "labels": held["labels"],
        "state": "OPEN",
        "updatedAt": "2026-07-19T12:00:00Z",
        "comments": [],
    }
    stale_rows = rows(stale)
    check(
        "atomic setup: pre-triage checklist reproduces stale G6",
        stale_rows["g6_triage_success"]["status"] == schema.STATUS_UNMET
        and stale_rows["g6_merge_recommendation"]["status"] == schema.STATUS_UNMET,
    )

    evaluations = []
    writes = []
    original_evaluate = am.evaluate_candidate

    def evaluate_once(*args, **kwargs):
        result = original_evaluate(*args, **kwargs)
        evaluations.append(
            {
                "card_entry": args[2],
                "token": os.environ.get("GH_TOKEN"),
                "criteria": result["criteria"],
            }
        )
        return result

    old_env = {
        key: os.environ.get(key)
        for key in (
            "GH_TOKEN",
            "WHEELHOUSE_FLEET_TOKEN",
            "WHEELHOUSE_AUTOMERGE_HAS_TOKEN",
            "GITHUB_REPOSITORY_OWNER",
        )
    }
    os.environ.update(
        {
            "GH_TOKEN": "card-token",
            "WHEELHOUSE_FLEET_TOKEN": "fleet-token",
            "WHEELHOUSE_AUTOMERGE_HAS_TOKEN": "true",
            "GITHUB_REPOSITORY_OWNER": "kunchenguid",
        }
    )
    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(render_card, "get_card", return_value=card)
            )
            stack.enter_context(
                patch.object(
                    render_card,
                    "_edit_issue_body",
                    side_effect=lambda number, body, remove_labels=None: writes.append(
                        (number, body, remove_labels)
                    ),
                )
            )
            stack.enter_context(patch.object(am, "evaluate_candidate", evaluate_once))
            stack.enter_context(
                patch.object(
                    core, "same_closing_issue_overlap", return_value=(True, "")
                )
            )
            stack.enter_context(
                patch.object(
                    core,
                    "load_config",
                    return_value={"auto_merge": True, "repos": {"axi": {}}},
                )
            )
            stack.enter_context(
                patch.object(core, "maintainers", return_value={"kunchenguid"})
            )
            stack.enter_context(
                patch.object(
                    am, "vision_on_default_branch", return_value=(True, VISION)
                )
            )
            stack.enter_context(patch.object(am, "live_pr", return_value=live_pr()))
            stack.enter_context(
                patch.object(am, "has_prior_merged_pr", return_value=False)
            )
            stack.enter_context(
                patch.object(
                    am,
                    "immutable_compare_files",
                    return_value=(
                        ["README.md", "catalog.yaml", "docs/index.html"],
                        True,
                        True,
                    ),
                )
            )
            applied = render_card.update_card_triage(
                623,
                HEAD,
                triage={
                    "summary": "Adds a verified community catalog entry.",
                    "product_implications": "No runtime behavior changes.",
                    "evidence": "target.txt: catalog-only changes",
                    "recommended_action": "merge",
                    "recommended_reason": "focused and verified",
                    "automerge": {
                        "behavior_class": "A",
                        "changes_existing_or_default_behavior": False,
                        "optin_default_off": False,
                        "aligns_with_vision": True,
                        "recommend_merge": True,
                    },
                },
                owner="kunchenguid",
                vision_sha=VISION,
                base_sha=BASE,
                automerge_behavior_available=True,
            )
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    check("atomic: triage update is applied", applied is True)
    check("atomic: evaluate_candidate runs exactly once", len(evaluations) == 1)
    projected_state = evaluations[0]["card_entry"]["state"]
    check(
        "atomic: evaluator receives the post-triage card state",
        projected_state.get("triage_status") == "succeeded"
        and projected_state.get("triage_recommendation", {}).get("action") == "merge",
    )
    check(
        "security: evaluator reads with fleet token and card write restores its token",
        evaluations[0]["token"] == "fleet-token"
        and os.environ.get("GH_TOKEN") == old_env["GH_TOKEN"],
    )
    check("atomic: one issue-body write carries the whole projection", len(writes) == 1)
    written_body = writes[0][1]
    written_state = core.parse_state_block(written_body)
    written_rows = {
        row["id"]: row for row in written_state[render_card.AUTOMERGE_CRITERIA_FIELD]
    }
    check(
        "atomic: hidden criteria are exactly the one evaluator result",
        written_state[render_card.AUTOMERGE_CRITERIA_FIELD]
        == evaluations[0]["criteria"],
    )
    check(
        "atomic: updated triage and G6 checklist agree",
        "### Triage" in written_body
        and "merge - focused and verified" in written_body
        and written_rows["g6_triage_success"]["status"] == schema.STATUS_MET
        and written_rows["g6_merge_recommendation"]["status"] == schema.STATUS_MET
        and "✅ **MET** `G6 - successful triage for current head`" in written_body
        and "✅ **MET** `G6 - top-level recommendation is merge`" in written_body,
    )


def test_every_triage_body_writer_uses_the_atomic_projection():
    writers = (
        render_card.mark_triage_queued,
        render_card.publish_triage_budget_deferral,
        render_card.clear_triage_queued,
        render_card.update_card_triage,
    )
    check(
        "atomic: every queued, deferred, cleared, or completed triage write re-evaluates",
        all(
            inspect.getsource(writer).count("_atomic_automerge_card_body(") == 1
            for writer in writers
        ),
    )


def test_axi_pr96_shape_surfaces_after_ci_wait_with_honest_evidence():
    # Production #96 shape: safe 3-file docs/catalog PR, green checks, first-time
    # contributor, and no pre-existing card on the auto-approve scan.
    result = evaluate(card_value=None, prior=False, require_claim=False)
    criterion_rows = rows(result)
    check(
        "axi#96: absent card is explicit UNMET, not a silent disappearance",
        criterion_rows["g1_card_identity"]["status"] == schema.STATUS_UNMET,
    )
    check(
        "axi#96: first-time contributor history is explicit UNMET",
        criterion_rows["g3_prior_merge"]["status"] == schema.STATUS_UNMET,
    )
    check(
        "axi#96: safe docs/catalog paths clear exclusions",
        criterion_rows["g2_exclusions_clear"]["status"] == schema.STATUS_MET,
    )
    check(
        "axi#96: 3 files and 20 lines clear both blast limits",
        criterion_rows["g5_file_limit"]["status"] == schema.STATUS_MET
        and criterion_rows["g5_line_limit"]["status"] == schema.STATUS_MET,
    )
    rendered = render_card.render(
        item(automerge_criteria=result["criteria"]), held=True
    )
    check(
        "axi#96: real card render carries target and criterion UI",
        "[axi#96]" in rendered["title"]
        and "G3 - prior merged contribution in this repo" in rendered["body"],
    )


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
    check(
        "refresh: changed criterion evidence triggers display refresh",
        render_card.automerge_criteria_stale(next_item, state) is True,
    )
    check(
        "refresh: criterion-only change remains non-material",
        render_card.material_changed(next_item, state) is False,
    )


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
