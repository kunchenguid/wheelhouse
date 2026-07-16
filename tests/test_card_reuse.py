#!/usr/bin/env python3
"""End-to-end closed-card reuse tests with an in-memory GitHub boundary."""

import copy
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)
import apply_decision  # noqa: E402
import auto_merge  # noqa: E402
import reconcile  # noqa: E402
import render_card as rc  # noqa: E402
import wheelhouse_core as core  # noqa: E402

_failures = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        _failures.append(name)


def label_objects(names):
    return [{"name": name} for name in sorted(set(names))]


def label_names(issue):
    return {
        label if isinstance(label, str) else label.get("name", "")
        for label in issue.get("labels", [])
    }


def item(head="a" * 40, kind="pr-review", **overrides):
    bucket = (
        "needs-ci-approval"
        if kind == "ci-approval"
        else core.classify(
            False,
            "pass",
            "green",
            True,
            cross_repo=False,
            mergeable="MERGEABLE",
        )
    )
    base = {
        "repo": "wheelhouse",
        "number": 42,
        "kind": kind,
        "head_sha": head,
        "updated_at": "2026-07-13T12:00:00Z",
        "title": "Ready PR",
        "author": "contributor",
        "bucket": bucket,
        "comp": "none" if kind == "ci-approval" else "pass",
        "tests": "none" if kind == "ci-approval" else "green",
        "url": "https://github.com/kunchenguid/wheelhouse/pull/42",
        "summary": "compliance and tests are current",
        "recommendation": "Merge after review.",
        "priority": "high" if kind == "ci-approval" else "med",
        "auto_triage": True,
        "base_sha": "b" * 40,
        "automerge_vision_sha": "c" * 40,
    }
    base.update(overrides)
    return base


def scan_payload(items, open_target=True):
    pr_numbers = []
    issue_numbers = []
    if open_target:
        for entry in items or []:
            number = int(entry.get("number") or 0)
            if not number:
                continue
            if entry.get("kind") == "issue-triage":
                issue_numbers.append(number)
            else:
                pr_numbers.append(number)
        if not pr_numbers and not issue_numbers:
            pr_numbers = [42]
    return {
        "repos": {
            "wheelhouse": {
                "ok": True,
                "truncated": False,
                "open_pr_numbers": pr_numbers,
                "open_issue_numbers": issue_numbers,
                "indeterminate_pr_numbers": [],
                "ci_wait_pr_numbers": [],
                "ci_wait_refresh_items": [],
            }
        },
        "items": items,
    }


def valid_triage():
    return {
        "summary": "The change is narrow and grounded.",
        "product_implications": "Default behavior stays stable.",
        "evidence": 'target.txt: "The change is narrow and grounded"',
        "recommended_action": "merge",
        "recommended_reason": "Checks and behavior are safe.",
        "automerge": {
            "behavior_class": "A",
            "aligns_with_vision": True,
            "changes_existing_or_default_behavior": False,
            "recommend_merge": True,
            "optin_default_off": False,
        },
    }


class LifecycleGitHub:
    """Stateful GitHub boundary used by actual reconcile and render modules.

    Issue-by-number reads are immediately consistent. Open-list and label-search
    visibility can lag independently via `list_index_lag_seconds` and
    `search_index_lag_seconds` (fake time advanced by `_lifecycle_sleep`).
    """

    def __init__(self, initial_item=None, start_empty=False):
        self.issues = {}
        self.next_number = 8
        self.clock = 0
        self.fake_time = 0.0
        self.list_index_lag_seconds = 0.0
        self.search_index_lag_seconds = 0.0
        self.list_visible_at = {}
        self.search_visible_at = {}
        self.create_calls = 0
        self.close_calls = 0
        self.fail_list_state = ""
        self.fail_prepare = ""
        self.inject_duplicate_on_reopen = False
        self.inject_open_duplicate_after_create = False
        self.fail_post_reopen_view_once = False
        self.fail_open_list_after_reopen = False
        self.fail_open_list_after_create = False
        self.just_created = False
        self.fail_reopen_after_mutation = False
        self.direct_issue_overrides = {}
        self.labels_on_reopen = []
        self.just_reopened = False
        self.run_number = 0
        self.workflow_calls = []
        self.issue_edit_calls = 0
        self.timeline_failures = set()
        self.budget_reservations = 0
        if not start_empty:
            self.add_card(initial_item or item(), number=7)

    def _timestamp(self):
        self.clock += 1
        minute, second = divmod(self.clock, 60)
        return "2026-07-13T13:%02d:%02dZ" % (minute, second)

    def _touch(self, issue):
        issue["updated_at"] = self._timestamp()

    def _mark_index_visibility(self, number, *, list_lag=None, search_lag=None):
        list_delay = (
            self.list_index_lag_seconds if list_lag is None else float(list_lag)
        )
        search_delay = (
            self.search_index_lag_seconds if search_lag is None else float(search_lag)
        )
        self.list_visible_at[number] = self.fake_time + max(0.0, list_delay)
        self.search_visible_at[number] = self.fake_time + max(0.0, search_delay)

    def advance_time(self, seconds):
        self.fake_time += float(seconds)

    def _list_visible(self, number):
        return self.fake_time >= self.list_visible_at.get(number, 0.0)

    def _search_visible(self, number):
        # REST label list is the production open-list path; search lag is tracked
        # independently so tests can prove both indexes lag without affecting
        # authoritative issue-by-number reads.
        return self.fake_time >= self.search_visible_at.get(number, 0.0)

    def add_card(
        self,
        current_item,
        number,
        state="OPEN",
        author=rc.CARD_AUTOMATION_AUTHOR,
        list_lag=0.0,
        search_lag=0.0,
        held=False,
        has_token=False,
    ):
        card = rc.render(current_item, held=held or rc.should_hold(current_item, has_token))
        self.issues[number] = {
            "number": number,
            "body": card["body"],
            "labels": label_objects(card["labels"]),
            "title": card["title"],
            "state": state,
            "updated_at": self._timestamp(),
            "author": author,
            "closed_at": "",
            "closed_by": "",
            "comments": [],
            "timeline": [],
        }
        self._mark_index_visibility(number, list_lag=list_lag, search_lag=search_lag)
        self.next_number = max(self.next_number, number + 1)
        return self.issues[number]

    def _rest(self, issue):
        row = {
            "number": issue["number"],
            "body": issue["body"],
            "labels": copy.deepcopy(issue["labels"]),
            "title": issue["title"],
            "state": issue["state"].lower(),
            "updated_at": issue["updated_at"],
            "user": {"login": issue["author"]},
            "closed_at": issue["closed_at"] or None,
            "closed_by": (
                {"login": issue["closed_by"]} if issue["closed_by"] else None
            ),
            "comments": len(issue["comments"]),
        }
        return row

    def _view(self, issue):
        return {
            "number": issue["number"],
            "body": issue["body"],
            "labels": copy.deepcopy(issue["labels"]),
            "title": issue["title"],
            "state": issue["state"],
            "updatedAt": issue["updated_at"],
            "author": {
                "login": (
                    rc.GET_CARD_AUTOMATION_AUTHOR
                    if issue["author"] == rc.CARD_AUTOMATION_AUTHOR
                    else issue["author"]
                )
            },
            "comments": copy.deepcopy(issue["comments"]),
        }

    def cards_snapshot(self):
        rows = []
        for issue in self.issues.values():
            if issue["state"] != "OPEN":
                continue
            rows.append(
                {
                    "number": issue["number"],
                    "body": issue["body"],
                    "labels": copy.deepcopy(issue["labels"]),
                    "title": issue["title"],
                    "updated_at": issue["updated_at"],
                    "author": issue["author"],
                    "comments": len(issue["comments"]),
                }
            )
        return rows

    def _apply_label_args(self, issue, args):
        names = label_names(issue)
        for index, arg in enumerate(args):
            if arg == "--add-label":
                names.add(args[index + 1])
            elif arg == "--remove-label":
                names.discard(args[index + 1])
        issue["labels"] = label_objects(names)

    def _close(self, issue):
        issue["state"] = "CLOSED"
        provenance = rc.reconcile_soft_close_provenance(issue["body"])
        issue["closed_at"] = (
            provenance["at"] if provenance else "2026-07-13T13:59:59Z"
        )
        issue["closed_by"] = rc.CARD_AUTOMATION_AUTHOR
        issue["updated_at"] = issue["closed_at"]
        issue["timeline"].append(
            {
                "event": "closed",
                "created_at": issue["closed_at"],
                "actor": {"login": rc.CARD_AUTOMATION_AUTHOR},
            }
        )

    def post_close_activity(self, number, actor=rc.CARD_AUTOMATION_AUTHOR):
        issue = self.issues[number]
        issue["updated_at"] = "2099-01-01T00:00:00Z"
        issue["timeline"].append(
            {
                "event": "labeled",
                "created_at": issue["updated_at"],
                "actor": {"login": actor},
            }
        )

    def _api_list(self, endpoint):
        parsed = urlparse(endpoint)
        query = parse_qs(parsed.query)
        requested_state = (query.get("state") or [""])[0].upper()
        marker = unquote((query.get("labels") or [""])[0])
        if (
            self.just_reopened
            and self.fail_open_list_after_reopen
            and requested_state == "OPEN"
        ):
            raise RuntimeError("simulated persistent post-reopen list failure")
        if (
            self.just_created
            and self.fail_open_list_after_create
            and requested_state == "OPEN"
        ):
            raise RuntimeError("simulated persistent post-create list failure")
        if self.fail_list_state == requested_state:
            raise RuntimeError("simulated incomplete %s pagination" % requested_state)
        rows = []
        for issue in self.issues.values():
            if issue["state"] != requested_state:
                continue
            if marker not in label_names(issue):
                continue
            # Open-list and search indexes lag independently of issue-by-number.
            if requested_state == "OPEN" and not (
                self._list_visible(issue["number"]) and self._search_visible(issue["number"])
            ):
                continue
            rows.append(self._rest(issue))
        return SimpleNamespace(returncode=0, stdout=json.dumps([rows]), stderr="")

    def gh(self, args, check=True):
        if args[:2] == ["label", "create"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:3] == ["api", "--paginate", "--slurp"]:
            return self._api_list(args[3])
        if args[:3] == ["api", "--method", "PATCH"]:
            number = int(args[3].rsplit("/", 1)[-1])
            issue = self.issues[number]
            names = {
                value.removeprefix("labels[]=")
                for value in args
                if value.startswith("labels[]=")
            }
            issue["labels"] = label_objects(names)
            self._close(issue)
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(self._rest(issue)), stderr=""
            )
        if args and args[0] == "api":
            if self.just_reopened and self.fail_post_reopen_view_once:
                self.just_reopened = False
                self.fail_post_reopen_view_once = False
                raise RuntimeError("simulated post-reopen read failure")
            if "/timeline?" in args[1]:
                number = int(args[1].split("/issues/", 1)[1].split("/", 1)[0])
                if number in self.timeline_failures:
                    raise RuntimeError("simulated unreadable timeline")
                query = parse_qs(urlparse(args[1]).query)
                page = int((query.get("page") or ["1"])[0])
                per_page = int((query.get("per_page") or ["100"])[0])
                start = (page - 1) * per_page
                rows = self.issues[number]["timeline"][start : start + per_page]
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps(rows), stderr=""
                )
            number = int(args[1].rsplit("/", 1)[-1])
            if number in self.direct_issue_overrides:
                override = self.direct_issue_overrides[number]
                if override is None:
                    raise RuntimeError("issue not found")
                if isinstance(override, Exception):
                    raise override
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps(override), stderr=""
                )
            issue = self.issues.get(number)
            if not issue:
                raise RuntimeError("issue not found")
            # Direct issue-by-number is immediately consistent even while list lags.
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(self._rest(issue)), stderr=""
            )
        if args[:2] == ["workflow", "run"]:
            self.workflow_calls.append(list(args))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["issue", "view"]:
            issue = self.issues.get(int(args[2]))
            return SimpleNamespace(
                returncode=0 if issue else 1,
                stdout=json.dumps(self._view(issue)) if issue else "",
                stderr="" if issue else "not found",
            )
        if args[:2] == ["issue", "create"]:
            number = self.next_number
            self.next_number += 1
            self.create_calls += 1
            with open(args[args.index("--body-file") + 1]) as body_file:
                body = body_file.read()
            names = [
                args[index + 1]
                for index, arg in enumerate(args)
                if arg == "--label"
            ]
            self.issues[number] = {
                "number": number,
                "body": body,
                "labels": label_objects(names),
                "title": args[args.index("--title") + 1],
                "state": "OPEN",
                "updated_at": self._timestamp(),
                "author": rc.CARD_AUTOMATION_AUTHOR,
                "closed_at": "",
                "closed_by": "",
                "comments": [],
                "timeline": [],
            }
            self._mark_index_visibility(number)
            self.just_created = True
            if self.inject_open_duplicate_after_create:
                self.inject_open_duplicate_after_create = False
                peer_number = self.next_number
                self.next_number += 1
                peer = copy.deepcopy(self.issues[number])
                peer["number"] = peer_number
                peer["updated_at"] = self._timestamp()
                self.issues[peer_number] = peer
                # Peer is already list-visible so uniqueness can observe it.
                self.list_visible_at[peer_number] = self.fake_time
                self.search_visible_at[peer_number] = self.fake_time
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/kunchenguid/wheelhouse/issues/%s\n" % number,
                stderr="",
            )
        if args[:2] == ["issue", "edit"]:
            self.issue_edit_calls += 1
            issue = self.issues[int(args[2])]
            is_closed_prepare = issue["state"] == "CLOSED" and "--body-file" in args
            if is_closed_prepare and self.fail_prepare == "before":
                raise RuntimeError("simulated body update failure")
            if "--body-file" in args:
                with open(args[args.index("--body-file") + 1]) as body_file:
                    new_body = body_file.read()
                if is_closed_prepare and self.fail_prepare == "labels-only":
                    self._apply_label_args(issue, args)
                    self._touch(issue)
                    raise RuntimeError("simulated label-only partial update")
                issue["body"] = new_body
                if is_closed_prepare and self.fail_prepare == "body-only":
                    self._touch(issue)
                    raise RuntimeError("simulated body-only partial update")
            self._apply_label_args(issue, args)
            if "--title" in args:
                issue["title"] = args[args.index("--title") + 1]
            self._touch(issue)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["issue", "comment"]:
            issue = self.issues[int(args[2])]
            issue["comments"].append(
                {
                    "body": args[args.index("--body") + 1],
                    "author": {"login": rc.CARD_AUTOMATION_AUTHOR},
                }
            )
            self._touch(issue)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["issue", "close"]:
            self.close_calls += 1
            self._close(self.issues[int(args[2])])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["issue", "reopen"]:
            issue = self.issues[int(args[2])]
            self.labels_on_reopen.append(set(label_names(issue)))
            issue["state"] = "OPEN"
            issue["closed_at"] = ""
            issue["closed_by"] = ""
            self._touch(issue)
            self.just_reopened = True
            # Reopen is immediately list-visible by default (pre-existing identity).
            self.list_visible_at[issue["number"]] = self.fake_time
            self.search_visible_at[issue["number"]] = self.fake_time
            if self.inject_duplicate_on_reopen:
                self.inject_duplicate_on_reopen = False
                duplicate = copy.deepcopy(issue)
                duplicate["number"] = self.next_number
                duplicate["updated_at"] = self._timestamp()
                self.issues[self.next_number] = duplicate
                self.list_visible_at[self.next_number] = self.fake_time
                self.search_visible_at[self.next_number] = self.fake_time
                self.next_number += 1
            if self.fail_reopen_after_mutation:
                self.fail_reopen_after_mutation = False
                raise RuntimeError("simulated ambiguous reopen response")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError("unexpected gh call: %r" % (args,))

    def _sleep(self, seconds):
        self.advance_time(seconds)

    def _with_boundary(self, callback):
        old_gh = rc._gh
        old_sleep = rc._lifecycle_sleep
        old_reserve = rc.reserve_triage_budget
        old_owner = os.environ.get("GITHUB_REPOSITORY_OWNER")
        rc._gh = self.gh
        rc._lifecycle_sleep = self._sleep
        # Daily-ledger behavior has its own exhaustive offline boundary suite in
        # test_triage_budget.py. This lifecycle fixture focuses on the card side
        # of the verified queue checkpoint while preserving the real permit API.
        def reserve(_number, _queued_item, _ceiling):
            self.budget_reservations += 1
            return True

        rc.reserve_triage_budget = reserve
        os.environ["GITHUB_REPOSITORY_OWNER"] = "kunchenguid"
        try:
            return callback()
        finally:
            rc._gh = old_gh
            rc._lifecycle_sleep = old_sleep
            rc.reserve_triage_budget = old_reserve
            if old_owner is None:
                os.environ.pop("GITHUB_REPOSITORY_OWNER", None)
            else:
                os.environ["GITHUB_REPOSITORY_OWNER"] = old_owner

    def event_upsert(self, current_item, has_token=False):
        return self._with_boundary(
            lambda: rc.upsert_card(current_item, has_token=has_token)
        )

    def run_reconcile(self, scan, token=False):
        self.run_number += 1

        def run():
            old_argv = sys.argv[:]
            old_env = {
                key: os.environ.get(key)
                for key in (
                    "GITHUB_ACTIONS",
                    "GITHUB_EVENT_NAME",
                    "GITHUB_RUN_NUMBER",
                    "WHEELHOUSE_AUTO_TRIAGE_HAS_TOKEN",
                )
            }
            try:
                os.environ.update(
                    GITHUB_ACTIONS="true",
                    GITHUB_EVENT_NAME="schedule",
                    GITHUB_RUN_NUMBER=str(self.run_number),
                    WHEELHOUSE_AUTO_TRIAGE_HAS_TOKEN="true" if token else "false",
                )
                with tempfile.TemporaryDirectory() as directory:
                    scan_path = os.path.join(directory, "scan.json")
                    cards_path = os.path.join(directory, "cards.json")
                    with open(scan_path, "w") as output:
                        json.dump(scan, output)
                    with open(cards_path, "w") as output:
                        json.dump(self.cards_snapshot(), output)
                    sys.argv = ["reconcile.py", scan_path, cards_path]
                    with redirect_stdout(io.StringIO()) as output:
                        reconcile.main()
                    return output.getvalue()
            finally:
                sys.argv = old_argv
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

        return self._with_boundary(run)

    def soft_close(self):
        waiting = scan_payload([])
        self.run_reconcile(waiting)
        self.run_reconcile(waiting)
        assert self.issues[7]["state"] == "CLOSED"
        assert rc.reconcile_soft_close_provenance(self.issues[7]["body"])

    def normalized(self, number):
        return self._with_boundary(lambda: rc._get_lifecycle_issue(number))


def add_triage_and_verdict(issue, current_item):
    queued = rc.body_with_triage_queued(issue["body"], current_item)
    issue["body"] = rc.body_with_triage_result(
        queued,
        current_item["head_sha"],
        triage=valid_triage(),
        base_sha=current_item["base_sha"],
        vision_sha=current_item["automerge_vision_sha"],
    )


def test_same_head_reopens_same_issue():
    current = item()
    github = LifecycleGitHub(current)
    add_triage_and_verdict(github.issues[7], current)
    github.soft_close()
    github.run_reconcile(scan_payload([current]))
    state = core.parse_state_block(github.issues[7]["body"])
    check(
        "same-head: reconcile reopens the same issue number",
        github.issues[7]["state"] == "OPEN"
        and set(github.issues) == {7}
        and github.create_calls == 0,
    )
    check(
        "same-head: normal refresh semantics preserve current triage/verdict",
        state.get("triaged_sha") == current["head_sha"]
        and state.get("automerge_verdict") is not None,
    )
    check(
        "same-head: soft-close state is cleared on reuse",
        not rc.reconcile_absence_needs_clear(github.issues[7]["body"]),
    )


def test_new_head_reopens_and_drops_stale_analysis():
    old = item()
    github = LifecycleGitHub(old)
    add_triage_and_verdict(github.issues[7], old)
    old_state = core.parse_state_block(github.issues[7]["body"])
    old_state[rc.AUTOMERGE_CRITERIA_FIELD] = [{"forged": "old"}]
    old_state[rc.AUTOMERGE_CRITERIA_VERSION_FIELD] = 1
    github.issues[7]["body"] = rc._replace_state_block(
        github.issues[7]["body"], old_state
    )
    github.soft_close()

    current = item(head="d" * 40)
    github.run_reconcile(scan_payload([current]), token=True)
    state = core.parse_state_block(github.issues[7]["body"])
    check(
        "new-head: same issue is current and only current triage remains",
        github.issues[7]["state"] == "OPEN"
        and github.create_calls == 0
        and state.get("head_sha") == current["head_sha"]
        and state.get("triaged_sha") == current["head_sha"]
        and state.get("triage_status") == "queued"
        and "triage_recommendation" not in state,
    )
    check(
        "new-head: stale verdict, criteria, audit, and absence remnants are gone",
        "automerge_verdict" not in state
        and "automerge_audit_intent" not in state
        and "automerge_audit_pending" not in state
        and not rc.reconcile_absence_needs_clear(github.issues[7]["body"])
        and all(
            "forged" not in row
            for row in state.get(rc.AUTOMERGE_CRITERIA_FIELD, [])
        ),
    )
    check(
        "new-head: normal current-head triage lifecycle is queued and held",
        state.get("held") is True
        and rc.HOLD_LABEL in label_names(github.issues[7])
        and len(github.workflow_calls) == 1,
    )
    check(
        "new-head: no auto-merge eligibility before fresh triage",
        auto_merge.verdict_eligible(state.get("automerge_verdict"))[0] is False,
    )
    check(
        "new-head: target-updated warning is posted",
        any("Target updated: head moved" in comment["body"] for comment in github.issues[7]["comments"]),
    )


def test_census_head_mismatch_reuses_after_trusted_post_close_activity_once():
    """F1 separates the head move trigger, the old updatedAt mask, and the
    visible duplicate-card symptom from the proven same-head reuse path."""
    old = item(head="c7c2e78a" + "0" * 32)
    current = item(head="4d19b725" + "0" * 32)
    github = LifecycleGitHub(old)
    github.soft_close()
    github.post_close_activity(7)

    first = github.run_reconcile(scan_payload([current]))
    writes_after_first = github.issue_edit_calls
    github.run_reconcile(scan_payload([current]))
    check(
        "F1: trusted post-close automation no longer masks new-head reuse",
        github.issues[7]["state"] == "OPEN"
        and github.create_calls == 0
        and core.parse_state_block(github.issues[7]["body"])["head_sha"]
        == current["head_sha"]
        and "reopened card #7" in first,
    )
    check(
        "F1: repaired head-mismatch path converges on the second scan",
        github.issue_edit_calls == writes_after_first,
    )


def test_ci_approval_to_pr_review_reuses_issue():
    old = item(kind="ci-approval")
    github = LifecycleGitHub(old)
    github.soft_close()
    current = item(kind="pr-review")
    github.run_reconcile(scan_payload([current]))
    state = core.parse_state_block(github.issues[7]["body"])
    names = label_names(github.issues[7])
    check(
        "kind transition: closed CI card reopens as PR review on issue 7",
        github.issues[7]["state"] == "OPEN"
        and state.get("kind") == "pr-review"
        and github.create_calls == 0,
    )
    check(
        "kind transition: managed labels/body contain only current identity",
        "kind:pr-review" in names
        and "kind:ci-approval" not in names
        and len([name for name in names if name.startswith("target:")]) == 1
        and "opt:merge" in github.issues[7]["body"],
    )


def test_census_ci_approval_head_move_reuses_once():
    """F6 proves the non-model CI kind uses the same widened lifecycle path."""
    old = item(kind="ci-approval", head="339a3470" + "0" * 32)
    current = item(kind="ci-approval", head="2d8cd317" + "0" * 32)
    github = LifecycleGitHub(old)
    github.soft_close()
    github.post_close_activity(7)
    github.run_reconcile(scan_payload([current]))
    writes = github.issue_edit_calls
    github.run_reconcile(scan_payload([current]))
    check(
        "F6: CI-approval card reopens at the current head without creation",
        github.issues[7]["state"] == "OPEN"
        and github.create_calls == 0
        and core.parse_state_block(github.issues[7]["body"])["head_sha"]
        == current["head_sha"],
    )
    check("F6: CI-approval reuse converges", github.issue_edit_calls == writes)


def test_issue_updated_at_refresh_and_queue_write_ownership():
    old = item(
        kind="issue-triage",
        head="",
        updated_at="2026-07-15T14:05:50Z",
        bucket="issue-triage",
        comp="n/a",
        tests="n/a",
        priority="low",
        url="https://github.com/kunchenguid/wheelhouse/issues/42",
    )
    current = dict(old, updated_at="2026-07-15T14:07:59Z")

    ineligible = LifecycleGitHub(old)
    ineligible.run_reconcile(scan_payload([current]), token=False)
    ineligible_writes = ineligible.issue_edit_calls
    ineligible.run_reconcile(scan_payload([current]), token=False)
    ineligible_state = core.parse_state_block(ineligible.issues[7]["body"])
    check(
        "F4: ineligible issue advisory still gets one deterministic refresh",
        ineligible_state["updated_at"] == current["updated_at"]
        and ineligible.issue_edit_calls == ineligible_writes
        and ineligible.budget_reservations == 0
        and ineligible.workflow_calls == [],
    )

    eligible = LifecycleGitHub(old)
    eligible.run_reconcile(scan_payload([current]), token=True)
    eligible_state = core.parse_state_block(eligible.issues[7]["body"])
    check(
        "F4: eligible issue revision is owned by one queued write and dispatch",
        eligible.issue_edit_calls == 1
        and eligible_state["updated_at"] == current["updated_at"]
        and eligible_state["triage_status"] == "queued"
        and eligible.budget_reservations == 1
        and len(eligible.workflow_calls) == 1,
    )


def test_reconcile_and_ingest_share_reuse_operation():
    current = item(head="e" * 40)
    reconcile_side = LifecycleGitHub(item())
    reconcile_side.soft_close()
    reconcile_side.run_reconcile(scan_payload([current]))

    ingest_side = LifecycleGitHub(item())
    ingest_side.soft_close()
    ingest_number = ingest_side.event_upsert(current)
    check(
        "shared operation: reconcile and ingest both choose card 7",
        reconcile_side.issues[7]["state"] == "OPEN"
        and ingest_number == 7
        and ingest_side.issues[7]["state"] == "OPEN",
    )
    check(
        "shared operation: neither path mints another issue",
        reconcile_side.create_calls == 0 and ingest_side.create_calls == 0,
    )


def test_reopened_card_participates_in_normal_systems():
    current = item(head="f" * 40)
    current[rc.AUTOMERGE_CRITERIA_FIELD] = [
        {
            "id": "repo_opt_in",
            "label": "G0 - repository opt-in",
            "status": "met",
            "evidence": "enabled",
        }
    ]
    github = LifecycleGitHub(item())
    github.soft_close()
    github.run_reconcile(scan_payload([current]))

    refreshed = dict(current, priority="high")
    number = github.event_upsert(refreshed)
    issue = github.issues[7]
    state = core.parse_state_block(issue["body"])
    index = auto_merge._card_index(github.cards_snapshot())
    decision, _ = apply_decision.parse_slash(
        "/merge", apply_decision.ALLOWED[state["kind"]]
    )
    check(
        "normal participation: reopened card refreshes in place",
        number == 7
        and state.get("priority") == "high"
        and "priority:high" in label_names(issue),
    )
    check(
        "normal participation: decision and trusted-card indexing recognize it",
        decision == "merge" and index[("wheelhouse", "42")]["issue"] == 7,
    )
    check(
        "normal participation: triage and criteria use current state",
        rc.should_auto_triage(refreshed, state, issue["labels"], has_token=True)
        and rc.automerge_criteria_stale(refreshed, state) is False,
    )


def _remove_provenance(github):
    github.issues[7]["body"] = rc.body_without_reconcile_absence(
        github.issues[7]["body"]
    )


def test_forbidden_candidates_never_reopen():
    mutations = {
        "explicit owner resolution": _remove_provenance,
        "declined decision": _remove_provenance,
        "auto-merged resolution": _remove_provenance,
        "manual author": lambda github: github.issues[7].update(author="owner"),
        "owner close actor": lambda github: github.issues[7].update(closed_by="owner"),
        "post-close owner edit": lambda github: github._touch(github.issues[7]),
        "blocked": lambda github: github.issues[7].update(
            labels=label_objects(label_names(github.issues[7]) | {"blocked"})
        ),
        "held": lambda github: github.issues[7].update(
            body=rc._replace_state_block(
                github.issues[7]["body"],
                dict(core.parse_state_block(github.issues[7]["body"]), held=True),
            )
        ),
        "audit intent": lambda github: github.issues[7].update(
            body=rc._replace_state_block(
                github.issues[7]["body"],
                dict(
                    core.parse_state_block(github.issues[7]["body"]),
                    automerge_audit_intent={"card_issue": 7},
                ),
            )
        ),
        "pending audit": lambda github: github.issues[7].update(
            body=rc._replace_state_block(
                github.issues[7]["body"],
                dict(
                    core.parse_state_block(github.issues[7]["body"]),
                    automerge_audit_pending={"card_issue": 7},
                ),
            )
        ),
    }
    all_closed = True
    for name, mutate in mutations.items():
        github = LifecycleGitHub(item())
        github.soft_close()
        mutate(github)
        candidate = github.normalized(7)
        try:
            eligible, _reason = rc.reusable_closed_card(candidate, item())
        except rc.CardLifecycleError:
            eligible = False
        all_closed = all_closed and not eligible and github.issues[7]["state"] == "CLOSED"
    check(
        "forbidden resolved/declined/auto-merged/owner/blocked/held/audit cases stay closed",
        all_closed,
    )


def test_hard_close_clears_reuse_provenance():
    github = LifecycleGitHub(item())
    github.run_reconcile(scan_payload([]))
    check(
        "hard-close setup: first absence exists",
        rc.reconcile_absence_count(github.issues[7]["body"]) == 1,
    )
    github.run_reconcile(scan_payload([], open_target=False))
    check(
        "hard close: target closure consumes immediately without reuse provenance",
        github.issues[7]["state"] == "CLOSED"
        and not rc.reconcile_absence_needs_clear(github.issues[7]["body"]),
    )


def test_legacy_card_is_not_guessed_reusable():
    github = LifecycleGitHub(item())
    github.soft_close()
    github.issues[7]["body"] = rc.body_without_reconcile_absence(
        github.issues[7]["body"]
    )
    number = github.event_upsert(item(head="1" * 40))
    check(
        "legacy: card without provenance remains closed",
        github.issues[7]["state"] == "CLOSED",
    )
    check(
        "legacy: current safe create behavior remains available",
        number == 8 and github.issues[8]["state"] == "OPEN" and github.create_calls == 1,
    )


def test_multiple_candidates_select_highest_and_incomplete_lookup_fails():
    duplicate = LifecycleGitHub(item())
    duplicate.soft_close()
    clone = copy.deepcopy(duplicate.issues[7])
    clone["number"] = 8
    duplicate.issues[8] = clone
    duplicate.next_number = 9
    selected = duplicate.event_upsert(item(head="2" * 40))

    incomplete = LifecycleGitHub(item())
    incomplete.soft_close()
    incomplete.fail_list_state = "CLOSED"
    incomplete_failed = False
    try:
        incomplete.event_upsert(item(head="3" * 40))
    except rc.CardLifecycleError:
        incomplete_failed = True
    check(
        "multiple trusted candidates: highest number is selected deterministically",
        selected == 8
        and duplicate.create_calls == 0
        and duplicate.issues[8]["state"] == "OPEN"
        and duplicate.issues[7]["state"] == "CLOSED",
    )
    check(
        "ambiguity: incomplete closed lookup fails closed without creation",
        incomplete_failed
        and incomplete.create_calls == 0
        and incomplete.issues[7]["state"] == "CLOSED",
    )


def test_post_close_timeline_refuses_human_unreadable_and_incomplete_history():
    outcomes = []

    human = LifecycleGitHub(item())
    human.soft_close()
    human.post_close_activity(7, actor="owner")
    outcomes.append(
        human._with_boundary(
            lambda: rc.reusable_closed_card(human.normalized(7), item())[0]
        )
    )

    unreadable = LifecycleGitHub(item())
    unreadable.soft_close()
    unreadable.post_close_activity(7)
    unreadable.timeline_failures.add(7)
    outcomes.append(
        unreadable._with_boundary(
            lambda: rc.reusable_closed_card(unreadable.normalized(7), item())[0]
        )
    )

    incomplete = LifecycleGitHub(item())
    incomplete.soft_close()
    incomplete.post_close_activity(7)
    incomplete.issues[7]["timeline"] = [
        {
            "event": "labeled",
            "created_at": incomplete.issues[7]["updated_at"],
            "actor": {"login": rc.CARD_AUTOMATION_AUTHOR},
        }
    ] * (
        rc.POST_CLOSE_TIMELINE_PAGE_SIZE * rc.POST_CLOSE_TIMELINE_MAX_PAGES
    )
    outcomes.append(
        incomplete._with_boundary(
            lambda: rc.reusable_closed_card(incomplete.normalized(7), item())[0]
        )
    )
    check(
        "post-close trust: human, unreadable, and incomplete histories refuse reuse",
        outcomes == [False, False, False],
    )


def test_malformed_target_marker_fails_without_create():
    github = LifecycleGitHub(item())
    github.soft_close()
    state = core.parse_state_block(github.issues[7]["body"])
    state["repo"] = "different-repo"
    github.issues[7]["body"] = rc._replace_state_block(github.issues[7]["body"], state)
    failed = False
    try:
        github.event_upsert(item())
    except rc.CardLifecycleError:
        failed = True
    check(
        "malformed target identity fails closed without creating a card",
        failed and github.create_calls == 0 and github.issues[7]["state"] == "CLOSED",
    )


def test_candidate_identity_disagreement_remains_an_error():
    github = LifecycleGitHub(item())
    github.soft_close()
    conflicting = copy.deepcopy(github.issues[7])
    conflicting["number"] = 8
    state = core.parse_state_block(conflicting["body"])
    state["number"] = 99
    conflicting["body"] = rc._replace_state_block(conflicting["body"], state)
    github.issues[8] = conflicting
    failed = False
    try:
        github.event_upsert(item(head="5" * 40))
    except rc.CardLifecycleError:
        failed = True
    check(
        "candidate identity disagreement fails closed without creation",
        failed
        and github.create_calls == 0
        and all(issue["state"] == "CLOSED" for issue in github.issues.values()),
    )


def test_live_races_stop_reuse_before_mutation():
    outcomes = []

    owner_action = LifecycleGitHub(item())
    owner_action.soft_close()
    candidate = owner_action.normalized(7)
    owner_action.issues[7]["comments"].append(
        {"body": "I am resolving this", "author": {"login": "owner"}}
    )
    owner_action._touch(owner_action.issues[7])
    try:
        owner_action._with_boundary(
            lambda: rc.reuse_closed_card(item(), candidate, has_token=False)
        )
    except rc.CardLifecycleError:
        outcomes.append(owner_action.issues[7]["state"] == "CLOSED")

    now_open = LifecycleGitHub(item())
    now_open.soft_close()
    candidate = now_open.normalized(7)
    now_open.issues[7].update(state="OPEN", closed_at="", closed_by="")
    now_open._touch(now_open.issues[7])
    try:
        now_open._with_boundary(
            lambda: rc.reuse_closed_card(item(), candidate, has_token=False)
        )
    except rc.CardLifecycleError:
        outcomes.append(now_open.issues[7]["state"] == "OPEN")

    provenance_change = LifecycleGitHub(item())
    provenance_change.soft_close()
    candidate = provenance_change.normalized(7)
    provenance_change.issues[7]["body"] = rc.body_without_reconcile_absence(
        provenance_change.issues[7]["body"]
    )
    provenance_change._touch(provenance_change.issues[7])
    try:
        provenance_change._with_boundary(
            lambda: rc.reuse_closed_card(item(), candidate, has_token=False)
        )
    except rc.CardLifecycleError:
        outcomes.append(provenance_change.issues[7]["state"] == "CLOSED")

    check(
        "live races: owner activity, a now-open issue, and changed provenance stop reuse",
        outcomes == [True, True, True]
        and owner_action.create_calls == 0
        and now_open.create_calls == 0
        and provenance_change.create_calls == 0,
    )


def test_partial_prepare_failures_stay_closed_non_actionable():
    safe = True
    for mode in ("before", "body-only", "labels-only"):
        github = LifecycleGitHub(item())
        github.soft_close()
        github.fail_prepare = mode
        try:
            github.event_upsert(item(head="4" * 40))
        except RuntimeError:
            pass
        issue = github.issues[7]
        safe = safe and issue["state"] == "CLOSED" and github.create_calls == 0
    check(
        "partial body/label failures leave the reused issue closed and non-actionable",
        safe,
    )


def test_serialization_and_sequential_race_converge_to_one_card():
    github = LifecycleGitHub(item())
    github.soft_close()
    current = item(head="5" * 40)
    first = github.event_upsert(current)
    second = github.event_upsert(current)
    open_numbers = [
        number for number, issue in github.issues.items() if issue["state"] == "OPEN"
    ]
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    with open(os.path.join(root, ".github", "workflows", "ingest.yml")) as source:
        ingest_workflow = source.read()
    with open(
        os.path.join(root, ".github", "workflows", "scan-backstop.yml")
    ) as source:
        backstop_workflow = source.read()
    check(
        "serialization: ingest and backstop share the global lifecycle group",
        "group: wheelhouse-backstop" in ingest_workflow
        and "group: wheelhouse-backstop" in backstop_workflow,
    )
    check(
        "serialization: sequential contenders converge to one open issue",
        first == second == 7 and open_numbers == [7] and github.create_calls == 0,
    )


def test_post_operation_uniqueness_rolls_back_local_reopen():
    github = LifecycleGitHub(item())
    github.soft_close()
    github.inject_duplicate_on_reopen = True
    failed = False
    try:
        github.event_upsert(item(head="6" * 40))
    except rc.CardLifecycleError:
        failed = True
    open_numbers = [
        number for number, issue in github.issues.items() if issue["state"] == "OPEN"
    ]
    check(
        "post verification: ambiguity is detected",
        failed,
    )
    check(
        "post verification: the locally reopened card is rolled back closed",
        github.issues[7]["state"] == "CLOSED"
        and "needs-decision" not in label_names(github.issues[7])
        and "needs-decision" not in label_names(github.issues[8])
        and open_numbers == [8],
    )


def test_post_reopen_read_failure_rolls_back_local_reopen():
    github = LifecycleGitHub(item())
    github.soft_close()
    github.fail_open_list_after_reopen = True
    failed = False
    try:
        github.event_upsert(item(head="7" * 40))
    except rc.CardLifecycleError:
        failed = True
    check(
        "post-reopen read failure: verification fails closed",
        failed,
    )
    check(
        "post-reopen read failure: the locally reopened card is rolled back",
        github.issues[7]["state"] == "CLOSED"
        and "resolved" in label_names(github.issues[7])
        and "needs-decision" not in label_names(github.issues[7])
        and github.create_calls == 0,
    )
    check(
        "post-reopen read failure: staging never exposes decision labels",
        github.labels_on_reopen
        and "resolved" in github.labels_on_reopen[-1]
        and "needs-decision" not in github.labels_on_reopen[-1],
    )


def test_ambiguous_reopen_response_forces_inert_card_closed():
    github = LifecycleGitHub(item())
    github.soft_close()
    github.fail_reopen_after_mutation = True
    failed = False
    try:
        github.event_upsert(item(head="8" * 40))
    except rc.CardLifecycleError:
        failed = True
    check(
        "ambiguous reopen: local issue is closed and inert",
        failed
        and github.issues[7]["state"] == "CLOSED"
        and "resolved" in label_names(github.issues[7])
        and "needs-decision" not in label_names(github.issues[7]),
    )


def test_auto_merge_duplicate_and_authorization_gates_unchanged():
    github = LifecycleGitHub(item())
    first = github.cards_snapshot()[0]
    second = copy.deepcopy(first)
    second["number"] = 8
    index = auto_merge._card_index([first, second])
    state = core.parse_state_block(first["body"])
    state[rc.RECONCILE_ABSENCE_FIELD] = {
        "version": rc.RECONCILE_ABSENCE_VERSION,
        "threshold": rc.RECONCILE_ABSENCE_THRESHOLD,
        "count": 1,
        "run_number": 1,
    }
    denial_only = auto_merge.verdict_eligible(state.get("automerge_verdict"))[0]
    check(
        "auto-merge: duplicate trusted open identities remain absent from index",
        ("wheelhouse", "42") not in index,
    )
    check(
        "auto-merge: lifecycle provenance cannot authorize a behavior verdict",
        denial_only is False,
    )


def test_full_lifecycle_wait_then_new_head():
    old = item()
    github = LifecycleGitHub(old)
    add_triage_and_verdict(github.issues[7], old)
    check(
        "full lifecycle: scan starts merge-ready",
        old["bucket"] == "merge-ready",
    )
    waiting_bucket = core.classify(
        False,
        "pass",
        "green",
        True,
        cross_repo=False,
        mergeable="CONFLICTING",
    )
    check("full lifecycle: conflict enters waiting phase", waiting_bucket == "needs-rebase")
    github.soft_close()
    github.run_reconcile(scan_payload([]))
    check(
        "full lifecycle: waiting phase leaves the machine-soft-closed card closed",
        github.issues[7]["state"] == "CLOSED",
    )

    current = item(head="7" * 40)
    github.run_reconcile(scan_payload([current]))
    issue = github.issues[7]
    state = core.parse_state_block(issue["body"])
    target_labels = [name for name in label_names(issue) if name.startswith("target:")]
    before_fresh = auto_merge.verdict_eligible(state.get("automerge_verdict"))[0]
    check(
        "full lifecycle: one issue, one target label, one open card",
        set(github.issues) == {7}
        and issue["state"] == "OPEN"
        and target_labels == ["target:wheelhouse-42"],
    )
    check(
        "full lifecycle: new-head return has no stale triage or merge eligibility",
        "triaged_sha" not in state
        and "automerge_verdict" not in state
        and before_fresh is False,
    )

    add_triage_and_verdict(issue, current)
    fresh_state = core.parse_state_block(issue["body"])
    after_fresh = auto_merge.verdict_eligible(
        fresh_state.get("automerge_verdict")
    )[0]
    check(
        "full lifecycle: a current-head successful triage can restore verdict eligibility",
        fresh_state.get("triaged_sha") == current["head_sha"] and after_fresh is True,
    )


def test_list_lag_create_is_retained_and_queued_once():
    """Production shape: create response + direct read OK, open-list lag 0..30s+."""
    # Probe sleeps advance fake time by up to ~(ATTEMPTS-1)*DELAY (~0.5s).
    probe_budget = (rc.LIFECYCLE_VERIFY_ATTEMPTS - 1) * rc.LIFECYCLE_VERIFY_DELAY_SECONDS
    for lag in (0.0, 0.5, 5.0, 30.0, 45.0):
        github = LifecycleGitHub(start_empty=True)
        github.list_index_lag_seconds = lag
        github.search_index_lag_seconds = lag
        current = item(head=("a" if lag == 0 else "b") * 40)
        # Isolate each lag case on a distinct target so markers never collide.
        current["number"] = 100 + int(lag * 10)
        current["url"] = "https://github.com/kunchenguid/wheelhouse/pull/%s" % (
            current["number"],
        )
        closes_before = github.close_calls
        creates_before = github.create_calls
        with io.StringIO() as buf, redirect_stdout(buf):
            number = github.event_upsert(current, has_token=True)
            admission_out = buf.getvalue()
        issue = github.issues[number]
        state = core.parse_state_block(issue["body"])
        check(
            "list-lag %.1fs: create retained open without rollback" % lag,
            number is not None
            and issue["state"] == "OPEN"
            and github.close_calls == closes_before
            and github.create_calls == creates_before + 1
            and "resolved" not in label_names(issue)
            and "card-admission rollback" not in admission_out,
        )
        expect_lag_notice = lag > probe_budget
        check(
            "list-lag %.1fs: direct-read admission succeeds for held agent card" % lag,
            state.get("held") is True
            and rc.HOLD_LABEL in label_names(issue)
            and "needs-decision" in label_names(issue)
            and "card-admission direct_ok" in admission_out
            and (
                ("list_index_lag" in admission_out)
                if expect_lag_notice
                else (
                    "card-admission unique" in admission_out
                    or "list_index_lag" in admission_out
                )
            ),
        )

        # After index convergence, same target is stable: no mint/close churn.
        github.advance_time(lag + 1.0)
        closes_mid = github.close_calls
        creates_mid = github.create_calls
        again = github.event_upsert(current, has_token=True)
        check(
            "list-lag %.1fs: repeated upsert after convergence does not mint or close"
            % lag,
            again == number
            and github.create_calls == creates_mid
            and github.close_calls == closes_mid
            and github.issues[number]["state"] == "OPEN",
        )

        out = github.run_reconcile(scan_payload([current]), token=True)
        open_numbers = [
            n for n, iss in github.issues.items() if iss["state"] == "OPEN"
        ]
        check(
            "list-lag %.1fs: eventually one open card and no create churn" % lag,
            open_numbers == [number]
            and github.create_calls == creates_mid
            and "0 admission rollback(s)" in out
            and "destructive card-admission rollback" not in out,
        )


def test_list_lag_never_rolls_back_valid_create():
    github = LifecycleGitHub(start_empty=True)
    github.list_index_lag_seconds = 30.0
    github.search_index_lag_seconds = 60.0
    current = item()
    with io.StringIO() as buf, redirect_stdout(buf):
        number = github.event_upsert(current, has_token=False)
        out = buf.getvalue()
    issue = github.issues[number]
    check(
        "list lag alone never labels resolved or closes",
        issue["state"] == "OPEN"
        and "resolved" not in label_names(issue)
        and github.close_calls == 0
        and "list_index_lag" in out
        and "card-admission rollback" not in out,
    )
    check(
        "deterministic CI path also survives list lag",
        github.event_upsert(item(kind="ci-approval", number=99, head="c" * 40))
        and all(
            iss["state"] == "OPEN" and "resolved" not in label_names(iss)
            for iss in github.issues.values()
        ),
    )


def test_trusted_open_duplicate_before_or_after_create_fails_closed():
    # Existing trusted open card before create: lookup finds it, no create.
    existing = item(head="d" * 40)
    github = LifecycleGitHub(existing)
    number = github.event_upsert(existing, has_token=True)
    check(
        "pre-existing open card is reused without a second create",
        number == 7 and github.create_calls == 0 and github.issues[7]["state"] == "OPEN",
    )

    # Peer becomes list-visible after create while new card is still lagging.
    racing = LifecycleGitHub(start_empty=True)
    racing.list_index_lag_seconds = 30.0
    racing.inject_open_duplicate_after_create = True
    failed = False
    outcome = None
    try:
        racing.event_upsert(item(head="e" * 40), has_token=True)
    except rc.CardAdmissionError as error:
        failed = True
        outcome = error.outcome
        should_rollback = error.should_rollback
    created = [
        n for n, iss in racing.issues.items() if iss.get("body")
    ]
    closed_resolved = [
        n
        for n, iss in racing.issues.items()
        if iss["state"] == "CLOSED" and "resolved" in label_names(iss)
    ]
    check(
        "post-create observed duplicate fails closed with rollback of new card",
        failed
        and outcome == rc.CARD_ADMISSION_DUPLICATE
        and should_rollback is True
        and closed_resolved
        and racing.close_calls >= 1,
    )
    # Exactly one peer may remain open (the injected duplicate).
    open_after = [n for n, iss in racing.issues.items() if iss["state"] == "OPEN"]
    check(
        "post-create duplicate leaves only the alternate open peer",
        len(open_after) == 1 and open_after[0] not in closed_resolved,
    )
    del created  # silence unused in strict linters


def test_malformed_direct_objects_fail_closed():
    cases = []

    def run_case(name, mutate_issue, expect_resolved=True, expected_state="CLOSED"):
        github = LifecycleGitHub(start_empty=True)
        current = item(head="f" * 40)
        card = rc.render(current, held=False)
        created = {}
        original_create = rc._create_card

        def create_then_poison(rendered):
            number = original_create(rendered)
            created["number"] = number
            mutate_issue(github, number)
            return number

        failed = False
        outcome = None
        should_rollback = None
        try:

            def attempt():
                nonlocal failed, outcome, should_rollback
                rc._create_card = create_then_poison
                try:
                    return rc._create_and_verify_card(current, card)
                except rc.CardAdmissionError as error:
                    failed = True
                    outcome = error.outcome
                    should_rollback = error.should_rollback
                    raise
                finally:
                    rc._create_card = original_create

            github._with_boundary(attempt)
        except rc.CardAdmissionError:
            pass
        except Exception:
            rc._create_card = original_create
            raise
        finally:
            rc._create_card = original_create
        number = created.get("number")
        issue = github.issues.get(number) if number else None
        ok = (
            failed
            and outcome == rc.CARD_ADMISSION_MALFORMED
            and should_rollback is True
            and issue is not None
            and issue["state"] == expected_state
            and (not expect_resolved or "resolved" in label_names(issue))
        )
        cases.append((name, ok))

    def close_direct(github, number):
        # Already closed before rollback runs - fail closed on direct read.
        github.issues[number]["state"] = "CLOSED"
        github.issues[number]["closed_at"] = "2026-07-13T14:00:00Z"
        github.issues[number]["closed_by"] = rc.CARD_AUTOMATION_AUTHOR

    def wrong_author(github, number):
        github.issues[number]["author"] = "human-contributor"

    def wrong_target(github, number):
        state = core.parse_state_block(github.issues[number]["body"])
        state["repo"] = "other-repo"
        github.issues[number]["body"] = rc._replace_state_block(
            github.issues[number]["body"], state
        )
        names = label_names(github.issues[number])
        names.discard("repo:wheelhouse")
        names.discard("target:wheelhouse-42")
        names.add("repo:other-repo")
        names.add("target:other-repo-42")
        github.issues[number]["labels"] = label_objects(names)

    def wrong_kind(github, number):
        state = core.parse_state_block(github.issues[number]["body"])
        state["kind"] = "ci-approval"
        github.issues[number]["body"] = rc._replace_state_block(
            github.issues[number]["body"], state
        )
        names = label_names(github.issues[number])
        names.discard("kind:pr-review")
        names.add("kind:ci-approval")
        github.issues[number]["labels"] = label_objects(names)

    def body_mismatch(github, number):
        github.issues[number]["body"] = (
            github.issues[number]["body"] + "\n<!-- tampered -->\n"
        )

    run_case("closed", close_direct, expect_resolved=False)
    run_case("untrusted author", wrong_author)
    run_case("wrong target", wrong_target, expect_resolved=False, expected_state="OPEN")
    run_case("wrong kind", wrong_kind, expect_resolved=False, expected_state="OPEN")
    run_case("body mismatch", body_mismatch, expect_resolved=False, expected_state="OPEN")
    check(
        "malformed direct objects preserve changed bodies and close unchanged creates",
        all(ok for _name, ok in cases),
    )
    if not all(ok for _name, ok in cases):
        for name, ok in cases:
            if not ok:
                print("  detail FAIL malformed case: %s" % name)


def test_thirty_sequential_and_burst_creates_under_list_lag():
    github = LifecycleGitHub(start_empty=True)
    github.list_index_lag_seconds = 30.0
    github.search_index_lag_seconds = 30.0
    created_numbers = []
    for index in range(30):
        current = item(
            head=("%x" % (index % 16)) * 40,
            number=200 + index,
            title="PR %s" % (200 + index),
        )
        current["url"] = "https://github.com/kunchenguid/wheelhouse/pull/%s" % (
            current["number"],
        )
        number = github.event_upsert(current, has_token=(index % 2 == 0))
        created_numbers.append(number)
        check(
            "sequential create %s retained open under 30s lag" % (index + 1),
            number is not None
            and github.issues[number]["state"] == "OPEN"
            and "resolved" not in label_names(github.issues[number]),
        )
    check(
        "30 sequential creates: no close/rollback churn",
        github.create_calls == 30
        and github.close_calls == 0
        and len(set(created_numbers)) == 30,
    )

    # Burst/concurrent-shaped: three waves of 10 distinct targets, lag still on.
    burst_numbers = []
    for wave in range(3):
        wave_items = []
        for offset in range(10):
            n = 500 + wave * 10 + offset
            wave_items.append(
                item(
                    head=("%x" % ((wave + offset) % 16)) * 40,
                    number=n,
                    title="burst %s" % n,
                    url="https://github.com/kunchenguid/wheelhouse/pull/%s" % n,
                )
            )
        for current in wave_items:
            burst_numbers.append(github.event_upsert(current, has_token=True))
    check(
        "burst creates under lag: all retained, no mint/close churn pair",
        github.create_calls == 60
        and github.close_calls == 0
        and all(
            github.issues[n]["state"] == "OPEN" and "resolved" not in label_names(github.issues[n])
            for n in burst_numbers
        ),
    )

    # Index converges; repeated scheduled scans do not re-mint.
    github.advance_time(31.0)
    creates_after = github.create_calls
    scan_items = [
        item(
            head="1" * 40,
            number=200 + index,
            url="https://github.com/kunchenguid/wheelhouse/pull/%s" % (200 + index),
        )
        for index in range(30)
    ]
    github.run_reconcile(
        {
            "repos": {
                "wheelhouse": {
                    "ok": True,
                    "truncated": False,
                    "open_pr_numbers": [it["number"] for it in scan_items],
                    "open_issue_numbers": [],
                    "indeterminate_pr_numbers": [],
                    "ci_wait_pr_numbers": [],
                    "ci_wait_refresh_items": [],
                }
            },
            "items": scan_items,
        },
        token=True,
    )
    github.run_reconcile(
        {
            "repos": {
                "wheelhouse": {
                    "ok": True,
                    "truncated": False,
                    "open_pr_numbers": [it["number"] for it in scan_items],
                    "open_issue_numbers": [],
                    "indeterminate_pr_numbers": [],
                    "ci_wait_pr_numbers": [],
                    "ci_wait_refresh_items": [],
                }
            },
            "items": scan_items,
        },
        token=True,
    )
    check(
        "repeated scans after lag: no additional creates for the same 30 targets",
        github.create_calls == creates_after,
    )


def test_list_error_defers_without_destructive_rollback():
    github = LifecycleGitHub(start_empty=True)
    # Lookup must succeed (empty open list); only the post-create list probe fails.
    github.fail_open_list_after_create = True
    current = item(head="9" * 40)
    failed = False
    outcome = None
    should_rollback = None
    number = None
    try:
        github.event_upsert(current, has_token=True)
    except rc.CardAdmissionError as error:
        failed = True
        outcome = error.outcome
        should_rollback = error.should_rollback
        number = error.number
    check(
        "incomplete open-list probe defers without rollback",
        failed
        and outcome == rc.CARD_ADMISSION_RETAINED_DEFERRED
        and should_rollback is False
        and number is not None
        and github.close_calls == 0
        and github.issues[number]["state"] == "OPEN"
        and "resolved" not in label_names(github.issues[number]),
    )


def test_ci_and_held_agent_cards_under_list_lag():
    github = LifecycleGitHub(start_empty=True)
    github.list_index_lag_seconds = 30.0
    pr_item = item(kind="pr-review", number=701, head="1" * 40)
    pr_item["url"] = "https://github.com/kunchenguid/wheelhouse/pull/701"
    issue_item = item(
        kind="issue-triage",
        number=702,
        head="",
        updated_at="2026-07-13T15:00:00Z",
        bucket="needs-triage",
        comp="none",
        tests="none",
        url="https://github.com/kunchenguid/wheelhouse/issues/702",
    )
    ci_item = item(kind="ci-approval", number=703, head="2" * 40, priority="high")
    ci_item["url"] = "https://github.com/kunchenguid/wheelhouse/pull/703"

    pr_number = github.event_upsert(pr_item, has_token=True)
    issue_number = github.event_upsert(issue_item, has_token=True)
    ci_number = github.event_upsert(ci_item, has_token=False)

    pr_state = core.parse_state_block(github.issues[pr_number]["body"])
    issue_state = core.parse_state_block(github.issues[issue_number]["body"])
    ci_state = core.parse_state_block(github.issues[ci_number]["body"])
    check(
        "held agent pr/issue cards survive list lag",
        pr_state.get("held") is True
        and issue_state.get("held") is True
        and rc.HOLD_LABEL in label_names(github.issues[pr_number])
        and rc.HOLD_LABEL in label_names(github.issues[issue_number])
        and github.close_calls == 0,
    )
    check(
        "deterministic ci-approval card survives list lag without held state",
        ci_state.get("held") is not True
        and rc.HOLD_LABEL not in label_names(github.issues[ci_number])
        and github.issues[ci_number]["state"] == "OPEN"
        and github.close_calls == 0,
    )


def main():
    test_same_head_reopens_same_issue()
    test_new_head_reopens_and_drops_stale_analysis()
    test_census_head_mismatch_reuses_after_trusted_post_close_activity_once()
    test_ci_approval_to_pr_review_reuses_issue()
    test_census_ci_approval_head_move_reuses_once()
    test_issue_updated_at_refresh_and_queue_write_ownership()
    test_reconcile_and_ingest_share_reuse_operation()
    test_reopened_card_participates_in_normal_systems()
    test_forbidden_candidates_never_reopen()
    test_hard_close_clears_reuse_provenance()
    test_legacy_card_is_not_guessed_reusable()
    test_multiple_candidates_select_highest_and_incomplete_lookup_fails()
    test_post_close_timeline_refuses_human_unreadable_and_incomplete_history()
    test_malformed_target_marker_fails_without_create()
    test_candidate_identity_disagreement_remains_an_error()
    test_live_races_stop_reuse_before_mutation()
    test_partial_prepare_failures_stay_closed_non_actionable()
    test_serialization_and_sequential_race_converge_to_one_card()
    test_post_operation_uniqueness_rolls_back_local_reopen()
    test_post_reopen_read_failure_rolls_back_local_reopen()
    test_ambiguous_reopen_response_forces_inert_card_closed()
    test_auto_merge_duplicate_and_authorization_gates_unchanged()
    test_full_lifecycle_wait_then_new_head()
    test_list_lag_create_is_retained_and_queued_once()
    test_list_lag_never_rolls_back_valid_create()
    test_trusted_open_duplicate_before_or_after_create_fails_closed()
    test_malformed_direct_objects_fail_closed()
    test_thirty_sequential_and_burst_creates_under_list_lag()
    test_list_error_defers_without_destructive_rollback()
    test_ci_and_held_agent_cards_under_list_lag()
    if _failures:
        print("\n%d failure(s): %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("\nall card-reuse tests passed")


if __name__ == "__main__":
    main()
