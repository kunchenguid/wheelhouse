#!/usr/bin/env python3
"""
Unit-exercise reconcile routing and activity reflection with NO network.

Run: python tests/test_reconcile.py
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)
import reconcile  # noqa: E402

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def labels(*names):
    return [{"name": n} for n in names]


def body_state(
    repo="wheelhouse",
    number=42,
    kind="pr-review",
    head_sha="oldsha",
    comp="pass",
    tests="green",
    priority="med",
    render_version=None,
    updated_at="2024-01-01T00:00:00Z",
    activity_reflected_at="2024-01-01T00:00:00Z",
):
    state = {
        "repo": repo,
        "number": number,
        "kind": kind,
        "head_sha": head_sha,
        "options": ["merge", "close", "hold"],
        "comp": comp,
        "tests": tests,
        "priority": priority,
        "updated_at": updated_at,
        "activity_reflected_at": activity_reflected_at,
    }
    if render_version is not None:
        state["render_version"] = render_version
    return "<!-- wheelhouse-state: %s -->" % json.dumps(state, separators=(",", ":"))


def work_item(**overrides):
    item = {
        "repo": "wheelhouse",
        "number": 42,
        "kind": "pr-review",
        "head_sha": "oldsha",
        "title": "Ready PR",
        "author": "contributor",
        "bucket": "merge-ready",
        "comp": "pass",
        "tests": "green",
        "updated_at": "2024-01-01T00:00:00Z",
        "url": "https://github.com/kunchenguid/wheelhouse/pull/42",
        "summary": "compliance=pass tests=green",
        "recommendation": "Merge - compliance and tests are green.",
        "priority": "med",
    }
    item.update(overrides)
    return item


def run_reconcile(scan, cards, current_cards=None):
    calls = {"upsert": [], "close": [], "reflect": []}
    current_by_number = {
        c["number"]: c for c in (cards if current_cards is None else current_cards)
    }

    def fake_upsert(item, existing=None, has_token=False):
        calls["upsert"].append(
            {"item": item, "existing": existing, "has_token": has_token}
        )

    def fake_close(number, message, label="resolved"):
        calls["close"].append({"number": number, "message": message, "label": label})

    def fake_get_card(number):
        return current_by_number.get(int(number))

    def fake_reflect(number, item, body, card_updated_at=""):
        new_body = reconcile.render_card.body_with_activity_reflected(
            body, item, card_updated_at=card_updated_at
        )
        calls["reflect"].append(
            {
                "number": number,
                "item": item,
                "body": body,
                "card_updated_at": card_updated_at,
                "body_after": new_body,
            }
        )
        if new_body == body:
            return False
        current_by_number[int(number)]["body"] = new_body
        return True

    old_argv = sys.argv[:]
    old_upsert = reconcile.render_card.upsert_card
    old_close = reconcile.render_card.close_card
    old_get_card = reconcile.render_card.get_card
    old_reflect = reconcile.render_card.reflect_activity
    reconcile.render_card.upsert_card = fake_upsert
    reconcile.render_card.close_card = fake_close
    reconcile.render_card.get_card = fake_get_card
    reconcile.render_card.reflect_activity = fake_reflect
    try:
        with tempfile.TemporaryDirectory() as d:
            scan_path = os.path.join(d, "scan.json")
            cards_path = os.path.join(d, "cards.json")
            with open(scan_path, "w") as f:
                json.dump(scan, f)
            with open(cards_path, "w") as f:
                json.dump(cards, f)
            sys.argv = ["reconcile.py", scan_path, cards_path]
            with redirect_stdout(io.StringIO()):
                reconcile.main()
    finally:
        sys.argv = old_argv
        reconcile.render_card.upsert_card = old_upsert
        reconcile.render_card.close_card = old_close
        reconcile.render_card.get_card = old_get_card
        reconcile.render_card.reflect_activity = old_reflect
    return calls


def scan_payload(
    items=None,
    open_pr_numbers=None,
    open_issue_numbers=None,
    ok=True,
    truncated=False,
    indeterminate_pr_numbers=None,
):
    return {
        "repos": {
            "wheelhouse": {
                "ok": ok,
                "open_pr_numbers": [42] if open_pr_numbers is None else open_pr_numbers,
                "open_issue_numbers": []
                if open_issue_numbers is None
                else open_issue_numbers,
                "indeterminate_pr_numbers": []
                if indeterminate_pr_numbers is None
                else indeterminate_pr_numbers,
                "truncated": truncated,
            }
        },
        "items": [] if items is None else items,
    }


def card(labels_, kind="pr-review", render_version=None):
    return {
        "number": 7,
        "body": body_state(kind=kind, render_version=render_version),
        "labels": labels_,
        "title": "[wheelhouse#42] Ready PR",
        "updated_at": "2024-01-02T00:00:00Z",
    }


def test_refresh_uses_known_card_when_target_label_missing():
    calls = run_reconcile(
        scan_payload(items=[work_item(priority="high")]),
        [
            card(
                labels(
                    "needs-decision",
                    "repo:wheelhouse",
                    "kind:pr-review",
                    "priority:med",
                )
            )
        ],
    )
    check("reconcile: refresh called once", len(calls["upsert"]) == 1)
    existing = calls["upsert"][0]["existing"] if calls["upsert"] else None
    check(
        "reconcile: refresh receives known card row",
        existing is not None and existing["number"] == 7,
    )
    check(
        "reconcile: known row has missing target label",
        existing is not None
        and all(
            label.get("name") != "target:wheelhouse-42" for label in existing["labels"]
        ),
    )
    check("reconcile: no close for refreshed worklist item", calls["close"] == [])


def test_refresh_uses_current_labels_before_upsert():
    snapshot = card(
        labels(
            "needs-decision",
            "repo:wheelhouse",
            "kind:pr-review",
            "priority:med",
            "target:wheelhouse-42",
        )
    )
    current = card(
        labels(
            "needs-decision",
            "processing",
            "repo:wheelhouse",
            "kind:pr-review",
            "priority:med",
            "target:wheelhouse-42",
        )
    )
    calls = run_reconcile(
        scan_payload(items=[work_item(priority="high")]),
        [snapshot],
        current_cards=[current],
    )
    check("reconcile: processing current card is not refreshed", calls["upsert"] == [])
    check("reconcile: processing current card is not closed", calls["close"] == [])


def test_open_target_that_left_worklist_is_consumed():
    calls = run_reconcile(
        scan_payload(items=[]),
        [
            card(
                labels(
                    "needs-decision",
                    "repo:wheelhouse",
                    "kind:pr-review",
                    "priority:med",
                    "target:wheelhouse-42",
                )
            )
        ],
    )
    check("reconcile: no upsert for item outside worklist", calls["upsert"] == [])
    check(
        "reconcile: pure pending card outside worklist closed",
        len(calls["close"]) == 1 and calls["close"][0]["number"] == 7,
    )
    check(
        "reconcile: close message says no maintainer decision needed",
        "no longer needs a maintainer decision" in calls["close"][0]["message"],
    )


def test_indeterminate_pr_card_is_frozen():
    # #111 invariant: an open PR whose mergeability was unreadable this scan
    # (UNKNOWN did not settle) is reported in `indeterminate_pr_numbers` and emits
    # no worklist item. Its existing card must be FROZEN - neither closed/consumed
    # nor refreshed - so an UNKNOWN reading can never flip worklist membership or
    # mint/close a card. This is the same mergeable-UNKNOWN oscillation that
    # minted 10 duplicate cards for lavish-axi#111.
    calls = run_reconcile(
        scan_payload(items=[], indeterminate_pr_numbers=[42]),
        [
            card(
                labels(
                    "needs-decision",
                    "repo:wheelhouse",
                    "kind:pr-review",
                    "priority:med",
                    "target:wheelhouse-42",
                )
            )
        ],
    )
    check("reconcile-freeze: indeterminate card is NOT closed", calls["close"] == [])
    check("reconcile-freeze: indeterminate card is NOT refreshed", calls["upsert"] == [])


def test_ci_approval_card_with_no_pending_run_is_consumed():
    calls = run_reconcile(
        scan_payload(items=[]),
        [
            card(
                labels(
                    "needs-decision",
                    "repo:wheelhouse",
                    "kind:ci-approval",
                    "priority:med",
                    "target:wheelhouse-42",
                ),
                kind="ci-approval",
            )
        ],
    )
    check(
        "reconcile: stale ci-approval card is closed",
        len(calls["close"]) == 1 and calls["close"][0]["number"] == 7,
    )
    check(
        "reconcile: stale ci-approval close consumes the card",
        "consuming this card" in calls["close"][0]["message"],
    )


def test_ci_approval_worklist_item_creates_fresh_card():
    item = work_item(
        kind="ci-approval",
        bucket="needs-ci-approval",
        comp="none",
        tests="none",
        recommendation="Approve CI to get a test signal.",
    )
    calls = run_reconcile(scan_payload(items=[item]), [])
    check(
        "reconcile: returned ci-approval item creates a fresh card",
        len(calls["upsert"]) == 1 and calls["upsert"][0]["existing"] is None,
    )
    check(
        "reconcile: fresh card is ci-approval",
        calls["upsert"] and calls["upsert"][0]["item"]["kind"] == "ci-approval",
    )


def test_open_target_that_left_worklist_uses_current_labels_before_close():
    snapshot = card(
        labels(
            "needs-decision",
            "repo:wheelhouse",
            "kind:pr-review",
            "priority:med",
            "target:wheelhouse-42",
        )
    )
    current = card(
        labels(
            "needs-decision",
            "processing",
            "repo:wheelhouse",
            "kind:pr-review",
            "priority:med",
            "target:wheelhouse-42",
        )
    )
    calls = run_reconcile(
        scan_payload(items=[]),
        [snapshot],
        current_cards=[current],
    )
    check(
        "reconcile: processing current card outside worklist is not closed",
        calls["close"] == [],
    )


def test_render_stale_only_pure_card_is_refreshed_via_reconcile():
    """No material field differs, but the card's render_version is missing
    (stale) - the OR-trigger should still refresh it once."""
    matched_options = ["merge", "close", "hold"]
    stale_card = card(
        labels(
            "needs-decision",
            "repo:wheelhouse",
            "kind:pr-review",
            "priority:med",
            "target:wheelhouse-42",
        ),
    )
    calls = run_reconcile(
        scan_payload(items=[work_item(options=matched_options)]),
        [stale_card],
    )
    check(
        "reconcile: render-stale-only card refreshed once via OR-trigger",
        len(calls["upsert"]) == 1,
    )
    check(
        "reconcile: render-stale refresh receives known card row",
        calls["upsert"] and calls["upsert"][0]["existing"]["number"] == 7,
    )


def test_render_fresh_and_materially_unchanged_card_is_noop_via_reconcile():
    """Neither trigger fires when the card is both materially unchanged AND
    already carries the current render_version, with no newer activity stamp
    needed - a full no-op."""
    matched_options = ["merge", "close", "hold"]
    fresh_card = card(
        labels(
            "needs-decision",
            "repo:wheelhouse",
            "kind:pr-review",
            "priority:med",
            "target:wheelhouse-42",
        ),
        render_version=reconcile.render_card.CARD_RENDER_VERSION,
    )
    calls = run_reconcile(
        scan_payload(items=[work_item(options=matched_options)]),
        [fresh_card],
    )
    check(
        "reconcile: no upsert when materially unchanged and render-fresh",
        calls["upsert"] == [],
    )
    check("reconcile: no close for a card still in the worklist", calls["close"] == [])


def test_target_activity_newer_gets_state_only_reflection_once():
    matched_options = ["merge", "close", "hold"]
    fresh_card = card(
        labels(
            "needs-decision",
            "repo:wheelhouse",
            "kind:pr-review",
            "priority:med",
            "target:wheelhouse-42",
        ),
        render_version=reconcile.render_card.CARD_RENDER_VERSION,
    )
    active = work_item(
        options=matched_options,
        updated_at="2024-06-01T00:00:00Z",
    )
    calls = run_reconcile(scan_payload(items=[active]), [fresh_card])
    check("reconcile: activity reflection edits once", len(calls["reflect"]) == 1)
    check("reconcile: activity reflection is not a full refresh", calls["upsert"] == [])
    check(
        "reconcile: activity reflection does not close/comment via close path",
        calls["close"] == [],
    )
    before = calls["reflect"][0]["body"] if calls["reflect"] else ""
    after = calls["reflect"][0]["body_after"] if calls["reflect"] else ""
    check(
        "reconcile: activity reflection changes only hidden state",
        reconcile.render_card._STATE_BLOCK_RE.sub("STATE", before)
        == reconcile.render_card._STATE_BLOCK_RE.sub("STATE", after),
    )
    next_card = dict(fresh_card, body=after, updated_at="2024-06-01T00:00:01Z")
    second = run_reconcile(scan_payload(items=[active]), [next_card])
    check("reconcile: second activity pass is no-op", second["reflect"] == [])


def test_target_activity_reflection_skips_non_pending_and_bad_timestamps():
    matched_options = ["merge", "close", "hold"]
    active = work_item(options=matched_options, updated_at="2024-06-01T00:00:00Z")
    for label_name in ("processing", "resolved", "blocked"):
        calls = run_reconcile(
            scan_payload(items=[active]),
            [
                card(
                    labels(
                        "needs-decision",
                        label_name,
                        "repo:wheelhouse",
                        "kind:pr-review",
                    ),
                    render_version=reconcile.render_card.CARD_RENDER_VERSION,
                )
            ],
        )
        check(
            "reconcile: %s card is not activity-stamped" % label_name,
            calls["reflect"] == [],
        )
    missing_needs = run_reconcile(
        scan_payload(items=[active]),
        [
            card(
                labels("repo:wheelhouse", "kind:pr-review"),
                render_version=reconcile.render_card.CARD_RENDER_VERSION,
            )
        ],
    )
    malformed = run_reconcile(
        scan_payload(items=[work_item(options=matched_options, updated_at="bad")]),
        [
            card(
                labels("needs-decision", "repo:wheelhouse", "kind:pr-review"),
                render_version=reconcile.render_card.CARD_RENDER_VERSION,
            )
        ],
    )
    missing = run_reconcile(
        scan_payload(items=[work_item(options=matched_options, updated_at="")]),
        [
            card(
                labels("needs-decision", "repo:wheelhouse", "kind:pr-review"),
                render_version=reconcile.render_card.CARD_RENDER_VERSION,
            )
        ],
    )
    check(
        "reconcile: missing needs-decision is not activity-stamped",
        missing_needs["reflect"] == [],
    )
    check(
        "reconcile: malformed target updated_at is not activity-stamped",
        malformed["reflect"] == [],
    )
    check(
        "reconcile: missing target updated_at is not activity-stamped",
        missing["reflect"] == [],
    )


def test_target_activity_reflection_uses_legacy_card_updated_at_baseline():
    matched_options = ["merge", "close", "hold"]
    legacy = card(
        labels("needs-decision", "repo:wheelhouse", "kind:pr-review"),
        render_version=reconcile.render_card.CARD_RENDER_VERSION,
    )
    state = reconcile.core.parse_state_block(legacy["body"])
    state.pop("activity_reflected_at", None)
    legacy["body"] = reconcile.render_card._replace_state_block(legacy["body"], state)
    active = work_item(options=matched_options, updated_at="2024-01-02T00:00:00Z")
    no_churn = run_reconcile(scan_payload(items=[active]), [legacy])
    check(
        "reconcile: legacy card baseline prevents one-time stamp",
        no_churn["reflect"] == [],
    )
    newer = dict(legacy, updated_at="2024-01-02T00:00:00Z")
    newer_activity = work_item(
        options=matched_options, updated_at="2024-06-01T00:00:00Z"
    )
    stamped = run_reconcile(scan_payload(items=[newer_activity]), [newer])
    check(
        "reconcile: legacy card stamps once target passes card updated_at",
        len(stamped["reflect"]) == 1,
    )


def test_failed_repo_scan_leaves_cards_unstamped():
    calls = run_reconcile(
        scan_payload(items=[], ok=False),
        [
            card(
                labels("needs-decision", "repo:wheelhouse", "kind:pr-review"),
                render_version=reconcile.render_card.CARD_RENDER_VERSION,
            )
        ],
    )
    check(
        "reconcile: failed repo scan does not reflect activity", calls["reflect"] == []
    )
    check("reconcile: failed repo scan does not close card", calls["close"] == [])


def test_render_stale_processing_card_is_not_refreshed_via_reconcile():
    """The is_refreshable guard still gates the render-version trigger inside
    reconcile: a processing card is never refreshed just because it is
    render-stale."""
    matched_options = ["merge", "close", "hold"]
    stale_card = card(
        labels(
            "needs-decision",
            "processing",
            "repo:wheelhouse",
            "kind:pr-review",
            "priority:med",
            "target:wheelhouse-42",
        ),
    )
    calls = run_reconcile(
        scan_payload(items=[work_item(options=matched_options)]),
        [stale_card],
    )
    check(
        "reconcile: render-stale processing card is NOT refreshed",
        calls["upsert"] == [],
    )


def test_open_target_without_needs_decision_is_left_alone():
    calls = run_reconcile(
        scan_payload(items=[]),
        [
            card(
                labels(
                    "repo:wheelhouse",
                    "kind:pr-review",
                    "priority:med",
                    "target:wheelhouse-42",
                )
            )
        ],
    )
    check("reconcile: card missing needs-decision is not closed", calls["close"] == [])
    check(
        "reconcile: card missing needs-decision is not upserted", calls["upsert"] == []
    )


def test_truncated_repo_scan_does_not_self_heal_close_missing_issue():
    issue_card = card(
        labels(
            "needs-decision",
            "repo:wheelhouse",
            "kind:issue-triage",
            "priority:low",
            "target:wheelhouse-42",
        ),
        kind="issue-triage",
    )
    calls = run_reconcile(
        scan_payload(
            items=[],
            open_pr_numbers=[],
            open_issue_numbers=list(range(1, 101)),
            truncated=True,
        ),
        [issue_card],
    )
    check(
        "reconcile: truncated scan leaves possibly unseen issue card open",
        calls["close"] == [],
    )


def main():
    test_refresh_uses_known_card_when_target_label_missing()
    test_refresh_uses_current_labels_before_upsert()
    test_open_target_that_left_worklist_is_consumed()
    test_indeterminate_pr_card_is_frozen()
    test_ci_approval_card_with_no_pending_run_is_consumed()
    test_ci_approval_worklist_item_creates_fresh_card()
    test_open_target_that_left_worklist_uses_current_labels_before_close()
    test_render_stale_only_pure_card_is_refreshed_via_reconcile()
    test_render_fresh_and_materially_unchanged_card_is_noop_via_reconcile()
    test_target_activity_newer_gets_state_only_reflection_once()
    test_target_activity_reflection_skips_non_pending_and_bad_timestamps()
    test_target_activity_reflection_uses_legacy_card_updated_at_baseline()
    test_failed_repo_scan_leaves_cards_unstamped()
    test_render_stale_processing_card_is_not_refreshed_via_reconcile()
    test_open_target_without_needs_decision_is_left_alone()
    test_truncated_repo_scan_does_not_self_heal_close_missing_issue()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all reconcile tests passed")


if __name__ == "__main__":
    main()
