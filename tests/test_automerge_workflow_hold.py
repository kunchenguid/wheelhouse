#!/usr/bin/env python3
"""End-to-end durable auto-merge workflow-history hold regressions."""

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)
import wheelhouse_core as core  # noqa: E402
import render_card  # noqa: E402
import apply_decision  # noqa: E402
import auto_merge as am  # noqa: E402
import automerge_criteria as criteria_schema  # noqa: E402
import reconcile  # noqa: E402

_failures = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        _failures.append(name)


HEAD_ONE = "a" * 40
HEAD_TWO = "d" * 40
HEAD_THREE = "e" * 40
BASE_SHA = "b" * 40
VISION_SHA = "c" * 40
HISTORY_COMMIT = "f" * 40


def eligible_verdict():
    return {
        "behavior_class": "A",
        "aligns_with_vision": True,
        "changes_existing_or_default_behavior": False,
        "recommend_merge": True,
        "vision_sha": VISION_SHA,
        "base_sha": BASE_SHA,
    }


def item_for(head, kind="pr-review"):
    return {
        "repo": "fmt",
        "number": 5,
        "kind": kind,
        "bucket": "merge-ready" if kind == "pr-review" else "needs-ci-approval",
        "head_sha": head,
        "comp": "pass",
        "tests": "green",
        "priority": "med",
        "options": ["merge", "close", "investigate", "hold"],
        "updated_at": "2026-07-13T00:00:00Z",
        "author": "alice",
        "title": "history-only workflow touch",
        "summary": "compliance=pass tests=green",
        "recommendation": "Merge - compliance and tests are green.",
        "url": "https://github.com/owner/fmt/pull/5",
    }


def scan_for(item, open_target=True):
    return {
        "items": [item] if open_target else [],
        "repos": {
            "fmt": {
                "ok": True,
                "truncated": False,
                "open_pr_numbers": [5] if open_target else [],
                "open_issue_numbers": [],
            }
        },
    }


def state_with_fresh_verdict(body, head):
    state = core.parse_state_block(body)
    state.update(
        {
            "triaged_sha": head,
            "triage_status": "succeeded",
            "triage_recommendation": {"action": "merge", "reason": ""},
            "automerge_verdict": eligible_verdict(),
        }
    )
    return render_card._replace_state_block(body, state)


class LifecycleWorld:
    def __init__(self, head=HEAD_ONE, history_mode="history"):
        self.head = head
        self.history_mode = history_mode
        self.clock = 0
        self.fail_hold_body_writes = False
        self.on_label_metadata_create = None
        self.after_hold_card_edit = None
        self.metrics = {
            "history_reads": 0,
            "merge_calls": 0,
            "claim_label_writes": 0,
            "audit_intent_writes": 0,
            "hold_body_writes": 0,
            "hold_label_writes": 0,
            "hold_atomic_writes": 0,
            "comments": 0,
        }
        self.card_write_tokens = []
        self.card_read_tokens = []
        self.history_read_tokens = []
        self.merge_tokens = []
        self.closed_cards = []
        rendered = render_card.render(item_for(head))
        self.card = {
            "number": 101,
            "body": state_with_fresh_verdict(rendered["body"], head),
            "labels": [{"name": label} for label in rendered["labels"]],
            "author": render_card.CARD_AUTOMATION_AUTHOR,
            "updatedAt": "2026-07-13T00:00:00Z",
            "comments": [],
            "state": "OPEN",
            "title": "card",
        }
        self.pr = {
            "head": {"sha": head},
            "base": {"sha": BASE_SHA},
            "mergeable": True,
            "mergeable_state": "clean",
            "additions": 10,
            "deletions": 5,
            "changed_files": 1,
            "commits": 1,
            "user": {"login": "alice", "type": "User"},
            "labels": [],
            "merged": False,
            "state": "open",
            "html_url": "https://github.com/owner/fmt/pull/5",
            "merge_commit_sha": "9" * 40,
        }
        self._saved = {}
        self._old_env = {}

    def touch(self):
        self.clock += 1
        self.card["updatedAt"] = "2026-07-13T00:%02d:%02dZ" % (
            self.clock // 60,
            self.clock % 60,
        )

    def card_snapshot(self):
        return {
            "number": self.card["number"],
            "body": self.card["body"],
            "updated_at": self.card["updatedAt"],
            "author": render_card.CARD_AUTOMATION_AUTHOR,
            "comments": len(self.card["comments"]),
            "labels": copy.deepcopy(self.card["labels"]),
            "title": self.card["title"],
        }

    def get_card(self, number):
        if int(number) != self.card["number"]:
            return None
        self.card_read_tokens.append(os.environ.get("GH_TOKEN"))
        result = copy.deepcopy(self.card)
        result["author"] = {"login": render_card.GET_CARD_AUTOMATION_AUTHOR}
        return result

    def gh(self, args, check=True, input_text=None):
        token = os.environ.get("GH_TOKEN")
        if args[:2] == ["issue", "edit"]:
            before = core.parse_state_block(self.card["body"]) or {}
            body = self.card["body"]
            if "--body-file" in args:
                body_path = args[args.index("--body-file") + 1]
                with open(body_path, encoding="utf-8") as handle:
                    body = handle.read()
            after = core.parse_state_block(body) or {}
            adding_hold = (
                render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD not in before
                and render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD in after
            )
            if adding_hold and self.fail_hold_body_writes:
                raise RuntimeError("simulated manual-hold persistence failure")
            self.card["body"] = body
            additions = [args[i + 1] for i, value in enumerate(args) if value == "--add-label"]
            removals = [
                args[i + 1] for i, value in enumerate(args) if value == "--remove-label"
            ]
            names = {label["name"] for label in self.card["labels"]}
            names.update(additions)
            names.difference_update(removals)
            self.card["labels"] = [{"name": name} for name in sorted(names)]
            if {"processing", am.AUTO_MERGE_CLAIM_LABEL} & (
                set(additions) | set(removals)
            ):
                self.metrics["claim_label_writes"] += 1
            if render_card.AUTOMERGE_WORKFLOW_HOLD_LABEL in additions:
                self.metrics["hold_label_writes"] += 1
            if adding_hold:
                self.metrics["hold_body_writes"] += 1
                if render_card.AUTOMERGE_WORKFLOW_HOLD_LABEL in additions:
                    self.metrics["hold_atomic_writes"] += 1
            self.card_write_tokens.append(token)
            self.touch()
            if adding_hold and self.after_hold_card_edit:
                self.after_hold_card_edit(self)
        elif args[:2] == ["issue", "comment"]:
            self.metrics["comments"] += 1
            self.card_write_tokens.append(token)
            self.card["comments"].append(
                {
                    "id": self.metrics["comments"],
                    "body": args[args.index("--body") + 1],
                    "author": {"login": render_card.GET_CARD_AUTOMATION_AUTHOR},
                }
            )
            self.touch()
        elif args and args[0] == "label":
            self.card_write_tokens.append(token)
            if self.on_label_metadata_create:
                callback = self.on_label_metadata_create
                self.on_label_metadata_create = None
                callback(self)
        return subprocess.CompletedProcess(args, 0, "", "")

    def edit_body(self, number, body, remove_labels=None):
        before = core.parse_state_block(self.card["body"]) or {}
        after = core.parse_state_block(body) or {}
        adding_hold = (
            render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD not in before
            and render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD in after
        )
        if adding_hold and self.fail_hold_body_writes:
            raise RuntimeError("simulated manual-hold persistence failure")
        if am.AUDIT_INTENT_FIELD not in before and am.AUDIT_INTENT_FIELD in after:
            self.metrics["audit_intent_writes"] += 1
        if adding_hold:
            self.metrics["hold_body_writes"] += 1
        self.card["body"] = body
        if remove_labels:
            names = {label["name"] for label in self.card["labels"]}
            names.difference_update(remove_labels)
            self.card["labels"] = [{"name": name} for name in sorted(names)]
        self.card_write_tokens.append(os.environ.get("GH_TOKEN"))
        self.touch()

    def immutable_compare_files(self, slug, base, head, expected):
        if self.history_mode == "net":
            return ([".github/workflows/ci.yml"], True, True)
        return (["src/app.py"], True, True)

    def target_gh(self, path, method="GET", fields=None, paginate=False, slurp=False, **kwargs):
        if path in ("/repos/owner/fmt/pulls/5", "repos/owner/fmt/pulls/5"):
            return copy.deepcopy(self.pr)
        if path.startswith("/repos/owner/fmt/pulls/5/files"):
            paths = (
                [{"filename": ".github/workflows/ci.yml"}]
                if self.history_mode == "net"
                else [{"filename": "src/app.py"}]
            )
            return [paths]
        if path.startswith("/repos/owner/fmt/pulls/5/commits"):
            if self.history_mode == "incomplete":
                return [[]]
            return [[{"sha": HISTORY_COMMIT}]]
        if path.startswith("/repos/owner/fmt/commits/%s" % HISTORY_COMMIT):
            self.metrics["history_reads"] += 1
            self.history_read_tokens.append(os.environ.get("GH_TOKEN"))
            if self.history_mode == "unreadable":
                raise RuntimeError("transient commit read failure")
            paths = (
                [{"filename": ".github/workflows/ci.yml"}]
                if self.history_mode == "history"
                else [{"filename": "src/app.py"}]
            )
            return [{"files": paths}]
        if path == "/repos/owner/fmt/pulls/5/merge":
            self.metrics["merge_calls"] += 1
            self.merge_tokens.append(os.environ.get("GH_TOKEN"))
            self.pr["merged"] = True
            self.pr["state"] = "closed"
            return {"merged": True, "sha": "9" * 40}
        raise AssertionError("unexpected target API call: %s" % path)

    def install(self):
        self._saved = {
            "load_config": core.load_config,
            "maintainers": core.maintainers,
            "get_owner": core.get_owner,
            "gh_rest": core.gh_rest,
            "get_card": render_card.get_card,
            "gh": render_card._gh,
            "ensure_labels": render_card.ensure_labels,
            "edit_body": render_card._edit_issue_body,
            "vision": am.vision_on_default_branch,
            "prior": am.has_prior_merged_pr,
            "live": am.live_pr,
            "compare": am.immutable_compare_files,
            "checks": am.live_check_status,
            "closed_intents": am.closed_audit_intent_entries,
            "pending": am.pending_audit_records,
            "triage_token": reconcile.auto_triage_has_token,
        }
        for name in (
            "GH_TOKEN",
            "WHEELHOUSE_CARD_TOKEN",
            "WHEELHOUSE_AUTOMERGE_HAS_TOKEN",
            "GITHUB_REPOSITORY_OWNER",
            "GITHUB_REPOSITORY",
            "GITHUB_ACTIONS",
            "GITHUB_EVENT_NAME",
        ):
            self._old_env[name] = os.environ.get(name)
        core.load_config = lambda: {
            "auto_merge": True,
            "thank_on_merge": False,
            "repos": {"fmt": {"auto_merge": True}},
        }
        core.maintainers = lambda: {"owner"}
        core.get_owner = lambda: "owner"
        core.gh_rest = self.target_gh
        render_card.get_card = self.get_card
        render_card._gh = self.gh
        render_card.ensure_labels = lambda labels: [
            self.gh(["label", "create", label]) for label in labels
        ]
        render_card._edit_issue_body = self.edit_body
        am.vision_on_default_branch = lambda slug: (True, VISION_SHA)
        am.has_prior_merged_pr = lambda slug, author: True
        am.live_pr = lambda slug, number: copy.deepcopy(self.pr)
        am.immutable_compare_files = self.immutable_compare_files
        am.live_check_status = lambda *args: (True, "comp=pass tests=green")
        am.closed_audit_intent_entries = lambda token: {}
        am.pending_audit_records = lambda: []
        reconcile.auto_triage_has_token = lambda: False
        os.environ.update(
            {
                "WHEELHOUSE_CARD_TOKEN": "card-token",
                "WHEELHOUSE_AUTOMERGE_HAS_TOKEN": "true",
                "GITHUB_REPOSITORY_OWNER": "owner",
                "GITHUB_REPOSITORY": "owner/wheelhouse",
                "GITHUB_ACTIONS": "false",
                "GITHUB_EVENT_NAME": "workflow_dispatch",
            }
        )
        return self

    def restore(self):
        core.load_config = self._saved["load_config"]
        core.maintainers = self._saved["maintainers"]
        core.get_owner = self._saved["get_owner"]
        core.gh_rest = self._saved["gh_rest"]
        render_card.get_card = self._saved["get_card"]
        render_card._gh = self._saved["gh"]
        render_card.ensure_labels = self._saved["ensure_labels"]
        render_card._edit_issue_body = self._saved["edit_body"]
        am.vision_on_default_branch = self._saved["vision"]
        am.has_prior_merged_pr = self._saved["prior"]
        am.live_pr = self._saved["live"]
        am.immutable_compare_files = self._saved["compare"]
        am.live_check_status = self._saved["checks"]
        am.closed_audit_intent_entries = self._saved["closed_intents"]
        am.pending_audit_records = self._saved["pending"]
        reconcile.auto_triage_has_token = self._saved["triage_token"]
        for name, value in self._old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def set_head(self, head):
        self.head = head
        self.pr.update({"head": {"sha": head}, "merged": False, "state": "open"})

    def set_fresh_verdict(self, head):
        self.card["body"] = state_with_fresh_verdict(self.card["body"], head)
        self.touch()

    def run_reconcile(self, scan, cards, criteria_payload=None, close_card=None):
        old_close = render_card.close_card
        if close_card is not None:
            render_card.close_card = close_card
        try:
            with tempfile.TemporaryDirectory(dir=".") as directory:
                scan_path = os.path.join(directory, "scan.json")
                cards_path = os.path.join(directory, "cards.json")
                result_path = os.path.join(directory, "automerge.json")
                with open(scan_path, "w", encoding="utf-8") as handle:
                    json.dump(scan, handle)
                with open(cards_path, "w", encoding="utf-8") as handle:
                    json.dump(cards, handle)
                with open(result_path, "w", encoding="utf-8") as handle:
                    json.dump(criteria_payload or {}, handle)
                old_argv = sys.argv
                sys.argv = ["reconcile.py", scan_path, cards_path, result_path]
                try:
                    with redirect_stdout(io.StringIO()):
                        reconcile.main()
                finally:
                    sys.argv = old_argv
        finally:
            render_card.close_card = old_close

    def record(self, payload):
        with tempfile.TemporaryDirectory(dir=".") as directory:
            path = os.path.join(directory, "automerge.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.environ["GH_TOKEN"] = "card-token"
            with redirect_stdout(io.StringIO()):
                am.cmd_record(path)

    def run_hour(self, reconcile_after=True, record_after=True):
        item = item_for(self.head)
        scan = scan_for(item)
        cards = [self.card_snapshot()]
        before = dict(self.metrics)
        os.environ["GH_TOKEN"] = "card-token"
        claims = am.claim_cards(scan, cards)
        validated = am.validate_claimed_cards(claims)
        os.environ["GH_TOKEN"] = "fleet-token"
        criteria = am.collect_card_criteria(scan, cards)
        payload = am.act_on_scan(scan, validated)
        payload["criteria"] = criteria
        if record_after:
            self.record(payload)
        if reconcile_after:
            os.environ["GH_TOKEN"] = "card-token"
            self.run_reconcile(scan, cards, payload)
        delta = {key: self.metrics[key] - before[key] for key in self.metrics}
        return claims, validated, payload, delta


def test_two_hour_hold_and_head_lifecycle():
    world = LifecycleWorld().install()
    try:
        claims_one, validated_one, payload_one, delta_one = world.run_hour()
        state_one = core.parse_state_block(world.card["body"])
        status_one, hold_one = render_card.automerge_workflow_hold_status(
            state_one, HEAD_ONE
        )
        labels_one = {label["name"] for label in world.card["labels"]}
        check("hour one: exactly one eligible card is claimed", len(claims_one) == 1)
        check("hour one: the claim is validated", len(validated_one) == 1)
        check("hour one: authoritative history is scanned once", delta_one["history_reads"] == 1)
        check("hour one: no merge endpoint is called", delta_one["merge_calls"] == 0)
        check("hour one: one audit intent is staged", delta_one["audit_intent_writes"] == 1)
        check("hour one: one trusted matching-head hold is persisted", status_one == "matching")
        check("hour one: hold carries exact denial reason", hold_one["reason"] == render_card.AUTOMERGE_WORKFLOW_HOLD_REASON)
        check("hour one: hold carries commit and bounded path evidence", hold_one["commit_sha"] == HISTORY_COMMIT and hold_one["paths"] == [".github/workflows/ci.yml"])
        check("hour one: source PR is visible in trusted state", hold_one["source_pr_url"] == "https://github.com/owner/fmt/pull/5")
        check("hour one: dedicated managed label is present", render_card.AUTOMERGE_WORKFLOW_HOLD_LABEL in labels_one)
        check("hour one: hold body and label use one card edit", delta_one["hold_atomic_writes"] == 1)
        check("hour one: owner-visible section appears exactly once", world.card["body"].count("### Manual merge required") == 1)
        check("hour one: owner-visible section explains history-only shape", "complete current net diff is clean" in world.card["body"] and "commit history" in world.card["body"])
        check("hour one: no target-facing or duplicate comment is posted", delta_one["comments"] == 0)
        check("hour one: claim releases only after hold persistence", "processing" not in labels_one and am.AUTO_MERGE_CLAIM_LABEL not in labels_one)
        check("hour one: audit intent clears only after hold persistence", am.AUDIT_INTENT_FIELD not in state_one)
        check("hour one: result handoff carries one structured hold", len(payload_one["workflow_holds"]) == 1)

        claims_two, validated_two, payload_two, delta_two = world.run_hour()
        state_two = core.parse_state_block(world.card["body"])
        check("hour two: matching head is not claimed", claims_two == [] and validated_two == [])
        check("hour two: zero claim-label writes", delta_two["claim_label_writes"] == 0)
        check("hour two: zero history reads", delta_two["history_reads"] == 0)
        check("hour two: zero audit-intent writes", delta_two["audit_intent_writes"] == 0)
        check("hour two: zero duplicate comments", delta_two["comments"] == 0)
        check("hour two: zero merge calls", delta_two["merge_calls"] == 0)
        check("hour two: hold remains matching and unique", render_card.automerge_workflow_hold_status(state_two, HEAD_ONE)[0] == "matching" and world.card["body"].count("### Manual merge required") == 1)

        rows = {row["id"]: row for row in payload_two["criteria"][0]["criteria"]}
        check("criteria: matching-head G7 is an evaluated UNMET fact", rows["g7_immediate_recheck"]["status"] == criteria_schema.STATUS_UNMET)
        check("criteria: G7 carries sanitized commit/path/source evidence", HISTORY_COMMIT[:8] in rows["g7_immediate_recheck"]["evidence"] and ".github/workflows/ci.yml" in rows["g7_immediate_recheck"]["evidence"] and "https://github.com/owner/fmt/pull/5" in rows["g7_immediate_recheck"]["evidence"])

        forged = [
            {"id": key, "label": criteria_schema.CRITERIA_LABELS[key], "status": "met", "evidence": "forged"}
            for key in criteria_schema.CRITERIA_IDS
        ]
        forged_state = dict(state_two, automerge_criteria=forged)
        world.card["body"] = render_card._replace_state_block(world.card["body"], forged_state)
        writes_before_forgery = world.metrics["claim_label_writes"]
        os.environ["GH_TOKEN"] = "card-token"
        forged_claims = am.claim_cards(scan_for(item_for(HEAD_ONE)), [world.card_snapshot()])
        check("criteria: forged displayed MET rows do not bypass trusted hold", forged_claims == [] and world.metrics["claim_label_writes"] == writes_before_forgery)

        world.set_head(HEAD_TWO)
        current_cards = [world.card_snapshot()]
        os.environ["GH_TOKEN"] = "card-token"
        world.run_reconcile(scan_for(item_for(HEAD_TWO)), current_cards)
        refreshed_state = core.parse_state_block(world.card["body"])
        refreshed_labels = {label["name"] for label in world.card["labels"]}
        check("new head: authoritative refresh clears old hold state", render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD not in refreshed_state)
        check("new head: authoritative refresh clears managed hold label", render_card.AUTOMERGE_WORKFLOW_HOLD_LABEL not in refreshed_labels)
        check("new head: stale verdict and triage are dropped", "automerge_verdict" not in refreshed_state and "triaged_sha" not in refreshed_state)
        os.environ["GH_TOKEN"] = "card-token"
        no_fresh_claim = am.claim_cards(scan_for(item_for(HEAD_TWO)), [world.card_snapshot()])
        check("new head: no claim before fresh current-head triage", no_fresh_claim == [])

        world.set_fresh_verdict(HEAD_TWO)
        world.history_mode = "clean"
        history_before_clean = world.metrics["history_reads"]
        merge_before_clean = world.metrics["merge_calls"]
        os.environ["GH_TOKEN"] = "card-token"
        clean_claims = am.claim_cards(scan_for(item_for(HEAD_TWO)), [world.card_snapshot()])
        clean_validated = am.validate_claimed_cards(clean_claims)
        os.environ["GH_TOKEN"] = "fleet-token"
        clean_payload = am.act_on_scan(scan_for(item_for(HEAD_TWO)), clean_validated)
        check("clean new head: current-head verdict restores one claim", len(clean_claims) == 1)
        check("clean new head: unchanged final history gate still runs", world.metrics["history_reads"] - history_before_clean == 1)
        check("clean new head: final gate can reach the unchanged merge endpoint", world.metrics["merge_calls"] - merge_before_clean == 1 and len(clean_payload["merges"]) == 1)
    finally:
        world.restore()


def test_changed_history_head_establishes_one_new_hold():
    world = LifecycleWorld().install()
    try:
        world.run_hour()
        world.set_head(HEAD_THREE)
        os.environ["GH_TOKEN"] = "card-token"
        world.run_reconcile(scan_for(item_for(HEAD_THREE)), [world.card_snapshot()])
        world.set_fresh_verdict(HEAD_THREE)
        history_before = world.metrics["history_reads"]
        world.run_hour(reconcile_after=False)
        state = core.parse_state_block(world.card["body"])
        status, hold = render_card.automerge_workflow_hold_status(state, HEAD_THREE)
        check("changed history head: one fresh authoritative history scan runs", world.metrics["history_reads"] - history_before == 1)
        check("changed history head: one new head-scoped hold is established", status == "matching" and hold["head_sha"] == HEAD_THREE)
        check("changed history head: visible reason remains unique", world.card["body"].count("### Manual merge required") == 1)
        before_second = dict(world.metrics)
        world.run_hour(reconcile_after=False)
        check("changed history head: second same-head pass performs no history scan", world.metrics["history_reads"] == before_second["history_reads"])
        check("changed history head: second same-head pass performs no hold write", world.metrics["hold_body_writes"] == before_second["hold_body_writes"] and world.metrics["hold_label_writes"] == before_second["hold_label_writes"])
    finally:
        world.restore()


def test_net_diff_and_unproven_history_never_create_specialized_hold():
    for mode in ("net", "unreadable", "incomplete"):
        world = LifecycleWorld(history_mode=mode).install()
        try:
            claims, _, payload, _ = world.run_hour(reconcile_after=False)
            state = core.parse_state_block(world.card["body"])
            check("%s: card can enter ordinary evaluation" % mode, len(claims) == 1)
            check("%s: no specialized hold is persisted" % mode, render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD not in state)
            check("%s: no specialized hold handoff is emitted" % mode, payload["workflow_holds"] == [])
            check("%s: target never merges" % mode, world.metrics["merge_calls"] == 0)
            if mode == "net":
                check("net diff: G2 fails before final history gate", world.metrics["history_reads"] == 0)
            else:
                check("%s: unverifiable history remains generic fail-closed" % mode, payload["holds"] and world.metrics["history_reads"] <= 1)
        finally:
            world.restore()


def test_malformed_stale_and_persistence_failure_fail_closed():
    malformed = LifecycleWorld().install()
    try:
        state = core.parse_state_block(malformed.card["body"])
        state[render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD] = {
            "version": 1,
            "head_sha": HEAD_ONE,
            "reason": render_card.AUTOMERGE_WORKFLOW_HOLD_REASON,
        }
        malformed.card["body"] = render_card._replace_state_block(malformed.card["body"], state)
        writes_before = malformed.metrics["claim_label_writes"]
        stderr = io.StringIO()
        os.environ["GH_TOKEN"] = "card-token"
        with redirect_stderr(stderr):
            claims = am.claim_cards(scan_for(item_for(HEAD_ONE)), [malformed.card_snapshot()])
        check("malformed same-head hold: claim fails closed", claims == [])
        check("malformed same-head hold: no processing label write occurs", malformed.metrics["claim_label_writes"] == writes_before)
        check("malformed same-head hold: loud diagnostic is emitted", "malformed" in stderr.getvalue())
    finally:
        malformed.restore()

    stale = LifecycleWorld(head=HEAD_TWO).install()
    try:
        old_result = {
            "repo": "fmt",
            "number": "5",
            "slug": "owner/fmt",
            "head_sha": HEAD_ONE,
            "card_issue": 101,
        }
        old_gate = {
            "status": apply_decision.WORKFLOW_GATE_BLOCKED,
            "reason": apply_decision.WORKFLOW_GATE_HISTORY_ONLY_REASON,
            "net_diff_complete": True,
            "commit_sha": HISTORY_COMMIT,
            "paths": [".github/workflows/ci.yml"],
        }
        old_hold = am.workflow_hold_from_gate(old_result, old_gate)
        state = core.parse_state_block(stale.card["body"])
        state[render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD] = old_hold
        stale.card["body"] = render_card._replace_state_block(stale.card["body"], state)
        stale.card["labels"].append({"name": render_card.AUTOMERGE_WORKFLOW_HOLD_LABEL})
        os.environ["GH_TOKEN"] = "card-token"
        claims = am.claim_cards(scan_for(item_for(HEAD_TWO)), [stale.card_snapshot()])
        check("stale different-head hold: cannot authorize before refresh", claims == [] and stale.metrics["merge_calls"] == 0)
        stale.run_reconcile(scan_for(item_for(HEAD_TWO)), [stale.card_snapshot()])
        refreshed = core.parse_state_block(stale.card["body"])
        check("stale different-head hold: authoritative refresh clears state", render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD not in refreshed)
        check("stale different-head hold: authoritative refresh clears label", render_card.AUTOMERGE_WORKFLOW_HOLD_LABEL not in {label["name"] for label in stale.card["labels"]})
    finally:
        stale.restore()

    failed = LifecycleWorld().install()
    try:
        failed.fail_hold_body_writes = True
        cards = [failed.card_snapshot()]
        scan = scan_for(item_for(HEAD_ONE))
        os.environ["GH_TOKEN"] = "card-token"
        claims = am.claim_cards(scan, cards)
        validated = am.validate_claimed_cards(claims)
        os.environ["GH_TOKEN"] = "fleet-token"
        payload = am.act_on_scan(scan, validated)
        record_failed = False
        try:
            failed.record(payload)
        except RuntimeError:
            record_failed = True
        state = core.parse_state_block(failed.card["body"])
        labels = {label["name"] for label in failed.card["labels"]}
        check("persistence failure: act handoff remains loud and retryable", payload["ambiguous_outcomes"] and record_failed)
        check("persistence failure: final-gate audit intent remains durable", state.get(am.AUDIT_INTENT_FIELD, {}).get(am.AUDIT_FINAL_GATE_PENDING_FIELD) is True)
        check("persistence failure: exclusive claim remains recoverable", {"needs-decision", "processing", am.AUTO_MERGE_CLAIM_LABEL}.issubset(labels))
        check("persistence failure: card never returns to pure reclaimable state", labels != {"needs-decision", "repo:fmt", "kind:pr-review", "priority:med", "target:fmt-5"})
        writes_before = failed.metrics["claim_label_writes"]
        os.environ["GH_TOKEN"] = "card-token"
        recovered_claims = am.claim_cards(scan, [failed.card_snapshot()])
        check("persistence failure: next hour preserves one existing claim without relabeling", len(recovered_claims) == 1 and failed.metrics["claim_label_writes"] == writes_before)
    finally:
        failed.restore()


def test_hold_persistence_rejects_card_snapshot_races():
    race_cases = {
        "body": lambda world: world.card.update(
            {"body": world.card["body"] + "\nOwner edit preserved.\n"}
        ),
        "labels": lambda world: world.card["labels"].append({"name": "owner-note"}),
        "comments": lambda world: world.card["comments"].append(
            {
                "id": 999,
                "body": "Owner context preserved.",
                "author": {"login": "owner"},
            }
        ),
        "updatedAt": lambda world: world.touch(),
    }
    for race_name, mutate in race_cases.items():
        world = LifecycleWorld().install()
        try:
            result = {
                "repo": "fmt",
                "number": "5",
                "slug": "owner/fmt",
                "head_sha": HEAD_ONE,
                "card_issue": 101,
            }
            gate = {
                "status": apply_decision.WORKFLOW_GATE_BLOCKED,
                "reason": apply_decision.WORKFLOW_GATE_HISTORY_ONLY_REASON,
                "net_diff_complete": True,
                "commit_sha": HISTORY_COMMIT,
                "paths": [".github/workflows/ci.yml"],
            }
            hold = am.workflow_hold_from_gate(result, gate)
            intent = {
                "repo": "fmt",
                "number": "5",
                "head_sha": HEAD_ONE,
                "card_issue": 101,
                am.AUDIT_FINAL_GATE_PENDING_FIELD: True,
            }
            state = core.parse_state_block(world.card["body"])
            state[am.AUDIT_INTENT_FIELD] = intent
            world.card["body"] = render_card._replace_state_block(
                world.card["body"], state
            )
            world.card["labels"].extend(
                [{"name": "processing"}, {"name": am.AUTO_MERGE_CLAIM_LABEL}]
            )
            original_get_card = render_card.get_card
            reads = 0

            def racing_get_card(number):
                nonlocal reads
                reads += 1
                if reads == 2:
                    mutate(world)
                return original_get_card(number)

            render_card.get_card = racing_get_card
            failed_closed = False
            reads_before = len(world.card_read_tokens)
            try:
                am.persist_workflow_hold(
                    am._workflow_hold_handoff(result, hold), card_token="card-token"
                )
            except RuntimeError as error:
                failed_closed = "changed before persistence" in str(error)
            finally:
                render_card.get_card = original_get_card
            raced_state = core.parse_state_block(world.card["body"])
            raced_labels = {label["name"] for label in world.card["labels"]}
            check(
                "%s race: persistence fails closed" % race_name,
                failed_closed,
            )
            check(
                "%s race: hold never changes the card" % race_name,
                render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD not in raced_state
                and render_card.AUTOMERGE_WORKFLOW_HOLD_LABEL not in raced_labels
                and world.metrics["hold_body_writes"] == 0
                and world.metrics["hold_label_writes"] == 0,
            )
            check(
                "%s race: audit and claim remain recoverable" % race_name,
                raced_state.get(am.AUDIT_INTENT_FIELD) == intent
                and {"needs-decision", "processing", am.AUTO_MERGE_CLAIM_LABEL}.issubset(
                    raced_labels
                ),
            )
            check(
                "%s race: reads and attempted writes use only the card token"
                % race_name,
                world.card_read_tokens[reads_before:] == ["card-token", "card-token"]
                and set(world.card_write_tokens) == {"card-token"},
            )
        finally:
            world.restore()


def test_hold_persistence_rejects_split_write_window_races():
    race_cases = {
        "label metadata owner edit": (
            "before",
            lambda world: world.card.update(
                {"body": world.card["body"] + "\nOwner edit preserved.\n"}
            ),
        ),
        "post-edit owner comment": (
            "after",
            lambda world: world.card["comments"].append(
                {
                    "id": 1001,
                    "body": "Please hold this PR.",
                    "author": {"login": "owner"},
                }
            ),
        ),
        "post-edit handler transition": (
            "after",
            lambda world: world.card["labels"].append({"name": "decision:merge"}),
        ),
    }
    for race_name, (window, mutate) in race_cases.items():
        world = LifecycleWorld().install()
        try:
            result = {
                "repo": "fmt",
                "number": "5",
                "slug": "owner/fmt",
                "head_sha": HEAD_ONE,
                "card_issue": 101,
            }
            gate = {
                "status": apply_decision.WORKFLOW_GATE_BLOCKED,
                "reason": apply_decision.WORKFLOW_GATE_HISTORY_ONLY_REASON,
                "net_diff_complete": True,
                "commit_sha": HISTORY_COMMIT,
                "paths": [".github/workflows/ci.yml"],
            }
            hold = am.workflow_hold_from_gate(result, gate)
            intent = {
                "repo": "fmt",
                "number": "5",
                "head_sha": HEAD_ONE,
                "card_issue": 101,
                am.AUDIT_FINAL_GATE_PENDING_FIELD: True,
            }
            state = core.parse_state_block(world.card["body"])
            state[am.AUDIT_INTENT_FIELD] = intent
            world.card["body"] = render_card._replace_state_block(
                world.card["body"], state
            )
            world.card["labels"].extend(
                [{"name": "processing"}, {"name": am.AUTO_MERGE_CLAIM_LABEL}]
            )
            if window == "before":
                world.on_label_metadata_create = mutate
            else:
                world.after_hold_card_edit = mutate
            failed_closed = False
            try:
                am.persist_workflow_hold(
                    am._workflow_hold_handoff(result, hold), card_token="card-token"
                )
            except RuntimeError as error:
                failed_closed = (
                    "changed before persistence" in str(error)
                    or "could not confirm persisted" in str(error)
                )
            raced_state = core.parse_state_block(world.card["body"])
            raced_labels = {label["name"] for label in world.card["labels"]}
            check("%s: persistence fails closed" % race_name, failed_closed)
            check(
                "%s: audit and claim remain recoverable" % race_name,
                raced_state.get(am.AUDIT_INTENT_FIELD) == intent
                and {"needs-decision", "processing", am.AUTO_MERGE_CLAIM_LABEL}.issubset(
                    raced_labels
                ),
            )
            check(
                "%s: claim is never released" % race_name,
                world.metrics["claim_label_writes"] == 0,
            )
            check(
                "%s: hold transition uses at most one atomic card edit" % race_name,
                world.metrics["hold_atomic_writes"] == (0 if window == "before" else 1),
            )
            check(
                "%s: every persistence operation uses the card token" % race_name,
                set(world.card_write_tokens) == {"card-token"},
            )
        finally:
            world.restore()


def test_refresh_reuse_hard_close_and_token_boundaries():
    world = LifecycleWorld().install()
    try:
        world.run_hour(reconcile_after=False)
        state = core.parse_state_block(world.card["body"])
        hold = state[render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD]
        reflected_item = item_for(HEAD_ONE)
        reflected_item["updated_at"] = "2026-07-14T00:00:00Z"
        reflected = render_card.body_with_activity_reflected(
            world.card["body"], reflected_item, card_updated_at="2026-07-13T00:00:00Z"
        )
        check("same-head activity write preserves workflow hold", core.parse_state_block(reflected).get(render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD) == hold)
        queued = render_card.body_with_triage_queued(world.card["body"], item_for(HEAD_ONE))
        check("same-head triage write preserves workflow hold", core.parse_state_block(queued).get(render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD) == hold)

        closed_body = render_card.body_with_reconcile_absence(
            world.card["body"],
            render_card.RECONCILE_ABSENCE_THRESHOLD,
            run_number=2,
            closed_at="2026-07-13T01:00:00Z",
        )
        candidate = copy.deepcopy(world.card)
        candidate.update({"body": closed_body, "state": "CLOSED"})
        same_card, _ = render_card._reused_card_render(item_for(HEAD_ONE), candidate, False)
        same_state = core.parse_state_block(same_card["body"])
        check("same-head machine-soft-close reuse preserves workflow hold", same_state.get(render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD) == hold and render_card.AUTOMERGE_WORKFLOW_HOLD_LABEL in same_card["labels"])
        changed_card, _ = render_card._reused_card_render(item_for(HEAD_TWO), candidate, False)
        changed_state = core.parse_state_block(changed_card["body"])
        check("new-head reuse clears workflow hold", render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD not in changed_state and render_card.AUTOMERGE_WORKFLOW_HOLD_LABEL not in changed_card["labels"])
        incompatible = item_for(HEAD_ONE, kind="ci-approval")
        incompatible_card, _ = render_card._reused_card_render(incompatible, candidate, False)
        check("incompatible-kind reuse clears workflow hold", render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD not in core.parse_state_block(incompatible_card["body"]))

        closed = []
        def hard_close(number, message, label="resolved", expected=None):
            closed.append((number, message))
            world.card["state"] = "CLOSED"
        os.environ["GH_TOKEN"] = "card-token"
        world.run_reconcile(scan_for(item_for(HEAD_ONE), open_target=False), [world.card_snapshot()], close_card=hard_close)
        check("manual source merge/close hard-closes held card immediately", len(closed) == 1 and world.card["state"] == "CLOSED")

        check("hold path: every card write uses only the default card token", world.card_write_tokens and set(world.card_write_tokens) == {"card-token"})
        check("hold path: authoritative history reads use FLEET_TOKEN", world.history_read_tokens == ["fleet-token"])
        check("hold path: no target mutation occurs", world.metrics["merge_calls"] == 0)
        check("manual hold label remains refreshable and blocked stays unchanged", render_card.is_refreshable(["needs-decision", render_card.AUTOMERGE_WORKFLOW_HOLD_LABEL]) and "blocked" in render_card.NON_REFRESHABLE_LABELS)
        check("manual hold is non-material", render_card.AUTOMERGE_WORKFLOW_HOLD_FIELD not in render_card.MATERIAL_FIELDS)
    finally:
        world.restore()


def test_structured_authoritative_gate_contract():
    world = LifecycleWorld().install()
    try:
        os.environ["GH_TOKEN"] = "fleet-token"
        gate = apply_decision._workflow_merge_gate("owner", "fmt", 5, world.pr)
        check("shared gate: history-only result is structured", gate["status"] == apply_decision.WORKFLOW_GATE_BLOCKED and gate["reason"] == apply_decision.WORKFLOW_GATE_HISTORY_ONLY_REASON)
        check("shared gate: structured result carries exact source evidence", gate["commit_sha"] == HISTORY_COMMIT and gate["paths"] == [".github/workflows/ci.yml"] and gate["net_diff_complete"] is True)
        message, terminal = apply_decision.do_merge("owner", "fmt", 5, HEAD_ONE)
        check("direct owner decision: authoritative refusal remains blocked", terminal == "blocked" and "merge by hand in the GitHub UI" in message)
        check("direct owner decision: refusal never calls merge endpoint", world.metrics["merge_calls"] == 0)
    finally:
        world.restore()


def main():
    test_two_hour_hold_and_head_lifecycle()
    test_changed_history_head_establishes_one_new_hold()
    test_net_diff_and_unproven_history_never_create_specialized_hold()
    test_malformed_stale_and_persistence_failure_fail_closed()
    test_hold_persistence_rejects_card_snapshot_races()
    test_hold_persistence_rejects_split_write_window_races()
    test_refresh_reuse_hard_close_and_token_boundaries()
    test_structured_authoritative_gate_contract()
    if _failures:
        print("\n%d failure(s):" % len(_failures))
        for failure in _failures:
            print(" - " + failure)
        raise SystemExit(1)
    print("\nall auto-merge workflow-hold tests passed")


if __name__ == "__main__":
    main()
