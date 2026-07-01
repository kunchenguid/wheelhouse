#!/usr/bin/env python3
"""
Unit-exercise the card-refresh logic with NO network (pure functions only).

Run: python tests/test_card_refresh.py   (stdlib only; exits non-zero on failure)

An open decision card must reflect CURRENT target state, not just the snapshot
taken when it was created. These tests cover the three pure pieces both the
event path (`render_card.upsert_card`) and the backstop (`reconcile.py`) rely on:

  * change detection - `material_changed` is true iff a material field
    (head_sha / compliance / tests / kind / priority / options) differs from
    the card's stored state, with legacy cards missing the new fields treated
    as changed exactly once (a safe one-time refresh that backfills them), and
    the legacy `triage-state` marker still parsing;
  * the refreshability guard - `is_refreshable` refuses to rewrite a card that
    is mid-decision (`processing`/`resolved`/`blocked`), so a refresh never
    clobbers an in-flight decision or races the handler;
  * the label replace - `plan_label_update` removes stale wheelhouse-managed
    labels (`repo:`/`kind:`/`priority:`/`target:`) while keeping
    `needs-decision` and any human-added label, and is a no-op when nothing
    changed;
  * the state block now carries the material fields and round-trips, so the
    change check is cheap and deterministic.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import render_card as rc  # noqa: E402
import wheelhouse_core as core  # noqa: E402

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def item(**over):
    """A representative scanned pr-review item; override any field."""
    base = {
        "repo": "lavish-axi", "number": 42, "kind": "pr-review",
        "head_sha": "abc1234def", "title": "Add a thing", "author": "someone",
        "bucket": "merge-ready", "comp": "pass", "tests": "green",
        "url": "https://github.com/o/lavish-axi/pull/42",
        "summary": "compliance=pass tests=green", "recommendation": "Merge it.",
        "priority": "med", "options": ["merge", "close", "hold"],
    }
    base.update(over)
    return base


def state_of(it):
    """The parsed state block a freshly rendered card for `it` would carry."""
    return core.parse_state_block(rc.render(it)["body"])


# --------------------------------------------------------------------------- #
# author display: visible to owner, never a notifying @mention
# --------------------------------------------------------------------------- #
def test_render_shows_author_without_mention():
    body = rc.render(item(author="chrishsu"))["body"]
    check("render: author login visible", "by chrishsu" in body)
    check("render: author not @-mentioned", "@chrishsu" not in body)


# --------------------------------------------------------------------------- #
# state block now carries the material fields and round-trips
# --------------------------------------------------------------------------- #
def test_state_block_carries_material_fields():
    st = state_of(item())
    check("state: carries head_sha", st.get("head_sha") == "abc1234def")
    check("state: carries comp", st.get("comp") == "pass")
    check("state: carries tests", st.get("tests") == "green")
    check("state: carries kind", st.get("kind") == "pr-review")
    check("state: carries priority", st.get("priority") == "med")
    check("state: options is material", "options" in rc.MATERIAL_FIELDS)
    # legacy fields are still there (the handler reads these).
    check("state: still carries repo/number/options",
          st.get("repo") == "lavish-axi" and st.get("number") == 42
          and st.get("options") == ["merge", "close", "hold"])


# --------------------------------------------------------------------------- #
# change detection
# --------------------------------------------------------------------------- #
def test_material_changed_round_trip_is_noop():
    it = item()
    check("change: a card vs its own freshly rendered state -> unchanged",
          rc.material_changed(it, state_of(it)) is False)


def test_each_material_field_triggers_a_change():
    it = item()
    st = state_of(it)
    check("change: head_sha differs -> changed",
          rc.material_changed(item(head_sha="9999999"), st) is True)
    check("change: compliance differs -> changed",
          rc.material_changed(item(comp="fail"), st) is True)
    check("change: tests differs -> changed",
          rc.material_changed(item(tests="fail"), st) is True)
    check("change: kind differs -> changed",
          rc.material_changed(item(kind="ci-approval"), st) is True)
    check("change: priority differs -> changed",
          rc.material_changed(item(priority="high"), st) is True)


def test_options_set_change_triggers_but_reorder_does_not():
    it = item(options=["merge", "close", "hold"])
    st = state_of(it)
    check("change: option removed -> changed",
          rc.material_changed(item(options=["merge", "hold"]), st) is True)
    check("change: option added -> changed",
          rc.material_changed(item(options=["merge", "close", "hold", "approve-ci"]), st) is True)
    check("change: options reordered -> NOT changed",
          rc.material_changed(item(options=["hold", "close", "merge"]), st) is False)


def test_render_preserves_options_order_in_state_block():
    st = state_of(item(options=["hold", "merge", "close"]))
    check("state: options order stays as provided",
          st.get("options") == ["hold", "merge", "close"])


def test_non_material_change_is_not_a_trigger():
    # Title / summary / recommendation re-render naturally - they must NOT flag
    # a material change on their own.
    it = item()
    st = state_of(it)
    check("change: title-only change -> NOT changed",
          rc.material_changed(item(title="Totally different title"), st) is False)
    check("change: summary/recommendation-only change -> NOT changed",
          rc.material_changed(item(summary="x", recommendation="y"), st) is False)


def test_legacy_card_missing_new_fields_refreshes_once():
    # A card written before the refresh feature carries only the old fields.
    legacy_body = ('<!-- wheelhouse-state: {"repo":"lavish-axi","number":42,'
                   '"kind":"pr-review","head_sha":"abc1234def",'
                   '"options":["merge","close","hold"]} -->')
    legacy = core.parse_state_block(legacy_body)
    it = item()  # same target, same head_sha
    check("legacy: missing comp/tests/priority -> changed (one safe refresh)",
          rc.material_changed(it, legacy) is True)
    # After that one refresh the state carries the full set, so it no-ops.
    check("legacy: after refresh the same item is a no-op",
          rc.material_changed(it, state_of(it)) is False)


def test_legacy_triage_marker_still_parses_for_change_check():
    legacy_body = ('<!-- triage-state: {"repo":"lavish-axi","number":42,'
                   '"kind":"pr-review","head_sha":"abc1234def",'
                   '"options":["merge","close","hold"]} -->')
    st = core.parse_state_block(legacy_body)
    check("legacy: triage-state marker parses", st is not None and st["number"] == 42)
    check("legacy: triage-state card flagged changed (backfills new fields)",
          rc.material_changed(item(), st) is True)


def test_change_check_handles_missing_state():
    check("change: None state -> changed (safe refresh)",
          rc.material_changed(item(), None) is True)


# --------------------------------------------------------------------------- #
# refreshability guard: never rewrite a card mid-decision
# --------------------------------------------------------------------------- #
def labels(*names):
    """Mimic `gh issue list --json labels` (list of objects)."""
    return [{"name": n} for n in names]


def test_is_refreshable_pure_needs_decision():
    check("guard: pure needs-decision card is refreshable",
          rc.is_refreshable(labels("needs-decision", "repo:lavish-axi",
                                   "kind:pr-review", "priority:med",
                                   "target:lavish-axi-42")) is True)
    check("guard: no labels -> NOT refreshable", rc.is_refreshable([]) is False)
    check("guard: None labels -> NOT refreshable", rc.is_refreshable(None) is False)
    check("guard: missing needs-decision -> NOT refreshable",
          rc.is_refreshable(labels("repo:lavish-axi", "kind:pr-review",
                                   "priority:med", "target:lavish-axi-42")) is False)


def test_is_refreshable_blocks_mid_decision():
    check("guard: processing card is NOT refreshable",
          rc.is_refreshable(labels("needs-decision", "processing")) is False)
    check("guard: resolved card is NOT refreshable",
          rc.is_refreshable(labels("resolved")) is False)
    check("guard: blocked card is NOT refreshable",
          rc.is_refreshable(labels("blocked", "repo:lavish-axi")) is False)


def test_is_refreshable_accepts_plain_strings():
    # reconcile passes label objects; defend the plain-string shape too.
    check("guard: plain-string labels handled",
          rc.is_refreshable(["needs-decision", "kind:pr-review"]) is True)
    check("guard: plain-string labels missing needs-decision blocked",
          rc.is_refreshable(["kind:pr-review"]) is False)
    check("guard: plain-string processing blocked",
          rc.is_refreshable(["needs-decision", "processing"]) is False)


def test_upsert_refetches_known_card_before_refresh():
    calls = {"refresh": 0, "create": 0}
    existing = {
        "number": 7,
        "body": rc.render(item())["body"],
        "labels": labels("needs-decision", "repo:lavish-axi", "kind:pr-review",
                         "priority:med", "target:lavish-axi-42"),
    }
    current = {
        "number": 7,
        "body": existing["body"],
        "labels": labels("needs-decision", "processing", "repo:lavish-axi",
                         "kind:pr-review", "priority:med", "target:lavish-axi-42"),
        "state": "OPEN",
    }

    old_get_card = rc.get_card
    old_refresh = rc._refresh_card
    old_create = rc._create_card
    old_ensure = rc.ensure_labels
    rc.get_card = lambda number: current if int(number) == 7 else None
    rc._refresh_card = lambda *args: calls.__setitem__("refresh", calls["refresh"] + 1)
    rc._create_card = lambda *args: calls.__setitem__("create", calls["create"] + 1)
    rc.ensure_labels = lambda labels_: None
    try:
        result = rc.upsert_card(item(priority="high"), existing=existing)
    finally:
        rc.get_card = old_get_card
        rc._refresh_card = old_refresh
        rc._create_card = old_create
        rc.ensure_labels = old_ensure

    check("upsert: known card number is returned", result == 7)
    check("upsert: current processing card is not refreshed", calls["refresh"] == 0)
    check("upsert: current processing card does not duplicate", calls["create"] == 0)


def test_upsert_parses_state_block_after_refetch():
    calls = {"refresh": 0, "old_state": None}
    existing = {
        "number": 7,
        "body": rc.render(item())["body"],
        "labels": labels("needs-decision", "repo:lavish-axi", "kind:pr-review",
                         "priority:med", "target:lavish-axi-42"),
    }
    current = {
        "number": 7,
        "body": existing["body"],
        "labels": existing["labels"],
        "state": "OPEN",
    }

    def fake_refresh(number, card, existing_, item_, old_state):
        calls["refresh"] += 1
        calls["old_state"] = old_state
        return number

    old_get_card = rc.get_card
    old_refresh = rc._refresh_card
    old_ensure = rc.ensure_labels
    rc.get_card = lambda number: current if int(number) == 7 else None
    rc._refresh_card = fake_refresh
    rc.ensure_labels = lambda labels_: None
    try:
        result = rc.upsert_card(item(priority="high"), existing=existing)
    finally:
        rc.get_card = old_get_card
        rc._refresh_card = old_refresh
        rc.ensure_labels = old_ensure

    check("upsert: refreshable refetched card is refreshed", result == 7 and calls["refresh"] == 1)
    check("upsert: parsed state block used instead of issue state",
          isinstance(calls["old_state"], dict) and calls["old_state"].get("priority") == "med")


def test_refresh_preserves_same_head_triage_cache_and_section():
    it = item()
    triaged = rc.body_with_triage_result(
        rc.body_with_triage_queued(rc.render(it)["body"], it),
        it["head_sha"],
        triage={
            "summary": "Keeps useful context.",
            "product_implications": "No product risk.",
            "recommended_next_step": "merge - still safe.",
        },
    )
    existing = {
        "body": triaged,
        "labels": labels("needs-decision", "repo:lavish-axi", "kind:pr-review",
                         "priority:med", "target:lavish-axi-42"),
    }
    old_state = core.parse_state_block(triaged)
    card = rc.render(item(priority="high"))
    calls = {}

    old_write = rc._write_body
    old_gh = rc._gh
    old_unlink = rc.os.unlink

    def fake_write(body):
        calls["body"] = body
        return "/tmp/wheelhouse-test-body"

    rc._write_body = fake_write
    rc._gh = lambda args, check=True: None
    rc.os.unlink = lambda path: None
    try:
        rc._refresh_card(7, card, existing, item(priority="high"), old_state)
    finally:
        rc._write_body = old_write
        rc._gh = old_gh
        rc.os.unlink = old_unlink

    state = core.parse_state_block(calls["body"])
    check("refresh: same-head triage section is preserved", "Keeps useful context." in calls["body"])
    check("refresh: same-head triaged_sha is preserved", state.get("triaged_sha") == it["head_sha"])
    check("refresh: material priority still updates", state.get("priority") == "high")


def test_refresh_drops_triage_when_head_changes():
    old = item()
    triaged = rc.body_with_triage_result(
        rc.body_with_triage_queued(rc.render(old)["body"], old),
        old["head_sha"],
        triage={
            "summary": "Old head context.",
            "product_implications": "No longer current.",
            "recommended_next_step": "merge - old head.",
        },
    )
    existing = {
        "body": triaged,
        "labels": labels("needs-decision", "repo:lavish-axi", "kind:pr-review",
                         "priority:med", "target:lavish-axi-42"),
    }
    new = item(head_sha="newhead999")
    card = rc.render(new)
    calls = {"comments": []}

    old_write = rc._write_body
    old_gh = rc._gh
    old_unlink = rc.os.unlink

    def fake_write(body):
        calls["body"] = body
        return "/tmp/wheelhouse-test-body"

    rc._write_body = fake_write
    rc._gh = lambda args, check=True: calls["comments"].append(args) if "comment" in args else None
    rc.os.unlink = lambda path: None
    try:
        rc._refresh_card(7, card, existing, new, core.parse_state_block(triaged))
    finally:
        rc._write_body = old_write
        rc._gh = old_gh
        rc.os.unlink = old_unlink

    state = core.parse_state_block(calls["body"])
    check("refresh: new head drops old triage section", "Old head context." not in calls["body"])
    check("refresh: new head drops triaged_sha", "triaged_sha" not in state)
    check("refresh: new head state is current", state.get("head_sha") == "newhead999")


def test_refresh_drops_triage_when_kind_changes():
    old = item()
    triaged = rc.body_with_triage_result(
        rc.body_with_triage_queued(rc.render(old)["body"], old),
        old["head_sha"],
        triage={
            "summary": "PR review context.",
            "product_implications": "Only valid for PR review.",
            "recommended_next_step": "merge - old kind.",
        },
    )
    existing = {
        "body": triaged,
        "labels": labels("needs-decision", "repo:lavish-axi", "kind:pr-review",
                         "priority:med", "target:lavish-axi-42"),
    }
    new = item(kind="ci-approval", options=["approve-ci", "close", "hold"])
    card = rc.render(new)
    calls = {}

    old_write = rc._write_body
    old_gh = rc._gh
    old_unlink = rc.os.unlink

    def fake_write(body):
        calls["body"] = body
        return "/tmp/wheelhouse-test-body"

    rc._write_body = fake_write
    rc._gh = lambda args, check=True: None
    rc.os.unlink = lambda path: None
    try:
        rc._refresh_card(7, card, existing, new, core.parse_state_block(triaged))
    finally:
        rc._write_body = old_write
        rc._gh = old_gh
        rc.os.unlink = old_unlink

    state = core.parse_state_block(calls["body"])
    check("refresh: same-head kind change drops triage section", "PR review context." not in calls["body"])
    check("refresh: same-head kind change drops triaged_sha", "triaged_sha" not in state)
    check("refresh: same-head kind change keeps new kind", state.get("kind") == "ci-approval")


# --------------------------------------------------------------------------- #
# label replace: stale managed labels removed, needs-decision + human kept
# --------------------------------------------------------------------------- #
def test_plan_label_update_replaces_stale_managed():
    desired = rc.card_labels(item(priority="high", kind="ci-approval"))
    # The card currently has the OLD priority/kind labels.
    current = labels("needs-decision", "repo:lavish-axi", "kind:pr-review",
                     "priority:med", "target:lavish-axi-42")
    to_add, to_remove = rc.plan_label_update(desired, current)
    check("label: new priority/kind added",
          "priority:high" in to_add and "kind:ci-approval" in to_add)
    check("label: stale priority/kind removed",
          "priority:med" in to_remove and "kind:pr-review" in to_remove)
    check("label: unchanged managed label (repo/target) not re-added or removed",
          "repo:lavish-axi" not in to_add and "repo:lavish-axi" not in to_remove
          and "target:lavish-axi-42" not in to_remove)
    check("label: needs-decision never removed", "needs-decision" not in to_remove)


def test_plan_label_update_keeps_human_labels():
    desired = rc.card_labels(item())
    current = labels("needs-decision", "repo:lavish-axi", "kind:pr-review",
                     "priority:med", "target:lavish-axi-42", "wontfix", "good-first-issue")
    to_add, to_remove = rc.plan_label_update(desired, current)
    check("label: human-added labels are never removed",
          "wontfix" not in to_remove and "good-first-issue" not in to_remove)


def test_plan_label_update_noop_when_identical():
    desired = rc.card_labels(item())
    current = labels(*desired)  # same set already present
    to_add, to_remove = rc.plan_label_update(desired, current)
    check("label: identical labels -> nothing to add", to_add == [])
    check("label: identical labels -> nothing to remove", to_remove == [])


def main():
    test_render_shows_author_without_mention()
    test_state_block_carries_material_fields()
    test_material_changed_round_trip_is_noop()
    test_each_material_field_triggers_a_change()
    test_options_set_change_triggers_but_reorder_does_not()
    test_render_preserves_options_order_in_state_block()
    test_non_material_change_is_not_a_trigger()
    test_legacy_card_missing_new_fields_refreshes_once()
    test_legacy_triage_marker_still_parses_for_change_check()
    test_change_check_handles_missing_state()
    test_is_refreshable_pure_needs_decision()
    test_is_refreshable_blocks_mid_decision()
    test_is_refreshable_accepts_plain_strings()
    test_upsert_refetches_known_card_before_refresh()
    test_upsert_parses_state_block_after_refetch()
    test_refresh_preserves_same_head_triage_cache_and_section()
    test_refresh_drops_triage_when_head_changes()
    test_refresh_drops_triage_when_kind_changes()
    test_plan_label_update_replaces_stale_managed()
    test_plan_label_update_keeps_human_labels()
    test_plan_label_update_noop_when_identical()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all card-refresh tests passed")


if __name__ == "__main__":
    main()
