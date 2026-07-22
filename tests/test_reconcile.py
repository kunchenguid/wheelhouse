#!/usr/bin/env python3
"""
Unit-exercise reconcile routing and activity reflection with NO network.

Run: python tests/test_reconcile.py
"""

import copy
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from types import SimpleNamespace

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
        "bucket": (
            "needs-ci-approval"
            if kind == "ci-approval"
            else "issue-triage"
            if kind == "issue-triage"
            else "merge-ready"
        ),
        "projection_freshness": "",
        "projection_head_sha": "",
        "projection_complete": False,
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


def run_reconcile(
    scan,
    cards,
    current_cards=None,
    criteria_payload=None,
    guarded_upsert_result=7,
    card_after_upsert=None,
):
    calls = {
        "upsert": [],
        "close": [],
        "reflect": [],
        "state": [],
        "triage_rows": [],
    }
    current_by_number = {
        c["number"]: c for c in (cards if current_cards is None else current_cards)
    }

    def fake_upsert(
        item,
        existing=None,
        has_token=False,
        preserve_reconcile_absence=False,
        expected_existing=None,
    ):
        calls["upsert"].append(
            {
                "item": item,
                "existing": existing,
                "has_token": has_token,
                "preserve_reconcile_absence": preserve_reconcile_absence,
                "expected_existing": expected_existing,
            }
        )
        if expected_existing is not None and card_after_upsert is not None:
            current_by_number[int(existing["number"])] = card_after_upsert
            return guarded_upsert_result
        return (existing or {}).get("number", 7)

    def fake_maybe_queue(_item, row, _has_token, owner=""):
        calls["triage_rows"].append(row)
        return False

    def fake_close(number, message, label="resolved", expected=None):
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

    def fake_update_absence(number, body, count, run_number=0, closed_at=""):
        new_body = reconcile.render_card.body_with_reconcile_absence(
            body, count, run_number=run_number, closed_at=closed_at
        )
        calls["state"].append(
            {
                "operation": "set",
                "number": number,
                "count": count,
                "closed_at": closed_at,
                "run_number": run_number,
                "body": body,
                "body_after": new_body,
            }
        )
        if new_body == body:
            return False
        current_by_number[int(number)]["body"] = new_body
        return True

    def fake_clear_absence(number, body):
        new_body = reconcile.render_card.body_without_reconcile_absence(body)
        calls["state"].append(
            {
                "operation": "clear",
                "number": number,
                "body": body,
                "body_after": new_body,
            }
        )
        if new_body == body:
            return False
        current_by_number[int(number)]["body"] = new_body
        return True

    old_argv = sys.argv[:]
    old_github_actions = os.environ.get("GITHUB_ACTIONS")
    old_event_name = os.environ.get("GITHUB_EVENT_NAME")
    old_run_number = os.environ.get("GITHUB_RUN_NUMBER")
    old_owner = os.environ.get("GITHUB_REPOSITORY_OWNER")
    old_upsert = reconcile.render_card.upsert_card
    old_close = reconcile.render_card.close_card
    old_get_card = reconcile.render_card.get_card
    old_reflect = reconcile.render_card.reflect_activity
    old_update_absence = reconcile.render_card.update_reconcile_absence
    old_clear_absence = reconcile.render_card.clear_reconcile_absence
    old_maybe_queue = reconcile.maybe_queue_auto_triage
    reconcile.render_card.upsert_card = fake_upsert
    reconcile.render_card.close_card = fake_close
    reconcile.render_card.get_card = fake_get_card
    reconcile.render_card.reflect_activity = fake_reflect
    reconcile.render_card.update_reconcile_absence = fake_update_absence
    reconcile.render_card.clear_reconcile_absence = fake_clear_absence
    reconcile.maybe_queue_auto_triage = fake_maybe_queue
    try:
        os.environ["GITHUB_ACTIONS"] = "true"
        os.environ["GITHUB_EVENT_NAME"] = "schedule"
        os.environ["GITHUB_RUN_NUMBER"] = "100"
        os.environ["GITHUB_REPOSITORY_OWNER"] = "owner"
        with tempfile.TemporaryDirectory() as d:
            scan_path = os.path.join(d, "scan.json")
            cards_path = os.path.join(d, "cards.json")
            with open(scan_path, "w") as f:
                json.dump(scan, f)
            with open(cards_path, "w") as f:
                json.dump(cards, f)
            sys.argv = ["reconcile.py", scan_path, cards_path]
            if criteria_payload is not None:
                criteria_path = os.path.join(d, "automerge.json")
                if criteria_payload == "malformed":
                    with open(criteria_path, "w") as f:
                        f.write("{malformed")
                elif criteria_payload != "missing":
                    with open(criteria_path, "w") as f:
                        json.dump(criteria_payload, f)
                sys.argv.append(criteria_path)
            with redirect_stdout(io.StringIO()):
                reconcile.main()
    finally:
        sys.argv = old_argv
        if old_github_actions is None:
            os.environ.pop("GITHUB_ACTIONS", None)
        else:
            os.environ["GITHUB_ACTIONS"] = old_github_actions
        if old_event_name is None:
            os.environ.pop("GITHUB_EVENT_NAME", None)
        else:
            os.environ["GITHUB_EVENT_NAME"] = old_event_name
        if old_run_number is None:
            os.environ.pop("GITHUB_RUN_NUMBER", None)
        else:
            os.environ["GITHUB_RUN_NUMBER"] = old_run_number
        if old_owner is None:
            os.environ.pop("GITHUB_REPOSITORY_OWNER", None)
        else:
            os.environ["GITHUB_REPOSITORY_OWNER"] = old_owner
        reconcile.render_card.upsert_card = old_upsert
        reconcile.render_card.close_card = old_close
        reconcile.render_card.get_card = old_get_card
        reconcile.render_card.reflect_activity = old_reflect
        reconcile.render_card.update_reconcile_absence = old_update_absence
        reconcile.render_card.clear_reconcile_absence = old_clear_absence
        reconcile.maybe_queue_auto_triage = old_maybe_queue
    return calls


def scan_payload(
    items=None,
    open_pr_numbers=None,
    open_issue_numbers=None,
    ok=True,
    truncated=False,
    indeterminate_pr_numbers=None,
    ci_wait_pr_numbers=None,
    ci_wait_refresh_items=None,
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
                "ci_wait_pr_numbers": []
                if ci_wait_pr_numbers is None
                else ci_wait_pr_numbers,
                "ci_wait_refresh_items": []
                if ci_wait_refresh_items is None
                else ci_wait_refresh_items,
                "truncated": truncated,
            }
        },
        "items": [] if items is None else items,
    }


def ci_wait_refresh_item(number=42, head_sha="newsha", comp="none", tests="none"):
    """A refresh-ONLY pr-review item for a PR mid fork-CI approval/run wait, as
    `build_repo` emits it: the new head with an honestly non-green pending state."""
    return {
        "repo": "wheelhouse",
        "number": number,
        "kind": "pr-review",
        "head_sha": head_sha,
        "updated_at": "2024-01-01T00:00:00Z",
        "title": "Ready PR",
        "author": "contributor",
        "bucket": "ci-running",
        "comp": comp,
        "tests": tests,
        "url": "https://github.com/kunchenguid/wheelhouse/pull/%d" % number,
        "summary": "head moved to %s - fork CI re-approved, checks re-running"
        % head_sha[:8],
        "recommendation": "Checks are re-running at the new head; wait, then re-review.",
        "priority": "low",
    }


def card(labels_, kind="pr-review", render_version=None):
    return {
        "number": 7,
        "body": body_state(kind=kind, render_version=render_version),
        "labels": labels_,
        "title": "[wheelhouse#42] Ready PR",
        "updated_at": "2024-01-02T00:00:00Z",
    }


class ReconcileLifecycle:
    """In-memory GitHub boundary around the real reconcile entrypoint/renderer."""

    def __init__(self, item):
        rendered = reconcile.render_card.render(item)
        self.issue = {
            "number": 7,
            "body": rendered["body"],
            "labels": labels(*rendered["labels"]),
            "title": rendered["title"],
            "updatedAt": "2024-01-02T00:00:00Z",
            "state": "OPEN",
            "author": {"login": "app/github-actions"},
            "comments": [],
        }
        self.close_calls = []
        self.body_writes = 0
        self.body_write_attempts = 0
        self._clock = 0
        self.fail_body_write_attempts = set()
        self.fail_close_attempts = set()
        self.close_attempts = 0
        self.run_number = 0

    def _tick(self):
        self._clock += 1
        self.issue["updatedAt"] = "2024-01-02T00:%02d:00Z" % self._clock

    def cards_snapshot(self):
        if self.issue["state"] != "OPEN":
            return []
        return [
            {
                "number": self.issue["number"],
                "body": self.issue["body"],
                "labels": copy.deepcopy(self.issue["labels"]),
                "title": self.issue["title"],
                "updated_at": self.issue["updatedAt"],
                "comments": len(self.issue["comments"]),
            }
        ]

    def _gh(self, args, check=True):
        if args[:3] == ["api", "--method", "PATCH"]:
            if self.close_attempts in self.fail_close_attempts:
                raise RuntimeError("simulated close failure")
            names = {
                value.removeprefix("labels[]=")
                for value in args
                if value.startswith("labels[]=")
            }
            self.issue["labels"] = labels(*sorted(names))
            provenance = reconcile.render_card.reconcile_soft_close_provenance(
                self.issue["body"]
            )
            closed_at = (provenance or {}).get("at", "2026-07-13T12:00:00Z")
            self.issue["state"] = "CLOSED"
            self.issue["updatedAt"] = closed_at
            self.issue["closedAt"] = closed_at
            self.issue["closedBy"] = {"login": "app/github-actions"}
            self.close_calls.append(
                {
                    "number": self.issue["number"],
                    "body": self.issue["body"],
                    "labels": copy.deepcopy(self.issue["labels"]),
                }
            )
            response = {
                "number": self.issue["number"],
                "body": self.issue["body"],
                "labels": copy.deepcopy(self.issue["labels"]),
                "title": self.issue["title"],
                "state": "closed",
                "updated_at": closed_at,
                "user": {"login": "github-actions[bot]"},
                "closed_at": closed_at,
                "closed_by": {"login": "github-actions[bot]"},
                "comments": len(self.issue["comments"]),
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")
        if args and args[0] == "api":
            response = {
                "number": self.issue["number"],
                "body": self.issue["body"],
                "labels": copy.deepcopy(self.issue["labels"]),
                "title": self.issue["title"],
                "state": self.issue["state"].lower(),
                "updated_at": self.issue["updatedAt"],
                "user": {"login": "github-actions[bot]"},
                "closed_at": self.issue.get("closedAt"),
                "closed_by": self.issue.get("closedBy"),
                "comments": len(self.issue["comments"]),
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")
        if args[:2] == ["issue", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(self.issue),
                stderr="",
            )
        if args[:2] == ["label", "create"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["issue", "comment"]:
            body = args[args.index("--body") + 1]
            self.issue["comments"].append({"body": body})
            self._tick()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["issue", "edit"]:
            if "--body-file" in args:
                self.body_write_attempts += 1
                if self.body_write_attempts in self.fail_body_write_attempts:
                    raise RuntimeError("simulated body write failure")
                path = args[args.index("--body-file") + 1]
                with open(path) as f:
                    self.issue["body"] = f.read()
                self.body_writes += 1
            names = {
                label["name"] if isinstance(label, dict) else label
                for label in self.issue["labels"]
            }
            for index, arg in enumerate(args):
                if arg == "--add-label":
                    names.add(args[index + 1])
                elif arg == "--remove-label":
                    names.discard(args[index + 1])
            self.issue["labels"] = labels(*sorted(names))
            self._tick()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError("unexpected gh call: %r" % (args,))

    def run(self, scan, event_name="schedule"):
        old_argv = sys.argv[:]
        old_gh = reconcile.render_card._gh
        old_close = reconcile.render_card.close_card
        old_token = os.environ.get("WHEELHOUSE_AUTO_TRIAGE_HAS_TOKEN")
        old_github_actions = os.environ.get("GITHUB_ACTIONS")
        old_event_name = os.environ.get("GITHUB_EVENT_NAME")
        old_run_number = os.environ.get("GITHUB_RUN_NUMBER")
        old_owner = os.environ.get("GITHUB_REPOSITORY_OWNER")
        reconcile.render_card._gh = self._gh

        def guarded_close(*args, **kwargs):
            self.close_attempts += 1
            return old_close(*args, **kwargs)

        reconcile.render_card.close_card = guarded_close
        os.environ["WHEELHOUSE_AUTO_TRIAGE_HAS_TOKEN"] = "false"
        self.run_number += 1
        os.environ["GITHUB_ACTIONS"] = "true"
        os.environ["GITHUB_EVENT_NAME"] = event_name
        os.environ["GITHUB_RUN_NUMBER"] = str(self.run_number)
        os.environ["GITHUB_REPOSITORY_OWNER"] = "owner"
        try:
            with tempfile.TemporaryDirectory() as d:
                scan_path = os.path.join(d, "scan.json")
                cards_path = os.path.join(d, "cards.json")
                with open(scan_path, "w") as f:
                    json.dump(scan, f)
                with open(cards_path, "w") as f:
                    json.dump(self.cards_snapshot(), f)
                sys.argv = ["reconcile.py", scan_path, cards_path]
                with redirect_stdout(io.StringIO()):
                    reconcile.main()
        finally:
            sys.argv = old_argv
            reconcile.render_card._gh = old_gh
            reconcile.render_card.close_card = old_close
            if old_token is None:
                os.environ.pop("WHEELHOUSE_AUTO_TRIAGE_HAS_TOKEN", None)
            else:
                os.environ["WHEELHOUSE_AUTO_TRIAGE_HAS_TOKEN"] = old_token
            if old_github_actions is None:
                os.environ.pop("GITHUB_ACTIONS", None)
            else:
                os.environ["GITHUB_ACTIONS"] = old_github_actions
            if old_event_name is None:
                os.environ.pop("GITHUB_EVENT_NAME", None)
            else:
                os.environ["GITHUB_EVENT_NAME"] = old_event_name
            if old_run_number is None:
                os.environ.pop("GITHUB_RUN_NUMBER", None)
            else:
                os.environ["GITHUB_RUN_NUMBER"] = old_run_number
            if old_owner is None:
                os.environ.pop("GITHUB_REPOSITORY_OWNER", None)
            else:
                os.environ["GITHUB_REPOSITORY_OWNER"] = old_owner


def _body_with_absence(body, count=1, run_number=99):
    return reconcile.render_card.body_with_reconcile_absence(
        body,
        count,
        run_number=run_number,
        closed_at="2026-07-13T12:00:00Z" if count == 2 else "",
    )


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
    check(
        "reconcile: refresh carries the validated snapshot into final guard",
        calls["upsert"][0].get("expected_existing") == existing,
    )
    check("reconcile: no close for refreshed worklist item", calls["close"] == [])


def test_upsert_rejects_change_after_reconcile_validation():
    expected = _pr_review_card()
    raced = copy.deepcopy(expected)
    raced["body"] += "\nowner selected a decision"
    raced["state"] = "OPEN"
    raced["updatedAt"] = raced.pop("updated_at")
    old_get_card = reconcile.render_card.get_card
    old_gh = reconcile.render_card._gh
    gh_calls = []
    reconcile.render_card.get_card = lambda _number: raced
    reconcile.render_card._gh = lambda args, check=True: gh_calls.append(args)
    try:
        result = reconcile.render_card.upsert_card(
            work_item(priority="high"),
            existing=expected,
            expected_existing=expected,
        )
    finally:
        reconcile.render_card.get_card = old_get_card
        reconcile.render_card._gh = old_gh
    check(
        "race: nested refresh read rejects owner change before mutation",
        result is None and gh_calls == [],
    )


def test_rejected_nested_refresh_does_not_bypass_triage_snapshot_guard():
    snapshot = _pr_review_card()
    raced = copy.deepcopy(snapshot)
    raced["body"] += "\nowner selected a decision"
    calls = run_reconcile(
        scan_payload(items=[work_item(priority="high")]),
        [snapshot],
        guarded_upsert_result=None,
        card_after_upsert=raced,
    )
    check(
        "race: rejected nested refresh cannot pass raced body to triage",
        len(calls["upsert"]) == 1 and calls["triage_rows"] == [None],
    )


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


def test_open_target_that_left_worklist_records_first_absence():
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
        "reconcile: first conclusive absence leaves card open",
        calls["close"] == [],
    )
    check(
        "reconcile: first conclusive absence persists count one",
        len(calls["state"]) == 1
        and calls["state"][0].get("count") == 1
        and reconcile.render_card.reconcile_absence_count(
            calls["state"][0]["body_after"]
        )
        == 1,
    )


def test_reconcile_run_number_requires_trusted_actions_identity():
    old_actions = os.environ.get("GITHUB_ACTIONS")
    old_event_name = os.environ.get("GITHUB_EVENT_NAME")
    old_number = os.environ.get("GITHUB_RUN_NUMBER")
    try:
        os.environ.pop("GITHUB_ACTIONS", None)
        os.environ["GITHUB_RUN_NUMBER"] = "42"
        outside_actions = reconcile._reconcile_run_number()
        os.environ["GITHUB_ACTIONS"] = "true"
        os.environ["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        manual = reconcile._reconcile_run_number()
        os.environ["GITHUB_EVENT_NAME"] = "schedule"
        invalid = []
        for value in ("", "true", "-1", "0", "9007199254740992"):
            os.environ["GITHUB_RUN_NUMBER"] = value
            invalid.append(reconcile._reconcile_run_number())
        os.environ["GITHUB_RUN_NUMBER"] = "42"
        valid = reconcile._reconcile_run_number()
    finally:
        if old_actions is None:
            os.environ.pop("GITHUB_ACTIONS", None)
        else:
            os.environ["GITHUB_ACTIONS"] = old_actions
        if old_event_name is None:
            os.environ.pop("GITHUB_EVENT_NAME", None)
        else:
            os.environ["GITHUB_EVENT_NAME"] = old_event_name
        if old_number is None:
            os.environ.pop("GITHUB_RUN_NUMBER", None)
        else:
            os.environ["GITHUB_RUN_NUMBER"] = old_number
    check(
        "reconcile: run identity is bounded and GitHub Actions-only",
        outside_actions == 0
        and manual == 0
        and invalid == [0, 0, 0, 0, 0]
        and valid == 42,
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
    check(
        "reconcile-freeze: indeterminate card is NOT refreshed", calls["upsert"] == []
    )


def _pr_review_card():
    return card(
        labels(
            "needs-decision",
            "repo:wheelhouse",
            "kind:pr-review",
            "priority:med",
            "target:wheelhouse-42",
        )
    )


def test_ci_wait_card_is_frozen_not_consumed():
    # #551 core fix: a PR whose fork CI was auto-approved this scan (or whose
    # approved checks are still running) emits NO worklist item while it awaits
    # terminal checks. Its existing pr-review card must be FROZEN - never consumed
    # merely because its target is mid-approval/CI-wait. Same fail-safe family as
    # the indeterminate freeze. (No refresh item here -> pure freeze, no upsert.)
    calls = run_reconcile(
        scan_payload(items=[], ci_wait_pr_numbers=[42]),
        [_pr_review_card()],
    )
    check("ci-wait-freeze: mid-approval-wait card is NOT closed", calls["close"] == [])
    check(
        "ci-wait-freeze: no refresh item -> card is left untouched",
        calls["upsert"] == [],
    )


def test_ci_wait_refresh_kills_stale_head_masquerade():
    # Anti-masquerade: when the scan OBSERVES the head moved, the existing card
    # (old head, merge-ready/green) is refreshed in place to the new head's honest
    # pending state so it can no longer masquerade as current - and it is still
    # not consumed.
    calls = run_reconcile(
        scan_payload(
            items=[],
            ci_wait_pr_numbers=[42],
            ci_wait_refresh_items=[ci_wait_refresh_item(head_sha="newsha")],
        ),
        [_pr_review_card()],
    )
    check("ci-wait-antimasq: card is NOT closed while frozen", calls["close"] == [])
    check(
        "ci-wait-antimasq: existing card is refreshed to the new head once",
        len(calls["upsert"]) == 1
        and calls["upsert"][0]["existing"] is not None
        and calls["upsert"][0]["item"]["head_sha"] == "newsha",
    )
    check(
        "ci-wait-antimasq: refresh renders a non-green (honest pending) state",
        calls["upsert"]
        and calls["upsert"][0]["item"]["tests"] != "green"
        and calls["upsert"][0]["item"]["comp"] != "pass",
    )


def test_ci_wait_refresh_never_creates_a_card():
    # Defer creation: a ci_wait refresh item with NO existing card must not mint a
    # new card - new-card creation waits until checks are terminal and the PR
    # classifies into a real bucket.
    calls = run_reconcile(
        scan_payload(
            items=[],
            ci_wait_pr_numbers=[42],
            ci_wait_refresh_items=[ci_wait_refresh_item()],
        ),
        [],  # no existing card
    )
    check("ci-wait-antimasq: no existing card -> no create", calls["upsert"] == [])
    check("ci-wait-antimasq: no existing card -> no close", calls["close"] == [])


def test_ci_wait_refresh_is_noop_when_card_already_current():
    # No churn: once the card already reflects the new head's pending state, a
    # later scan with the same refresh item makes NO further edit.
    already = card(
        labels(
            "needs-decision",
            "repo:wheelhouse",
            "kind:pr-review",
            "priority:med",
            "target:wheelhouse-42",
        ),
        render_version=reconcile.render_card.CARD_RENDER_VERSION,
    )
    state = reconcile.core.parse_state_block(already["body"])
    state.update(
        {
            "head_sha": "newsha",
            "comp": "none",
            "tests": "none",
            "bucket": "ci-running",
            # Match a card already refreshed once by the anti-masquerade path: the
            # pending refresh item is priority `low` and carries the full default
            # option set (incl. `investigate`), so nothing material differs.
            "priority": "low",
            "options": reconcile.render_card.card_options({"kind": "pr-review"}),
        }
    )
    already["body"] = reconcile.render_card._replace_state_block(already["body"], state)
    calls = run_reconcile(
        scan_payload(
            items=[],
            ci_wait_pr_numbers=[42],
            ci_wait_refresh_items=[
                ci_wait_refresh_item(head_sha="newsha", comp="none", tests="none")
            ],
        ),
        [already],
    )
    check(
        "ci-wait-antimasq: already-current card is not re-refreshed",
        calls["upsert"] == [],
    )
    check("ci-wait-antimasq: already-current card is not closed", calls["close"] == [])


def test_ci_wait_freeze_releases_when_checks_terminal():
    # The freeze never wedges a card: once checks are terminal the PR classifies
    # into a real bucket and emits a normal worklist item, so reconcile refreshes
    # it to the terminal state (here merge-ready/green at the new head) and the
    # freeze is gone (ci_wait empty).
    stale = card(
        labels(
            "needs-decision",
            "repo:wheelhouse",
            "kind:pr-review",
            "priority:med",
            "target:wheelhouse-42",
        ),
    )
    state = reconcile.core.parse_state_block(stale["body"])
    state.update({"head_sha": "newsha", "comp": "none", "tests": "none"})
    stale["body"] = reconcile.render_card._replace_state_block(stale["body"], state)
    calls = run_reconcile(
        scan_payload(
            items=[work_item(head_sha="newsha")],  # terminal: merge-ready green
            ci_wait_pr_numbers=[],
        ),
        [stale],
    )
    check(
        "ci-wait-release: terminal checks -> card refreshed to real bucket",
        len(calls["upsert"]) == 1
        and calls["upsert"][0]["item"]["head_sha"] == "newsha"
        and calls["upsert"][0]["item"]["tests"] == "green",
    )
    check(
        "ci-wait-release: card not consumed when it reclassifies", calls["close"] == []
    )


def test_conclusive_absence_starts_hysteresis_despite_freeze_machinery():
    # A target outside the worklist and outside every freeze records a qualifying
    # absence. It is not consumed until a second conclusive scan.
    calls = run_reconcile(
        scan_payload(items=[], ci_wait_pr_numbers=[]),
        [_pr_review_card()],
    )
    check(
        "ci-wait: conclusive non-frozen absence records count one",
        calls["close"] == []
        and len(calls["state"]) == 1
        and calls["state"][0].get("count") == 1,
    )


def test_axi96_ci_wait_then_terminal_scan_surfaces_card_with_criteria():
    # Live #96 lifecycle regression: the safe fork-CI approval scan intentionally
    # emits no worklist card while checks run, but the first terminal green scan
    # must surface the PR and thread authoritative criterion rows into the normal
    # card generation path. It must never silently disappear after terminal CI.
    waiting_refresh = ci_wait_refresh_item(number=96, head_sha="head96")
    waiting_refresh["repo"] = "axi"
    waiting = {
        "repos": {
            "axi": {
                "ok": True,
                "open_pr_numbers": [96],
                "open_issue_numbers": [],
                "indeterminate_pr_numbers": [],
                "ci_wait_pr_numbers": [96],
                "ci_wait_refresh_items": [waiting_refresh],
                "truncated": False,
            }
        },
        "items": [],
    }
    waiting_calls = run_reconcile(waiting, [])
    check(
        "axi#96 lifecycle: approve/wait scan intentionally creates no transient card",
        waiting_calls["upsert"] == [],
    )

    terminal_item = work_item(
        repo="axi",
        number=96,
        head_sha="head96",
        title="docs: add jj-axi to community catalog",
        author="aivv73",
        url="https://github.com/kunchenguid/axi/pull/96",
    )
    criteria = [
        {
            "id": "g3_prior_merge",
            "status": "unmet",
            "evidence": "aivv73 has no prior merged PR in axi",
        }
    ]
    terminal = {
        "repos": {
            "axi": {
                "ok": True,
                "open_pr_numbers": [96],
                "open_issue_numbers": [],
                "indeterminate_pr_numbers": [],
                "ci_wait_pr_numbers": [],
                "ci_wait_refresh_items": [],
                "truncated": False,
            }
        },
        "items": [terminal_item],
    }
    payload = {
        "criteria": [
            {
                "repo": "axi",
                "number": 96,
                "head_sha": "head96",
                "criteria": criteria,
            }
        ]
    }
    terminal_calls = run_reconcile(terminal, [], criteria_payload=payload)
    created = terminal_calls["upsert"]
    check(
        "axi#96 lifecycle: terminal green scan creates the normal decision card",
        len(created) == 1
        and created[0]["item"]["number"] == 96
        and created[0]["item"]["tests"] == "green",
    )
    check(
        "axi#96 lifecycle: authoritative criteria reach card generation",
        created
        and created[0]["item"].get(reconcile.render_card.AUTOMERGE_CRITERIA_FIELD)
        == criteria,
    )


def test_optional_automerge_handoff_never_aborts_reconciliation():
    scan = scan_payload(items=[work_item()])
    for payload in ("missing", "malformed", []):
        calls = run_reconcile(scan, [], criteria_payload=payload)
        check(
            "optional criteria handoff %r still reconciles the queue" % payload,
            len(calls["upsert"]) == 1,
        )


def test_ci_wait_freeze_does_not_shield_a_different_pr():
    # The freeze is keyed by PR number: a ci_wait entry for #99 must not stop
    # card #42 from recording its own first qualifying absence.
    calls = run_reconcile(
        scan_payload(items=[], ci_wait_pr_numbers=[99]),
        [_pr_review_card()],
    )
    check(
        "ci-wait: freeze for a different PR does not shield card #42",
        calls["close"] == []
        and len(calls["state"]) == 1
        and calls["state"][0].get("count") == 1,
    )


def test_ci_approval_card_with_no_pending_run_uses_hysteresis():
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
        "reconcile: stale ci-approval card stays open after one absence",
        calls["close"] == [],
    )
    check(
        "reconcile: stale ci-approval card records count one",
        len(calls["state"]) == 1 and calls["state"][0].get("count") == 1,
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


def test_blocked_open_target_that_left_worklist_is_not_soft_healed():
    """Card #447: a failed decision lands as blocked; soft self-heal must not
    consume it while the target is still open (even if it left the worklist)."""
    blocked_card = card(
        labels(
            "blocked",
            "repo:wheelhouse",
            "kind:pr-review",
            "priority:med",
            "target:wheelhouse-42",
        )
    )
    calls = run_reconcile(scan_payload(items=[]), [blocked_card])
    check(
        "reconcile: blocked card with open target outside worklist is not soft-closed",
        calls["close"] == [],
    )
    # Same protection when needs-decision is still present (e.g. mid-transition).
    blocked_pending = card(
        labels(
            "needs-decision",
            "blocked",
            "repo:wheelhouse",
            "kind:pr-review",
            "priority:med",
            "target:wheelhouse-42",
        )
    )
    calls = run_reconcile(scan_payload(items=[]), [blocked_pending])
    check(
        "reconcile: needs-decision+blocked open target is not soft-closed",
        calls["close"] == [],
    )


def test_blocked_card_hard_closes_when_target_no_longer_open():
    """Hard-close still auto-cleans a blocked card once the target is
    merged/closed - no stuck cards for genuinely-done targets."""
    blocked_card = card(
        labels(
            "blocked",
            "repo:wheelhouse",
            "kind:pr-review",
            "priority:med",
            "target:wheelhouse-42",
        )
    )
    calls = run_reconcile(
        scan_payload(items=[], open_pr_numbers=[]),
        [blocked_card],
    )
    check(
        "reconcile: blocked card hard-closes when target is no longer open",
        len(calls["close"]) == 1 and calls["close"][0]["number"] == 7,
    )
    check(
        "reconcile: hard-close message says target is no longer open",
        "no longer open" in calls["close"][0]["message"],
    )


def test_automerge_audit_state_survives_hard_close():
    for field in ("automerge_audit_intent", "automerge_audit_pending"):
        pending = card(
            labels(
                "needs-decision",
                "processing",
                "repo:wheelhouse",
                "kind:pr-review",
                "priority:med",
                "target:wheelhouse-42",
            )
        )
        state = reconcile.core.parse_state_block(pending["body"])
        state[field] = {"repo": "wheelhouse", "number": 42}
        pending["body"] = reconcile.render_card._replace_state_block(
            pending["body"], state
        )
        calls = run_reconcile(
            scan_payload(items=[], open_pr_numbers=[]),
            [pending],
        )
        check(
            "reconcile: %s survives target hard-close until audit recovery" % field,
            calls["close"] == [],
        )


def test_automerge_audit_state_survives_stale_snapshot_hard_close():
    snapshot = card(
        labels(
            "needs-decision",
            "processing",
            "repo:wheelhouse",
            "kind:pr-review",
            "priority:med",
            "target:wheelhouse-42",
        )
    )
    current = dict(snapshot)
    state = reconcile.core.parse_state_block(current["body"])
    state["automerge_audit_pending"] = {"repo": "wheelhouse", "number": 42}
    current["body"] = reconcile.render_card._replace_state_block(current["body"], state)
    calls = run_reconcile(
        scan_payload(items=[], open_pr_numbers=[]),
        [snapshot],
        current_cards=[current],
    )
    check(
        "reconcile: live pending audit blocks stale-snapshot hard-close",
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
    check(
        "reconcile: truncated scan does not record an absence",
        calls["state"] == [],
    )


def test_lifecycle_present_absent_present_reuses_and_clears():
    item = work_item()
    lifecycle = ReconcileLifecycle(item)
    present = scan_payload(items=[item])
    absent = scan_payload(items=[])

    lifecycle.run(present)
    lifecycle.run(absent)
    first_absence_writes = lifecycle.body_writes
    check(
        "lifecycle: first absence keeps the real card open",
        lifecycle.issue["state"] == "OPEN" and lifecycle.close_calls == [],
    )
    check(
        "lifecycle: first absence stores exact count one",
        reconcile.render_card.reconcile_absence_count(lifecycle.issue["body"]) == 1,
    )

    lifecycle.run(present)
    check(
        "lifecycle: present return keeps the same issue number",
        lifecycle.issue["number"] == 7 and lifecycle.issue["state"] == "OPEN",
    )
    check(
        "lifecycle: present return clears absence with one required write",
        reconcile.render_card.reconcile_absence_count(lifecycle.issue["body"]) == 0
        and not reconcile.render_card.reconcile_absence_needs_clear(
            lifecycle.issue["body"]
        )
        and lifecycle.body_writes == first_absence_writes + 1,
    )
    check("lifecycle: present-absent-present never closes", lifecycle.close_calls == [])


def test_failed_present_reset_cannot_authorize_later_close():
    item = work_item()
    lifecycle = ReconcileLifecycle(item)
    absent = scan_payload(items=[])
    lifecycle.run(absent)
    lifecycle.fail_body_write_attempts = {2}
    lifecycle.run(scan_payload(items=[item]))
    check(
        "reset failure: stale first absence remains tied to its old run",
        reconcile.render_card.reconcile_absence_count(lifecycle.issue["body"]) == 1
        and reconcile.render_card.reconcile_absence_run_number(
            lifecycle.issue["body"]
        )
        == 1,
    )
    lifecycle.run(absent)
    check(
        "reset failure: later absence starts a fresh streak",
        lifecycle.issue["state"] == "OPEN"
        and lifecycle.close_calls == []
        and reconcile.render_card.reconcile_absence_count(lifecycle.issue["body"]) == 1
        and reconcile.render_card.reconcile_absence_run_number(
            lifecycle.issue["body"]
        )
        == 3
    )


def test_lifecycle_two_absences_close_with_provenance():
    item = work_item()
    lifecycle = ReconcileLifecycle(item)
    lifecycle.run(scan_payload(items=[item]))
    absent = scan_payload(items=[])
    lifecycle.run(absent)
    check(
        "lifecycle: pass one does not close",
        lifecycle.issue["state"] == "OPEN" and lifecycle.close_calls == [],
    )
    lifecycle.run(absent)
    provenance = reconcile.render_card.reconcile_soft_close_provenance(
        lifecycle.issue["body"]
    )
    check(
        "lifecycle: pass two closes exactly once",
        lifecycle.issue["state"] == "CLOSED" and len(lifecycle.close_calls) == 1,
    )
    check(
        "lifecycle: soft-close owner copy remains unchanged",
        lifecycle.issue["comments"][-1]["body"]
        == "Self-healed by the scheduled backstop: wheelhouse#42 no longer needs "
        "a maintainer decision in the current scan - consuming this card.",
    )
    check(
        "lifecycle: trusted provenance is in the body before close",
        provenance is not None
        and provenance.get("actor")
        == reconcile.render_card.RECONCILE_SOFT_CLOSE_ACTOR
        and reconcile.render_card.reconcile_soft_close_provenance(
            lifecycle.close_calls[0]["body"]
        )
        == provenance,
    )


def test_lifecycle_failed_close_refreshes_provenance_before_retry():
    lifecycle = ReconcileLifecycle(work_item())
    absent = scan_payload(items=[])
    lifecycle.run(absent)
    lifecycle.fail_close_attempts = {1}
    lifecycle.run(absent)
    failed_provenance = reconcile.render_card.reconcile_soft_close_provenance(
        lifecycle.issue["body"]
    )
    partial_state = lifecycle.issue["state"]
    partial_labels = {
        label["name"] if isinstance(label, dict) else label
        for label in lifecycle.issue["labels"]
    }
    lifecycle.run(absent)
    retried_provenance = reconcile.render_card.reconcile_soft_close_provenance(
        lifecycle.issue["body"]
    )
    candidate = {
        "number": lifecycle.issue["number"],
        "body": lifecycle.issue["body"],
        "labels": lifecycle.issue["labels"],
        "state": lifecycle.issue["state"],
        "updatedAt": lifecycle.issue["updatedAt"],
        "author": lifecycle.issue["author"],
        "closedAt": lifecycle.issue.get("closedAt", ""),
        "closedBy": lifecycle.issue.get("closedBy"),
    }
    reusable, _reason = reconcile.render_card.reusable_closed_card(
        candidate, work_item()
    )
    check(
        "close retry: failed close leaves a trusted threshold record open",
        failed_provenance is not None
        and partial_state == "OPEN"
        and "needs-decision" in partial_labels
        and "resolved" not in partial_labels
        and lifecycle.close_attempts == 2,
    )
    check(
        "close retry: next scheduled run refreshes provenance before closing",
        lifecycle.issue["state"] == "CLOSED"
        and lifecycle.body_writes == 3
        and reconcile.render_card.reconcile_absence_run_number(
            lifecycle.close_calls[0]["body"]
        )
        == 3
        and retried_provenance
        == reconcile.render_card.reconcile_soft_close_provenance(
            lifecycle.close_calls[0]["body"]
        )
        and reusable,
    )


def test_owner_resolution_cannot_be_retried_as_reconcile_close():
    lifecycle = ReconcileLifecycle(work_item())
    absent = scan_payload(items=[])
    lifecycle.run(absent)
    lifecycle.fail_close_attempts = {1}
    lifecycle.run(absent)
    names = {
        label["name"] if isinstance(label, dict) else label
        for label in lifecycle.issue["labels"]
    }
    names.add("resolved")
    names.discard("needs-decision")
    lifecycle.issue["labels"] = labels(*sorted(names))
    lifecycle._tick()
    lifecycle.run(absent)
    check(
        "owner resolution: threshold provenance is never refreshed or closed",
        lifecycle.issue["state"] == "OPEN"
        and lifecycle.close_attempts == 1
        and lifecycle.body_writes == 2
        and reconcile.render_card.reconcile_absence_run_number(
            lifecycle.issue["body"]
        )
        == 2,
    )


def test_lifecycle_pr_and_issue_cards_share_threshold():
    cases = [
        (
            "pr-review",
            work_item(),
            scan_payload(items=[]),
        ),
        (
            "issue-triage",
            work_item(
                kind="issue-triage",
                head_sha="",
                bucket="issue-triage",
                priority="low",
                comp="n/a",
                tests="n/a",
            ),
            scan_payload(items=[], open_pr_numbers=[], open_issue_numbers=[42]),
        ),
    ]
    for kind, item, absent in cases:
        lifecycle = ReconcileLifecycle(item)
        lifecycle.run(absent)
        first_open = lifecycle.issue["state"] == "OPEN"
        first_count = reconcile.render_card.reconcile_absence_count(
            lifecycle.issue["body"]
        )
        lifecycle.run(absent)
        check(
            "lifecycle: %s uses fixed threshold two" % kind,
            first_open
            and first_count == 1
            and lifecycle.issue["state"] == "CLOSED"
            and len(lifecycle.close_calls) == 1,
        )


def test_lifecycle_kind_transition_reuses_and_resets():
    initial = work_item(
        kind="ci-approval",
        bucket="needs-ci-approval",
        comp="none",
        tests="none",
        priority="high",
    )
    lifecycle = ReconcileLifecycle(initial)
    lifecycle.run(scan_payload(items=[]))
    check(
        "transition: old kind has one absence",
        reconcile.render_card.reconcile_absence_count(lifecycle.issue["body"]) == 1,
    )
    returned = work_item(kind="pr-review", bucket="review-needed")
    writes_before_return = lifecycle.body_writes
    lifecycle.run(scan_payload(items=[returned]))
    state = reconcile.core.parse_state_block(lifecycle.issue["body"])
    label_names = reconcile._label_names(lifecycle.issue["labels"])
    check(
        "transition: same card refreshes to new kind",
        lifecycle.issue["number"] == 7
        and lifecycle.issue["state"] == "OPEN"
        and state.get("kind") == "pr-review"
        and "kind:pr-review" in label_names
        and "kind:ci-approval" not in label_names,
    )
    check(
        "transition: kind refresh resets old absence state in the same write",
        not reconcile.render_card.reconcile_absence_needs_clear(
            lifecycle.issue["body"]
        )
        and lifecycle.body_writes == writes_before_return + 1,
    )


def test_intervening_runs_break_absence_adjacency():
    cases = [
        ("failed", scan_payload(items=[], ok=False), "schedule"),
        ("truncated", scan_payload(items=[], truncated=True), "schedule"),
        (
            "UNKNOWN",
            scan_payload(items=[], indeterminate_pr_numbers=[42]),
            "schedule",
        ),
        ("CI-wait", scan_payload(items=[], ci_wait_pr_numbers=[42]), "schedule"),
        ("manual", scan_payload(items=[]), "workflow_dispatch"),
    ]
    for name, intervening, event_name in cases:
        lifecycle = ReconcileLifecycle(work_item())
        lifecycle.run(scan_payload(items=[]))
        original = lifecycle.issue["body"]
        original_writes = lifecycle.body_writes
        lifecycle.run(intervening, event_name=event_name)
        preserved = (
            lifecycle.issue["body"] == original
            and lifecycle.body_writes == original_writes
            and lifecycle.close_calls == []
        )
        lifecycle.run(scan_payload(items=[]))
        restarted = (
            lifecycle.issue["state"] == "OPEN"
            and lifecycle.close_calls == []
            and reconcile.render_card.reconcile_absence_count(
                lifecycle.issue["body"]
            )
            == 1
            and reconcile.render_card.reconcile_absence_run_number(
                lifecycle.issue["body"]
            )
            == 3
        )
        lifecycle.run(scan_payload(items=[]))
        check(
            "freeze lifecycle: %s run preserves state but breaks adjacency" % name,
            preserved
            and restarted
            and lifecycle.issue["state"] == "CLOSED"
            and len(lifecycle.close_calls) == 1,
        )


def test_ci_wait_antimasquerade_refresh_preserves_absence():
    lifecycle = ReconcileLifecycle(work_item())
    lifecycle.issue["body"] = _body_with_absence(lifecycle.issue["body"], 1)
    writes_before = lifecycle.body_writes
    lifecycle.run(
        scan_payload(
            items=[],
            ci_wait_pr_numbers=[42],
            ci_wait_refresh_items=[ci_wait_refresh_item(head_sha="newsha")],
        )
    )
    state = reconcile.core.parse_state_block(lifecycle.issue["body"])
    check(
        "ci-wait freeze: required head refresh preserves count one",
        state.get("head_sha") == "newsha"
        and reconcile.render_card.reconcile_absence_count(lifecycle.issue["body"])
        == 1,
    )
    check(
        "ci-wait freeze: anti-masquerade uses one body write and never closes",
        lifecycle.body_writes == writes_before + 1 and lifecycle.close_calls == [],
    )


def test_malformed_and_legacy_state_cannot_accelerate_close():
    absent = scan_payload(items=[])
    legacy = ReconcileLifecycle(work_item())
    legacy.run(absent)
    check(
        "absence schema: legacy missing state starts at count one without close",
        legacy.issue["state"] == "OPEN"
        and reconcile.render_card.reconcile_absence_count(legacy.issue["body"]) == 1,
    )

    malformed_records = [
        True,
        {"version": 2, "threshold": 2, "count": True, "run_number": 1},
        {"version": 2, "threshold": 2, "count": -1, "run_number": 1},
        {"version": 2, "threshold": 2, "count": 999999999, "run_number": 1},
        {"version": 3, "threshold": 2, "count": 1, "run_number": 1},
        {"version": 2, "threshold": 2, "count": 1},
        {
            "version": 2,
            "threshold": 2,
            "count": 2,
            "run_number": 1,
            "soft_close": {
                "actor": "owner",
                "reason": "open-target-worklist-absence",
                "at": "2026-07-13T12:00:00Z",
            },
        },
    ]
    all_safe = True
    for record in malformed_records:
        lifecycle = ReconcileLifecycle(work_item())
        state = reconcile.core.parse_state_block(lifecycle.issue["body"])
        state[reconcile.render_card.RECONCILE_ABSENCE_FIELD] = record
        lifecycle.issue["body"] = reconcile.render_card._replace_state_block(
            lifecycle.issue["body"], state
        )
        lifecycle.run(absent)
        all_safe = all_safe and lifecycle.issue["state"] == "OPEN"
        all_safe = all_safe and reconcile.render_card.reconcile_absence_count(
            lifecycle.issue["body"]
        ) in (0, 1)
    check(
        "absence schema: malformed/boolean/negative/unbounded/wrong provenance never closes",
        all_safe,
    )

    duplicate = ReconcileLifecycle(work_item())
    state = reconcile.core.parse_state_block(duplicate.issue["body"])
    raw = json.dumps(state, separators=(",", ":"))
    raw = raw[:-1] + (
        ',"reconcile_absence":{"version":2,"threshold":2,"count":1,"run_number":1}'
        ',"reconcile_absence":{"version":2,"threshold":2,"count":1,"run_number":1}}'
    )
    duplicate.issue["body"] = reconcile.render_card._STATE_BLOCK_RE.sub(
        "<!-- wheelhouse-state: %s -->" % raw,
        duplicate.issue["body"],
        count=1,
    )
    duplicate.run(absent)
    check(
        "absence schema: duplicate record fails closed with no close permission",
        duplicate.issue["state"] == "OPEN"
        and duplicate.close_calls == []
        and reconcile.render_card.reconcile_absence_count(duplicate.issue["body"]) == 0,
    )


def test_stale_snapshot_races_do_not_mutate_or_close():
    snapshot = _pr_review_card()
    snapshot["body"] = _body_with_absence(snapshot["body"], 1)

    processing = copy.deepcopy(snapshot)
    processing["labels"] = processing["labels"] + labels("processing")
    processing_calls = run_reconcile(
        scan_payload(items=[]), [snapshot], current_cards=[processing]
    )
    check(
        "race: processing transition blocks second-pass state write and close",
        processing_calls["state"] == [] and processing_calls["close"] == [],
    )
    processing_return = run_reconcile(
        scan_payload(items=[work_item(options=["merge", "close", "hold"])]),
        [snapshot],
        current_cards=[processing],
    )
    check(
        "race: processing transition blocks worklist-return counter clear",
        processing_return["state"] == [] and processing_return["upsert"] == [],
    )

    owner_edit = copy.deepcopy(snapshot)
    owner_edit["body"] += "\nowner selected a decision"
    owner_calls = run_reconcile(
        scan_payload(items=[]), [snapshot], current_cards=[owner_edit]
    )
    check(
        "race: owner body edit blocks second-pass state write and close",
        owner_calls["state"] == [] and owner_calls["close"] == [],
    )

    owner_comment = copy.deepcopy(snapshot)
    owner_comment["comments"] = [{"body": "I am deciding this now"}]
    comment_calls = run_reconcile(
        scan_payload(items=[]), [snapshot], current_cards=[owner_comment]
    )
    check(
        "race: owner comment blocks second-pass state write and close",
        comment_calls["state"] == [] and comment_calls["close"] == [],
    )

    resolved = copy.deepcopy(snapshot)
    resolved["state"] = "CLOSED"
    resolved_calls = run_reconcile(
        scan_payload(items=[]), [snapshot], current_cards=[resolved]
    )
    check(
        "race: owner resolution blocks second-pass state write and close",
        resolved_calls["state"] == [] and resolved_calls["close"] == [],
    )

    hard_processing = copy.deepcopy(_pr_review_card())
    hard_processing["labels"] += labels("processing")
    hard_calls = run_reconcile(
        scan_payload(items=[], open_pr_numbers=[]),
        [_pr_review_card()],
        current_cards=[hard_processing],
    )
    check(
        "race: processing transition blocks stale-snapshot hard close",
        hard_calls["close"] == [],
    )


def test_hard_close_bypasses_count_and_nonrefreshable_labels():
    for label_name in ("blocked", "processing"):
        hard = card(
            labels(
                label_name,
                "repo:wheelhouse",
                "kind:pr-review",
                "priority:med",
                "target:wheelhouse-42",
            ),
            render_version=reconcile.render_card.CARD_RENDER_VERSION,
        )
        hard["body"] = _body_with_absence(hard["body"], 1)
        calls = run_reconcile(
            scan_payload(items=[], open_pr_numbers=[]),
            [hard],
        )
        check(
            "hard close: %s card with count one closes immediately" % label_name,
            len(calls["close"]) == 1 and calls["close"][0]["number"] == 7,
        )


def test_audit_protections_ignore_absence_count():
    for field in ("automerge_audit_intent", "automerge_audit_pending"):
        protected = _pr_review_card()
        state = reconcile.core.parse_state_block(protected["body"])
        state[reconcile.render_card.RECONCILE_ABSENCE_FIELD] = {
            "version": 2,
            "threshold": 2,
            "count": 1,
            "run_number": 99,
        }
        state[field] = {"repo": "wheelhouse", "number": 42}
        protected["body"] = reconcile.render_card._replace_state_block(
            protected["body"], state
        )
        calls = run_reconcile(
            scan_payload(items=[], open_pr_numbers=[]),
            [protected],
        )
        check(
            "audit: %s still protects count-one card" % field,
            calls["state"] == [] and calls["close"] == [],
        )


def main():
    test_refresh_uses_known_card_when_target_label_missing()
    test_upsert_rejects_change_after_reconcile_validation()
    test_rejected_nested_refresh_does_not_bypass_triage_snapshot_guard()
    test_refresh_uses_current_labels_before_upsert()
    test_open_target_that_left_worklist_records_first_absence()
    test_reconcile_run_number_requires_trusted_actions_identity()
    test_indeterminate_pr_card_is_frozen()
    test_ci_wait_card_is_frozen_not_consumed()
    test_ci_wait_refresh_kills_stale_head_masquerade()
    test_ci_wait_refresh_never_creates_a_card()
    test_ci_wait_refresh_is_noop_when_card_already_current()
    test_ci_wait_freeze_releases_when_checks_terminal()
    test_conclusive_absence_starts_hysteresis_despite_freeze_machinery()
    test_axi96_ci_wait_then_terminal_scan_surfaces_card_with_criteria()
    test_optional_automerge_handoff_never_aborts_reconciliation()
    test_ci_wait_freeze_does_not_shield_a_different_pr()
    test_ci_approval_card_with_no_pending_run_uses_hysteresis()
    test_ci_approval_worklist_item_creates_fresh_card()
    test_open_target_that_left_worklist_uses_current_labels_before_close()
    test_blocked_open_target_that_left_worklist_is_not_soft_healed()
    test_blocked_card_hard_closes_when_target_no_longer_open()
    test_automerge_audit_state_survives_hard_close()
    test_automerge_audit_state_survives_stale_snapshot_hard_close()
    test_render_stale_only_pure_card_is_refreshed_via_reconcile()
    test_render_fresh_and_materially_unchanged_card_is_noop_via_reconcile()
    test_target_activity_newer_gets_state_only_reflection_once()
    test_target_activity_reflection_skips_non_pending_and_bad_timestamps()
    test_target_activity_reflection_uses_legacy_card_updated_at_baseline()
    test_failed_repo_scan_leaves_cards_unstamped()
    test_render_stale_processing_card_is_not_refreshed_via_reconcile()
    test_open_target_without_needs_decision_is_left_alone()
    test_truncated_repo_scan_does_not_self_heal_close_missing_issue()
    test_lifecycle_present_absent_present_reuses_and_clears()
    test_failed_present_reset_cannot_authorize_later_close()
    test_lifecycle_two_absences_close_with_provenance()
    test_lifecycle_failed_close_refreshes_provenance_before_retry()
    test_owner_resolution_cannot_be_retried_as_reconcile_close()
    test_lifecycle_pr_and_issue_cards_share_threshold()
    test_lifecycle_kind_transition_reuses_and_resets()
    test_intervening_runs_break_absence_adjacency()
    test_ci_wait_antimasquerade_refresh_preserves_absence()
    test_malformed_and_legacy_state_cannot_accelerate_close()
    test_stale_snapshot_races_do_not_mutate_or_close()
    test_hard_close_bypasses_count_and_nonrefreshable_labels()
    test_audit_protections_ignore_absence_count()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all reconcile tests passed")


if __name__ == "__main__":
    main()
