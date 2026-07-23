#!/usr/bin/env python3
"""Offline regression coverage for the inert bounded triage replay path."""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from contextlib import contextmanager, nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from scripts import agent_claim  # noqa: E402
import render_card as rc  # noqa: E402
import triage_replay as replay  # noqa: E402

# Replay tests exercise exact-revision lifecycle behavior; the atomic
# evaluator/write integration has dedicated coverage in test_automerge_card_ui.py.
rc._evaluate_automerge_card_projection = lambda *args, **kwargs: (
    rc.criteria_schema.unavailable_criteria("offline replay fixture")
)


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


def base_item(number=17, kind="pr-review", revision="abcdef1"):
    return {
        "repo": "wheelhouse",
        "number": number,
        "kind": kind,
        "head_sha": revision if kind == "pr-review" else "",
        "updated_at": revision if kind == "issue-triage" else "2026-07-16T10:00:00Z",
        "title": "Replay this exact source",
        "author": "contributor",
        "bucket": "merge-ready" if kind == "pr-review" else "issue-triage",
        "comp": "pass" if kind == "pr-review" else "n/a",
        "tests": "green" if kind == "pr-review" else "n/a",
        "url": "https://github.com/example/wheelhouse/pull/%s" % number,
        "summary": "offline replay fixture",
        "recommendation": "Review it.",
        "priority": "med",
        "auto_triage": True,
        "auto_triage_issues": True,
        "triage_attempt_cap_per_revision": 2,
    }


def source(
    number=17,
    kind="pr-review",
    revision="abcdef1",
    state="open",
    author_login="contributor",
    author_type="User",
):
    value = {
        "number": number,
        "state": state,
        "title": "Replay this exact source",
        "html_url": "https://github.com/example/wheelhouse/pull/%s" % number,
        "updated_at": "2026-07-16T10:00:00Z",
        "user": {"login": author_login, "type": author_type},
    }
    if kind == "pr-review":
        value["head"] = {"sha": revision}
    else:
        value["updated_at"] = revision
    return value


def card(number=42, target=17, kind="pr-review", revision="abcdef1", status="error"):
    candidate = base_item(target, kind, revision)
    rendered = rc.render(candidate)
    body = rendered["body"]
    state = rc._unique_state_block(body)
    if status is None:
        pass
    else:
        state = rc._state_with_triage(
            state,
            revision,
            status,
            error="structural triage failure" if status == "error" else None,
        )
        body = rc._replace_state_block(body, state)
    return {
        "number": number,
        "title": rendered["title"],
        "body": body,
        "labels": [{"name": name} for name in rendered["labels"]],
        "state": "OPEN",
        "updatedAt": "2026-07-16T10:01:00Z",
        "author": {"login": rc.CARD_AUTOMATION_AUTHOR},
        "comments": [],
    }


def config():
    return {
        "repos": {"wheelhouse": {"name": "wheelhouse"}},
        "maintainer": "co-maintainer",
        "auto_triage": True,
        "auto_triage_issues": True,
        "triage_attempt_cap_per_revision": 2,
        "triage_attempt_caps": {"wheelhouse": 2},
        "triage_daily_ceiling": 1200,
    }


@contextmanager
def replay_environment(
    cards,
    sources,
    remaining=1200,
    stub_queue=True,
    stub_claim=True,
    card_read_hook=None,
):
    card_reads = []
    source_reads = []
    edits = []
    queued = []
    dispatched = []
    claims = []

    def get_card(number):
        card_reads.append(number)
        if card_read_hook is not None:
            card_read_hook(number, len(card_reads), cards)
        value = cards.get(number)
        return copy.deepcopy(value) if value is not None else None

    def edit(number, body, remove_labels=None):
        edits.append((number, body))
        cards[number]["body"] = body
        cards[number]["updatedAt"] = "2026-07-16T10:02:00Z"

    def source_read(owner, repo, number, kind):
        source_reads.append((owner, repo, number, kind))
        value = sources.get((repo, number, kind))
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def mark(number, item, body, prepare_body=None, publish_budget_deferral=True):
        queued.append((number, item["repo"], item["number"]))
        body = prepare_body(body) if prepare_body else body
        new_body = rc.body_with_triage_queued(body, item, attempt_cap=2)
        assert new_body != body
        edits.append((number, new_body))
        cards[number]["body"] = new_body
        return object()

    def dispatch(permit):
        dispatched.append(permit)

    replacements = {
        "get_card": get_card,
        "_edit_issue_body": edit,
        "triage_budget_remaining": lambda ceiling: min(remaining, ceiling),
        "auto_triage_has_token": lambda: True,
        "dispatch_triage_workflow": dispatch,
    }
    if stub_queue:
        replacements["mark_triage_queued"] = mark
    old_env = dict(os.environ)
    os.environ.update(
        {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REPOSITORY_OWNER": "owner",
            "GITHUB_REPOSITORY": "owner/wheelhouse",
            "GITHUB_ACTOR": "owner",
            "GITHUB_RUN_NUMBER": "77",
            "WHEELHOUSE_AUTO_TRIAGE_HAS_TOKEN": "true",
            "WHEELHOUSE_AUTO_TRIAGE_HAS_READONLY_TOKEN": "false",
        }
    )
    try:

        def supersede(**kwargs):
            claims.append(kwargs)
            return {
                "event_key": "a" * 64,
                "superseded": False,
            }

        claim_context = (
            patched(
                replay.agent_claim,
                {
                    "supersede_triage_claim": supersede,
                    "triage_replay_duplicate_only_evidence": lambda **kwargs: False,
                },
            )
            if stub_claim
            else nullcontext()
        )
        with (
            patched(rc, replacements),
            patched(replay, {"_source_json": source_read}),
            patched(replay.core, {"load_config": config}),
            claim_context,
        ):
            yield {
                "card_reads": card_reads,
                "source_reads": source_reads,
                "edits": edits,
                "queued": queued,
                "dispatched": dispatched,
                "claims": claims,
            }
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def cards_file(numbers):
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(
        [{"number": number, "body": "untrusted listing data"} for number in numbers],
        handle,
    )
    handle.close()
    return handle.name


def exact_fixture(numbers):
    cards = {}
    sources = {}
    revisions = {}
    for number in numbers:
        revision = "%07x" % number
        target = number + 20_000
        cards[number] = card(number=number, target=target, revision=revision)
        sources[("wheelhouse", target, "pr-review")] = source(
            number=target, revision=revision
        )
        revisions[number] = revision
    return cards, sources, revisions


def exact_plan_lines(output):
    return [
        line
        for line in output.splitlines()
        if line.startswith("replay exact-selector/v1 admitted card #")
    ]


def attempt_reset_fixture(cohort=None):
    cohort = cohort or replay.ATTEMPT_RESET_COHORT
    cards = {}
    sources = {}
    for card_number, prior_marker in sorted(cohort.items()):
        revision = prior_marker["revision"]
        kind = "issue-triage" if revision.endswith("Z") else "pr-review"
        target = card_number + 10_000
        value = card(
            number=card_number,
            target=target,
            kind=kind,
            revision=revision,
        )
        state = rc._unique_state_block(value["body"])
        state[rc.TRIAGE_ATTEMPTS_FIELD] = {
            "version": rc.TRIAGE_ATTEMPTS_VERSION,
            "kind": kind,
            "revision": revision,
            "count": 2,
        }
        state[replay.REPLAY_FIELD] = dict(prior_marker)
        value["body"] = rc._replace_state_block(value["body"], state)
        cards[card_number] = value
        sources[("wheelhouse", target, kind)] = source(
            number=target,
            kind=kind,
            revision=revision,
        )
    supplied = ",".join(str(number) for number in sorted(cards))
    return cards, sources, supplied


def test_terminal_error_is_cleared_and_queued_once_then_second_wave_noops():
    cards = {42: card()}
    sources = {("wheelhouse", 17, "pr-review"): source()}
    path = cards_file([42])
    try:
        with replay_environment(cards, sources) as calls:
            first = replay.run(path, "wave-one", 25)
            first_state = rc._unique_state_block(cards[42]["body"])
            assert first == {
                "eligible": 1,
                "planned": 1,
                "deferred": 0,
                "written": 1,
                "queued": 1,
            }
            assert first_state[replay.REPLAY_FIELD]["version"] == 1
            assert first_state[replay.REPLAY_FIELD]["cleared"] == "error"
            assert first_state["triage_status"] == "queued"
            assert first_state["triage_attempts"]["count"] == 2
            assert len(calls["queued"]) == len(calls["dispatched"]) == 1
            second = replay.run(path, "wave-two", 25)
            assert second["eligible"] == second["written"] == second["queued"] == 0
            assert len(calls["queued"]) == len(calls["dispatched"]) == 1
            assert all(number == 42 for number in calls["card_reads"])
            assert all(read[2:] == (17, "pr-review") for read in calls["source_reads"])
    finally:
        os.unlink(path)


def test_sanctioned_attempt_reset_grants_exact_cohort_one_reentry():
    cards, sources, supplied = attempt_reset_fixture()
    path = cards_file([])
    try:
        with replay_environment(cards, sources) as calls:
            result = replay.run(
                path,
                replay.ATTEMPT_RESET_WAVE,
                len(replay.ATTEMPT_RESET_COHORT),
                attempts_reset_cards=supplied,
            )
            assert result == {
                "eligible": 19,
                "planned": 19,
                "deferred": 0,
                "written": 19,
                "queued": 19,
            }
            assert len(calls["queued"]) == len(calls["dispatched"]) == 19
            for number, value in cards.items():
                state = rc._unique_state_block(value["body"])
                assert state[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 2
                assert state[replay.REPLAY_FIELD] == {
                    "version": replay.ATTEMPT_RESET_REPLAY_VERSION,
                    "wave": replay.ATTEMPT_RESET_WAVE,
                    "revision": replay.ATTEMPT_RESET_COHORT[number]["revision"],
                    "cleared": "error",
                    "at": state[replay.REPLAY_FIELD]["at"],
                    "run_number": 77,
                    "attempt_reset": True,
                }
            assert config()["triage_attempt_cap_per_revision"] == 2
            assert config()["triage_attempt_caps"]["wheelhouse"] == 2
            try:
                replay.run(
                    path,
                    replay.ATTEMPT_RESET_WAVE,
                    len(replay.ATTEMPT_RESET_COHORT),
                    attempts_reset_cards=supplied,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("attempt reset was reusable")
            assert len(calls["queued"]) == len(calls["dispatched"]) == 19
    finally:
        os.unlink(path)


def test_array_recovery_attempt_reset_grants_exact_cohort_one_reentry():
    cohort = replay.ARRAY_RECOVERY_ATTEMPT_RESET_COHORT
    wave = replay.ARRAY_RECOVERY_ATTEMPT_RESET_WAVE
    cards, sources, supplied = attempt_reset_fixture(cohort)
    path = cards_file([])
    try:
        with replay_environment(cards, sources) as calls:
            result = replay.run(
                path,
                wave,
                len(cohort),
                attempts_reset_cards=supplied,
            )
            assert result == {
                "eligible": 15,
                "planned": 15,
                "deferred": 0,
                "written": 15,
                "queued": 15,
            }
            assert len(calls["queued"]) == len(calls["dispatched"]) == 15
            for number, value in cards.items():
                state = rc._unique_state_block(value["body"])
                assert state[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 2
                assert state[replay.REPLAY_FIELD] == {
                    "version": replay.ATTEMPT_RESET_REPLAY_VERSION,
                    "wave": wave,
                    "revision": cohort[number]["revision"],
                    "cleared": "error",
                    "at": state[replay.REPLAY_FIELD]["at"],
                    "run_number": 77,
                    "attempt_reset": True,
                }
            try:
                replay.run(
                    path,
                    wave,
                    len(cohort),
                    attempts_reset_cards=supplied,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("array recovery attempt reset was reusable")
            assert len(calls["queued"]) == len(calls["dispatched"]) == 15
    finally:
        os.unlink(path)


def test_array_recovery_attempt_reset_requires_exact_wave_cohort_and_limit():
    cohort = replay.ARRAY_RECOVERY_ATTEMPT_RESET_COHORT
    wave = replay.ARRAY_RECOVERY_ATTEMPT_RESET_WAVE
    cards, sources, supplied = attempt_reset_fixture(cohort)
    invalid_inputs = (
        ("wrong-wave", supplied),
        (replay.ATTEMPT_RESET_WAVE, supplied),
        (wave, supplied + ",9999"),
        (wave, ",".join(supplied.split(",")[:-1])),
        (wave, supplied + ",154"),
    )
    for candidate_wave, value in invalid_inputs:
        try:
            replay._attempt_reset_scope(candidate_wave, value)
        except ValueError:
            pass
        else:
            raise AssertionError((candidate_wave, value))

    path = cards_file([])
    try:
        with replay_environment(cards, sources) as calls:
            try:
                replay.run(
                    path,
                    wave,
                    len(cohort) - 1,
                    attempts_reset_cards=supplied,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("attempt reset accepted a non-cohort limit")
            assert not calls["card_reads"] and not calls["source_reads"]
            assert not calls["edits"] and not calls["queued"]
            assert not calls["dispatched"]
    finally:
        os.unlink(path)


def test_array_recovery_attempt_reset_mismatches_are_atomic_zero_write():
    cohort = replay.ARRAY_RECOVERY_ATTEMPT_RESET_COHORT
    wave = replay.ARRAY_RECOVERY_ATTEMPT_RESET_WAVE

    cards, sources, supplied = attempt_reset_fixture(cohort)
    changed = min(cards)
    state = rc._unique_state_block(cards[changed]["body"])
    state[replay.REPLAY_FIELD]["at"] = "2026-07-17T20:00:00Z"
    cards[changed]["body"] = rc._replace_state_block(cards[changed]["body"], state)
    path = cards_file([])
    try:
        with replay_environment(cards, sources) as calls:
            try:
                replay.run(
                    path,
                    wave,
                    len(cohort),
                    attempts_reset_cards=supplied,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("attempt reset accepted a wrong prior marker")
            assert not calls["claims"]
            assert not calls["edits"] and not calls["queued"]
            assert not calls["dispatched"]
    finally:
        os.unlink(path)

    cards, sources, supplied = attempt_reset_fixture(cohort)
    changed = max(cards)

    def race_card(number, read_count, live_cards):
        if read_count == len(cohort) + len(live_cards):
            state = rc._unique_state_block(live_cards[number]["body"])
            state["triage_status"] = "queued"
            live_cards[number]["body"] = rc._replace_state_block(
                live_cards[number]["body"], state
            )

    path = cards_file([])
    before = {number: value["body"] for number, value in cards.items()}
    try:
        with replay_environment(cards, sources, card_read_hook=race_card) as calls:
            try:
                replay.run(
                    path,
                    wave,
                    len(cohort),
                    attempts_reset_cards=supplied,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("attempt reset mutated before full preflight")
            assert len(calls["card_reads"]) >= len(cohort) * 2
            assert not calls["claims"]
            assert not calls["edits"] and not calls["queued"]
            assert not calls["dispatched"]
            changed_only = {
                number: value["body"]
                for number, value in cards.items()
                if value["body"] != before[number]
            }
            assert set(changed_only) == {changed}
    finally:
        os.unlink(path)


def test_attempt_reset_later_race_pauses_then_resumes_exact_cohort():
    cohort = replay.ARRAY_RECOVERY_ATTEMPT_RESET_COHORT
    wave = replay.ARRAY_RECOVERY_ATTEMPT_RESET_WAVE
    cards, sources, supplied = attempt_reset_fixture(cohort)
    changed = max(cards)
    race_read = len(cohort) * 2 + 3 * (len(cohort) - 1) + 1
    raced = False

    def race_card(number, read_count, live_cards):
        nonlocal raced
        if not raced and number == changed and read_count == race_read:
            state = rc._unique_state_block(live_cards[number]["body"])
            state["triage_status"] = "queued"
            live_cards[number]["body"] = rc._replace_state_block(
                live_cards[number]["body"], state
            )
            raced = True

    path = cards_file([])
    try:
        with replay_environment(cards, sources, card_read_hook=race_card) as calls:
            try:
                replay.run(
                    path,
                    wave,
                    len(cohort),
                    attempts_reset_cards=supplied,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("attempt reset continued after a later-card race")
            assert raced
            assert len(calls["queued"]) == len(cohort) - 1
            for number, value in cards.items():
                state = rc._unique_state_block(value["body"])
                if number == changed:
                    assert state[replay.REPLAY_FIELD] == cohort[number]
                    state["triage_status"] = "error"
                    value["body"] = rc._replace_state_block(value["body"], state)
                else:
                    assert state[replay.REPLAY_FIELD]["version"] == (
                        replay.ATTEMPT_RESET_REPLAY_VERSION
                    )
            resumed = replay.run(
                path,
                wave,
                len(cohort),
                attempts_reset_cards=supplied,
            )
            assert resumed == {
                "eligible": len(cohort),
                "planned": len(cohort),
                "deferred": 0,
                "written": 1,
                "queued": 1,
            }
            assert len(calls["queued"]) == len(cohort)
            assert all(
                rc._unique_state_block(value["body"])[replay.REPLAY_FIELD]["version"]
                == replay.ATTEMPT_RESET_REPLAY_VERSION
                for value in cards.values()
            )
    finally:
        os.unlink(path)


def test_attempt_reset_resume_requires_only_pending_budget():
    cohort = replay.ARRAY_RECOVERY_ATTEMPT_RESET_COHORT
    wave = replay.ARRAY_RECOVERY_ATTEMPT_RESET_WAVE
    cards, sources, supplied = attempt_reset_fixture(cohort)
    pending = max(cards)
    for number, value in cards.items():
        if number == pending:
            continue
        state = rc._unique_state_block(value["body"])
        revision = cohort[number]["revision"]
        for field in replay.TRIAGE_NON_SUCCESS_FIELDS:
            state.pop(field, None)
        state["triaged_sha"] = revision
        state["triage_status"] = "queued"
        state[replay.REPLAY_FIELD] = replay._marker(
            wave, revision, "error", 77, attempt_reset=True
        )
        value["body"] = rc._replace_state_block(value["body"], state)

    path = cards_file([])
    try:
        with replay_environment(cards, sources, remaining=1) as calls:
            result = replay.run(
                path,
                wave,
                len(cohort),
                attempts_reset_cards=supplied,
            )
            assert result == {
                "eligible": len(cohort),
                "planned": len(cohort),
                "deferred": 0,
                "written": 1,
                "queued": 1,
            }
            assert len(calls["queued"]) == len(calls["dispatched"]) == 1
            assert calls["queued"][0][0] == pending
            assert all(
                rc._unique_state_block(value["body"])[replay.REPLAY_FIELD]["version"]
                == replay.ATTEMPT_RESET_REPLAY_VERSION
                for value in cards.values()
            )
    finally:
        os.unlink(path)


def test_attempt_reset_refuses_outside_scope_and_any_state_mismatch():
    _, _, supplied = attempt_reset_fixture()
    assert (
        replay._attempt_reset_count(
            {
                rc.TRIAGE_ATTEMPTS_FIELD: {
                    "version": True,
                    "kind": "pr-review",
                    "revision": "abcdef1",
                    "count": 2,
                }
            },
            "pr-review",
            "abcdef1",
            2,
        )
        is None
    )
    invalid_inputs = (
        ("wrong-wave", supplied),
        (replay.ATTEMPT_RESET_WAVE, supplied + ",9999"),
        (
            replay.ATTEMPT_RESET_WAVE,
            ",".join(supplied.split(",")[:-1]),
        ),
        (replay.ATTEMPT_RESET_WAVE, supplied + ",1367"),
    )
    for wave, value in invalid_inputs:
        try:
            replay._attempt_reset_scope(wave, value)
        except ValueError:
            pass
        else:
            raise AssertionError((wave, value))

    cards, sources, supplied = attempt_reset_fixture()
    moved = min(cards)
    state = rc._unique_state_block(cards[moved]["body"])
    old_revision = replay.ATTEMPT_RESET_COHORT[moved]["revision"]
    kind = state["kind"]
    moved_revision = "2026-07-18T00:00:00Z" if kind == "issue-triage" else "f" * 40
    state["updated_at" if kind == "issue-triage" else "head_sha"] = moved_revision
    state["triaged_sha"] = moved_revision
    state[rc.TRIAGE_ATTEMPTS_FIELD]["revision"] = moved_revision
    state[replay.REPLAY_FIELD]["revision"] = moved_revision
    cards[moved]["body"] = rc._replace_state_block(cards[moved]["body"], state)
    target = state["number"]
    sources.pop(("wheelhouse", target, kind))
    sources[("wheelhouse", target, kind)] = source(
        number=target,
        kind=kind,
        revision=moved_revision,
    )
    path = cards_file([])
    try:
        with replay_environment(cards, sources) as calls:
            try:
                replay.run(
                    path,
                    replay.ATTEMPT_RESET_WAVE,
                    len(replay.ATTEMPT_RESET_COHORT),
                    attempts_reset_cards=supplied,
                )
            except ValueError:
                pass
            else:
                raise AssertionError((moved, old_revision, moved_revision))
            assert not calls["edits"] and not calls["queued"]
            assert not calls["dispatched"]
    finally:
        os.unlink(path)


def test_attempt_reset_binds_complete_prior_marker_identity():
    cards, sources, supplied = attempt_reset_fixture()
    changed = min(cards)
    state = rc._unique_state_block(cards[changed]["body"])
    state[replay.REPLAY_FIELD]["at"] = "2026-07-17T20:00:00Z"
    cards[changed]["body"] = rc._replace_state_block(cards[changed]["body"], state)
    path = cards_file([])
    try:
        with replay_environment(cards, sources) as calls:
            try:
                replay.run(
                    path,
                    replay.ATTEMPT_RESET_WAVE,
                    len(replay.ATTEMPT_RESET_COHORT),
                    attempts_reset_cards=supplied,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("attempt reset accepted wrong prior marker")
            assert not calls["claims"]
            assert not calls["edits"] and not calls["queued"]
            assert not calls["dispatched"]
    finally:
        os.unlink(path)


def test_attempt_reset_second_read_mismatch_is_atomic_zero_write():
    cards, sources, supplied = attempt_reset_fixture()
    changed = max(cards)

    def race_card(number, read_count, live_cards):
        if read_count == len(replay.ATTEMPT_RESET_COHORT) + len(live_cards):
            state = rc._unique_state_block(live_cards[number]["body"])
            state["triage_status"] = "queued"
            live_cards[number]["body"] = rc._replace_state_block(
                live_cards[number]["body"], state
            )

    path = cards_file([])
    before = {number: value["body"] for number, value in cards.items()}
    try:
        with replay_environment(cards, sources, card_read_hook=race_card) as calls:
            try:
                replay.run(
                    path,
                    replay.ATTEMPT_RESET_WAVE,
                    len(replay.ATTEMPT_RESET_COHORT),
                    attempts_reset_cards=supplied,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("attempt reset mutated before full preflight")
            assert len(calls["card_reads"]) >= len(replay.ATTEMPT_RESET_COHORT) * 2
            assert not calls["claims"]
            assert not calls["edits"] and not calls["queued"]
            assert not calls["dispatched"]
            changed_only = {
                number: value["body"]
                for number, value in cards.items()
                if value["body"] != before[number]
            }
            assert set(changed_only) == {changed}
    finally:
        os.unlink(path)


def test_v2_reset_marker_is_never_ordinary_replay_evidence():
    parked = card(status="queued")
    state = rc._unique_state_block(parked["body"])
    state[rc.TRIAGE_ATTEMPTS_FIELD] = {
        "version": rc.TRIAGE_ATTEMPTS_VERSION,
        "kind": "pr-review",
        "revision": "abcdef1",
        "count": 2,
    }
    state[replay.REPLAY_FIELD] = {
        "version": replay.ATTEMPT_RESET_REPLAY_VERSION,
        "wave": replay.ARRAY_RECOVERY_ATTEMPT_RESET_WAVE,
        "revision": "abcdef1",
        "cleared": "error",
        "at": "2026-07-17T20:00:00Z",
        "run_number": 77,
        "attempt_reset": True,
    }
    parked["body"] = rc._replace_state_block(parked["body"], state)
    cards = {42: parked}
    sources = {("wheelhouse", 17, "pr-review"): source()}
    path = cards_file([42])
    calls = {"duplicate": 0}

    def duplicate(**kwargs):
        calls["duplicate"] += 1
        return True

    try:
        with (
            replay_environment(cards, sources, stub_claim=False) as replay_calls,
            patched(
                replay.agent_claim,
                {
                    "supersede_triage_claim": lambda **kwargs: {
                        "event_key": "a" * 64,
                        "superseded": False,
                    },
                    "triage_replay_duplicate_only_evidence": duplicate,
                },
            ),
        ):
            result = replay.run(path, "ordinary-wave", 25)
            assert result["eligible"] == result["written"] == result["queued"] == 0
            assert calls["duplicate"] == 0
            assert not replay_calls["edits"] and not replay_calls["queued"]
            assert not replay_calls["dispatched"]
    finally:
        os.unlink(path)


def test_queue_failure_does_not_unlock_card_for_later_schedule():
    cards = {42: card()}
    sources = {("wheelhouse", 17, "pr-review"): source()}
    path = cards_file([42])
    before = cards[42]["body"]
    try:
        with replay_environment(cards, sources, stub_queue=False) as calls:
            with patched(
                rc,
                {"reserve_triage_budget": lambda number, item, ceiling: False},
            ):
                result = replay.run(path, "wave-one", 25)
            state = rc._unique_state_block(cards[42]["body"])
            assert result == {
                "eligible": 1,
                "planned": 1,
                "deferred": 0,
                "written": 0,
                "queued": 0,
            }
            assert cards[42]["body"] == before
            assert replay.REPLAY_FIELD not in state
            assert state["triage_status"] == "error"
            assert (
                not calls["edits"] and not calls["queued"] and not calls["dispatched"]
            )
    finally:
        os.unlink(path)


def test_claim_tombstone_failure_refuses_replay_before_attempt_or_reservation():
    cards = {42: card()}
    sources = {("wheelhouse", 17, "pr-review"): source()}
    path = cards_file([42])
    before = cards[42]["body"]

    def fail_tombstone(**kwargs):
        raise RuntimeError("simulated claim PATCH failure")

    try:
        with (
            replay_environment(cards, sources, stub_claim=False) as calls,
            patched(
                replay.agent_claim,
                {"supersede_triage_claim": fail_tombstone},
            ),
        ):
            result = replay.run(path, "claim-write-failure", 25)
        state = rc._unique_state_block(cards[42]["body"])
        assert result["eligible"] == 1
        assert result["written"] == result["queued"] == 0
        assert cards[42]["body"] == before
        assert state["triage_status"] == "error"
        assert replay.REPLAY_FIELD not in state
        assert not calls["edits"] and not calls["queued"] and not calls["dispatched"]
    finally:
        os.unlink(path)


def test_absent_cache_gets_absent_marker_and_one_queued_attempt():
    revision = "2026-07-16T10:00:00Z"
    cards = {42: card(kind="issue-triage", revision=revision, status=None)}
    sources = {
        ("wheelhouse", 17, "issue-triage"): source(
            kind="issue-triage", revision=revision
        )
    }
    path = cards_file([42])
    try:
        with replay_environment(cards, sources) as calls:
            result = replay.run(path, "absent-wave", 25)
            state = rc._unique_state_block(cards[42]["body"])
            assert result["queued"] == 1
            assert state[replay.REPLAY_FIELD]["cleared"] == "absent"
            assert state["triage_attempts"]["count"] == 1
            assert len(calls["edits"]) == 1
            assert calls["source_reads"] == [
                ("owner", "wheelhouse", 17, "issue-triage"),
                ("owner", "wheelhouse", 17, "issue-triage"),
            ]
    finally:
        os.unlink(path)


def test_same_revision_refresh_preserves_replay_marker():
    value = card()
    state = rc._unique_state_block(value["body"])
    state[replay.REPLAY_FIELD] = valid_marker()
    marked = rc._replace_state_block(value["body"], state)
    refreshed = rc.render(base_item())["body"]
    preserved = rc._preserve_same_revision_triage(
        refreshed, marked, base_item(), state, owner="owner"
    )
    new_state = rc._unique_state_block(preserved)
    assert new_state[replay.REPLAY_FIELD] == valid_marker()


def inspect(card_value, source_value=None):
    cards = {42: card_value}
    sources = {
        ("wheelhouse", 17, "pr-review"): source_value
        if source_value is not None
        else source()
    }
    with replay_environment(cards, sources):
        return replay.inspect_candidate(42, config(), "owner", True)


def with_state(card_value, mutate):
    value = copy.deepcopy(card_value)
    state = rc._unique_state_block(value["body"])
    mutate(state)
    value["body"] = rc._replace_state_block(value["body"], state)
    return value


def test_never_cleared_matrix_fails_closed():
    queued = card(status="queued")
    succeeded = card(status="succeeded")
    stale = with_state(
        card(), lambda state: state.__setitem__("triaged_sha", "deadbee")
    )
    closed = copy.deepcopy(card())
    closed["state"] = "CLOSED"
    held_queued = with_state(queued, lambda state: state.__setitem__("held", True))
    non_refreshable = copy.deepcopy(card())
    non_refreshable["labels"].append({"name": "processing"})
    wrong_kind = with_state(
        card(), lambda state: state.__setitem__("kind", "ci-approval")
    )
    wrong_kind["labels"] = [
        {"name": "kind:ci-approval"} if row["name"].startswith("kind:") else row
        for row in wrong_kind["labels"]
    ]
    malformed = copy.deepcopy(card())
    malformed["body"] += "\n<!-- wheelhouse-state: {} -->"
    unparseable_status = with_state(
        card(), lambda state: state.__setitem__("triage_status", {"bad": True})
    )
    cases = [
        (queued, source(), "queued"),
        (succeeded, source(), "succeeded"),
        (stale, source(), "stale revision"),
        (closed, source(), "closed card"),
        (held_queued, source(), "held queued"),
        (non_refreshable, source(), "non-refreshable"),
        (wrong_kind, source(), "wrong kind"),
        (card(), source(state="closed"), "source closed"),
        (card(), RuntimeError("404"), "source 404"),
        (card(), source(revision="deadbee"), "source moved"),
        (malformed, source(), "malformed state"),
        (unparseable_status, source(), "unparseable status"),
    ]
    for value, live, label in cases:
        plan, reason = inspect(value, live)
        assert plan is None, (label, reason)


def valid_marker(revision="abcdef1"):
    return {
        "version": 1,
        "wave": "old-wave",
        "revision": revision,
        "cleared": "error",
        "at": "2026-07-16T10:00:00Z",
        "run_number": 12,
    }


def test_marker_mismatch_matrix_never_clears_or_resets_cap():
    markers = []
    wrong_version = valid_marker()
    wrong_version["version"] = 2
    markers.append(wrong_version)
    markers.append(valid_marker("deadbee"))
    forged = valid_marker()
    forged["extra"] = "forged"
    markers.append(forged)
    malformed = "not-an-object"
    markers.append(malformed)
    for marker in markers:
        value = with_state(
            card(), lambda state: state.__setitem__(replay.REPLAY_FIELD, marker)
        )
        before = value["body"]
        state_before = rc._unique_state_block(before)
        plan, reason = inspect(value)
        assert plan is None
        assert reason == "replay-marker-untrusted"
        assert value["body"] == before
        assert rc.triage_attempt_count(state_before, "pr-review", "abcdef1", 2) == 1


def test_replay_applies_scan_author_filter_to_live_source():
    cases = [
        (source(author_login="owner"), "owner"),
        (source(author_login="co-maintainer"), "maintainer"),
        (source(author_login="github-actions[bot]"), "bot suffix"),
        (source(author_login="app", author_type="Bot"), "bot type"),
    ]
    for live, label in cases:
        plan, reason = inspect(card(), live)
        assert plan is None, label
        assert reason == "source-author-excluded", (label, reason)


def test_dry_run_and_budget_bound_list_plans_with_zero_writes():
    cards = {42: card(number=42, target=17), 43: card(number=43, target=18)}
    sources = {
        ("wheelhouse", 17, "pr-review"): source(17),
        ("wheelhouse", 18, "pr-review"): source(18),
    }
    path = cards_file([43, 42])
    before = {number: value["body"] for number, value in cards.items()}
    try:
        output = StringIO()
        with (
            replay_environment(cards, sources, remaining=1) as calls,
            redirect_stdout(output),
        ):
            result = replay.run(path, "dry-wave", 25, dry_run=True)
            assert result == {"eligible": 2, "planned": 1, "deferred": 1, "written": 0}
            assert (
                not calls["edits"] and not calls["queued"] and not calls["dispatched"]
            )
            assert before == {number: value["body"] for number, value in cards.items()}
            assert "DRY-RUN card #42" in output.getvalue()
            assert "replay deferred 1 candidates" in output.getvalue()
            assert "writes=0" in output.getvalue()
    finally:
        os.unlink(path)


def test_exact_selector_isolates_non_prefix_cohort_and_emits_revisions():
    all_numbers = [
        508,
        1421,
        1454,
        1460,
        1483,
        1532,
        1537,
        1567,
        1579,
        1580,
        1581,
        1582,
        1584,
        1585,
        1586,
        1587,
        1588,
        1589,
        1590,
        1591,
        1592,
        1593,
        1594,
        1595,
        1596,
        1597,
        1598,
        1599,
        1600,
        1601,
        1602,
    ]
    requested = (1483, 1584, 1585, 1586, 1594, 1598)
    selector = "v1:" + ",".join(str(number) for number in requested)
    cards, sources, revisions = exact_fixture(all_numbers)
    path = cards_file(list(reversed(all_numbers)))
    try:
        output = StringIO()
        with (
            replay_environment(cards, sources) as calls,
            redirect_stdout(output),
        ):
            result = replay.run(
                path,
                "missing-re-recovery-r2",
                len(requested),
                dry_run=True,
                exact_cards=selector,
            )
        assert result == {
            "eligible": len(requested),
            "planned": len(requested),
            "deferred": 0,
            "written": 0,
        }
        assert calls["card_reads"] == list(requested) * 2
        assert [read[2] for read in calls["source_reads"]] == [
            cards[number]["number"] + 20_000 for number in requested
        ] * 2
        assert not calls["edits"] and not calls["queued"] and not calls["claims"]
        assert "canonical=%s count=6" % selector in output.getvalue()
        assert exact_plan_lines(output.getvalue()) == [
            "replay exact-selector/v1 admitted card #%s: revision=%s clear=error"
            % (number, revisions[number])
            for number in requested
        ]
        assert "card #508" not in output.getvalue()
    finally:
        os.unlink(path)


def test_exact_selector_dry_run_and_write_plans_are_identical():
    requested = (1483, 1584, 1585, 1586, 1594, 1598)
    selector = "v1:" + ",".join(str(number) for number in requested)

    def execute(dry_run):
        cards, sources, revisions = exact_fixture(requested)
        path = cards_file([1, *reversed(requested)])
        output = StringIO()
        try:
            with (
                replay_environment(cards, sources) as calls,
                redirect_stdout(output),
            ):
                result = replay.run(
                    path,
                    "missing-re-recovery-r2",
                    len(requested),
                    dry_run=dry_run,
                    exact_cards=selector,
                )
            return result, calls, output.getvalue(), revisions, cards
        finally:
            os.unlink(path)

    dry_result, dry_calls, dry_output, revisions, _ = execute(True)
    write_result, write_calls, write_output, _, written_cards = execute(False)
    expected_plans = [
        "replay exact-selector/v1 admitted card #%s: revision=%s clear=error"
        % (number, revisions[number])
        for number in requested
    ]
    assert exact_plan_lines(dry_output) == exact_plan_lines(write_output)
    assert exact_plan_lines(write_output) == expected_plans
    assert dry_result["planned"] == write_result["planned"] == len(requested)
    assert dry_result["written"] == 0
    assert write_result["written"] == write_result["queued"] == len(requested)
    assert not dry_calls["edits"] and not dry_calls["queued"]
    assert [entry[0] for entry in write_calls["queued"]] == list(requested)
    assert all(
        rc._unique_state_block(written_cards[number]["body"])[replay.REPLAY_FIELD][
            "revision"
        ]
        == revisions[number]
        for number in requested
    )


def test_exact_selector_contract_rejects_malformed_and_limit_mismatches_before_reads():
    assert replay._exact_card_scope("") == ()
    assert replay._exact_card_scope("v1:3,1,2") == (1, 2, 3)
    malformed = (
        "v1:",
        "v1:1,",
        "v1:,1",
        "v1:1,,2",
        "v1:1-2",
        "v1:*",
        "v1:1x",
        "v1:01",
        "v1:1,1",
        "v2:1",
        " v1:1",
        "v1:1 ",
        "v1:" + ",".join(str(number) for number in range(1, 27)),
        "v1:9007199254740992",
        "v1:" + "9" * replay.EXACT_SELECTOR_MAX_BYTES,
    )
    path = cards_file([])
    try:
        with replay_environment({}, {}) as calls:
            for value in malformed:
                try:
                    replay.run(path, "exact-contract", 1, exact_cards=value)
                except ValueError:
                    pass
                else:
                    raise AssertionError("accepted malformed selector %r" % value)
            try:
                replay.run(path, "exact-contract", 1, exact_cards="v1:1,2")
            except ValueError:
                pass
            else:
                raise AssertionError("accepted limit-inconsistent selector")
            assert not calls["card_reads"] and not calls["source_reads"]
            assert not calls["edits"] and not calls["queued"] and not calls["claims"]
            assert not calls["dispatched"]
    finally:
        os.unlink(path)


def test_exact_selector_requested_rejections_are_atomic_and_never_substitute():
    requested = (42, 43)
    selector = "v1:42,43"

    def attempt(mutator):
        cards, sources, _ = exact_fixture((1, *requested))
        mutator(cards, sources)
        path = cards_file([1, *requested])
        output = StringIO()
        try:
            with (
                replay_environment(cards, sources) as calls,
                redirect_stdout(output),
            ):
                try:
                    replay.run(
                        path,
                        "exact-rejection",
                        len(requested),
                        exact_cards=selector,
                    )
                except ValueError:
                    pass
                else:
                    raise AssertionError("exact selector accepted rejected request")
            assert 1 not in calls["card_reads"]
            assert not calls["edits"] and not calls["queued"] and not calls["claims"]
            assert not calls["dispatched"]
            assert "refused card #43" in output.getvalue()
        finally:
            os.unlink(path)

    attempt(lambda cards, sources: cards.pop(43))

    def non_refreshable(cards, sources):
        cards[43]["labels"].append({"name": "processing"})

    attempt(non_refreshable)

    def head_moved(cards, sources):
        sources[("wheelhouse", 20_043, "pr-review")]["head"]["sha"] = "deadbee"

    attempt(head_moved)

    def already_recovered(cards, sources):
        state = rc._unique_state_block(cards[43]["body"])
        state["triage_status"] = "succeeded"
        cards[43]["body"] = rc._replace_state_block(cards[43]["body"], state)

    attempt(already_recovered)

    def already_replayed(cards, sources):
        state = rc._unique_state_block(cards[43]["body"])
        state[replay.REPLAY_FIELD] = valid_marker("%07x" % 43)
        state["triage_status"] = "queued"
        cards[43]["body"] = rc._replace_state_block(cards[43]["body"], state)

    attempt(already_replayed)

    def attempt_exhausted(cards, sources):
        state = rc._unique_state_block(cards[43]["body"])
        state[rc.TRIAGE_ATTEMPTS_FIELD] = {
            "version": rc.TRIAGE_ATTEMPTS_VERSION,
            "kind": "pr-review",
            "revision": "%07x" % 43,
            "count": 2,
        }
        cards[43]["body"] = rc._replace_state_block(cards[43]["body"], state)

    attempt(attempt_exhausted)


def test_exact_selector_refuses_budget_and_preflight_races_before_writes():
    requested = (42, 43)
    selector = "v1:42,43"
    cards, sources, _ = exact_fixture(requested)
    path = cards_file([1, *requested])
    try:
        output = StringIO()
        with (
            replay_environment(cards, sources, remaining=1) as calls,
            redirect_stdout(output),
        ):
            try:
                replay.run(path, "exact-budget", 2, exact_cards=selector)
            except ValueError:
                pass
            else:
                raise AssertionError("exact selector accepted partial budget")
        assert "refused cards #42,#43: insufficient-budget" in output.getvalue()
        assert not calls["edits"] and not calls["queued"] and not calls["claims"]
    finally:
        os.unlink(path)

    cards, sources, _ = exact_fixture(requested)

    def race_card(number, read_count, live_cards):
        if number == 43 and read_count == 4:
            live_cards[number]["labels"].append({"name": "processing"})

    path = cards_file([1, *requested])
    try:
        output = StringIO()
        with (
            replay_environment(cards, sources, card_read_hook=race_card) as calls,
            redirect_stdout(output),
        ):
            try:
                replay.run(path, "exact-race", 2, exact_cards=selector)
            except ValueError:
                pass
            else:
                raise AssertionError("exact selector mutated after preflight race")
        assert "refused card #43: card-not-refreshable" in output.getvalue()
        assert not calls["edits"] and not calls["queued"] and not calls["claims"]
    finally:
        os.unlink(path)


def test_exact_selector_never_replaces_reviewed_revision_during_write():
    cards, sources, revisions = exact_fixture((42,))
    path = cards_file([42])
    output = StringIO()

    def advance_card_and_target(number, read_count, live_cards):
        if read_count != 3:
            return
        replacement_revision = "deadbee"
        live_cards[number] = card(
            number=number,
            target=20_042,
            revision=replacement_revision,
        )
        sources[("wheelhouse", 20_042, "pr-review")] = source(
            number=20_042,
            revision=replacement_revision,
        )

    try:
        with (
            replay_environment(
                cards,
                sources,
                card_read_hook=advance_card_and_target,
            ) as calls,
            redirect_stdout(output),
        ):
            try:
                replay.run(
                    path,
                    "exact-revision-race",
                    1,
                    exact_cards="v1:42",
                )
            except ValueError:
                pass
            else:
                raise AssertionError("exact selector replaced the reviewed revision")
        assert (
            "replay exact-selector/v1 admitted card #42: revision=%s clear=error"
            % revisions[42]
            in output.getvalue()
        )
        assert "card-raced-before-replay" in output.getvalue()
        assert not calls["edits"] and not calls["queued"] and not calls["claims"]
        assert not calls["dispatched"]
    finally:
        os.unlink(path)


def test_exact_selector_keeps_claim_tombstone_authoritative():
    cards, sources, _ = exact_fixture((42,))
    path = cards_file([1, 42])

    def fail_tombstone(**kwargs):
        raise RuntimeError("simulated claim PATCH failure")

    try:
        with (
            replay_environment(cards, sources, stub_claim=False) as calls,
            patched(replay.agent_claim, {"supersede_triage_claim": fail_tombstone}),
        ):
            try:
                replay.run(path, "exact-claim", 1, exact_cards="v1:42")
            except ValueError:
                pass
            else:
                raise AssertionError("exact selector bypassed claim tombstone")
        assert not calls["edits"] and not calls["queued"] and not calls["dispatched"]
    finally:
        os.unlink(path)


def test_no_exact_selector_preserves_legacy_sorted_prefix():
    numbers = (5, 2, 9)
    cards, sources, _ = exact_fixture(numbers)
    path = cards_file(numbers)
    try:
        output = StringIO()
        with replay_environment(cards, sources) as calls, redirect_stdout(output):
            result = replay.run(path, "legacy-prefix", 2, dry_run=True)
        assert result == {"eligible": 3, "planned": 2, "deferred": 1, "written": 0}
        assert "DRY-RUN card #2" in output.getvalue()
        assert "DRY-RUN card #5" in output.getvalue()
        assert "DRY-RUN card #9" not in output.getvalue()
        assert calls["card_reads"] == [2, 5, 9]
    finally:
        os.unlink(path)


def test_entry_conditions_reject_schedule_non_owner_bad_wave_and_bad_limit():
    old_env = dict(os.environ)
    valid = {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY_OWNER": "owner",
        "GITHUB_REPOSITORY": "owner/wheelhouse",
        "GITHUB_ACTOR": "owner",
        "GITHUB_RUN_NUMBER": "77",
        "WHEELHOUSE_AUTO_TRIAGE_HAS_READONLY_TOKEN": "false",
    }
    try:
        os.environ.update(valid)
        assert replay._entry("valid-wave", 25) == ("owner", 77)
        cases = [
            ({"GITHUB_EVENT_NAME": "schedule"}, "valid-wave", 25),
            ({"GITHUB_ACTOR": "someone-else"}, "valid-wave", 25),
            ({}, "Bad_Wave", 25),
            ({}, "", 25),
            ({}, "valid-wave", 0),
            ({}, "valid-wave", 26),
            ({"GITHUB_RUN_NUMBER": "not-a-number"}, "valid-wave", 25),
        ]
        for env_overrides, wave, limit in cases:
            os.environ.update(valid)
            os.environ.update(env_overrides)
            try:
                replay._entry(wave, limit)
            except ValueError:
                pass
            else:
                raise AssertionError((env_overrides, wave, limit))
    finally:
        os.environ.clear()
        os.environ.update(old_env)


class FakeRecordGh:
    def __init__(self):
        self.comments = []
        self.next_id = 1
        self.writes = []

    def __call__(self, *args):
        if "--paginate" in args:
            return [copy.deepcopy(self.comments)]
        method = args[args.index("--method") + 1] if "--method" in args else "GET"
        endpoint = next(value for value in args if value.startswith("repos/"))
        if method in {"POST", "PATCH"}:
            body = next(value[5:] for value in args if value.startswith("body="))
            self.writes.append((method, endpoint, body))
            if method == "POST":
                row = {
                    "id": self.next_id,
                    "body": body,
                    "user": {"login": "github-actions[bot]"},
                }
                self.next_id += 1
                self.comments.append(row)
                return copy.deepcopy(row)
            comment_id = int(endpoint.rsplit("/", 1)[-1])
            row = next(row for row in self.comments if row["id"] == comment_id)
            row["body"] = body
            return copy.deepcopy(row)
        comment_id = int(endpoint.rsplit("/", 1)[-1])
        return copy.deepcopy(
            next(row for row in self.comments if row["id"] == comment_id)
        )


class FakeClaimGh:
    def __init__(self):
        self.comments = []
        self.next_id = 1

    def __call__(self, *args):
        if "--paginate" in args:
            return [copy.deepcopy(self.comments)]
        method = args[args.index("--method") + 1] if "--method" in args else "GET"
        endpoint = next(value for value in args if value.startswith("repos/"))
        if method in {"POST", "PATCH"}:
            body = next(value[5:] for value in args if value.startswith("body="))
            if method == "POST":
                row = {
                    "id": self.next_id,
                    "body": body,
                    "user": {"login": "github-actions[bot]"},
                    "created_at": "2026-07-16T09:00:00Z",
                    "updated_at": "2026-07-16T09:00:00Z",
                }
                self.next_id += 1
                self.comments.append(row)
                return copy.deepcopy(row)
            comment_id = int(endpoint.rsplit("/", 1)[-1])
            row = next(row for row in self.comments if row["id"] == comment_id)
            row["body"] = body
            row["updated_at"] = "2026-07-16T11:00:00Z"
            return copy.deepcopy(row)
        comment_id = int(endpoint.rsplit("/", 1)[-1])
        return copy.deepcopy(
            next(row for row in self.comments if row["id"] == comment_id)
        )


def triage_claim_args():
    return argparse.Namespace(
        action="triage.pr.local",
        owner="owner",
        repo="wheelhouse",
        number=17,
        issue=42,
        revision="abcdef1",
        event_id="",
        repo_slug="owner/wheelhouse",
    )


def test_duplicate_only_evidence_requires_a_terminal_pre_replay_claim_and_record():
    args = triage_claim_args()
    identity = agent_claim.normalized_event_identity(
        action=args.action,
        owner=args.owner,
        repo=args.repo,
        number=args.number,
        card_issue=args.issue,
        revision=args.revision,
    )
    event_key = agent_claim.event_key_sha256(identity)
    marker = agent_claim.event_claim_marker(event_key)
    fake = FakeClaimGh()
    fake.comments.append(
        {
            "id": 1,
            "body": "Agent triage event finished with consumer.committed. %s" % marker,
            "user": {"login": "github-actions[bot]"},
            "created_at": "2026-07-16T09:00:00Z",
            "updated_at": "2026-07-16T09:00:00Z",
        }
    )
    evidence = dict(
        action=args.action,
        owner=args.owner,
        repo=args.repo,
        number=args.number,
        issue=args.issue,
        revision=args.revision,
        repo_slug=args.repo_slug,
        replayed_at="2026-07-16T10:00:00Z",
    )
    with patched(agent_claim, {"gh_json": fake}):
        assert agent_claim.triage_replay_duplicate_only_evidence(**evidence)
        fake.comments[0]["body"] = (
            "Agent event admitted and is being processed.\n\n%s" % marker
        )
        assert not agent_claim.triage_replay_duplicate_only_evidence(**evidence)
        fake.comments[0]["body"] = (
            "Agent triage event finished with consumer.committed. %s" % marker
        )
        fake.comments[0]["updated_at"] = "2026-07-16T11:00:00Z"
        assert not agent_claim.triage_replay_duplicate_only_evidence(**evidence)
        fake.comments[0]["updated_at"] = "2026-07-16T09:00:00Z"
        fake.comments.append(
            {
                "id": 2,
                "body": agent_claim.triage_record_body(
                    event_key, "abcdef1", "error", "consumer.rejected"
                ),
                "user": {"login": "github-actions[bot]"},
                "created_at": "2026-07-16T11:00:00Z",
                "updated_at": "2026-07-16T11:00:00Z",
            }
        )
        assert not agent_claim.triage_replay_duplicate_only_evidence(**evidence)


def test_replay_supersedes_failed_attempt_claim_before_exact_revision_readmission():
    cards = {42: card()}
    sources = {("wheelhouse", 17, "pr-review"): source()}
    path = cards_file([42])
    fake = FakeClaimGh()
    try:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "output"
            old_output = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = str(output_path)
            try:
                with (
                    patched(agent_claim, {"gh_json": fake}),
                    patched(replay.agent_claim, {"gh_json": fake}),
                ):
                    assert agent_claim.claim(triage_claim_args()) == 0
                    first_outputs = output_path.read_text(encoding="utf-8")
                    assert "admitted=true" in first_outputs
                    marker = next(
                        line.split("=", 1)[1]
                        for line in first_outputs.splitlines()
                        if line.startswith("marker=")
                    )
                    fake.comments[0]["body"] = (
                        "Agent triage event finished with consumer.committed. %s"
                        % marker
                    )

                    with replay_environment(cards, sources, stub_claim=False):
                        replayed = replay.run(path, "claim-gap", 25)
                    assert replayed["queued"] == 1
                    assert (
                        rc._unique_state_block(cards[42]["body"])["triage_status"]
                        == "queued"
                    )

                    output_path.write_text("", encoding="utf-8")
                    assert agent_claim.claim(triage_claim_args()) == 0
                    second_outputs = output_path.read_text(encoding="utf-8")
                    assert "admitted=true" in second_outputs
                    assert marker not in fake.comments[0]["body"]
            finally:
                if old_output is None:
                    os.environ.pop("GITHUB_OUTPUT", None)
                else:
                    os.environ["GITHUB_OUTPUT"] = old_output
    finally:
        os.unlink(path)


def test_duplicate_only_parked_replay_does_not_consume_cap_or_once_marker():
    parked = card()
    state = rc._unique_state_block(parked["body"])
    state = rc._state_with_triage(state, "abcdef1", "queued")
    state[rc.TRIAGE_ATTEMPTS_FIELD] = {
        "version": rc.TRIAGE_ATTEMPTS_VERSION,
        "kind": "pr-review",
        "revision": "abcdef1",
        "count": 2,
    }
    state[replay.REPLAY_FIELD] = valid_marker()
    parked["body"] = rc._replace_state_block(parked["body"], state)
    cards = {42: parked}
    sources = {("wheelhouse", 17, "pr-review"): source()}
    path = cards_file([42])
    fake = FakeClaimGh()
    try:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "output"
            old_output = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = str(output_path)
            try:
                with (
                    patched(agent_claim, {"gh_json": fake}),
                    patched(replay.agent_claim, {"gh_json": fake}),
                ):
                    assert agent_claim.claim(triage_claim_args()) == 0
                    claim_outputs = output_path.read_text(encoding="utf-8")
                    event_key = next(
                        line.split("=", 1)[1]
                        for line in claim_outputs.splitlines()
                        if line.startswith("event_key=")
                    )
                    marker = next(
                        line.split("=", 1)[1]
                        for line in claim_outputs.splitlines()
                        if line.startswith("marker=")
                    )
                    fake.comments[0]["body"] = (
                        "Agent triage event finished with consumer.committed. %s"
                        % marker
                    )
                    agent_claim.record_triage_result(
                        record_args(event_key, "error", "consumer.committed")
                    )

                    with replay_environment(cards, sources, stub_claim=False):
                        result = replay.run(path, "cohort-reentry", 25)

                    new_state = rc._unique_state_block(cards[42]["body"])
                    assert result["eligible"] == result["queued"] == 1
                    assert new_state["triage_status"] == "queued"
                    assert new_state[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 2
                    assert new_state[replay.REPLAY_FIELD]["wave"] == "cohort-reentry"
                    assert marker not in fake.comments[0]["body"]
            finally:
                if old_output is None:
                    os.environ.pop("GITHUB_OUTPUT", None)
                else:
                    os.environ["GITHUB_OUTPUT"] = old_output
    finally:
        os.unlink(path)


def test_duplicate_only_replay_retry_survives_post_tombstone_queue_deferral():
    parked = card()
    state = rc._unique_state_block(parked["body"])
    state = rc._state_with_triage(state, "abcdef1", "queued")
    state[rc.TRIAGE_ATTEMPTS_FIELD] = {
        "version": rc.TRIAGE_ATTEMPTS_VERSION,
        "kind": "pr-review",
        "revision": "abcdef1",
        "count": 2,
    }
    state[replay.REPLAY_FIELD] = valid_marker()
    parked["body"] = rc._replace_state_block(parked["body"], state)
    cards = {42: parked}
    sources = {("wheelhouse", 17, "pr-review"): source()}
    path = cards_file([42])
    fake = FakeClaimGh()
    try:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "output"
            old_output = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = str(output_path)
            try:
                with (
                    patched(agent_claim, {"gh_json": fake}),
                    patched(replay.agent_claim, {"gh_json": fake}),
                ):
                    assert agent_claim.claim(triage_claim_args()) == 0
                    claim_outputs = output_path.read_text(encoding="utf-8")
                    event_key = next(
                        line.split("=", 1)[1]
                        for line in claim_outputs.splitlines()
                        if line.startswith("event_key=")
                    )
                    marker = next(
                        line.split("=", 1)[1]
                        for line in claim_outputs.splitlines()
                        if line.startswith("marker=")
                    )
                    fake.comments[0]["body"] = (
                        "Agent triage event finished with consumer.committed. %s"
                        % marker
                    )
                    agent_claim.record_triage_result(
                        record_args(event_key, "error", "consumer.committed")
                    )
                    before = cards[42]["body"]

                    with replay_environment(
                        cards, sources, stub_queue=False, stub_claim=False
                    ):
                        with patched(
                            rc,
                            {
                                "_configured_triage_spend_limits": lambda item: (
                                    2,
                                    1200,
                                ),
                                "reserve_triage_budget": (
                                    lambda number, item, ceiling: False
                                ),
                            },
                        ):
                            failed = replay.run(path, "failed-reentry", 25)

                    assert failed["eligible"] == 1
                    assert failed["written"] == failed["queued"] == 0
                    assert cards[42]["body"] == before
                    assert marker not in fake.comments[0]["body"]
                    assert (
                        agent_claim.TRIAGE_CLAIM_SUPERSEDED_PREFIX
                        in fake.comments[0]["body"]
                    )

                    with replay_environment(cards, sources, stub_claim=False):
                        retried = replay.run(path, "retry-reentry", 25)

                    new_state = rc._unique_state_block(cards[42]["body"])
                    assert retried["eligible"] == retried["queued"] == 1
                    assert new_state["triage_status"] == "queued"
                    assert new_state[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 2
                    assert new_state[replay.REPLAY_FIELD]["wave"] == "retry-reentry"
            finally:
                if old_output is None:
                    os.environ.pop("GITHUB_OUTPUT", None)
                else:
                    os.environ["GITHUB_OUTPUT"] = old_output
    finally:
        os.unlink(path)


def test_admission_denial_terminalizes_only_the_exact_queued_revision():
    cards = {42: card(status="queued")}

    def get_card(number):
        return copy.deepcopy(cards.get(number))

    def edit(number, body, remove_labels=None):
        cards[number]["body"] = body

    with patched(rc, {"get_card": get_card, "_edit_issue_body": edit}):
        assert rc.update_card_triage(
            42,
            "abcdef1",
            error="Exact-revision admission denied (admission.duplicate).",
            require_queued=True,
        )
        terminal = rc._unique_state_block(cards[42]["body"])
        assert terminal["triaged_sha"] == "abcdef1"
        assert terminal["triage_status"] == "error"
        assert terminal["triage_error"].endswith("(admission.duplicate).")
        assert "### Triage" in cards[42]["body"]
        before = cards[42]["body"]
        assert not rc.update_card_triage(
            42,
            "abcdef1",
            error="duplicate late write",
            require_queued=True,
        )
        assert cards[42]["body"] == before


def record_args(event_key, status, code="consumer.committed"):
    return argparse.Namespace(
        issue=42,
        repo_slug="owner/wheelhouse",
        event_key=event_key,
        revision="abcdef1",
        status=status,
        code=code,
    )


def test_result_records_cover_success_failure_bound_and_duplicate_editing():
    fake = FakeRecordGh()
    success_key = "a" * 64
    failure_key = "b" * 64
    with patched(agent_claim, {"gh_json": fake}):
        agent_claim.record_triage_result(record_args(success_key, "succeeded"))
        agent_claim.record_triage_result(record_args(success_key, "succeeded"))
        agent_claim.record_triage_result(
            record_args(failure_key, "error", "consumer.rejected")
        )
        agent_claim.record_triage_result(
            record_args(success_key, "error", "consumer.rejected")
        )
    assert len(fake.comments) == 2
    assert [method for method, _, _ in fake.writes] == ["POST", "POST", "PATCH"]
    records = [agent_claim.parse_triage_record(row["body"]) for row in fake.comments]
    assert {record["status"] for record in records} == {"error"}
    assert all(len(row["body"].encode("utf-8")) < 512 for row in fake.comments)
    assert all(
        "target" not in row["body"] and "comment" not in row["body"]
        for row in fake.comments
    )


def _scan_workflow_step_plan(event_name, wave="", dry_run=False, exact_cards=""):
    """Evaluate the scan workflow's production step conditions for one event."""
    document = yaml.safe_load(
        (ROOT / ".github/workflows/scan-backstop.yml").read_text(encoding="utf-8")
    )
    values = {
        "github.event_name": event_name,
        "inputs.replay_wave": wave,
        "inputs.replay_dry_run": dry_run,
        "inputs.replay_exact_cards": exact_cards,
    }
    planned = []
    for step in document["jobs"]["reconcile"]["steps"]:
        condition = str(step.get("if", "true")).strip()
        if condition.startswith("${{") and condition.endswith("}}"):
            condition = condition[3:-2].strip()
        for name, value in values.items():
            condition = condition.replace(name, repr(value))
        expression = (
            condition.replace("always()", "True")
            .replace("&&", " and ")
            .replace("||", " or ")
            .replace("!(", "not (")
            .replace("true", "True")
            .replace("false", "False")
        )
        if eval(expression, {"__builtins__": {}}, {}):
            planned.append(step.get("name") or step.get("uses"))
    return planned


def test_workflow_exact_selector_replay_only_posture_matrix():
    prerequisites = {
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "Install deps",
    }
    replay_step = "Replay one bounded auto-triage wave"
    ordinary_steps = {
        "List open cards",
        "Scan the fleet",
        "Claim auto-merge decision cards",
        "Validate auto-merge decision cards",
        "Auto-merge eligible PRs",
        "Record auto-merges",
        "Reconcile the queue",
        "Check fleet-scan health",
    }

    # A raw non-empty selector isolates the run before selector validation.
    # The exact replay owner is the only project command that can act in either
    # write or dry-run mode; its script tests below retain exact-cohort and
    # writes=0 enforcement respectively.
    for dry_run in (False, True):
        planned = set(
            _scan_workflow_step_plan(
                "workflow_dispatch",
                wave="reviewed-wave",
                dry_run=dry_run,
                exact_cards="v1:41,42",
            )
        )
        assert planned == prerequisites | {replay_step}
        assert planned.isdisjoint(ordinary_steps)

    # Even malformed or incomplete raw input cannot fall through to ordinary
    # scan/backstop acting. It reaches the replay owner, which rejects it before
    # any exact-card read or write.
    malformed = set(
        _scan_workflow_step_plan(
            "workflow_dispatch",
            wave="",
            dry_run=False,
            exact_cards="not-a-selector",
        )
    )
    incomplete = set(
        _scan_workflow_step_plan(
            "workflow_dispatch",
            wave="",
            dry_run=False,
            exact_cards="v1:41,42",
        )
    )
    assert malformed == incomplete == prerequisites | {replay_step}
    try:
        replay._exact_card_scope("not-a-selector")
        assert False, "malformed exact selector accepted"
    except ValueError:
        pass

    # Empty exact-selector input preserves all prior owners: scheduled and
    # ordinary manual maintenance, generic write replay, and generic dry-run.
    scheduled = set(_scan_workflow_step_plan("schedule"))
    manual = set(_scan_workflow_step_plan("workflow_dispatch"))
    generic_write = set(
        _scan_workflow_step_plan(
            "workflow_dispatch", wave="reviewed-wave", dry_run=False
        )
    )
    generic_dry_run = set(
        _scan_workflow_step_plan(
            "workflow_dispatch", wave="reviewed-wave", dry_run=True
        )
    )
    assert scheduled == prerequisites | ordinary_steps
    assert manual == prerequisites | ordinary_steps
    assert generic_write == prerequisites | ordinary_steps | {replay_step}
    assert generic_dry_run == prerequisites | {"List open cards", replay_step}


def test_workflow_is_inert_and_reuses_existing_queue_and_record_boundaries():
    scan_text = (ROOT / ".github/workflows/scan-backstop.yml").read_text(
        encoding="utf-8"
    )
    scan = yaml.safe_load(scan_text)
    on_doc = scan.get(True) or scan.get("on")
    dispatch_inputs = on_doc["workflow_dispatch"]["inputs"]
    assert dispatch_inputs["replay_wave"]["default"] == ""
    assert dispatch_inputs["replay_limit"]["default"] == "25"
    assert "1..25" in dispatch_inputs["replay_limit"]["description"]
    assert dispatch_inputs["replay_dry_run"]["default"] is True
    assert dispatch_inputs["replay_exact_cards"]["default"] == ""
    assert "v1:N,N" in dispatch_inputs["replay_exact_cards"]["description"]
    assert "replay_limit" in dispatch_inputs["replay_exact_cards"]["description"]
    assert dispatch_inputs["replay_attempts_reset_cards"]["default"] == ""
    dry_run_guard = (
        "github.event_name == 'workflow_dispatch' && "
        "inputs.replay_wave != '' && inputs.replay_dry_run"
    )
    exact_isolation_guard = (
        "github.event_name == 'workflow_dispatch' && "
        "inputs.replay_exact_cards != ''"
    )
    assert scan["permissions"] == {
        "contents": "read",
        "issues": "write",
        "actions": "write",
    }
    assert scan["jobs"]["reconcile"]["if"] == (
        "github.event_name == 'schedule' || github.actor == github.repository_owner"
    )
    list_step = next(
        value
        for value in scan["jobs"]["reconcile"]["steps"]
        if value.get("name") == "List open cards"
    )
    assert exact_isolation_guard in list_step["if"]
    assert "!" in list_step["if"]
    write_capable_steps = {
        "Scan the fleet",
        "Claim auto-merge decision cards",
        "Validate auto-merge decision cards",
        "Auto-merge eligible PRs",
        "Record auto-merges",
        "Reconcile the queue",
        "Check fleet-scan health",
    }
    for guarded in write_capable_steps:
        guarded_step = next(
            value
            for value in scan["jobs"]["reconcile"]["steps"]
            if value.get("name") == guarded
        )
        condition = guarded_step.get("if", "")
        assert dry_run_guard in condition, guarded
        assert exact_isolation_guard in condition, guarded
        assert "!" in condition, guarded
    step = next(
        value
        for value in scan["jobs"]["reconcile"]["steps"]
        if value.get("name") == "Replay one bounded auto-triage wave"
    )
    assert "github.event_name == 'workflow_dispatch'" in step["if"]
    assert "inputs.replay_wave != ''" in step["if"]
    assert "inputs.replay_exact_cards != ''" in step["if"]
    assert "||" in step["if"]
    assert "scripts/triage_replay.py" in step["run"]
    assert "REPLAY_DRY_RUN" in step["run"]
    assert "args+=(--dry-run)" in step["run"]
    assert "REPLAY_EXACT_CARDS" in step["run"]
    assert 'args+=(--exact-cards "$REPLAY_EXACT_CARDS")' in step["run"]
    assert step["env"]["REPLAY_EXACT_CARDS"] == "${{ inputs.replay_exact_cards }}"
    assert "REPLAY_ATTEMPTS_RESET_CARDS" in step["run"]
    assert "--attempts-reset-cards" in step["run"]
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert step["env"]["FLEET_TOKEN"] == "${{ secrets.FLEET_TOKEN }}"
    assert step["env"]["WHEELHOUSE_FLEET_TOKEN"] == "${{ secrets.FLEET_TOKEN }}"
    assert (
        step["env"]["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"]
        == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN != '' }}"
    )
    assert "--dry-run" in (ROOT / "scripts/triage_replay.py").read_text(
        encoding="utf-8"
    )
    replay_text = (ROOT / "scripts/triage_replay.py").read_text(encoding="utf-8")
    assert replay.REPLAY_LIMIT_MAX == 25
    assert replay.EXACT_SELECTOR_VERSION == 1
    assert "v1:N[,N...]" in replay.parser().format_help()
    runtime_doc = (ROOT / "docs/AGENT_RUNTIME.md").read_text(encoding="utf-8")
    assert "replay_exact_cards" in runtime_doc
    assert "v1:1483,1584,1585,1586,1594,1598" in runtime_doc
    assert "no other card is substituted" in runtime_doc
    assert len(replay.ATTEMPT_RESET_COHORT) == 19
    assert replay.ATTEMPT_RESET_WAVE == "evidence-empty-e7-final"
    assert len(replay.ARRAY_RECOVERY_ATTEMPT_RESET_COHORT) == 15
    assert set(replay.ARRAY_RECOVERY_ATTEMPT_RESET_COHORT) == {
        154,
        481,
        572,
        907,
        951,
        1266,
        1275,
        1428,
        1430,
        1435,
        1436,
        1437,
        1441,
        1442,
        1443,
    }
    assert replay.ARRAY_RECOVERY_ATTEMPT_RESET_WAVE == "array-recovery-g1-final"
    assert replay.ATTEMPT_RESET_COHORTS == {
        replay.ATTEMPT_RESET_WAVE: replay.ATTEMPT_RESET_COHORT,
        replay.ARRAY_RECOVERY_ATTEMPT_RESET_WAVE: (
            replay.ARRAY_RECOVERY_ATTEMPT_RESET_COHORT
        ),
    }
    wheelhouse_config = yaml.safe_load(
        (ROOT / "wheelhouse.config.yml").read_text(encoding="utf-8")
    )
    assert wheelhouse_config["triage_attempt_cap_per_revision"] == 2
    assert wheelhouse_config["triage_daily_ceiling"] == 1200
    assert "reconcile.maybe_queue_auto_triage" in replay_text
    assert "dispatch_triage_workflow" not in replay_text
    assert replay.REPLAY_FIELD not in rc.MATERIAL_FIELDS
    triage_text = (ROOT / ".github/workflows/triage.yml").read_text(encoding="utf-8")
    assert "triage_queued_for_head" in triage_text
    assert "agent_claim.py record" in triage_text
    assert "wheelhouse-triage-record" in (ROOT / "scripts/agent_claim.py").read_text(
        encoding="utf-8"
    )
    denial = next(
        value
        for value in yaml.safe_load(triage_text)["jobs"]["triage"]["steps"]
        if value.get("id") == "admission-denial-consumer"
    )
    assert "steps.event-claim.outputs.admitted == 'false'" in denial["if"]
    assert "triage-fail" in denial["run"]
    assert "admission.duplicate" in denial["run"]
    assert "--queued-only" in denial["run"]
    assert "ADMISSION_DENIED" in triage_text


TESTS = [
    test_terminal_error_is_cleared_and_queued_once_then_second_wave_noops,
    test_sanctioned_attempt_reset_grants_exact_cohort_one_reentry,
    test_array_recovery_attempt_reset_grants_exact_cohort_one_reentry,
    test_array_recovery_attempt_reset_requires_exact_wave_cohort_and_limit,
    test_array_recovery_attempt_reset_mismatches_are_atomic_zero_write,
    test_attempt_reset_later_race_pauses_then_resumes_exact_cohort,
    test_attempt_reset_resume_requires_only_pending_budget,
    test_attempt_reset_refuses_outside_scope_and_any_state_mismatch,
    test_attempt_reset_binds_complete_prior_marker_identity,
    test_attempt_reset_second_read_mismatch_is_atomic_zero_write,
    test_v2_reset_marker_is_never_ordinary_replay_evidence,
    test_absent_cache_gets_absent_marker_and_one_queued_attempt,
    test_same_revision_refresh_preserves_replay_marker,
    test_queue_failure_does_not_unlock_card_for_later_schedule,
    test_claim_tombstone_failure_refuses_replay_before_attempt_or_reservation,
    test_never_cleared_matrix_fails_closed,
    test_marker_mismatch_matrix_never_clears_or_resets_cap,
    test_replay_applies_scan_author_filter_to_live_source,
    test_dry_run_and_budget_bound_list_plans_with_zero_writes,
    test_exact_selector_isolates_non_prefix_cohort_and_emits_revisions,
    test_exact_selector_dry_run_and_write_plans_are_identical,
    test_exact_selector_contract_rejects_malformed_and_limit_mismatches_before_reads,
    test_exact_selector_requested_rejections_are_atomic_and_never_substitute,
    test_exact_selector_refuses_budget_and_preflight_races_before_writes,
    test_exact_selector_never_replaces_reviewed_revision_during_write,
    test_exact_selector_keeps_claim_tombstone_authoritative,
    test_no_exact_selector_preserves_legacy_sorted_prefix,
    test_entry_conditions_reject_schedule_non_owner_bad_wave_and_bad_limit,
    test_result_records_cover_success_failure_bound_and_duplicate_editing,
    test_duplicate_only_evidence_requires_a_terminal_pre_replay_claim_and_record,
    test_replay_supersedes_failed_attempt_claim_before_exact_revision_readmission,
    test_duplicate_only_parked_replay_does_not_consume_cap_or_once_marker,
    test_duplicate_only_replay_retry_survives_post_tombstone_queue_deferral,
    test_admission_denial_terminalizes_only_the_exact_queued_revision,
    test_workflow_exact_selector_replay_only_posture_matrix,
    test_workflow_is_inert_and_reuses_existing_queue_and_record_boundaries,
]


if __name__ == "__main__":
    for test in TESTS:
        test()
        print("ok - %s" % test.__name__)
