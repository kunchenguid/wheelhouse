#!/usr/bin/env python3
"""Offline regression coverage for the verified base/VISION context-refresh
allowance (audit F13).

A queued re-triage triggered ONLY by a verified base-SHA or VISION-SHA movement
against an unchanged head consumes a separate small bounded allowance, never
the ordinary per-head retry budget. Every use binds the exact (head, base,
VISION) identity, so a repeated context grants nothing; the daily UTC
reservation ledger, the sealed dispatch permit, idempotency, and G6 verdict
revalidation are unchanged. Exhaustion emits an explicit bounded diagnostic
and performs no dispatch.

Run: python tests/test_triage_context_allowance.py
"""

import copy
import io
import json
import os
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from types import SimpleNamespace

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import auto_merge  # noqa: E402
import build_item  # noqa: E402
import card_projection  # noqa: E402
import decision_context  # noqa: E402
import reconcile  # noqa: E402
import render_card as rc  # noqa: E402
import target_observation  # noqa: E402
import wheelhouse_core as core  # noqa: E402

# Spend-guard tests isolate reservation ordering from cross-repo gate reads.
rc._evaluate_automerge_card_projection = lambda *args, **kwargs: (
    rc.criteria_schema.unavailable_criteria("offline context-allowance fixture")
)

HEAD = "h" * 40
HEAD2 = "9" * 40
B1, B2, B3, B4 = "1" * 40, "2" * 40, "3" * 40, "4" * 40
V1, V2, V3 = "a" * 40, "b" * 40, "c" * 40
PURE = ["needs-decision", "kind:pr-review"]


@contextmanager
def patched(module, replacements):
    originals = {name: getattr(module, name) for name in replacements}
    for name, value in replacements.items():
        setattr(module, name, value)
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(module, name, value)


def item(base_sha=B1, vision_sha=V1, head=HEAD, allowance=2, cap=2, **overrides):
    base = {
        "repo": "wheelhouse",
        "number": 42,
        "kind": "pr-review",
        "head_sha": head,
        "updated_at": "",
        "title": "A bounded triage candidate",
        "author": "contributor",
        "bucket": "merge-ready",
        "comp": "pass",
        "tests": "green",
        "url": "https://github.com/example/wheelhouse/pull/42",
        "summary": "safe offline fixture",
        "priority": "med",
        "auto_triage": True,
        "auto_triage_issues": True,
        "triage_attempt_cap_per_revision": cap,
        "triage_context_refresh_allowance": allowance,
        "base_sha": base_sha,
        "automerge_vision_sha": vision_sha,
    }
    base.update(overrides)
    return base


def state_of(body):
    return core.parse_state_block(body)


def successful_triage():
    return {
        "summary": "Adds lightweight context.",
        "product_implications": "Routine internal change.",
        "evidence": "target.txt: quoted a line from the change",
        "recommended_action": "merge",
        "recommended_reason": "Scope is small.",
        "automerge": {
            "behavior_class": "A",
            "behavior_assertions": [],
            "changes_existing_or_default_behavior": False,
            "optin_default_off": False,
            "aligns_with_vision": True,
            "recommend_merge": True,
        },
    }


def queue_and_succeed(body, it):
    queued = rc.body_with_triage_queued(body, it)
    assert queued != body, "queued write unexpectedly no-op"
    completed = rc.body_with_triage_result(
        queued,
        it["head_sha"],
        triage=successful_triage(),
        automerge_behavior_available=True,
        vision_sha=it["automerge_vision_sha"],
        base_sha=it["base_sha"],
    )
    return completed


def replay_cleared(body):
    """Mirror triage_replay's non-success cache clear exactly."""
    state = rc._unique_state_block(body)
    new_state = dict(state)
    for field in ("triaged_sha", "triage_status", "triage_error"):
        new_state.pop(field, None)
    return rc._replace_state_block(rc.remove_triage_section(body), new_state)


def write_config(data):
    handle, path = tempfile.mkstemp(suffix=".yml")
    with os.fdopen(handle, "w") as out:
        yaml.safe_dump(data, out)
    return path


def load_config_from(path):
    with patched(core, {"config_path": lambda: path}):
        return core.load_config()


# --------------------------------------------------------------------------- #
# config + typed plumbing
# --------------------------------------------------------------------------- #
def test_config_defaults_boundaries_and_override():
    path = write_config({"repos": [{"name": "a"}]})
    try:
        cfg = load_config_from(path)
        assert cfg["triage_context_refresh_allowance"] == 2
        assert cfg["triage_context_allowances"] == {"a": 2}
    finally:
        os.unlink(path)

    path = write_config(
        {
            "triage_context_refresh_allowance": 4,
            "repos": [
                {"name": "a"},
                {"name": "b", "triage_context_refresh_allowance": 0},
                {"name": "c", "triage_context_refresh_allowance": 5},
            ],
        }
    )
    try:
        cfg = load_config_from(path)
        assert cfg["triage_context_refresh_allowance"] == 4
        assert cfg["triage_context_allowances"] == {"a": 4, "b": 0, "c": 5}
    finally:
        os.unlink(path)

    # Every invalid class fails closed to zero (allowance disabled), loudly.
    for bad in (True, -1, 6, "2", 2.5, None):
        path = write_config({"triage_context_refresh_allowance": bad, "repos": []})
        stderr = io.StringIO()
        try:
            with redirect_stderr(stderr):
                cfg = load_config_from(path)
            assert cfg["triage_context_refresh_allowance"] == 0, bad
            assert "::error::" in stderr.getvalue(), bad
        finally:
            os.unlink(path)
    # A malformed per-repo override fails closed to zero even over a valid global.
    path = write_config(
        {
            "triage_context_refresh_allowance": 3,
            "repos": [{"name": "a", "triage_context_refresh_allowance": "x"}],
        }
    )
    stderr = io.StringIO()
    try:
        with redirect_stderr(stderr):
            cfg = load_config_from(path)
        assert cfg["triage_context_allowances"] == {"a": 0}
        assert "::error::" in stderr.getvalue()
    finally:
        os.unlink(path)

    # Direct helper + typed item preflight mirror the attempt-cap helpers.
    assert core._triage_context_allowance({}, 2) == 2
    assert core._triage_context_allowance({"triage_context_refresh_allowance": 1}, 2) == 1
    assert core._triage_context_allowance({"triage_context_refresh_allowance": 9}, 2) == 0
    assert rc.triage_context_allowance(item()) == 2
    assert rc.triage_context_allowance(item(allowance=0)) == 0
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        assert rc.triage_context_allowance(item(allowance=True)) == 0
    assert "::error::" in stderr.getvalue()


def test_ingest_normalization_carries_typed_allowance():
    config = {
        "repos": {
            "wheelhouse": {
                "name": "wheelhouse",
                "triage_context_refresh_allowance": 3,
            }
        },
        "auto_triage": True,
        "auto_triage_issues": True,
        "triage_attempt_cap_per_revision": 2,
        "triage_attempt_caps": {"wheelhouse": 2},
        "triage_context_refresh_allowance": 1,
        "triage_context_allowances": {"wheelhouse": 3},
    }
    payload = {
        "repo": "wheelhouse",
        "number": 42,
        "kind": "pr-review",
        "head_sha": HEAD,
    }
    with patched(build_item, {"load_config": lambda: config}):
        normalized = build_item.normalize(payload)
    assert normalized["triage_context_refresh_allowance"] == 3
    # A repo outside the maps reads the validated global value.
    config["repos"] = {}
    config["triage_context_allowances"] = {}
    payload["repo"] = "other"
    with patched(build_item, {"load_config": lambda: config}):
        normalized = build_item.normalize(payload)
    assert normalized["triage_context_refresh_allowance"] == 1


# --------------------------------------------------------------------------- #
# detection + record strictness
# --------------------------------------------------------------------------- #
def attempted_body(it):
    """A card whose ordinary first attempt succeeded at `it`'s context."""
    return queue_and_succeed(rc.render(it)["body"], it)


def test_context_refresh_detection_matrix():
    body = attempted_body(item())
    state = state_of(body)
    # Verified movements against the unchanged head.
    assert rc.triage_context_refresh(item(B2, V1), state) == (B2, V1)
    assert rc.triage_context_refresh(item(B1, V2), state) == (B1, V2)
    assert rc.triage_context_refresh(item(B2, V2), state) == (B2, V2)
    # No movement -> fresh -> not a context refresh.
    assert rc.triage_context_refresh(item(B1, V1), state) is None
    # Head moved -> ordinary new-revision path, not context.
    assert rc.triage_context_refresh(item(B1, V1, head=HEAD2), state) is None
    # Issue-triage never carries base/VISION context.
    assert (
        rc.triage_context_refresh(item(kind="issue-triage", updated_at="t"), state)
        is None
    )
    # Legacy card: attempt exists for this head but no recorded prior identity
    # (missing triaged_base_sha) -> ordinary budget owns the re-triage.
    legacy_state = dict(state)
    legacy_state.pop("triaged_base_sha", None)
    legacy_state.pop("automerge_verdict", None)
    assert rc.triage_context_refresh(item(B2, V1), legacy_state) is None
    # VISION appearing for the first time (no recorded prior vision identity)
    # is not a verified movement -> ordinary budget owns it.
    no_vision_state = dict(state)
    no_vision_state.pop("triaged_vision_sha", None)
    no_vision_state.pop("automerge_verdict", None)
    assert rc.triage_context_refresh(item(B1, V2), no_vision_state) is None
    # A replay-cleared cache (triaged_sha gone) is the ordinary retry path.
    cleared = state_of(replay_cleared(body))
    assert rc.triage_context_refresh(item(B2, V1), cleared) is None


def test_uses_record_strictness_matrix():
    revision = HEAD
    assert rc._triage_context_uses({}, revision) == ([], False)
    # A record keyed to another head is inert history, not spend and not malformed.
    stale = {
        rc.TRIAGE_CONTEXT_FIELD: {
            "version": 1,
            "kind": "pr-review",
            "revision": HEAD2,
            "uses": [{"base_sha": B2, "vision_sha": V1}],
        },
        "head_sha": revision,
    }
    assert rc._triage_context_uses(stale, revision) == ([], False)

    valid_uses = [{"base_sha": B2, "vision_sha": V1}]
    base_record = {
        "version": 1,
        "kind": "pr-review",
        "revision": revision,
        "uses": valid_uses,
    }
    good = {rc.TRIAGE_CONTEXT_FIELD: copy.deepcopy(base_record), "head_sha": revision}
    uses, untrusted = rc._triage_context_uses(good, revision)
    assert not untrusted and uses == valid_uses

    malformed = []
    def bad(record):
        malformed.append({rc.TRIAGE_CONTEXT_FIELD: record, "head_sha": revision})

    bad(None)
    bad("uses")
    bad({})
    bad({"version": 1, "kind": "pr-review", "revision": revision})  # missing uses
    bad(dict(base_record, extra=True))
    bad(dict(base_record, version=2))
    bad(dict(base_record, version=True))
    bad(dict(base_record, kind="issue-triage"))
    bad(dict(base_record, revision=""))
    bad(dict(base_record, revision=42))
    bad(dict(base_record, uses="x"))
    bad(dict(base_record, uses=[{"base_sha": B2}]))
    bad(dict(base_record, uses=[{"base_sha": B2, "vision_sha": V1, "x": 1}]))
    bad(dict(base_record, uses=[{"base_sha": 1, "vision_sha": V1}]))
    bad(dict(base_record, uses=[{"base_sha": B2, "vision_sha": None}]))
    # Duplicate identities can only be forged -> deny.
    bad(dict(base_record, uses=[{"base_sha": B2, "vision_sha": V1}] * 2))
    # Oversized history can only be forged -> deny.
    bad(
        dict(
            base_record,
            uses=[{"base_sha": str(i), "vision_sha": ""} for i in range(6)],
        )
    )
    for record in malformed:
        assert rc._triage_context_uses(record, revision) == ([], True), record
    # Record revision disagrees with the card's own head -> deny.
    mismatched = {
        rc.TRIAGE_CONTEXT_FIELD: copy.deepcopy(base_record),
        "head_sha": HEAD2,
    }
    assert rc._triage_context_uses(mismatched, revision) == ([], True)

    # The gate maps each class onto one bounded denial reason.
    state = state_of(attempted_body(item()))
    state[rc.TRIAGE_CONTEXT_FIELD] = copy.deepcopy(base_record)
    moved = item(B3, V1)
    ok, reason = rc.triage_context_allowance_gate(moved, state, allowance=2)
    assert ok and reason == ""
    ok, reason = rc.triage_context_allowance_gate(moved, state, allowance=1)
    assert not ok and reason == rc.TRIAGE_CONTEXT_EXHAUSTED
    ok, reason = rc.triage_context_allowance_gate(item(B2, V1), state, allowance=2)
    assert not ok and reason == rc.TRIAGE_CONTEXT_REPEAT
    state[rc.TRIAGE_CONTEXT_FIELD] = {"version": 2}
    ok, reason = rc.triage_context_allowance_gate(moved, state, allowance=2)
    assert not ok and reason == rc.TRIAGE_CONTEXT_UNTRUSTED


# --------------------------------------------------------------------------- #
# acceptance: the F13 scenario end to end
# --------------------------------------------------------------------------- #
def test_two_context_moves_consume_only_the_separate_allowance():
    """With the ordinary per-head cap at two, two distinct verified base or
    VISION changes consume ONLY the separate allowance and trigger explicit
    refresh behavior (queued status + rebound verdict) each time."""
    body = rc.render(item())["body"]
    state = state_of(body)
    assert rc.should_auto_triage(item(), state, PURE, has_token=True)
    body = queue_and_succeed(body, item())
    state = state_of(body)
    assert state[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 1
    assert rc.TRIAGE_CONTEXT_FIELD not in state

    # Verified move 1 (base): queues through the allowance; ordinary count stays.
    assert rc.should_auto_triage(item(B2, V1), state, PURE, has_token=True)
    body = queue_and_succeed(body, item(B2, V1))
    state = state_of(body)
    assert state[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 1
    assert state[rc.TRIAGE_CONTEXT_FIELD] == {
        "version": 1,
        "kind": "pr-review",
        "revision": HEAD,
        "uses": [{"base_sha": B2, "vision_sha": V1}],
    }
    assert state["triage_status"] == "succeeded"
    assert (state.get("automerge_verdict") or {}).get("base_sha") == B2

    # Verified move 2 (VISION this time): still only the allowance.
    assert rc.should_auto_triage(item(B2, V2), state, PURE, has_token=True)
    body = queue_and_succeed(body, item(B2, V2))
    state = state_of(body)
    assert state[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 1
    assert state[rc.TRIAGE_CONTEXT_FIELD]["uses"] == [
        {"base_sha": B2, "vision_sha": V1},
        {"base_sha": B2, "vision_sha": V2},
    ]
    verdict = state.get("automerge_verdict") or {}
    assert verdict.get("base_sha") == B2 and verdict.get("vision_sha") == V2

    # Move 3: the allowance (2) is exhausted - explicit bounded diagnostic,
    # no queued write, and the ordinary budget is still untouched.
    assert not rc.should_auto_triage(item(B3, V2), state, PURE, has_token=True)
    assert rc.body_with_triage_queued(body, item(B3, V2)) == body
    output = io.StringIO()
    with redirect_stdout(output):
        assert (
            rc.triage_context_deferral_reason(item(B3, V2), state, PURE, True)
            == rc.TRIAGE_CONTEXT_EXHAUSTED
        )
        rc.report_triage_context_deferral(42, item(B3, V2), rc.TRIAGE_CONTEXT_EXHAUSTED)
    text = output.getvalue()
    assert "::warning::triage-context-refresh context-allowance-exhausted" in text
    event = json.loads(text.split("wheelhouse-triage-budget-event ", 1)[1])
    assert event["event"] == "context.deferred"
    assert event["code"] == "context-allowance-exhausted"
    assert event["card"] == 42 and event["revision"] == HEAD
    assert state[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 1


def test_repeating_an_identical_context_identity_grants_nothing():
    body = attempted_body(item())  # ordinary attempt at (HEAD, B1, V1)
    # Allowance 3 so repetition (not exhaustion) is the binding constraint.
    body = queue_and_succeed(body, item(B2, V1, allowance=3))
    state = state_of(body)
    assert len(state[rc.TRIAGE_CONTEXT_FIELD]["uses"]) == 1
    # Returning to B1 is a NEW verified identity (the old verdict was cleared
    # by the B2 queue), so it lawfully consumes the second allowance unit.
    assert rc.should_auto_triage(item(B1, V1, allowance=3), state, PURE, True)
    body = queue_and_succeed(body, item(B1, V1, allowance=3))
    state = state_of(body)
    assert len(state[rc.TRIAGE_CONTEXT_FIELD]["uses"]) == 2
    # Moving to B2 again repeats an already-consumed identity: no attempt even
    # though one allowance unit remains.
    repeat = item(B2, V1, allowance=3)
    assert rc.triage_context_refresh(repeat, state) == (B2, V1)
    assert not rc.should_auto_triage(repeat, state, PURE, has_token=True)
    assert (
        rc.triage_context_deferral_reason(repeat, state, PURE, True)
        == rc.TRIAGE_CONTEXT_REPEAT
    )
    assert rc.body_with_triage_queued(body, repeat) == body
    output = io.StringIO()
    with redirect_stdout(output):
        rc.report_triage_context_deferral(42, repeat, rc.TRIAGE_CONTEXT_REPEAT)
    assert "context-identity-repeat" in output.getvalue()
    assert state[rc.TRIAGE_CONTEXT_FIELD]["uses"] == [
        {"base_sha": B2, "vision_sha": V1},
        {"base_sha": B1, "vision_sha": V1},
    ]
    # The reporter itself bounds a junk reason to the untrusted code.
    output = io.StringIO()
    with redirect_stdout(output):
        rc.report_triage_context_deferral(42, repeat, "bogus")
    assert rc.TRIAGE_CONTEXT_UNTRUSTED in output.getvalue()


def test_ordinary_same_context_failures_stay_on_the_original_cap():
    body = attempted_body(item())  # count 1 at (HEAD, B1, V1)
    body = queue_and_succeed(body, item(B2, V1))  # allowance use 1
    # The context attempt fails; an operator replay clears the cache. The
    # re-queue is ORDINARY (cache cleared) and consumes the original cap.
    failed = rc.body_with_triage_result(
        body, HEAD, triage=None, error="Claude did not return a result."
    )
    assert state_of(failed)["triage_status"] == "error"
    replayed = replay_cleared(failed)
    state = state_of(replayed)
    assert rc.triage_context_refresh(item(B2, V1), state) is None
    assert rc.should_auto_triage(item(B2, V1), state, PURE, True)
    requeued = rc.body_with_triage_queued(replayed, item(B2, V1))
    state = state_of(requeued)
    assert state[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 2
    # Replay must never mint context spend: the allowance history survived.
    assert state[rc.TRIAGE_CONTEXT_FIELD]["uses"] == [
        {"base_sha": B2, "vision_sha": V1}
    ]
    # A second replay at the same context hits the ORIGINAL cap (2), and the
    # deferral diagnostic is the ordinary one - the allowance is not consulted.
    replayed2 = replay_cleared(requeued)
    state = state_of(replayed2)
    assert not rc.should_auto_triage(item(B2, V1), state, PURE, True)
    assert rc.triage_attempt_deferral_needed(item(B2, V1), state, PURE, True)
    assert rc.triage_context_deferral_reason(item(B2, V1), state, PURE, True) == ""
    output = io.StringIO()
    with redirect_stdout(output):
        rc.report_triage_attempt_exhaustion(42, item(B2, V1))
    assert "attempt-cap-exhausted" in output.getvalue()


def test_allowance_zero_disables_context_refresh_only():
    body = attempted_body(item(allowance=0))
    state = state_of(body)
    moved = item(B2, V1, allowance=0)
    assert rc.triage_context_refresh(moved, state) == (B2, V1)
    assert not rc.should_auto_triage(moved, state, PURE, has_token=True)
    assert (
        rc.triage_context_deferral_reason(moved, state, PURE, True)
        == rc.TRIAGE_CONTEXT_EXHAUSTED
    )
    assert rc.body_with_triage_queued(body, moved) == body
    # The ordinary path is untouched: a new head still queues normally.
    new_head = item(B2, V1, head=HEAD2, allowance=0)
    fresh = state_of(rc.render(new_head)["body"])
    assert rc.should_auto_triage(new_head, fresh, PURE, has_token=True)


def test_issue_triage_never_touches_the_allowance():
    it = item(kind="issue-triage", updated_at="2026-07-16T12:00:00Z", head="")
    body = rc.body_with_triage_queued(rc.render(it)["body"], it)
    state = state_of(body)
    assert state[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 1
    assert rc.TRIAGE_CONTEXT_FIELD not in state
    assert rc.triage_context_refresh(it, state) is None
    # A newer updatedAt starts a new per-revision ordinary count, as before.
    newer = item(kind="issue-triage", updated_at="2026-07-16T13:00:00Z", head="")
    assert rc.should_auto_triage(newer, state, PURE, has_token=True)
    body2 = rc.body_with_triage_queued(body, newer)
    assert state_of(body2)[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 1
    assert rc.TRIAGE_CONTEXT_FIELD not in state_of(body2)


# --------------------------------------------------------------------------- #
# reservation, sealed permit, idempotency, G6
# --------------------------------------------------------------------------- #
def review_observation(base_sha=B1, head=HEAD):
    """A complete native ReviewObservation v2 for the fixture target."""
    checks = [
        {
            "name": "PR must be raised via no-mistakes",
            "role": "compliance",
            "outcome": "pass",
        },
        {"name": "tests", "role": "test", "outcome": "pass"},
    ]
    return target_observation.make_observation(
        "example",
        "wheelhouse",
        42,
        head_sha=head,
        base_sha=base_sha,
        expected_head_sha=head,
        observed_at="2026-07-16T10:00:00Z",
        source="bulk-scan",
        completeness={
            "complete": True,
            "target": True,
            "checks": True,
            "configured_checks": True,
            "changed_paths": True,
            "action_required_runs": True,
            "head_matches_expected": True,
            "check_contexts_seen": len(checks),
            "check_contexts_total": len(checks),
            "mergeability": "conclusive",
        },
        facts={
            "open": True,
            "title": "A bounded triage candidate",
            "author": "contributor",
            "updated_at": "2026-07-16T09:59:59Z",
            "draft": False,
            "cross_repo": False,
            "head_ref": "feature-42",
            "mergeable": "MERGEABLE",
            "ci": True,
            "comp": "pass",
            "tests": "green",
            "bucket": "merge-ready",
            "approval_phase": "not-required",
            "check_phase": "terminal",
            "configured_checks": checks,
        },
        changed_paths=target_observation.changed_path_facts(
            ["src/change.py"], complete=True
        ),
    )


def projection_card(it):
    """A production-shaped authoritative v2 projection card for `it`.

    The pr-review queue path (`mark_triage_queued`) refuses any card whose
    state block is not owned by the v2 projection writer, so the spend-boundary
    tests must exercise the real projected body, not a bare render.
    """
    obs = review_observation(base_sha=it["base_sha"], head=it["head_sha"])
    snapshot = decision_context.repository_snapshot([], "2026-07-16T10:00:00Z")
    context = decision_context.build_decision_context(obs, snapshot)
    projection = card_projection.plan_card_projection(
        dict(it, target_observation=obs, decision_context=context), prior={}
    )
    return {
        "number": 42,
        "title": projection["title"],
        "body": projection["body"],
        "labels": [{"name": name} for name in projection["managed_labels"]],
        "state": "OPEN",
        "updatedAt": "2026-07-16T10:00:01Z",
        "author": {"login": rc.GET_CARD_AUTOMATION_AUTHOR},
        "comments": [],
    }


def ledger_card_boundary(card):
    """In-memory card plus the projection writer's PATCH boundary."""
    current = copy.deepcopy(card)
    order = []

    def get_card(number):
        return copy.deepcopy(current)

    def gh(args, check=True):
        if args[:3] == ["api", "--method", "PATCH"] and "--input" in args:
            order.append("card-write")
            path = args[args.index("--input") + 1]
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            current["title"] = payload["title"]
            current["body"] = payload["body"]
            current["labels"] = [{"name": name} for name in payload["labels"]]
            current["updatedAt"] = "2026-07-16T10:00:02Z"
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        raise AssertionError("unexpected gh call: %r" % (args,))

    return current, order, get_card, gh


def test_context_queue_reserves_one_unit_and_returns_a_sealed_permit():
    card = projection_card(item())
    card["body"] = queue_and_succeed(card["body"], item())
    current, order, get_card, gh = ledger_card_boundary(card)
    body = current["body"]

    def reserve(number, queued_item, ceiling):
        order.append("reserve")
        return True

    config = {
        "repos": {"wheelhouse": {"name": "wheelhouse"}},
        "triage_attempt_cap_per_revision": 2,
        "triage_daily_ceiling": 100,
        "triage_context_refresh_allowance": 2,
        "triage_context_allowances": {"wheelhouse": 2},
    }
    moved = item(B2, V1)
    with (
        patched(
            rc,
            {"get_card": get_card, "reserve_triage_budget": reserve, "_gh": gh},
        ),
        patched(core, {"load_config": lambda: config}),
        redirect_stdout(io.StringIO()),
    ):
        permit = rc.mark_triage_queued(42, moved, body)
    assert isinstance(permit, rc._TriageDispatchPermit)
    assert order == ["reserve", "card-write"], order
    state = state_of(current["body"])
    assert state["triage_status"] == "queued"
    # One reservation bought one context-refresh queue; ordinary count unmoved.
    assert state[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 1
    assert state[rc.TRIAGE_CONTEXT_FIELD]["uses"] == [
        {"base_sha": B2, "vision_sha": V1}
    ]

    # Dispatch accepts only the sealed permit for this exact card/item.
    calls = []
    with patched(rc, {"_gh": lambda args, check=True: calls.append(args)}):
        rc.dispatch_triage_workflow(permit)
    assert calls and calls[0][:3] == ["workflow", "run", "triage.yml"]
    assert "head_sha=%s" % HEAD in calls[0]
    try:
        rc.dispatch_triage_workflow(object())
    except RuntimeError:
        pass
    else:
        raise AssertionError("dispatch accepted a forged permit")


def test_context_exhaustion_reserves_nothing_and_never_dispatches():
    card = projection_card(item())
    card["body"] = queue_and_succeed(card["body"], item())
    card["body"] = queue_and_succeed(card["body"], item(B2, V1))
    card["body"] = queue_and_succeed(card["body"], item(B2, V2))
    current, order, get_card, gh = ledger_card_boundary(card)
    body = current["body"]

    def reserve(number, queued_item, ceiling):
        order.append("reserve")
        return True

    config = {
        "repos": {"wheelhouse": {"name": "wheelhouse"}},
        "triage_attempt_cap_per_revision": 2,
        "triage_daily_ceiling": 100,
        "triage_context_refresh_allowance": 2,
        "triage_context_allowances": {"wheelhouse": 2},
    }
    output = io.StringIO()
    with (
        patched(
            rc,
            {"get_card": get_card, "reserve_triage_budget": reserve, "_gh": gh},
        ),
        patched(core, {"load_config": lambda: config}),
        redirect_stdout(output),
    ):
        permit = rc.mark_triage_queued(42, item(B3, V2), body)
    assert permit is None
    assert order == [], order  # no reservation, no card write, no dispatch
    text = output.getvalue()
    assert "context-allowance-exhausted" in text
    assert "context.deferred" in text

    # The reconcile path surfaces the same explicit bounded diagnostic.
    row = {
        "number": 42,
        "body": body,
        "labels": PURE,
        "state": state_of(body),
    }
    dispatched = []
    output = io.StringIO()
    with (
        patched(
            reconcile.render_card,
            {
                "mark_triage_queued": lambda number, queued_item, body: True,
                "dispatch_triage_workflow": lambda permit: dispatched.append(permit),
            },
        ),
        redirect_stdout(output),
    ):
        queued = reconcile.maybe_queue_auto_triage(item(B3, V2), row, True)
    assert queued is False
    assert dispatched == []
    assert "context-allowance-exhausted" in output.getvalue()
    assert "context.deferred" in output.getvalue()


def test_reservation_failure_consumes_no_allowance():
    card = projection_card(item())
    card["body"] = queue_and_succeed(card["body"], item())
    current, order, get_card, gh = ledger_card_boundary(card)
    body = current["body"]

    def reserve(number, queued_item, ceiling):
        order.append("reserve")
        return False

    config = {
        "repos": {"wheelhouse": {"name": "wheelhouse"}},
        "triage_attempt_cap_per_revision": 2,
        "triage_daily_ceiling": 100,
        "triage_context_refresh_allowance": 2,
        "triage_context_allowances": {"wheelhouse": 2},
    }
    with (
        patched(
            rc,
            {
                "get_card": get_card,
                "reserve_triage_budget": reserve,
                "_gh": gh,
                "publish_triage_budget_deferral": lambda *a, **k: None,
            },
        ),
        patched(core, {"load_config": lambda: config}),
    ):
        permit = rc.mark_triage_queued(42, item(B2, V1), body)
    assert permit is None
    assert order == ["reserve"], order
    assert rc.TRIAGE_CONTEXT_FIELD not in state_of(current["body"])


def test_idempotency_same_context_never_requeues():
    body = attempted_body(item())
    moved = item(B2, V1)
    queued = rc.body_with_triage_queued(body, moved)
    state = state_of(queued)
    # Queued for exactly this context: fresh, so no scan requeues it.
    assert state["triage_status"] == "queued"
    assert rc.triage_fresh(moved, state)
    assert not rc.should_auto_triage(moved, state, PURE, has_token=True)
    assert rc.body_with_triage_queued(queued, moved) == queued
    # Same after the attempt completes: success AND failure are both final.
    succeeded = rc.body_with_triage_result(
        queued,
        HEAD,
        triage=successful_triage(),
        automerge_behavior_available=True,
        vision_sha=V1,
        base_sha=B2,
    )
    state = state_of(succeeded)
    assert rc.triage_fresh(moved, state)
    assert not rc.should_auto_triage(moved, state, PURE, has_token=True)
    failed = rc.body_with_triage_result(
        queued, HEAD, triage=None, error="boom"
    )
    state = state_of(failed)
    assert rc.triage_fresh(moved, state)
    assert not rc.should_auto_triage(moved, state, PURE, has_token=True)


def test_g6_revalidation_binds_the_refreshed_context():
    body = attempted_body(item())
    verdict_before = state_of(body).get("automerge_verdict") or {}
    assert verdict_before.get("base_sha") == B1
    # After a verified base move, the refreshed verdict binds the NEW context.
    body = queue_and_succeed(body, item(B2, V1))
    verdict = state_of(body).get("automerge_verdict") or {}
    assert verdict == {
        "behavior_class": "A",
        "behavior_admission": {
            "contradicts_existing_contract": False,
            "version": 1,
        },
        "changes_existing_or_default_behavior": False,
        "optin_default_off": False,
        "aligns_with_vision": True,
        "recommend_merge": True,
        "vision_sha": V1,
        "base_sha": B2,
    }
    ok, cls, _reason = auto_merge.verdict_eligible(verdict)
    assert ok and cls == "A"
    # G6's live-base binding compares the persisted verdict against the live
    # base SHA: the refreshed verdict matches the new live base, while the
    # stale one does not (the exact comparison evaluate_candidate enforces).
    live_base, live_vision = B2, V1
    assert verdict["base_sha"] == live_base and verdict["vision_sha"] == live_vision
    assert verdict_before["base_sha"] != live_base
    facts, _ = auto_merge.behavior_verdict_facts(verdict)
    assert all(
        facts[key]["status"] == rc.criteria_schema.STATUS_MET
        for key in (
            "g6_behavior_class",
            "g6_vision_alignment",
            "g6_default_behavior",
            "g6_verdict_merge",
            "g6_class_c_mode",
        )
    )


# --------------------------------------------------------------------------- #
# non-materiality + same-revision preservation
# --------------------------------------------------------------------------- #
def test_allowance_record_is_nonmaterial_and_preserved_across_refresh():
    body = attempted_body(item())
    with_record = queue_and_succeed(body, item(B2, V1))
    state_with = state_of(with_record)
    assert rc.TRIAGE_CONTEXT_FIELD in state_with
    # The record must never drive a material refresh decision.
    stripped = dict(state_with)
    stripped.pop(rc.TRIAGE_CONTEXT_FIELD, None)
    assert rc.material_changed(item(B2, V1), state_with) == rc.material_changed(
        item(B2, V1), stripped
    )
    assert not rc.material_changed(item(B2, V1), state_with)

    # A same-revision refresh preserves the record through the triage lift.
    labels = ["needs-decision", "kind:pr-review"]
    refreshed_item = dict(item(B2, V1), priority="high")
    refreshed = rc._preserve_same_revision_triage(
        rc.render(refreshed_item)["body"],
        with_record,
        refreshed_item,
        state_of(with_record),
        owner="example",
    )
    assert (
        state_of(refreshed).get(rc.TRIAGE_CONTEXT_FIELD)
        == state_with[rc.TRIAGE_CONTEXT_FIELD]
    )

    # A queue at a NEW head starts a clean record: the head-keyed record from
    # the old head is inert history on the read path.
    new_head = item(B2, V1, head=HEAD2)
    uses, untrusted = rc._triage_context_uses(state_with, HEAD2)
    assert uses == [] and not untrusted
    fresh = state_of(rc.render(new_head)["body"])
    assert rc.TRIAGE_CONTEXT_FIELD not in fresh
    assert rc.should_auto_triage(new_head, fresh, PURE, has_token=True)


def test_item_level_invalid_allowance_fails_closed_loudly():
    body = attempted_body(item())
    state = state_of(body)
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        assert not rc.should_auto_triage(
            item(B2, V1, allowance="lots"), state, PURE, has_token=True
        )
    assert "::error::" in stderr.getvalue()
    assert rc.body_with_triage_queued(body, item(B2, V1, allowance="lots")) == body


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok - %s" % name)
    print("all context-allowance tests passed")
