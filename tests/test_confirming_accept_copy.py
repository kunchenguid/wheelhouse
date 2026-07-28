#!/usr/bin/env python3
"""Confirming/inert cards must not instruct ticking an absent Accept control.

Production defect (scan 5 / card #1721): an issue-triage card entered the
`wheelhouse:confirming-target-state` lifecycle while carrying a fresh admitted
recommendation. The confirming projection correctly suppressed every decision
checkbox and correctly kept the recommendation analysis, but
`_recommendation_section` still appended "Tick **Accept recommendation** to
apply it" - instructing the captain to operate a control that is not rendered.

All checks here are offline: no network, no live card or target mutation.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

import render_card as rc  # noqa: E402
import wheelhouse_core as core  # noqa: E402

FAILURES = []

ISSUE_UPDATED_AT = "2026-07-26T07:08:25Z"
# Production-shaped reason from card #1721 (truncated for fixtures).
ISSUE_REASON = (
    "Confirmed reproducible: locate_region() returns None when comments are "
    "stripped, so outside equals the full config, and HOOK_NAME in outside fires."
)


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def _issue_item(**over):
    base = {
        "repo": "firstmate",
        "number": 1064,
        "kind": "issue-triage",
        "title": (
            "kimi turn-end hook markers are TOML comments and do not survive "
            "Kimi CLI config rewrites"
        ),
        "author": "jokim1",
        "url": "https://github.com/kunchenguid/firstmate/issues/1064",
        "head_sha": "",
        "updated_at": ISSUE_UPDATED_AT,
        "bucket": "issue-triage",
        "comp": "n/a",
        "tests": "n/a",
        "priority": "low",
        "summary": "open issue, no linked PR",
        "options": list(rc.CHECKBOX_OPTIONS["issue-triage"]),
    }
    base.update(over)
    return base


def _published_with_recommendation(item=None, action="investigate", reason=ISSUE_REASON):
    item = item or _issue_item()
    body = rc.render(item, owner="kunchenguid")["body"]
    revision = rc.triage_revision(item)
    triage = {
        "summary": "Ownership markers strip under Kimi TOML rewrite.",
        "product_implications": "Recurring adapter install failure.",
        "recommended_action": action,
        "recommended_reason": reason,
        "evidence": "target.txt: \"ownership markers\"",
    }
    return rc.body_with_triage_result(
        body, revision, triage=triage, owner="kunchenguid"
    )


def _confirming_with_recommendation(**absence_kwargs):
    published = _published_with_recommendation()
    kwargs = {
        "run_number": 46,
        "reason": "target is outside the current maintainer worklist",
    }
    kwargs.update(absence_kwargs)
    return published, rc.body_with_reconcile_absence(published, 1, **kwargs)


def test_confirming_card_keeps_analysis_without_tick_instruction():
    published, confirming = _confirming_with_recommendation()
    state = core.parse_state_block(confirming)

    check(
        "confirming: analysis and action remain visible",
        "### Recommended action" in confirming
        and "- **Agent recommendation:** `investigate`" in confirming
        and ISSUE_REASON.split(",")[0] in confirming,
    )
    check(
        "confirming: decision controls stay suppressed",
        "<!-- opt:" not in confirming
        and "Decision controls are disabled until the scheduled confirmation completes."
        in confirming
        and "### Target state changed" in confirming
        and state.get("lifecycle_state") == "awaiting-scheduled-confirmation"
        and rc.decision_controls_suppressed(state=state, body=confirming),
    )
    check(
        "confirming: no instruction to tick or apply an absent Accept control",
        "Tick **Accept recommendation**" not in confirming
        and "to apply it" not in confirming
        and not rc.contradictory_accept_instruction(confirming)
        and rc.RECOMMENDATION_INERT_FRAMING in confirming,
    )
    check(
        "confirming: admission gate still true (authority unchanged)",
        rc.accept_recommendation_available(state) is True
        and state.get("triage_recommendation", {}).get("action") == "investigate",
    )
    check(
        "confirming: render version stamped current for migration exit",
        state.get("render_version") == rc.CARD_RENDER_VERSION
        and rc.CARD_RENDER_VERSION == 15,
    )


def test_published_card_keeps_actionable_accept_instruction_and_control():
    published = _published_with_recommendation()
    state = core.parse_state_block(published)

    check(
        "published: Accept checkbox is rendered",
        "<!-- opt:accept-recommendation -->" in published
        and "- [ ] Accept recommendation" in published,
    )
    check(
        "published: actionable Tick instruction is present exactly once",
        published.count("Tick **Accept recommendation**") == 1
        and rc.RECOMMENDATION_ACCEPT_INSTRUCTION in published
        and not rc.decision_controls_suppressed(state=state, body=published)
        and not rc.contradictory_accept_instruction(published),
    )
    check(
        "published: gate and options agree",
        rc.accept_recommendation_available(state) is True
        and "accept-recommendation" in (state.get("options") or []),
    )


def test_legacy_confirming_body_heals_under_renderer_and_census():
    """A pre-fix confirming body (the #1721 shape) heals without target writes."""
    published = _published_with_recommendation()
    # Build the buggy shape: confirming projection with the OLD actionable framing
    # forced back in, and render_version left behind.
    confirming = rc.body_with_reconcile_absence(
        published,
        1,
        run_number=46,
        reason="target is outside the current maintainer worklist",
    )
    # Simulate the production contradiction by restoring the old tick line on
    # top of an otherwise correct inert projection.
    buggy = confirming.replace(
        rc.RECOMMENDATION_INERT_FRAMING, rc.RECOMMENDATION_ACCEPT_INSTRUCTION
    )
    buggy_state = dict(core.parse_state_block(buggy))
    buggy_state["render_version"] = 13
    buggy = rc._replace_state_block(buggy, buggy_state)

    check(
        "legacy: fixture reproduces the scan-5 contradiction",
        rc.contradictory_accept_instruction(buggy)
        and "Tick **Accept recommendation**" in buggy
        and "<!-- opt:accept-recommendation -->" not in buggy
        and "Decision controls are disabled until the scheduled confirmation completes."
        in buggy,
    )

    healed = rc.body_with_controls_aware_recommendation(buggy, owner="kunchenguid")
    check(
        "legacy: controls-aware rewrite drops the tick instruction",
        not rc.contradictory_accept_instruction(healed)
        and "Tick **Accept recommendation**" not in healed
        and rc.RECOMMENDATION_INERT_FRAMING in healed
        and "### Recommended action" in healed
        and "<!-- opt:" not in healed,
    )
    healed_state = core.parse_state_block(healed)
    check(
        "legacy: healed body stamps current render version",
        healed_state.get("render_version") == rc.CARD_RENDER_VERSION
        and not rc.render_stale(healed_state),
    )

    # Re-entering the confirming projection owner also heals (and is idempotent).
    reprojected = rc.body_with_reconcile_absence(
        buggy,
        1,
        scheduled_epoch=46,
        reason="target is outside the current maintainer worklist",
    )
    check(
        "legacy: absence projection owner heals the same class",
        not rc.contradictory_accept_instruction(reprojected)
        and "Tick **Accept recommendation**" not in reprojected
        and "Decision controls are disabled until the scheduled confirmation completes."
        in reprojected,
    )
    check(
        "legacy: absence projection is idempotent once healed",
        rc.body_with_reconcile_absence(
            reprojected,
            1,
            scheduled_epoch=46,
            reason="target is outside the current maintainer worklist",
        )
        == reprojected
        or not rc.contradictory_accept_instruction(
            rc.body_with_reconcile_absence(
                reprojected,
                1,
                scheduled_epoch=46,
                reason="target is outside the current maintainer worklist",
            )
        ),
    )

    card_row = {
        "number": 1721,
        "url": "https://github.com/kunchenguid/wheelhouse/issues/1721",
        "body": buggy,
        "labels": [
            {"name": "needs-decision"},
            {"name": "kind:issue-triage"},
            {"name": "repo:firstmate"},
            {"name": rc.LIFECYCLE_CONFIRM_LABEL},
        ],
    }
    clean_row = {
        "number": 99,
        "url": "https://github.com/kunchenguid/wheelhouse/issues/99",
        "body": published,
        "labels": [
            {"name": "needs-decision"},
            {"name": "kind:issue-triage"},
            {"name": "repo:firstmate"},
        ],
    }
    healed_row = dict(card_row, number=1722, body=healed)
    report = rc.contradictory_accept_instruction_census(
        [card_row, clean_row, healed_row]
    )
    check(
        "census: identifies the full affected cohort",
        report["total"] == 3
        and report["clean"] == 2
        and len(report["affected"]) == 1
        and report["affected"][0]["number"] == 1721
        and report["affected"][0]["heals_under_renderer"] is True
        and report["healed_under_renderer"] == 1,
    )
    # Under the new renderer the affected class reaches zero.
    post = rc.contradictory_accept_instruction_census(
        [
            dict(card_row, body=healed),
            clean_row,
            healed_row,
        ]
    )
    check(
        "census: affected cohort reaches zero under the new renderer",
        post["affected"] == []
        and post["clean"] == 3
        and post["healed_under_renderer"] == 0,
    )


def test_same_revision_preserve_does_not_restore_controls_on_confirming():
    published, confirming = _confirming_with_recommendation()
    item = _issue_item()
    old_state = core.parse_state_block(confirming)
    # Fresh render of the same revision would otherwise be actionable.
    fresh = rc.render(item, owner="kunchenguid")["body"]
    preserved = rc._preserve_same_revision_triage(
        fresh,
        confirming,
        item,
        old_state,
        owner="kunchenguid",
    )
    # Production pairs the triage lift with absence re-application on CI-wait
    # anti-masquerade refreshes (see `_refresh_card`).
    final = rc._body_preserving_reconcile_absence(preserved, confirming)
    check(
        "preserve: paired absence re-application keeps the card inert",
        final is not None
        and "<!-- opt:" not in final
        and "### Target state changed" in final
        and "Decision controls are disabled until the scheduled confirmation completes."
        in final,
    )
    check(
        "preserve: no contradictory accept instruction after paired refresh",
        not rc.contradictory_accept_instruction(final)
        and "Tick **Accept recommendation**" not in final
        and "### Recommended action" in final,
    )
    # Even the triage-lift alone must not emit tick-without-control.
    check(
        "preserve: triage lift alone never creates the contradiction",
        not rc.contradictory_accept_instruction(preserved),
    )


def test_same_revision_lifecycle_exit_restores_actionable_accept():
    published, confirming = _confirming_with_recommendation()
    item = _issue_item()
    old_state = core.parse_state_block(confirming)
    fresh = rc.render(item, owner="kunchenguid")["body"]
    exited = rc._preserve_same_revision_triage(
        fresh,
        confirming,
        item,
        old_state,
        owner="kunchenguid",
    )
    state = core.parse_state_block(exited)
    check(
        "exit: new actionable projection restores Accept control",
        "<!-- opt:accept-recommendation -->" in exited
        and "Tick **Accept recommendation**" in exited
        and rc.RECOMMENDATION_ACCEPT_INSTRUCTION in exited,
    )
    check(
        "exit: old confirming posture does not suppress new projection",
        not rc.decision_controls_suppressed(state=state, body=exited)
        and not rc.contradictory_accept_instruction(exited)
        and rc.accept_recommendation_available(state),
    )
    check(
        "exit: ordinary published baseline remains unchanged",
        "<!-- opt:accept-recommendation -->" in published
        and "Tick **Accept recommendation**" in published,
    )


def test_confirming_lifecycle_copy_stays_coherent():
    _published, confirming = _confirming_with_recommendation()
    check(
        "lifecycle: important callout and confirmation fraction remain",
        "> [!IMPORTANT]" in confirming
        and "intentionally inert" in confirming
        and "Confirmation: `1/2` scheduled observations" in confirming
        and "Queue effect: `lifecycle-transition`" in confirming,
    )
    check(
        "lifecycle: single disabled-controls explanation (no duplicate conflict)",
        confirming.count(
            "Decision controls are disabled until the scheduled confirmation completes."
        )
        == 1
        and "Tick **Accept recommendation**" not in confirming,
    )
    cleared = rc.body_without_reconcile_absence(confirming)
    check(
        "lifecycle: clearing absence restores actionable controls + tick line",
        "<!-- opt:accept-recommendation -->" in cleared
        and "Tick **Accept recommendation**" in cleared
        and "### Target state changed" not in cleared
        and not rc.contradictory_accept_instruction(cleared),
    )


def test_render_version_bump_is_the_migration_owner():
    check(
        "migration: CARD_RENDER_VERSION advanced for the copy fix",
        rc.CARD_RENDER_VERSION == 15,
    )
    it = _issue_item()
    body = rc.render(it, owner="kunchenguid")["body"]
    state = core.parse_state_block(body)
    check(
        "migration: fresh cards stamp the current version",
        state.get("render_version") == 15,
    )
    stale = dict(state, render_version=13)
    check(
        "migration: version-13 cards are render-stale and refreshable",
        rc.render_stale(stale) is True
        and rc.refresh_needed(it, stale, ["needs-decision"]) is True,
    )
    published = _published_with_recommendation(it)
    confirming = rc.body_with_reconcile_absence(
        published,
        1,
        run_number=46,
        reason="target is outside the current maintainer worklist",
    )
    version_12_state = dict(core.parse_state_block(confirming), render_version=12)
    version_12 = rc._replace_state_block(confirming, version_12_state).replace(
        rc.RECOMMENDATION_INERT_FRAMING,
        rc.RECOMMENDATION_ACCEPT_INSTRUCTION,
    )
    shortcut_result = rc.body_with_controls_aware_recommendation(
        version_12, owner="kunchenguid"
    )
    check(
        "migration: v12 confirming card bypasses v14-only shortcut",
        shortcut_result == version_12
        and core.parse_state_block(shortcut_result).get("render_version") == 12
        and rc.render_stale(core.parse_state_block(shortcut_result)),
    )
    legacy_triage = version_12.replace(
        rc.TRIAGE_END,
        "- **Related:** #77\n"
        "- **Recommended next step:** investigate\n"
        + rc.TRIAGE_END,
    )
    full_render = rc.render(it, owner="kunchenguid")["body"]
    fully_migrated = rc._preserve_same_revision_triage(
        full_render,
        legacy_triage,
        it,
        core.parse_state_block(legacy_triage),
        owner="kunchenguid",
    )
    migrated_state = core.parse_state_block(fully_migrated)
    check(
        "migration: full renderer owns cumulative migrations",
        migrated_state.get("render_version") == rc.CARD_RENDER_VERSION
        and "**Recommended next step:**" not in fully_migrated
        and "kunchenguid/firstmate#77" in fully_migrated,
    )


def test_absence_reapplication_preserves_version_ownership():
    published = _published_with_recommendation()
    confirming = rc.body_with_reconcile_absence(
        published,
        1,
        run_number=46,
        reason="target is outside the current maintainer worklist",
    )
    for source_version, expected_version in (
        (12, 12),
        (13, rc.CARD_RENDER_VERSION),
        (rc.CARD_RENDER_VERSION, rc.CARD_RENDER_VERSION),
        (None, None),
        ("malformed", "malformed"),
        (13.0, 13.0),
        (True, True),
    ):
        state = dict(core.parse_state_block(confirming))
        if source_version is None:
            state.pop("render_version", None)
        else:
            state["render_version"] = source_version
        legacy = rc._replace_state_block(confirming, state).replace(
            rc.RECOMMENDATION_INERT_FRAMING,
            rc.RECOMMENDATION_ACCEPT_INSTRUCTION,
        )
        reapplied = rc.body_with_reconcile_absence(
            legacy,
            1,
            scheduled_epoch=47,
            reason="target is outside the current maintainer worklist",
        )
        reapplied_state = core.parse_state_block(reapplied)
        actual_version = reapplied_state.get("render_version")
        check(
            "absence reapply: %r version ownership" % source_version,
            actual_version == expected_version
            and rc.reconcile_absence_epoch(reapplied) == 47
            and not rc.contradictory_accept_instruction(reapplied),
        )
        if expected_version != rc.CARD_RENDER_VERSION:
            check(
                "absence reapply: %r remains render-stale" % source_version,
                rc.render_stale(reapplied_state),
            )


def main():
    test_confirming_card_keeps_analysis_without_tick_instruction()
    test_published_card_keeps_actionable_accept_instruction_and_control()
    test_legacy_confirming_body_heals_under_renderer_and_census()
    test_same_revision_preserve_does_not_restore_controls_on_confirming()
    test_same_revision_lifecycle_exit_restores_actionable_accept()
    test_confirming_lifecycle_copy_stays_coherent()
    test_render_version_bump_is_the_migration_owner()
    test_absence_reapplication_preserves_version_ownership()
    if FAILURES:
        print("\n%d FAILURE(S)" % len(FAILURES))
        for name in FAILURES:
            print(" - %s" % name)
        return 1
    print("\nAll confirming-accept-copy checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
