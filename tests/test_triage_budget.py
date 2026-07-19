#!/usr/bin/env python3
"""Offline regression coverage for automatic-triage spend guards."""

import ast
import copy
import io
import json
import os
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from types import SimpleNamespace

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import build_item  # noqa: E402
import reconcile  # noqa: E402
import render_card as rc  # noqa: E402
import wheelhouse_core as core  # noqa: E402

# Spend-guard tests isolate reservation ordering from cross-repo gate reads.
# The atomic evaluator/write integration is covered in test_automerge_card_ui.py.
rc._evaluate_automerge_card_projection = lambda *args, **kwargs: (
    rc.criteria_schema.unavailable_criteria("offline spend-guard fixture")
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


@contextmanager
def budget_boundary(fake):
    with patched(
        rc,
        {
            "_gh": fake,
            "_TRIAGE_BUDGET_LEDGER_NUMBER": None,
            "_TRIAGE_BUDGET_PASS_HALTED": False,
        },
    ):
        yield


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def item(kind="pr-review", revision="abc1234", **overrides):
    base = {
        "repo": "wheelhouse",
        "number": 17,
        "kind": kind,
        "head_sha": revision if kind == "pr-review" else "",
        "updated_at": revision if kind == "issue-triage" else "",
        "title": "A bounded triage candidate",
        "author": "contributor",
        "bucket": "merge-ready" if kind == "pr-review" else "issue-triage",
        "comp": "pass" if kind == "pr-review" else "n/a",
        "tests": "green" if kind == "pr-review" else "n/a",
        "url": "https://github.com/example/wheelhouse/pull/17",
        "summary": "safe offline fixture",
        "recommendation": "Review it.",
        "priority": "med",
        "auto_triage": True,
        "auto_triage_issues": True,
        "triage_attempt_cap_per_revision": 2,
    }
    base.update(overrides)
    return base


def card_state(candidate=None):
    candidate = candidate or item()
    return core.parse_state_block(rc.render(candidate)["body"])


def budget_issue(number=91, day="2026-07-16", reserved=0, **overrides):
    issue = {
        "number": number,
        "state": "closed",
        "title": rc.TRIAGE_BUDGET_TITLE,
        "body": rc.render_triage_budget_body(day, reserved),
        "labels": [{"name": rc.TRIAGE_BUDGET_LABEL}],
        "user": {"login": rc.CARD_AUTOMATION_AUTHOR},
    }
    issue.update(overrides)
    return issue


class FakeGh:
    """In-memory REST boundary for the closed budget-ledger issue."""

    def __init__(self, issue=None):
        self.issue = copy.deepcopy(issue)
        self.list_payload = None
        self.list_calls = 0
        self.get_calls = 0
        self.patch_calls = 0
        self.create_calls = 0
        self.fail_list = False
        self.fail_create = False
        self.fail_patch_call = 0
        self.fail_get_call = 0
        self.stale_get_call = 0
        self.before_patch = None

    @staticmethod
    def result(payload=None):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload) if payload is not None else "",
            stderr="",
        )

    @staticmethod
    def fields(args):
        values = {}
        for index, arg in enumerate(args[:-1]):
            if arg == "-f":
                key, value = args[index + 1].split("=", 1)
                values[key] = value
        return values

    def __call__(self, args, check=True):
        if args[:2] == ["label", "create"]:
            return self.result()
        if args[:3] == ["api", "--paginate", "--slurp"]:
            self.list_calls += 1
            if self.fail_list:
                raise RuntimeError("ledger list unavailable")
            if self.list_payload is not None:
                payload = self.list_payload
            else:
                payload = [[copy.deepcopy(self.issue)]] if self.issue else [[]]
            return self.result(payload)
        if args[:3] == ["api", "--method", "POST"]:
            self.create_calls += 1
            if self.fail_create:
                raise RuntimeError("ledger create unavailable")
            fields = self.fields(args)
            self.issue = budget_issue(
                number=117,
                body=fields["body"],
                title=fields["title"],
                state="open",
            )
            return self.result(self.issue)
        if args[:3] == ["api", "--method", "PATCH"]:
            self.patch_calls += 1
            if self.patch_calls == self.fail_patch_call:
                raise RuntimeError("ledger write unavailable")
            self.before_patch = copy.deepcopy(self.issue)
            fields = self.fields(args)
            self.issue["body"] = fields.get("body", self.issue["body"])
            self.issue["state"] = fields.get("state", self.issue["state"])
            return self.result(self.issue)
        if args[0] == "api" and len(args) == 2:
            self.get_calls += 1
            if self.get_calls == self.fail_get_call:
                raise RuntimeError("ledger read unavailable")
            result = copy.deepcopy(self.issue)
            if self.get_calls == self.stale_get_call and self.before_patch:
                result = copy.deepcopy(self.before_patch)
            return self.result(result)
        raise AssertionError("unexpected gh call: %r" % (args,))


def load_temp_config(payload):
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "wheelhouse.config.yml")
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle)
        stderr = io.StringIO()
        with patched(core, {"CONFIG_CANDIDATES": [path]}), redirect_stderr(stderr):
            config = core.load_config()
    return config, stderr.getvalue()


def test_config_defaults_boundaries_and_override():
    config, errors = load_temp_config({"repos": [{"name": "alpha"}]})
    assert config["triage_attempt_cap_per_revision"] == 2
    assert config["triage_daily_ceiling"] == 1200
    assert config["triage_attempt_caps"]["alpha"] == 2
    assert errors == ""

    config, errors = load_temp_config(
        {
            "triage_attempt_cap_per_revision": 5,
            "triage_daily_ceiling": 2000,
            "repos": [
                {"name": "alpha", "triage_attempt_cap_per_revision": 1},
                {"name": "beta", "triage_attempt_cap_per_revision": 4},
            ],
        }
    )
    assert config["triage_attempt_cap_per_revision"] == 5
    assert config["triage_daily_ceiling"] == 2000
    assert config["triage_attempt_caps"]["alpha"] == 1
    assert config["triage_attempt_caps"]["beta"] == 4
    assert errors == ""

    config, errors = load_temp_config(
        {
            "triage_attempt_cap_per_revision": 1,
            "triage_daily_ceiling": 1,
            "repos": [{"name": "alpha"}],
        }
    )
    assert config["triage_attempt_cap_per_revision"] == 1
    assert config["triage_daily_ceiling"] == 1
    assert config["triage_attempt_caps"]["alpha"] == 1
    assert errors == ""


def test_every_invalid_cap_and_ceiling_class_fails_closed_loudly():
    invalid_caps = [True, False, None, "2", 1.5, 0, -1, 6, [], {}]
    for value in invalid_caps:
        config, errors = load_temp_config(
            {
                "triage_attempt_cap_per_revision": value,
                "repos": [{"name": "alpha", "triage_attempt_cap_per_revision": value}],
            }
        )
        assert config["triage_attempt_cap_per_revision"] == 1
        assert config["triage_attempt_caps"]["alpha"] == 1
        assert "::error::" in errors
        assert "triage_attempt_cap_per_revision" in errors

    invalid_ceilings = [True, False, None, "100", 1.5, 0, -1, 2001, [], {}]
    for value in invalid_ceilings:
        config, errors = load_temp_config(
            {"triage_daily_ceiling": value, "repos": [{"name": "alpha"}]}
        )
        assert config["triage_daily_ceiling"] == 0
        assert "::error::" in errors
        assert "triage_daily_ceiling" in errors

    config, errors = load_temp_config(
        {"repos": [{"name": "alpha", "triage_daily_ceiling": 10}]}
    )
    assert config["triage_daily_ceiling"] == 0
    assert "per-repo overrides are not supported" in errors


def test_ingest_normalization_carries_typed_repo_cap():
    config = {
        "repos": {
            "wheelhouse": {
                "name": "wheelhouse",
                "triage_attempt_cap_per_revision": 4,
            }
        },
        "auto_triage": True,
        "auto_triage_issues": True,
        "triage_attempt_cap_per_revision": 2,
    }
    with patched(build_item, {"load_config": lambda: config}):
        normalized = build_item.normalize({"repo": "wheelhouse", "number": 17})
    assert normalized["triage_attempt_cap_per_revision"] == 4


def test_attempt_state_legacy_derivation_and_strict_matrix():
    revision = "abc1234"
    base = {"kind": "pr-review", "head_sha": revision}
    assert rc.triage_attempt_count(base, "pr-review", revision, 2) == 0
    assert (
        rc.triage_attempt_count(
            dict(base, triaged_sha=revision), "pr-review", revision, 2
        )
        == 1
    )

    valid = {
        "version": 1,
        "kind": "pr-review",
        "revision": revision,
        "count": 1,
    }
    assert (
        rc.triage_attempt_count(
            dict(base, triage_attempts=valid), "pr-review", revision, 2
        )
        == 1
    )
    zero_with_current_checkpoint = dict(valid, count=0)
    assert (
        rc.triage_attempt_count(
            dict(
                base,
                triaged_sha=revision,
                triage_attempts=zero_with_current_checkpoint,
            ),
            "pr-review",
            revision,
            2,
        )
        == 1
    )
    malformed = [
        None,
        True,
        "record",
        [],
        {},
        dict(valid, version=True),
        dict(valid, version=None),
        dict(valid, version=2),
        dict(valid, kind="issue-triage"),
        dict(valid, kind="unknown"),
        dict(valid, revision=True),
        dict(valid, revision=""),
        dict(valid, count=True),
        dict(valid, count=-1),
        dict(valid, count=6),
        dict(valid, count=10**100),
        dict(valid, count="1"),
        dict(valid, extra="field"),
    ]
    for record in malformed:
        state = dict(base, triage_attempts=record)
        assert rc.triage_attempt_count(state, "pr-review", revision, 2) == 2

    mismatched = dict(valid, revision="oldsha")
    assert (
        rc.triage_attempt_count(
            dict(base, triage_attempts=mismatched), "pr-review", revision, 2
        )
        == 2
    )
    assert (
        rc.triage_attempt_count(
            {
                "kind": "issue-triage",
                "updated_at": "2026-07-15T00:00:00Z",
                "triage_attempts": {
                    "version": 1,
                    "kind": "issue-triage",
                    "revision": "2026-07-16T00:00:00Z",
                    "count": 0,
                },
            },
            "issue-triage",
            "2026-07-16T00:00:00Z",
            2,
        )
        == 2
    )
    assert rc.triage_attempt_count({}, "ci-approval", revision, 2) == 2

    old_issue_revision = "2026-07-15T00:00:00Z"
    new_issue_revision = "2026-07-16T00:00:00Z"
    issue_state = {
        "kind": "issue-triage",
        "updated_at": old_issue_revision,
        "triage_attempts": {
            "version": 1,
            "kind": "issue-triage",
            "revision": old_issue_revision,
            "count": 2,
        },
    }
    assert (
        rc.triage_attempt_count(issue_state, "issue-triage", new_issue_revision, 2) == 0
    )

    capped_state = card_state(item())
    capped_state[rc.TRIAGE_ATTEMPTS_FIELD] = {
        "version": 1,
        "kind": "pr-review",
        "revision": revision,
        "count": 2,
    }
    labels = ["needs-decision", "kind:pr-review"]
    assert not rc.should_auto_triage(item(), capped_state, labels)
    assert rc.should_auto_triage(
        item(triage_attempt_cap_per_revision=4), capped_state, labels
    )


def test_queued_body_increments_attempt_with_cache_in_one_nonmaterial_write():
    candidate = item()
    rendered = rc.render(candidate)["body"]
    queued = rc.body_with_triage_queued(rendered, candidate, attempt_cap=2)
    state = core.parse_state_block(queued)
    assert state["triaged_sha"] == candidate["head_sha"]
    assert state["triage_status"] == "queued"
    assert state[rc.TRIAGE_ATTEMPTS_FIELD] == {
        "version": 1,
        "kind": "pr-review",
        "revision": candidate["head_sha"],
        "count": 1,
    }
    assert rc.TRIAGE_ATTEMPTS_FIELD not in rc.MATERIAL_FIELDS

    second_attempt_state = dict(state)
    second_attempt_state.pop("triaged_sha")
    second_attempt_state.pop("triage_status")
    second_attempt_body = rc._replace_state_block(queued, second_attempt_state)
    queued_again = rc.body_with_triage_queued(
        second_attempt_body, candidate, attempt_cap=2
    )
    assert core.parse_state_block(queued_again)[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 2
    exhausted = core.parse_state_block(queued_again)
    exhausted.pop("triaged_sha")
    exhausted.pop("triage_status")
    exhausted_body = rc._replace_state_block(queued_again, exhausted)
    assert (
        rc.body_with_triage_queued(exhausted_body, candidate, attempt_cap=2)
        == exhausted_body
    )
    duplicate_count_body = queued.replace('"count":1', '"count":1,"count":0')
    assert (
        rc.body_with_triage_queued(duplicate_count_body, candidate, attempt_cap=2)
        == duplicate_count_body
    )

    refreshed = rc.render(candidate)["body"]
    preserved = rc._preserve_same_revision_triage(
        refreshed,
        queued_again,
        candidate,
        core.parse_state_block(queued_again),
        owner="example",
    )
    assert core.parse_state_block(preserved)[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 2


def test_reconcile_refuses_a_revision_at_its_attempt_cap():
    candidate = item()
    state = card_state(candidate)
    state[rc.TRIAGE_ATTEMPTS_FIELD] = {
        "version": 1,
        "kind": "pr-review",
        "revision": candidate["head_sha"],
        "count": 2,
    }
    row = {
        "number": 42,
        "body": rc._replace_state_block(rc.render(candidate)["body"], state),
        "labels": ["needs-decision", "kind:pr-review"],
        "state": state,
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
        queued = reconcile.maybe_queue_auto_triage(candidate, row, True)
    assert queued is False
    assert dispatched == []
    assert "triage-attempt-cap exhausted" in output.getvalue()
    assert "attempts.exhausted" in output.getvalue()


def test_ledger_parser_rejects_every_malformed_class():
    valid = {"version": 1, "day": "2026-07-16", "reserved": 1}

    def body(record):
        return "<!-- %s: %s -->" % (
            rc.TRIAGE_BUDGET_MARKER,
            json.dumps(record, separators=(",", ":")),
        )

    assert rc.parse_triage_budget(body(valid)) == valid
    malformed = [
        dict(valid, version=True),
        dict(valid, version=2),
        dict(valid, day="2026-02-30"),
        dict(valid, day=True),
        dict(valid, reserved=True),
        dict(valid, reserved=-1),
        dict(valid, reserved=2001),
        dict(valid, reserved="1"),
        dict(valid, extra=1),
        {"version": 1, "day": "2026-07-16"},
    ]
    for record in malformed:
        assert rc.parse_triage_budget(body(record)) is None
    assert rc.parse_triage_budget("") is None
    assert rc.parse_triage_budget("<!-- wheelhouse-triage-budget: nope -->") is None
    assert rc.parse_triage_budget(body(valid) + "\n" + body(valid)) is None
    assert (
        rc.parse_triage_budget(
            "<!-- wheelhouse-triage-budget: "
            '{"version":1,"day":"2026-07-16","reserved":1,"reserved":0} -->'
        )
        is None
    )
    assert (
        rc.parse_triage_budget(
            body(valid) + "\n<!-- wheelhouse-triage-budget: malformed -->"
        )
        is None
    )


def test_ledger_creation_uses_returned_number_and_ignores_list_lag():
    fake = FakeGh(issue=None)
    output = io.StringIO()
    with budget_boundary(fake), redirect_stdout(output):
        reserved = rc.reserve_triage_budget(42, item(), ceiling=100, today="2026-07-16")
        reserved_again = rc.reserve_triage_budget(
            43, item(number=18), ceiling=100, today="2026-07-16"
        )
    assert reserved is True
    assert reserved_again is True
    assert fake.list_calls == 1
    assert fake.create_calls == 1
    assert fake.patch_calls == 3
    assert fake.get_calls == 4
    assert fake.issue["number"] == 117
    assert rc.parse_triage_budget(fake.issue["body"])["reserved"] == 2
    assert "budget.reserved" in output.getvalue()


def test_ledger_exhaustion_and_utc_day_rollover():
    exhausted = FakeGh(budget_issue(reserved=100))
    output = io.StringIO()
    with budget_boundary(exhausted), redirect_stdout(output):
        assert rc.reserve_triage_budget(42, item(), 100, today="2026-07-16") is False
    assert exhausted.patch_calls == 0
    assert "budget.exhausted" in output.getvalue()
    assert "deferred until the next UTC day" in output.getvalue()

    rollover = FakeGh(budget_issue(day="2026-07-15", reserved=2000))
    with budget_boundary(rollover), redirect_stdout(io.StringIO()):
        assert rc.reserve_triage_budget(42, item(), 100, today="2026-07-16") is True
    assert rc.parse_triage_budget(rollover.issue["body"]) == {
        "version": 1,
        "day": "2026-07-16",
        "reserved": 1,
    }


def test_replay_remaining_capacity_preflight_is_read_only_and_fail_closed():
    missing = FakeGh()
    with budget_boundary(missing), redirect_stdout(io.StringIO()):
        assert rc.triage_budget_remaining(1200, today="2026-07-16") == 1200
    assert missing.create_calls == missing.patch_calls == 0

    current = FakeGh(budget_issue(reserved=748))
    with budget_boundary(current), redirect_stdout(io.StringIO()):
        assert rc.triage_budget_remaining(1200, today="2026-07-16") == 452
    assert current.list_calls == current.get_calls == 1
    assert current.create_calls == current.patch_calls == 0

    rollover = FakeGh(budget_issue(day="2026-07-15", reserved=1200))
    with budget_boundary(rollover), redirect_stdout(io.StringIO()):
        assert rc.triage_budget_remaining(1200, today="2026-07-16") == 1200
    assert rollover.patch_calls == 0

    malformed = FakeGh(budget_issue(body="malformed"))
    output = io.StringIO()
    with budget_boundary(malformed), redirect_stdout(output):
        assert rc.triage_budget_remaining(1200, today="2026-07-16") == 0
    assert malformed.patch_calls == 0
    assert "failed closed" in output.getvalue()


def test_untrusted_duplicate_and_ambiguous_ledgers_halt_reservations():
    variants = [
        budget_issue(state="open"),
        budget_issue(title="lookalike"),
        budget_issue(labels=[{"name": rc.TRIAGE_BUDGET_LABEL}, {"name": "extra"}]),
        budget_issue(user={"login": "human"}),
        budget_issue(body="malformed"),
        budget_issue(body=rc.render_triage_budget_body("2026-07-16", 0) + "\nextra"),
        budget_issue(pull_request={"url": "x"}),
    ]
    for issue in variants:
        fake = FakeGh(issue)
        output = io.StringIO()
        with budget_boundary(fake), redirect_stdout(output):
            assert (
                rc.reserve_triage_budget(42, item(), 100, today="2026-07-16") is False
            )
        assert fake.patch_calls == 0
        assert "malformed-ledger" in output.getvalue()

    duplicate = FakeGh()
    duplicate.list_payload = [[budget_issue(1), budget_issue(2)]]
    with budget_boundary(duplicate), redirect_stdout(io.StringIO()):
        assert rc.reserve_triage_budget(42, item(), 100, today="2026-07-16") is False
        assert (
            rc.reserve_triage_budget(43, item(number=18), 100, today="2026-07-16")
            is False
        )
    assert duplicate.patch_calls == 0
    assert duplicate.list_calls == 1


def test_all_ledger_io_failures_halt_and_verified_write_leaks_safely():
    cases = []
    unreadable = FakeGh(budget_issue())
    unreadable.fail_list = True
    cases.append(unreadable)
    failed_create = FakeGh()
    failed_create.fail_create = True
    cases.append(failed_create)
    failed_read = FakeGh(budget_issue())
    failed_read.fail_get_call = 1
    cases.append(failed_read)
    failed_write = FakeGh(budget_issue())
    failed_write.fail_patch_call = 1
    cases.append(failed_write)
    failed_creation_verify = FakeGh()
    failed_creation_verify.stale_get_call = 1
    cases.append(failed_creation_verify)

    for fake in cases:
        with budget_boundary(fake), redirect_stdout(io.StringIO()):
            assert (
                rc.reserve_triage_budget(42, item(), 100, today="2026-07-16") is False
            )

    # The reservation PATCH lands, but the following by-number read returns the
    # old body. Queueing must stop while the ledger retains the increment.
    leaked = FakeGh(budget_issue(reserved=0))
    leaked.stale_get_call = 2
    with budget_boundary(leaked), redirect_stdout(io.StringIO()):
        assert rc.reserve_triage_budget(42, item(), 100, today="2026-07-16") is False
    assert rc.parse_triage_budget(leaked.issue["body"])["reserved"] == 1


def test_invalid_zero_ceiling_never_reads_or_writes_a_ledger():
    fake = FakeGh(budget_issue())
    output = io.StringIO()
    with budget_boundary(fake), redirect_stdout(output):
        assert rc.reserve_triage_budget(42, item(), 0) is False
    assert fake.list_calls == 0
    assert fake.patch_calls == 0
    assert "invalid-config" in output.getvalue()
    assert "budget.deferred" in output.getvalue()


def test_mark_queue_reserves_then_writes_and_verifies_one_attempt():
    candidate = item()
    body = rc.render(candidate)["body"]
    current = {
        "number": 42,
        "body": body,
        "state": "OPEN",
        "labels": [{"name": "needs-decision"}, {"name": "kind:pr-review"}],
        "author": {"login": rc.GET_CARD_AUTOMATION_AUTHOR},
    }
    order = []

    def get_card(number):
        return copy.deepcopy(current)

    def reserve(number, queued_item, ceiling):
        order.append("reserve")
        return True

    def edit(number, new_body, remove_labels=None):
        order.append("card-write")
        current["body"] = new_body

    config = {
        "repos": {
            "wheelhouse": {"name": "wheelhouse", "triage_attempt_cap_per_revision": 2}
        },
        "triage_attempt_cap_per_revision": 2,
        "triage_daily_ceiling": 100,
    }
    with (
        patched(
            rc,
            {
                "get_card": get_card,
                "reserve_triage_budget": reserve,
                "_edit_issue_body": edit,
            },
        ),
        patched(core, {"load_config": lambda: config}),
    ):
        permit = rc.mark_triage_queued(42, candidate, body)
    assert isinstance(permit, rc._TriageDispatchPermit)
    assert order == ["reserve", "card-write"]
    state = core.parse_state_block(current["body"])
    assert state["triage_status"] == "queued"
    assert state[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 1

    permit_item = permit.item
    permit_item["number"] = 999
    assert permit.item["number"] == candidate["number"]
    try:
        permit.number = 999
    except AttributeError:
        pass
    else:
        raise AssertionError("dispatch permit number unexpectedly remained mutable")

    calls = []
    with patched(rc, {"_gh": lambda args, check=True: calls.append(args)}):
        rc.dispatch_triage_workflow(permit)
    assert calls and calls[0][:3] == ["workflow", "run", "triage.yml"]


def test_reservation_or_card_verification_failure_never_dispatches():
    candidate = item()
    body = rc.render(candidate)["body"]
    current = {
        "number": 42,
        "body": body,
        "state": "OPEN",
        "labels": [{"name": "needs-decision"}],
        "author": {"login": rc.GET_CARD_AUTOMATION_AUTHOR},
    }
    config = {
        "repos": {
            "wheelhouse": {"name": "wheelhouse", "triage_attempt_cap_per_revision": 2}
        },
        "triage_attempt_cap_per_revision": 2,
        "triage_daily_ceiling": 100,
    }
    edits = []
    with (
        patched(
            rc,
            {
                "get_card": lambda number: copy.deepcopy(current),
                "reserve_triage_budget": lambda number, queued_item, ceiling: False,
                "_edit_issue_body": lambda *args, **kwargs: edits.append(args),
            },
        ),
        patched(core, {"load_config": lambda: config}),
    ):
        assert rc.mark_triage_queued(42, candidate, body) is None
    assert len(edits) == 1
    deferred_state = core.parse_state_block(edits[0][1])
    assert rc.TRIAGE_BUDGET_DEFERRED in edits[0][1]
    assert "<!-- opt:merge -->" in edits[0][1]
    assert "triaged_sha" not in deferred_state
    assert "triage_status" not in deferred_state
    assert rc.TRIAGE_ATTEMPTS_FIELD not in deferred_state

    reads = 0
    edits = []

    def racing_get(number):
        nonlocal reads
        reads += 1
        value = copy.deepcopy(current)
        if reads >= 2:
            value["body"] += "\nowner edit"
        return value

    with (
        patched(
            rc,
            {
                "get_card": racing_get,
                "reserve_triage_budget": lambda number, queued_item, ceiling: True,
                "_edit_issue_body": lambda *args, **kwargs: edits.append(args),
            },
        ),
        patched(core, {"load_config": lambda: config}),
        redirect_stdout(io.StringIO()),
    ):
        assert rc.mark_triage_queued(42, candidate, body) is None
    assert edits == []

    exhausted_state = core.parse_state_block(body)
    exhausted_state[rc.TRIAGE_ATTEMPTS_FIELD] = {
        "version": 1,
        "kind": "pr-review",
        "revision": candidate["head_sha"],
        "count": 2,
    }
    exhausted_body = rc._replace_state_block(body, exhausted_state)
    reserve_calls = []
    with (
        patched(
            rc,
            {
                "get_card": lambda number: (_ for _ in ()).throw(
                    AssertionError("exhausted attempts must not read the card")
                ),
                "reserve_triage_budget": lambda *args: reserve_calls.append(args),
            },
        ),
        patched(core, {"load_config": lambda: config}),
        redirect_stdout(io.StringIO()),
    ):
        assert rc.mark_triage_queued(42, candidate, exhausted_body) is None
    assert reserve_calls == []

    reads = 0
    written_body = body

    def stale_after_queue_get(number):
        nonlocal reads
        reads += 1
        value = copy.deepcopy(current)
        if reads == 3:
            value["body"] = body
        else:
            value["body"] = written_body
        return value

    def queue_edit(number, new_body, remove_labels=None):
        nonlocal written_body
        written_body = new_body

    with (
        patched(
            rc,
            {
                "get_card": stale_after_queue_get,
                "reserve_triage_budget": lambda number, queued_item, ceiling: True,
                "_edit_issue_body": queue_edit,
            },
        ),
        patched(core, {"load_config": lambda: config}),
        redirect_stdout(io.StringIO()),
    ):
        assert rc.mark_triage_queued(42, candidate, body) is None
    assert reads == 3
    assert core.parse_state_block(written_body)[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 1


def test_budget_denial_publishes_held_cards_without_spend_cache_and_later_retries():
    config = {
        "repos": {
            "wheelhouse": {"name": "wheelhouse", "triage_attempt_cap_per_revision": 2}
        },
        "triage_attempt_cap_per_revision": 2,
        "triage_daily_ceiling": 100,
    }
    utc_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def unavailable_ledger():
        fake = FakeGh(budget_issue(day=utc_day))
        fake.fail_list = True
        return fake

    scenarios = [
        ("invalid config", lambda: FakeGh(), dict(config, triage_daily_ceiling=0)),
        (
            "exhausted capacity",
            lambda: FakeGh(budget_issue(day=utc_day, reserved=1)),
            dict(config, triage_daily_ceiling=1),
        ),
        (
            "malformed ledger",
            lambda: FakeGh(budget_issue(day=utc_day, body="malformed")),
            config,
        ),
        ("unavailable ledger", unavailable_ledger, config),
    ]

    for label, fake_factory, scenario_config in scenarios:
        for kind, expected_checkbox in (
            ("pr-review", "<!-- opt:merge -->"),
            ("issue-triage", "<!-- opt:close -->"),
        ):
            candidate = item(
                kind=kind, revision="rev-%s-%s" % (kind, label.replace(" ", "-"))
            )
            held = rc.render(candidate, held=True)
            current = {
                "number": 42,
                "body": held["body"],
                "state": "OPEN",
                "labels": [{"name": name} for name in held["labels"]],
                "author": {"login": rc.GET_CARD_AUTOMATION_AUTHOR},
            }
            edits = []

            def edit(number, new_body, remove_labels=None):
                edits.append((number, new_body, remove_labels or []))
                current["body"] = new_body
                names = {entry["name"] for entry in current["labels"]}
                for removed in remove_labels or []:
                    names.discard(removed)
                current["labels"] = [{"name": name} for name in sorted(names)]

            replacements = {
                "get_card": lambda number: copy.deepcopy(current),
                "_edit_issue_body": edit,
            }
            with (
                budget_boundary(fake_factory()),
                patched(rc, replacements),
                patched(core, {"load_config": lambda: scenario_config}),
                redirect_stdout(io.StringIO()),
            ):
                permit = rc.mark_triage_queued(42, candidate, held["body"])
                assert permit is None, "%s %s unexpectedly reserved" % (label, kind)

            assert len(edits) == 1, "%s %s" % (label, kind)
            number, published_body, removed = edits[0]
            state = core.parse_state_block(published_body)
            assert number == 42
            assert rc.HOLD_LABEL in removed
            assert "held" not in state
            assert "triaged_sha" not in state
            assert "triage_status" not in state
            assert rc.TRIAGE_ATTEMPTS_FIELD not in state
            assert rc.TRIAGE_BUDGET_DEFERRED in published_body
            assert expected_checkbox in published_body
            assert rc.HOLD_LABEL not in {entry["name"] for entry in current["labels"]}
            assert rc.should_auto_triage(
                candidate,
                state,
                current["labels"],
                has_token=True,
            )

            retry_order = []

            def retry_reserve(number, queued_item, ceiling):
                retry_order.append("reserve")
                return True

            def retry_edit(number, new_body, remove_labels=None):
                retry_order.append("card-write")
                current["body"] = new_body

            with (
                patched(
                    rc,
                    {
                        "get_card": lambda number: copy.deepcopy(current),
                        "reserve_triage_budget": retry_reserve,
                        "_edit_issue_body": retry_edit,
                    },
                ),
                patched(core, {"load_config": lambda: config}),
                redirect_stdout(io.StringIO()),
            ):
                permit = rc.mark_triage_queued(42, candidate, current["body"])

            retry_state = core.parse_state_block(current["body"])
            assert isinstance(permit, rc._TriageDispatchPermit), "%s %s" % (label, kind)
            assert retry_order == ["reserve", "card-write"]
            assert retry_state["triage_status"] == "queued"
            assert retry_state["triaged_sha"] == rc.triage_revision(candidate)
            assert retry_state[rc.TRIAGE_ATTEMPTS_FIELD]["count"] == 1


def test_dispatch_capability_and_workflow_admission_make_bypass_impossible():
    gh_calls = []
    with patched(rc, {"_gh": lambda args, check=True: gh_calls.append(args)}):
        try:
            rc.dispatch_triage_workflow(None)
        except RuntimeError as error:
            assert "verified queue reservation" in str(error)
        else:
            raise AssertionError("rogue dispatch unexpectedly accepted")
    assert gh_calls == []

    assert rc.triage_queued_for_head(
        {"triaged_sha": "abc1234", "triage_status": "queued"}, "abc1234"
    )
    assert not rc.triage_queued_for_head({}, "abc1234")
    assert not rc.triage_queued_for_head(
        {"triaged_sha": "abc1234", "triage_status": "error"}, "abc1234"
    )

    workflow = read(".github/workflows/triage.yml")
    admission = workflow.index("triage_queued_for_head")
    claim = workflow.index("id: event-claim")
    task = workflow.index("id: claude-task")
    model = workflow.index("uses: ./.github/actions/claude-model-call")
    assert admission < claim < task < model
    assert "steps.resolve.outputs.skip != 'true'" in workflow[claim - 200 : claim + 200]
    assert "steps.event-claim.outputs.admitted == 'true'" in workflow


def test_source_boundary_and_concurrency_cover_every_current_writer():
    call_sites = []
    permit_constructors = []
    for relative in ("scripts/render_card.py", "scripts/reconcile.py"):
        tree = ast.parse(read(relative), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name == "dispatch_triage_workflow":
                call_sites.append((relative, len(node.args), len(node.keywords)))
            if name == "_TriageDispatchPermit":
                permit_constructors.append(relative)
    assert sorted(call_sites) == [
        ("scripts/reconcile.py", 1, 0),
        ("scripts/render_card.py", 1, 0),
    ]
    assert permit_constructors == ["scripts/render_card.py"]

    for workflow_path in (
        ".github/workflows/scan-backstop.yml",
        ".github/workflows/ingest.yml",
        ".github/workflows/decision-handler.yml",
    ):
        workflow = yaml.safe_load(read(workflow_path))
        concurrency = workflow.get("concurrency") or {}
        assert concurrency.get("group") == "wheelhouse-backstop"
        assert concurrency.get("queue") == "max"
        assert concurrency.get("cancel-in-progress") is False
    assert "scripts/reconcile.py" in read(".github/workflows/scan-backstop.yml")
    assert "render_card.py queue-triage" in read(".github/workflows/ingest.yml")


def test_one_reservation_prices_the_bounded_two_call_schema_repair():
    workflow = yaml.safe_load(read(".github/workflows/triage.yml"))
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
    ]
    model_calls = [
        step
        for step in steps
        if step.get("uses") == "./.github/actions/claude-model-call"
    ]
    assert len(model_calls) == 2
    repair = next(
        step for step in model_calls if step.get("id") == "claude-repair-model"
    )
    assert "claude-repair-task.outcome == 'success'" in repair.get("if", "")
    repair_task = next(
        step for step in steps if step.get("id") == "claude-repair-task"
    )
    assert "repair-claim.outputs.admitted == 'true'" in repair_task.get("if", "")
    workflow_text = read(".github/workflows/triage.yml")
    assert workflow_text.count("id: claude-repair-model") == 1
    task_builder = read("agent_runtime/task_builder.py")
    assert '"triage.schema-repair": (60_000, 75_000, 1, 0, 65_536)' in task_builder

    readme = read("README.md")
    assert "1200 automatic-triage reservations and 2400 model calls" in readme
    assert "Owner-triggered deep review and natural-language decisions are outside" in readme


TESTS = [
    test_config_defaults_boundaries_and_override,
    test_every_invalid_cap_and_ceiling_class_fails_closed_loudly,
    test_ingest_normalization_carries_typed_repo_cap,
    test_attempt_state_legacy_derivation_and_strict_matrix,
    test_queued_body_increments_attempt_with_cache_in_one_nonmaterial_write,
    test_reconcile_refuses_a_revision_at_its_attempt_cap,
    test_ledger_parser_rejects_every_malformed_class,
    test_ledger_creation_uses_returned_number_and_ignores_list_lag,
    test_ledger_exhaustion_and_utc_day_rollover,
    test_replay_remaining_capacity_preflight_is_read_only_and_fail_closed,
    test_untrusted_duplicate_and_ambiguous_ledgers_halt_reservations,
    test_all_ledger_io_failures_halt_and_verified_write_leaks_safely,
    test_invalid_zero_ceiling_never_reads_or_writes_a_ledger,
    test_mark_queue_reserves_then_writes_and_verifies_one_attempt,
    test_reservation_or_card_verification_failure_never_dispatches,
    test_budget_denial_publishes_held_cards_without_spend_cache_and_later_retries,
    test_dispatch_capability_and_workflow_admission_make_bypass_impossible,
    test_source_boundary_and_concurrency_cover_every_current_writer,
    test_one_reservation_prices_the_bounded_two_call_schema_repair,
]


if __name__ == "__main__":
    for test in TESTS:
        test()
        print("ok - %s" % test.__name__)
