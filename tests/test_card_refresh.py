#!/usr/bin/env python3
"""
Unit-exercise the card-refresh and activity-reflection logic with NO network.

Run: python tests/test_card_refresh.py   (stdlib only; exits non-zero on failure)

An open decision card must reflect CURRENT target state, not just the snapshot
taken when it was created. These tests cover the pure pieces both the
event path (`render_card.upsert_card`) and the backstop (`reconcile.py`) rely on:

  * change detection - `material_changed` is true iff a material field
    (head_sha / compliance / tests / kind / priority / options) differs from
    the card's stored state, with legacy cards missing the new fields treated
    as changed exactly once (a safe one-time refresh that backfills them), and
    the legacy `triage-state` marker still parsing;
  * the refreshability guard - `is_refreshable` refuses to rewrite a card that
    is mid-decision (`processing`/`resolved`/`blocked`), so a refresh never
    clobbers an in-flight decision or races the handler;
  * target activity reflection - a newer target `updated_at` makes one hidden
    state-only edit to `activity_reflected_at` for GitHub Recently updated sort,
    while malformed timestamps, non-pending cards, and legacy cards whose issue
    `updatedAt` is already newer are no-ops;
  * the label replace - `plan_label_update` removes stale wheelhouse-managed
    labels (`repo:`/`kind:`/`priority:`/`target:`) while keeping
    `needs-decision` and any human-added label, and is a no-op when nothing
    changed. Held-card tests cover the same helper's exact `pending-triage`
    label sync;
  * the state block now carries the material fields and round-trips, so the
    change check is cheap and deterministic.
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)
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
        "repo": "lavish-axi",
        "number": 42,
        "kind": "pr-review",
        "head_sha": "abc1234def",
        "title": "Add a thing",
        "author": "someone",
        "bucket": "merge-ready",
        "comp": "pass",
        "tests": "green",
        "updated_at": "2024-01-01T00:00:00Z",
        "url": "https://github.com/o/lavish-axi/pull/42",
        "summary": "compliance=pass tests=green",
        "recommendation": "Merge it.",
        "priority": "med",
        "options": ["merge", "close", "hold"],
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
    check(
        "state: carries activity_reflected_at",
        st.get("activity_reflected_at") == "2024-01-01T00:00:00Z",
    )
    check("state: options is material", "options" in rc.MATERIAL_FIELDS)
    # legacy fields are still there (the handler reads these).
    check(
        "state: still carries repo/number/options",
        st.get("repo") == "lavish-axi"
        and st.get("number") == 42
        and st.get("options") == ["merge", "close", "hold"],
    )


# --------------------------------------------------------------------------- #
# change detection
# --------------------------------------------------------------------------- #
def test_material_changed_round_trip_is_noop():
    it = item()
    check(
        "change: a card vs its own freshly rendered state -> unchanged",
        rc.material_changed(it, state_of(it)) is False,
    )


def test_each_material_field_triggers_a_change():
    it = item()
    st = state_of(it)
    check(
        "change: head_sha differs -> changed",
        rc.material_changed(item(head_sha="9999999"), st) is True,
    )
    check(
        "change: compliance differs -> changed",
        rc.material_changed(item(comp="fail"), st) is True,
    )
    check(
        "change: tests differs -> changed",
        rc.material_changed(item(tests="fail"), st) is True,
    )
    check(
        "change: kind differs -> changed",
        rc.material_changed(item(kind="ci-approval"), st) is True,
    )
    check(
        "change: priority differs -> changed",
        rc.material_changed(item(priority="high"), st) is True,
    )


def test_options_set_change_triggers_but_reorder_does_not():
    it = item(options=["merge", "close", "hold"])
    st = state_of(it)
    check(
        "change: option removed -> changed",
        rc.material_changed(item(options=["merge", "hold"]), st) is True,
    )
    check(
        "change: option added -> changed",
        rc.material_changed(item(options=["merge", "close", "hold", "investigate"]), st)
        is True,
    )
    check(
        "change: options reordered -> NOT changed",
        rc.material_changed(item(options=["hold", "close", "merge"]), st) is False,
    )


def test_render_preserves_options_order_in_state_block():
    st = state_of(item(options=["hold", "merge", "close"]))
    check(
        "state: options order stays as provided",
        st.get("options") == ["hold", "merge", "close"],
    )


def test_render_filters_non_checkbox_custom_options():
    it = item(
        options=[
            "request-changes",
            "merge",
            "comment",
            "decline",
            "bogus",
            "merge",
            "hold",
        ]
    )
    card = rc.render(it)
    st = core.parse_state_block(card["body"])
    check(
        "state: custom options keep only checkbox actions",
        st.get("options") == ["merge", "hold"],
    )
    check(
        "render: request-changes checkbox omitted",
        "opt:request-changes" not in card["body"],
    )
    check("render: comment checkbox omitted", "opt:comment" not in card["body"])
    check("render: decline checkbox omitted", "opt:decline" not in card["body"])
    check("render: unknown checkbox omitted", "opt:bogus" not in card["body"])
    check("render: valid checkbox remains", "opt:merge" in card["body"])


def test_non_material_change_is_not_a_trigger():
    # Title / summary / recommendation re-render naturally - they must NOT flag
    # a material change on their own.
    it = item()
    st = state_of(it)
    check(
        "change: title-only change -> NOT changed",
        rc.material_changed(item(title="Totally different title"), st) is False,
    )
    check(
        "change: summary/recommendation-only change -> NOT changed",
        rc.material_changed(item(summary="x", recommendation="y"), st) is False,
    )
    with_activity = dict(st)
    with_activity["activity_reflected_at"] = "2024-06-01T00:00:00Z"
    check(
        "change: activity_reflected_at-only change -> NOT material",
        rc.material_changed(item(), with_activity) is False,
    )
    check(
        "activity_reflected_at not in MATERIAL_FIELDS",
        "activity_reflected_at" not in rc.MATERIAL_FIELDS,
    )


def test_legacy_card_missing_new_fields_refreshes_once():
    # A card written before the refresh feature carries only the old fields.
    legacy_body = (
        '<!-- wheelhouse-state: {"repo":"lavish-axi","number":42,'
        '"kind":"pr-review","head_sha":"abc1234def",'
        '"options":["merge","close","hold"]} -->'
    )
    legacy = core.parse_state_block(legacy_body)
    it = item()  # same target, same head_sha
    check(
        "legacy: missing comp/tests/priority -> changed (one safe refresh)",
        rc.material_changed(it, legacy) is True,
    )
    # After that one refresh the state carries the full set, so it no-ops.
    check(
        "legacy: after refresh the same item is a no-op",
        rc.material_changed(it, state_of(it)) is False,
    )


def test_legacy_triage_marker_still_parses_for_change_check():
    legacy_body = (
        '<!-- triage-state: {"repo":"lavish-axi","number":42,'
        '"kind":"pr-review","head_sha":"abc1234def",'
        '"options":["merge","close","hold"]} -->'
    )
    st = core.parse_state_block(legacy_body)
    check("legacy: triage-state marker parses", st is not None and st["number"] == 42)
    check(
        "legacy: triage-state card flagged changed (backfills new fields)",
        rc.material_changed(item(), st) is True,
    )


def test_change_check_handles_missing_state():
    check(
        "change: None state -> changed (safe refresh)",
        rc.material_changed(item(), None) is True,
    )


# --------------------------------------------------------------------------- #
# target activity reflection: hidden state-only maintenance write
# --------------------------------------------------------------------------- #
def test_activity_reflection_stamps_only_state_block_once():
    old = item(updated_at="2024-01-01T00:00:00Z")
    body = rc.render(old)["body"]
    new = item(updated_at="2024-06-01T00:00:00Z")
    labels_ = labels("needs-decision", "kind:pr-review")
    check(
        "activity: newer target updated_at needs reflection",
        rc.activity_reflection_needed(
            new,
            core.parse_state_block(body),
            labels_,
            card_updated_at="2024-01-02T00:00:00Z",
        )
        is True,
    )
    stamped = rc.body_with_activity_reflected(
        body, new, card_updated_at="2024-01-02T00:00:00Z"
    )
    check("activity: body changed", stamped != body)
    check(
        "activity: only hidden state block changed",
        rc._STATE_BLOCK_RE.sub("STATE", stamped)
        == rc._STATE_BLOCK_RE.sub("STATE", body),
    )
    stamped_state = core.parse_state_block(stamped)
    check(
        "activity: state stamp advanced",
        stamped_state.get("activity_reflected_at") == "2024-06-01T00:00:00Z",
    )
    check(
        "activity: second pass is a no-op",
        rc.body_with_activity_reflected(
            stamped, new, card_updated_at="2024-01-02T00:00:00Z"
        )
        == stamped,
    )


def test_activity_reflection_guards_non_refreshable_and_bad_timestamps():
    st = state_of(item())
    newer = item(updated_at="2024-06-01T00:00:00Z")
    check(
        "activity: processing card is skipped",
        rc.activity_reflection_needed(
            newer, st, labels("needs-decision", "processing"), "2024-01-02T00:00:00Z"
        )
        is False,
    )
    check(
        "activity: missing needs-decision is skipped",
        rc.activity_reflection_needed(
            newer, st, labels("kind:pr-review"), "2024-01-02T00:00:00Z"
        )
        is False,
    )
    check(
        "activity: missing target updated_at is skipped",
        rc.activity_reflection_needed(
            item(updated_at=""), st, labels("needs-decision"), "2024-01-02T00:00:00Z"
        )
        is False,
    )
    check(
        "activity: malformed target updated_at is skipped",
        rc.activity_reflection_needed(
            item(updated_at="not-a-date"),
            st,
            labels("needs-decision"),
            "2024-01-02T00:00:00Z",
        )
        is False,
    )
    check(
        "activity: missing state is skipped",
        rc.activity_reflection_needed(
            newer, None, labels("needs-decision"), "2024-01-02T00:00:00Z"
        )
        is False,
    )


def test_activity_reflection_legacy_uses_card_updated_at_baseline():
    body = rc.render(item(updated_at="2024-01-01T00:00:00Z"))["body"]
    legacy_state = core.parse_state_block(body)
    legacy_state.pop("activity_reflected_at", None)
    legacy_body = rc._replace_state_block(body, legacy_state)
    same_or_older = item(updated_at="2024-01-02T00:00:00Z")
    newer = item(updated_at="2024-06-01T00:00:00Z")
    check(
        "activity: legacy card does not stamp when card updated_at is newer",
        rc.body_with_activity_reflected(
            legacy_body, same_or_older, card_updated_at="2024-01-03T00:00:00Z"
        )
        == legacy_body,
    )
    stamped = rc.body_with_activity_reflected(
        legacy_body, newer, card_updated_at="2024-01-03T00:00:00Z"
    )
    check(
        "activity: legacy card stamps after newer target activity",
        stamped != legacy_body,
    )
    check(
        "activity: legacy card without baseline does not one-time stamp",
        rc.body_with_activity_reflected(legacy_body, newer) == legacy_body,
    )


# --------------------------------------------------------------------------- #
# render_version: a non-material, one-time re-render trigger for display-only
# fixes (e.g. dropping the author @mention) that have no material trigger.
# --------------------------------------------------------------------------- #
def test_render_stamps_current_render_version():
    st = state_of(item())
    check(
        "render: stamps current render_version",
        st.get("render_version") == rc.CARD_RENDER_VERSION,
    )


def test_render_stale_true_when_missing_or_older():
    check(
        "render_stale: missing render_version -> stale",
        rc.render_stale({"head_sha": "abc"}) is True,
    )
    check("render_stale: None state -> stale", rc.render_stale(None) is True)
    check(
        "render_stale: older render_version -> stale",
        rc.render_stale({"render_version": rc.CARD_RENDER_VERSION - 1}) is True,
    )
    check(
        "render_stale: malformed render_version -> stale",
        rc.render_stale({"render_version": "not-a-version"}) is True,
    )


def test_render_stale_false_when_current_or_newer():
    st = state_of(item())
    check(
        "render_stale: freshly rendered state -> NOT stale",
        rc.render_stale(st) is False,
    )
    check(
        "render_stale: newer render_version -> NOT stale",
        rc.render_stale({"render_version": rc.CARD_RENDER_VERSION + 1}) is False,
    )


def test_render_version_is_not_material():
    it = item()
    st = state_of(it)
    stale_state = dict(st)
    stale_state["render_version"] = 0
    check(
        "change: render_version-only difference -> NOT material",
        rc.material_changed(it, stale_state) is False,
    )
    check(
        "render_version not in MATERIAL_FIELDS",
        "render_version" not in rc.MATERIAL_FIELDS,
    )


def test_upsert_refreshes_once_on_render_version_alone():
    """A pure needs-decision card with a stale render_version but otherwise
    unchanged material state refreshes exactly once, then no-ops (self-
    terminating), and does NOT emit the 'Target updated' comment since
    head_sha is unchanged."""
    it = item()
    fresh_body = rc.render(it)["body"]
    stale_state = core.parse_state_block(fresh_body)
    stale_state["render_version"] = 0
    stale_body = rc._replace_state_block(fresh_body, stale_state)
    existing = {
        "number": 7,
        "body": stale_body,
        "labels": labels(
            "needs-decision",
            "repo:lavish-axi",
            "kind:pr-review",
            "priority:med",
            "target:lavish-axi-42",
        ),
    }

    calls = {"comments": []}
    old_write = rc._write_body
    old_gh = rc._gh
    old_unlink = rc.os.unlink
    old_get_card = rc.get_card
    old_ensure = rc.ensure_labels

    def fake_write(body):
        calls["body"] = body
        return "/tmp/wheelhouse-test-body"

    rc._write_body = fake_write
    rc._gh = (
        lambda args, check=True: calls["comments"].append(args)
        if "comment" in args
        else None
    )
    rc.os.unlink = lambda path: None
    rc.get_card = lambda number: existing if int(number) == 7 else None
    rc.ensure_labels = lambda labels_: None
    try:
        result = rc.upsert_card(it, existing=existing)
    finally:
        rc._write_body = old_write
        rc._gh = old_gh
        rc.os.unlink = old_unlink
        rc.get_card = old_get_card
        rc.ensure_labels = old_ensure

    check("upsert: render-stale-only card is refreshed once", result == 7)
    new_state = core.parse_state_block(calls.get("body", ""))
    check(
        "upsert: refreshed body carries current render_version",
        new_state is not None
        and new_state.get("render_version") == rc.CARD_RENDER_VERSION,
    )
    check(
        "upsert: render-version refresh carries activity_reflected_at",
        new_state.get("activity_reflected_at") == it["updated_at"],
    )
    check(
        "upsert: same-head render-version refresh emits NO 'Target updated' comment",
        calls["comments"] == [],
    )

    # Self-terminating: refreshing again from the now-current body is a no-op.
    calls2 = {"comments": []}
    existing2 = dict(existing, body=calls["body"])
    rc._gh = (
        lambda args, check=True: calls2["comments"].append(args)
        if "comment" in args
        else None
    )
    rc.get_card = lambda number: existing2 if int(number) == 7 else None
    rc.ensure_labels = lambda labels_: None
    try:
        result2 = rc.upsert_card(it, existing=existing2)
    finally:
        rc._gh = old_gh
        rc.get_card = old_get_card
        rc.ensure_labels = old_ensure
    check("upsert: second call after refresh is a no-op", result2 == 7)
    check("upsert: no-op emits no comment", calls2["comments"] == [])


def test_render_version_refresh_preserves_triage_section():
    """A render-version-only refresh is a same-head_sha cosmetic refresh, so it
    must preserve an existing ### Triage section and its triaged_sha/status
    cache exactly like a material same-head refresh does."""
    it = item()
    triaged = rc.body_with_triage_result(
        rc.body_with_triage_queued(rc.render(it)["body"], it),
        it["head_sha"],
        triage={
            "summary": "Still useful context.",
            "product_implications": "No product risk.",
            "evidence": "target.txt: quoted a line from the change",
            "recommended_next_step": "merge - still safe.",
        },
    )
    stale_state = core.parse_state_block(triaged)
    stale_state["render_version"] = 0
    stale_body = rc._replace_state_block(triaged, stale_state)
    existing = {
        "number": 7,
        "body": stale_body,
        "labels": labels(
            "needs-decision",
            "repo:lavish-axi",
            "kind:pr-review",
            "priority:med",
            "target:lavish-axi-42",
        ),
    }

    calls = {"comments": []}
    old_write = rc._write_body
    old_gh = rc._gh
    old_unlink = rc.os.unlink
    old_get_card = rc.get_card
    old_ensure = rc.ensure_labels

    def fake_write(body):
        calls["body"] = body
        return "/tmp/wheelhouse-test-body"

    rc._write_body = fake_write
    rc._gh = (
        lambda args, check=True: calls["comments"].append(args)
        if "comment" in args
        else None
    )
    rc.os.unlink = lambda path: None
    rc.get_card = lambda number: existing if int(number) == 7 else None
    rc.ensure_labels = lambda labels_: None
    try:
        rc.upsert_card(it, existing=existing)
    finally:
        rc._write_body = old_write
        rc._gh = old_gh
        rc.os.unlink = old_unlink
        rc.get_card = old_get_card
        rc.ensure_labels = old_ensure

    new_state = core.parse_state_block(calls.get("body", ""))
    check(
        "render-version refresh: triage section preserved",
        "Still useful context." in calls.get("body", ""),
    )
    check(
        "render-version refresh: triaged_sha preserved",
        new_state is not None and new_state.get("triaged_sha") == it["head_sha"],
    )
    check(
        "render-version refresh: no 'Target updated' comment", calls["comments"] == []
    )


def test_render_version_refresh_labels_preserved_triage_status_lines():
    it = item()
    triaged = rc.body_with_triage_result(
        rc.body_with_triage_queued(rc.render(it)["body"], it),
        it["head_sha"],
        triage={
            "summary": "Waited for background terminal 60s.",
            "product_implications": "No product risk.",
            "evidence": "target.txt: quoted a line from the change",
            "recommended_next_step": "merge - still safe.",
        },
    )
    legacy = triaged.replace(
        "`[automated status]` Waited for background terminal 60s.",
        "Waited for background terminal 60s.",
    )
    stale_state = core.parse_state_block(legacy)
    stale_state["render_version"] = rc.CARD_RENDER_VERSION - 1
    stale_body = rc._replace_state_block(legacy, stale_state)
    existing = {
        "number": 7,
        "body": stale_body,
        "labels": labels(
            "needs-decision",
            "repo:lavish-axi",
            "kind:pr-review",
            "priority:med",
            "target:lavish-axi-42",
        ),
    }

    calls = {"comments": []}
    old_write = rc._write_body
    old_gh = rc._gh
    old_unlink = rc.os.unlink
    old_get_card = rc.get_card
    old_ensure = rc.ensure_labels

    def fake_write(body):
        calls["body"] = body
        return "/tmp/wheelhouse-test-body"

    rc._write_body = fake_write
    rc._gh = (
        lambda args, check=True: calls["comments"].append(args)
        if "comment" in args
        else None
    )
    rc.os.unlink = lambda path: None
    rc.get_card = lambda number: existing if int(number) == 7 else None
    rc.ensure_labels = lambda labels_: None
    try:
        rc.upsert_card(it, existing=existing)
    finally:
        rc._write_body = old_write
        rc._gh = old_gh
        rc.os.unlink = old_unlink
        rc.get_card = old_get_card
        rc.ensure_labels = old_ensure

    body = calls.get("body", "")
    state = core.parse_state_block(body)
    labeled_line = (
        "- **Summary:** `[automated status]` Waited for background terminal 60s."
    )
    check(
        "render-version refresh: preserved status line is labeled",
        labeled_line in body,
    )
    check(
        "render-version refresh: status line is not double-labeled",
        body.count("`[automated status]` Waited for background terminal 60s.") == 1,
    )
    check(
        "render-version refresh: labeled body stamped current version",
        state is not None and state.get("render_version") == rc.CARD_RENDER_VERSION,
    )


def test_render_version_refresh_qualifies_stale_triage_refs():
    """Retroactive fix (see AGENTS.md): a render-version-behind card carries a
    triage section cached before cross-repo qualification existed. The next
    refresh must re-qualify its bare `#N` refs and stamp the current
    render_version, both from GITHUB_REPOSITORY_OWNER + the card's own
    deterministic state repo - never from the model text."""
    it = item()
    triaged = rc.body_with_triage_result(
        rc.body_with_triage_queued(rc.render(it)["body"], it),
        it["head_sha"],
        triage={
            "summary": "Landed in #127 already.",
            "product_implications": "No product risk.",
            "evidence": "target.txt: quoted a line from the change",
            "recommended_next_step": "merge - fixed by #127.",
        },
    )
    stale_state = core.parse_state_block(triaged)
    stale_state["render_version"] = 1
    stale_body = rc._replace_state_block(triaged, stale_state)
    existing = {
        "number": 7,
        "body": stale_body,
        "labels": labels(
            "needs-decision",
            "repo:lavish-axi",
            "kind:pr-review",
            "priority:med",
            "target:lavish-axi-42",
        ),
    }

    calls = {"comments": []}
    old_write = rc._write_body
    old_gh = rc._gh
    old_unlink = rc.os.unlink
    old_get_card = rc.get_card
    old_ensure = rc.ensure_labels
    old_owner = os.environ.get("GITHUB_REPOSITORY_OWNER")

    def fake_write(body):
        calls["body"] = body
        return "/tmp/wheelhouse-test-body"

    rc._write_body = fake_write
    rc._gh = (
        lambda args, check=True: calls["comments"].append(args)
        if "comment" in args
        else None
    )
    rc.os.unlink = lambda path: None
    rc.get_card = lambda number: existing if int(number) == 7 else None
    rc.ensure_labels = lambda labels_: None
    os.environ["GITHUB_REPOSITORY_OWNER"] = "kunchenguid"
    try:
        rc.upsert_card(it, existing=existing)
    finally:
        rc._write_body = old_write
        rc._gh = old_gh
        rc.os.unlink = old_unlink
        rc.get_card = old_get_card
        rc.ensure_labels = old_ensure
        if old_owner is None:
            os.environ.pop("GITHUB_REPOSITORY_OWNER", None)
        else:
            os.environ["GITHUB_REPOSITORY_OWNER"] = old_owner

    body = calls.get("body", "")
    new_state = core.parse_state_block(body)
    check(
        "render-version refresh: stale bare ref qualified",
        "kunchenguid/lavish-axi#127" in body,
    )
    check(
        "render-version refresh: no bare #127 remains",
        "Landed in #127 already." not in body,
    )
    check(
        "render-version refresh: stamped current render_version",
        new_state is not None
        and new_state.get("render_version") == rc.CARD_RENDER_VERSION,
    )
    check(
        "render-version refresh: triaged_sha still preserved",
        new_state is not None and new_state.get("triaged_sha") == it["head_sha"],
    )


def test_render_version_current_and_qualified_triage_is_noop():
    """A card already at the current render_version, whose triage section is
    already qualified, must not be re-edited (idempotent - no churn loop)."""
    it = item()
    triaged = rc.body_with_triage_result(
        rc.body_with_triage_queued(rc.render(it)["body"], it),
        it["head_sha"],
        triage={
            "summary": "Landed in kunchenguid/lavish-axi#127 already.",
            "product_implications": "No product risk.",
            "evidence": "target.txt: quoted a line from the change",
            "recommended_next_step": "merge - fixed by kunchenguid/lavish-axi#127.",
        },
    )
    current_state = core.parse_state_block(triaged)
    check(
        "no-op fixture: render_version already current",
        current_state.get("render_version") == rc.CARD_RENDER_VERSION,
    )
    existing = {
        "number": 7,
        "body": triaged,
        "labels": labels(
            "needs-decision",
            "repo:lavish-axi",
            "kind:pr-review",
            "priority:med",
            "target:lavish-axi-42",
        ),
    }
    calls = {"refresh": 0}
    old_get_card = rc.get_card
    old_ensure = rc.ensure_labels
    old_refresh = rc._refresh_card
    old_owner = os.environ.get("GITHUB_REPOSITORY_OWNER")
    rc.get_card = lambda number: existing if int(number) == 7 else None
    rc.ensure_labels = lambda labels_: None
    rc._refresh_card = lambda *args, **kwargs: calls.__setitem__(
        "refresh", calls["refresh"] + 1
    )
    os.environ["GITHUB_REPOSITORY_OWNER"] = "kunchenguid"
    try:
        result = rc.upsert_card(it, existing=existing)
    finally:
        rc.get_card = old_get_card
        rc.ensure_labels = old_ensure
        rc._refresh_card = old_refresh
        if old_owner is None:
            os.environ.pop("GITHUB_REPOSITORY_OWNER", None)
        else:
            os.environ["GITHUB_REPOSITORY_OWNER"] = old_owner
    check("render-version current + qualified triage: no-op result", result == 7)
    check(
        "render-version current + qualified triage: no refresh", calls["refresh"] == 0
    )


def test_preserve_triage_leaves_already_qualified_urls_and_non_refs_untouched():
    """Direct test of `_preserve_same_revision_triage`: only a genuine bare
    `#N` autolink gets qualified. Already-qualified refs, full URLs, markdown
    link destinations, and non-reference `#` uses are left exactly as-is."""
    it = item()
    mixed = (
        "Bare #127. Already-qualified: see owner/other#5 for context. "
        "URL: see https://github.com/o/r/issues/127. "
        "Markdown link: [details](url#127). Not a ref: GH-123."
    )
    triaged = rc.body_with_triage_result(
        rc.body_with_triage_queued(rc.render(it)["body"], it),
        it["head_sha"],
        triage={
            "summary": mixed,
            "product_implications": "No product risk.",
            "evidence": "target.txt: quoted a line from the change",
            "recommended_next_step": "merge - still safe.",
        },
    )
    old_state = core.parse_state_block(triaged)
    fresh_body = rc.render(it)["body"]
    result = rc._preserve_same_revision_triage(
        fresh_body, triaged, it, old_state, owner="kunchenguid"
    )
    check("preserve: bare ref qualified", "kunchenguid/lavish-axi#127." in result)
    check(
        "preserve: already-qualified ref untouched",
        "owner/other#5" in result,
    )
    check(
        "preserve: URL untouched",
        "https://github.com/o/r/issues/127" in result,
    )
    check(
        "preserve: markdown link destination untouched",
        "[details](url#127)" in result,
    )
    check("preserve: non-ref GH-123 untouched", "GH-123" in result)


def test_preserve_triage_uses_state_repo_not_item_repo():
    """`owner`/`repo` for the retroactive qualification come from the card's
    deterministic `old_state["repo"]`, never from the item or model text -
    same trust rule as fresh triage rendering."""
    it = item(repo="attacker-controlled")
    triaged = rc.body_with_triage_result(
        rc.body_with_triage_queued(rc.render(item())["body"], item()),
        item()["head_sha"],
        triage={
            "summary": "See #127 for details.",
            "product_implications": "No product risk.",
            "evidence": "target.txt: quoted a line from the change",
            "recommended_next_step": "merge - still safe.",
        },
    )
    old_state = core.parse_state_block(triaged)
    check(
        "fixture: old_state repo is lavish-axi", old_state.get("repo") == "lavish-axi"
    )
    fresh_body = rc.render(it)["body"]
    result = rc._preserve_same_revision_triage(
        fresh_body, triaged, it, old_state, owner="kunchenguid"
    )
    check(
        "preserve: qualification uses state repo, not item repo",
        "kunchenguid/lavish-axi#127" in result,
    )
    check(
        "preserve: item's own repo never used for qualification",
        "attacker-controlled#127" not in result
        and "kunchenguid/attacker-controlled#127" not in result,
    )


def test_render_stale_alone_does_not_bypass_is_refreshable():
    """The is_refreshable guard still gates the render-version trigger: a
    processing/resolved/blocked card is never refreshed just because its
    render_version is stale."""
    it = item()
    fresh_body = rc.render(it)["body"]
    stale_state = core.parse_state_block(fresh_body)
    stale_state["render_version"] = 0
    stale_body = rc._replace_state_block(fresh_body, stale_state)
    existing = {
        "number": 7,
        "body": stale_body,
        "labels": labels(
            "needs-decision",
            "processing",
            "repo:lavish-axi",
            "kind:pr-review",
            "priority:med",
            "target:lavish-axi-42",
        ),
    }
    old_get_card = rc.get_card
    old_ensure = rc.ensure_labels
    rc.get_card = lambda number: existing if int(number) == 7 else None
    rc.ensure_labels = lambda labels_: None
    try:
        result = rc.upsert_card(it, existing=existing)
    finally:
        rc.get_card = old_get_card
        rc.ensure_labels = old_ensure
    check("upsert: render-stale processing card is NOT refreshed", result == 7)


# --------------------------------------------------------------------------- #
# refreshability guard: never rewrite a card mid-decision
# --------------------------------------------------------------------------- #
def labels(*names):
    """Mimic `gh issue list --json labels` (list of objects)."""
    return [{"name": n} for n in names]


def test_is_refreshable_pure_needs_decision():
    check(
        "guard: pure needs-decision card is refreshable",
        rc.is_refreshable(
            labels(
                "needs-decision",
                "repo:lavish-axi",
                "kind:pr-review",
                "priority:med",
                "target:lavish-axi-42",
            )
        )
        is True,
    )
    check("guard: no labels -> NOT refreshable", rc.is_refreshable([]) is False)
    check("guard: None labels -> NOT refreshable", rc.is_refreshable(None) is False)
    check(
        "guard: missing needs-decision -> NOT refreshable",
        rc.is_refreshable(
            labels(
                "repo:lavish-axi",
                "kind:pr-review",
                "priority:med",
                "target:lavish-axi-42",
            )
        )
        is False,
    )


def test_is_refreshable_blocks_mid_decision():
    check(
        "guard: processing card is NOT refreshable",
        rc.is_refreshable(labels("needs-decision", "processing")) is False,
    )
    check(
        "guard: resolved card is NOT refreshable",
        rc.is_refreshable(labels("resolved")) is False,
    )
    check(
        "guard: blocked card is NOT refreshable",
        rc.is_refreshable(labels("blocked", "repo:lavish-axi")) is False,
    )


def test_is_refreshable_accepts_plain_strings():
    # reconcile passes label objects; defend the plain-string shape too.
    check(
        "guard: plain-string labels handled",
        rc.is_refreshable(["needs-decision", "kind:pr-review"]) is True,
    )
    check(
        "guard: plain-string labels missing needs-decision blocked",
        rc.is_refreshable(["kind:pr-review"]) is False,
    )
    check(
        "guard: plain-string processing blocked",
        rc.is_refreshable(["needs-decision", "processing"]) is False,
    )


def test_upsert_refetches_known_card_before_refresh():
    calls = {"refresh": 0, "create": 0}
    existing = {
        "number": 7,
        "body": rc.render(item())["body"],
        "labels": labels(
            "needs-decision",
            "repo:lavish-axi",
            "kind:pr-review",
            "priority:med",
            "target:lavish-axi-42",
        ),
    }
    current = {
        "number": 7,
        "body": existing["body"],
        "labels": labels(
            "needs-decision",
            "processing",
            "repo:lavish-axi",
            "kind:pr-review",
            "priority:med",
            "target:lavish-axi-42",
        ),
        "state": "OPEN",
    }

    old_get_card = rc.get_card
    old_refresh = rc._refresh_card
    old_create = rc._create_card
    old_ensure = rc.ensure_labels
    rc.get_card = lambda number: current if int(number) == 7 else None
    rc._refresh_card = lambda *args, **kwargs: calls.__setitem__(
        "refresh", calls["refresh"] + 1
    )
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
    calls = {"refresh": 0, "old_state": None, "card_state": None}
    existing = {
        "number": 7,
        "body": rc.render(item())["body"],
        "labels": labels(
            "needs-decision",
            "repo:lavish-axi",
            "kind:pr-review",
            "priority:med",
            "target:lavish-axi-42",
        ),
    }
    current = {
        "number": 7,
        "body": existing["body"],
        "labels": existing["labels"],
        "state": "OPEN",
    }

    def fake_refresh(
        number,
        card,
        existing_,
        item_,
        old_state,
        preserve_triage=True,
        preserve_reconcile_absence=False,
    ):
        calls["refresh"] += 1
        calls["old_state"] = old_state
        calls["card_state"] = core.parse_state_block(card.get("body", ""))
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

    check(
        "upsert: refreshable refetched card is refreshed",
        result == 7 and calls["refresh"] == 1,
    )
    check(
        "upsert: parsed state block used instead of issue state",
        isinstance(calls["old_state"], dict)
        and calls["old_state"].get("priority") == "med",
    )
    check(
        "upsert: material refresh carries activity_reflected_at",
        calls["card_state"].get("activity_reflected_at")
        == item(priority="high")["updated_at"],
    )


def test_refresh_preserves_same_head_triage_cache_and_section():
    it = item()
    triaged = rc.body_with_triage_result(
        rc.body_with_triage_queued(rc.render(it)["body"], it),
        it["head_sha"],
        triage={
            "summary": "Keeps useful context.",
            "product_implications": "No product risk.",
            "evidence": "target.txt: quoted a line from the change",
            "recommended_next_step": "merge - still safe.",
        },
    )
    existing = {
        "body": triaged,
        "labels": labels(
            "needs-decision",
            "repo:lavish-axi",
            "kind:pr-review",
            "priority:med",
            "target:lavish-axi-42",
        ),
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
    check(
        "refresh: same-head triage section is preserved",
        "Keeps useful context." in calls["body"],
    )
    check(
        "refresh: same-head triaged_sha is preserved",
        state.get("triaged_sha") == it["head_sha"],
    )
    check("refresh: material priority still updates", state.get("priority") == "high")


def test_refresh_drops_triage_when_head_changes():
    old = item()
    triaged = rc.body_with_triage_result(
        rc.body_with_triage_queued(rc.render(old)["body"], old),
        old["head_sha"],
        triage={
            "summary": "Old head context.",
            "product_implications": "No longer current.",
            "evidence": "target.txt: quoted a line from the change",
            "recommended_next_step": "merge - old head.",
        },
    )
    existing = {
        "body": triaged,
        "labels": labels(
            "needs-decision",
            "repo:lavish-axi",
            "kind:pr-review",
            "priority:med",
            "target:lavish-axi-42",
        ),
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
    rc._gh = (
        lambda args, check=True: calls["comments"].append(args)
        if "comment" in args
        else None
    )
    rc.os.unlink = lambda path: None
    try:
        rc._refresh_card(7, card, existing, new, core.parse_state_block(triaged))
    finally:
        rc._write_body = old_write
        rc._gh = old_gh
        rc.os.unlink = old_unlink

    state = core.parse_state_block(calls["body"])
    check(
        "refresh: new head drops old triage section",
        "Old head context." not in calls["body"],
    )
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
            "evidence": "target.txt: quoted a line from the change",
            "recommended_next_step": "merge - old kind.",
        },
    )
    existing = {
        "body": triaged,
        "labels": labels(
            "needs-decision",
            "repo:lavish-axi",
            "kind:pr-review",
            "priority:med",
            "target:lavish-axi-42",
        ),
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
    check(
        "refresh: same-head kind change drops triage section",
        "PR review context." not in calls["body"],
    )
    check(
        "refresh: same-head kind change drops triaged_sha", "triaged_sha" not in state
    )
    check(
        "refresh: same-head kind change keeps new kind",
        state.get("kind") == "ci-approval",
    )


# --------------------------------------------------------------------------- #
# label replace: stale managed labels removed, needs-decision + human kept
# --------------------------------------------------------------------------- #
def test_plan_label_update_replaces_stale_managed():
    desired = rc.card_labels(item(priority="high", kind="ci-approval"))
    # The card currently has the OLD priority/kind labels.
    current = labels(
        "needs-decision",
        "repo:lavish-axi",
        "kind:pr-review",
        "priority:med",
        "target:lavish-axi-42",
    )
    to_add, to_remove = rc.plan_label_update(desired, current)
    check(
        "label: new priority/kind added",
        "priority:high" in to_add and "kind:ci-approval" in to_add,
    )
    check(
        "label: stale priority/kind removed",
        "priority:med" in to_remove and "kind:pr-review" in to_remove,
    )
    check(
        "label: unchanged managed label (repo/target) not re-added or removed",
        "repo:lavish-axi" not in to_add
        and "repo:lavish-axi" not in to_remove
        and "target:lavish-axi-42" not in to_remove,
    )
    check("label: needs-decision never removed", "needs-decision" not in to_remove)


def test_plan_label_update_keeps_human_labels():
    desired = rc.card_labels(item())
    current = labels(
        "needs-decision",
        "repo:lavish-axi",
        "kind:pr-review",
        "priority:med",
        "target:lavish-axi-42",
        "wontfix",
        "good-first-issue",
    )
    to_add, to_remove = rc.plan_label_update(desired, current)
    check(
        "label: human-added labels are never removed",
        "wontfix" not in to_remove and "good-first-issue" not in to_remove,
    )


def test_plan_label_update_noop_when_identical():
    desired = rc.card_labels(item())
    current = labels(*desired)  # same set already present
    to_add, to_remove = rc.plan_label_update(desired, current)
    check("label: identical labels -> nothing to add", to_add == [])
    check("label: identical labels -> nothing to remove", to_remove == [])


# --------------------------------------------------------------------------- #
# reconcile soft-close hysteresis is hidden, bounded, and non-material
# --------------------------------------------------------------------------- #
def test_reconcile_absence_schema_is_bounded_and_non_material():
    it = item()
    body = rc.render(it)["body"]
    first = rc.body_with_reconcile_absence(body, 1)
    first_state = core.parse_state_block(first)
    check(
        "absence: exact first-pass record round-trips",
        rc.reconcile_absence_count(first) == 1
        and first_state.get(rc.RECONCILE_ABSENCE_FIELD)
        == {"version": 1, "threshold": 2, "count": 1},
    )
    check(
        "absence: field is outside material semantics",
        rc.RECONCILE_ABSENCE_FIELD not in rc.MATERIAL_FIELDS
        and rc.material_changed(it, first_state) is False,
    )
    triaged = dict(first_state, triaged_sha=it["head_sha"], triage_status="succeeded")
    triaged_without = dict(triaged)
    triaged_without.pop(rc.RECONCILE_ABSENCE_FIELD, None)
    check(
        "absence: field does not affect triage freshness",
        rc.triage_fresh(it, triaged) == rc.triage_fresh(it, triaged_without) is True,
    )

    closed = rc.body_with_reconcile_absence(
        first, 2, closed_at="2026-07-13T12:00:00Z"
    )
    provenance = rc.reconcile_soft_close_provenance(closed)
    check(
        "absence: threshold record carries exact trusted provenance",
        rc.reconcile_absence_count(closed) == 2
        and provenance
        == {
            "actor": rc.RECONCILE_SOFT_CLOSE_ACTOR,
            "reason": rc.RECONCILE_SOFT_CLOSE_REASON,
            "at": "2026-07-13T12:00:00Z",
        },
    )
    check(
        "absence: unbounded count cannot be serialized",
        rc.body_with_reconcile_absence(first, 3) == first,
    )
    cleared = rc.body_without_reconcile_absence(closed)
    check(
        "absence: clear removes record without material change",
        not rc.reconcile_absence_needs_clear(cleared)
        and rc.material_changed(it, core.parse_state_block(cleared)) is False,
    )


def test_required_present_writes_fold_absence_reset():
    it = item(updated_at="2024-06-01T00:00:00Z")
    old = item(updated_at="2024-01-01T00:00:00Z")
    body = rc.body_with_reconcile_absence(rc.render(old)["body"], 1)
    reflected = rc.body_with_activity_reflected(
        body, it, card_updated_at="2024-01-02T00:00:00Z"
    )
    reflected_state = core.parse_state_block(reflected)
    check(
        "absence: activity write folds reset",
        reflected != body
        and reflected_state.get("activity_reflected_at") == it["updated_at"]
        and rc.RECONCILE_ABSENCE_FIELD not in reflected_state,
    )

    queued_source = rc.body_with_reconcile_absence(rc.render(it)["body"], 1)
    queued = rc.body_with_triage_queued(queued_source, it)
    queued_state = core.parse_state_block(queued)
    check(
        "absence: triage queued write folds reset",
        queued != queued_source
        and queued_state.get("triage_status") == "queued"
        and rc.RECONCILE_ABSENCE_FIELD not in queued_state,
    )
    check(
        "absence: fresh full render never inherits lifecycle state",
        rc.RECONCILE_ABSENCE_FIELD not in core.parse_state_block(rc.render(it)["body"]),
    )


def main():
    test_render_shows_author_without_mention()
    test_state_block_carries_material_fields()
    test_material_changed_round_trip_is_noop()
    test_each_material_field_triggers_a_change()
    test_options_set_change_triggers_but_reorder_does_not()
    test_render_preserves_options_order_in_state_block()
    test_render_filters_non_checkbox_custom_options()
    test_non_material_change_is_not_a_trigger()
    test_legacy_card_missing_new_fields_refreshes_once()
    test_legacy_triage_marker_still_parses_for_change_check()
    test_change_check_handles_missing_state()
    test_activity_reflection_stamps_only_state_block_once()
    test_activity_reflection_guards_non_refreshable_and_bad_timestamps()
    test_activity_reflection_legacy_uses_card_updated_at_baseline()
    test_render_stamps_current_render_version()
    test_render_stale_true_when_missing_or_older()
    test_render_stale_false_when_current_or_newer()
    test_render_version_is_not_material()
    test_upsert_refreshes_once_on_render_version_alone()
    test_render_version_refresh_preserves_triage_section()
    test_render_version_refresh_labels_preserved_triage_status_lines()
    test_render_version_refresh_qualifies_stale_triage_refs()
    test_render_version_current_and_qualified_triage_is_noop()
    test_preserve_triage_leaves_already_qualified_urls_and_non_refs_untouched()
    test_preserve_triage_uses_state_repo_not_item_repo()
    test_render_stale_alone_does_not_bypass_is_refreshable()
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
    test_reconcile_absence_schema_is_bounded_and_non_material()
    test_required_present_writes_fold_absence_reset()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all card-refresh tests passed")


if __name__ == "__main__":
    main()
