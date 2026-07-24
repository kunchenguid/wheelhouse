#!/usr/bin/env python3
"""Production-composed decision-label projection-race recovery tests."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import card_projection  # noqa: E402
import decision_label_recovery as recovery  # noqa: E402
import render_card  # noqa: E402
import test_option_b_architecture as option_b  # noqa: E402
import wheelhouse_core as core  # noqa: E402

FAILURES = []
T1 = "2026-07-23T12:00:01Z"
T2 = "2026-07-23T12:00:02Z"
T3 = "2026-07-23T12:00:03Z"


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def fixture():
    observation = option_b.observation()
    item = option_b.item_for(observation)
    projection = card_projection.plan_card_projection(item, prior={})
    trigger_body = projection["body"]
    current_body = render_card.body_with_activity_reflected(
        trigger_body,
        dict(item, updated_at="2026-07-23T13:00:00Z"),
        card_updated_at=T1,
    )
    base_labels = [{"name": label} for label in projection["managed_labels"]]
    label = "decision:merge"
    event = {
        "action": "labeled",
        "repository": {"full_name": "owner/wheelhouse"},
        "sender": {"login": "owner"},
        "label": {"name": label},
        "issue": {
            "id": 9001,
            "number": 77,
            "state": "open",
            "body": trigger_body,
            "updated_at": T1,
            "labels": base_labels + [{"name": label}],
        },
    }
    current = {
        "id": 9001,
        "number": 77,
        "state": "open",
        "body": current_body,
        "updated_at": T2,
        "labels": base_labels,
    }
    events = [
        [
            {
                "id": 101,
                "event": "labeled",
                "created_at": T1,
                "actor": {"login": "owner"},
                "label": {"name": label},
            },
            {
                "id": 102,
                "event": "unlabeled",
                "created_at": T2,
                "actor": {"login": "github-actions[bot]"},
                "label": {"name": label},
            },
        ]
    ]
    return event, current, events


def admit(event, current, events, **overrides):
    options = {
        "repo_slug": "owner/wheelhouse",
        "issue": 77,
        "sender": "owner",
        "authorized": True,
    }
    options.update(overrides)
    return recovery.admission_record(event, current, events, **options)


def changed_state_body(body, **changes):
    state = core.parse_state_block(body)
    state.update(changes)
    return render_card._replace_state_block(body, state)


def test_pure_admission_matrix():
    event, current, events = fixture()
    record, reason = admit(event, current, events)
    check(
        "accepted: exact authorized projection-erased decision label",
        record is not None
        and reason == "admission.ok"
        and recovery.parse_marker(recovery.marker(record)) == record,
    )
    supported = []
    for label in (
        "decision:merge",
        "decision:close",
        "decision:decline",
        "decision:hold",
        "decision:investigate",
    ):
        variant_event = copy.deepcopy(event)
        variant_events = copy.deepcopy(events)
        variant_event["label"]["name"] = label
        variant_event["issue"]["labels"][-1]["name"] = label
        for row in variant_events[0]:
            row["label"]["name"] = label
        supported.append(admit(variant_event, current, variant_events)[0] is not None)
    check(
        "accepted: every supported text-free PR decision label",
        all(supported),
    )
    denied = []
    denied.append(admit(event, current, events, authorized=False)[0] is None)
    foreign_actor = copy.deepcopy(events)
    foreign_actor[0][0]["actor"]["login"] = "other"
    denied.append(admit(event, current, foreign_actor)[0] is None)
    foreign_repo = copy.deepcopy(event)
    foreign_repo["repository"]["full_name"] = "owner/other"
    denied.append(admit(foreign_repo, current, events)[0] is None)
    wrong_card = copy.deepcopy(current)
    wrong_card["id"] = 9002
    denied.append(admit(event, wrong_card, events)[0] is None)
    wrong_target = copy.deepcopy(current)
    wrong_target["body"] = changed_state_body(
        current["body"], repo="different-repo"
    )
    denied.append(admit(event, wrong_target, events)[0] is None)
    new_head = copy.deepcopy(current)
    new_head["body"] = changed_state_body(current["body"], head_sha="new-head")
    denied.append(admit(event, new_head, events)[0] is None)
    unsupported = copy.deepcopy(event)
    unsupported["label"]["name"] = "decision:request-changes"
    unsupported["issue"]["labels"][-1]["name"] = "decision:request-changes"
    unsupported_events = copy.deepcopy(events)
    for row in unsupported_events[0]:
        row["label"]["name"] = "decision:request-changes"
    denied.append(admit(unsupported, current, unsupported_events)[0] is None)
    check(
        "denied: unauthorized, foreign, wrong-card, wrong-target, stale-head, unsupported",
        all(denied),
    )

    owner_removed = copy.deepcopy(events)
    owner_removed[0][1]["actor"]["login"] = "owner"
    ambiguous = copy.deepcopy(events)
    ambiguous[0].insert(
        1,
        {
            "id": 103,
            "event": "labeled",
            "created_at": T1,
            "actor": {"login": "owner"},
            "label": {"name": "decision:merge"},
        },
    )
    intentionally_removed = copy.deepcopy(events)
    intentionally_removed[0].extend(
        [
            {
                "id": 104,
                "event": "labeled",
                "created_at": T3,
                "actor": {"login": "owner"},
                "label": {"name": "decision:merge"},
            },
            {
                "id": 105,
                "event": "unlabeled",
                "created_at": T3,
                "actor": {"login": "owner"},
                "label": {"name": "decision:merge"},
            },
        ]
    )
    duplicate_label = copy.deepcopy(event)
    duplicate_label["issue"]["labels"].append({"name": "decision:close"})
    check(
        "denied: explicit removal, ambiguous history, intentional removal, label ambiguity",
        admit(event, current, owner_removed)[0] is None
        and admit(event, current, ambiguous)[0] is None
        and admit(event, current, intentionally_removed)[0] is None
        and admit(duplicate_label, current, events)[0] is None,
    )


class FakeGitHub:
    def __init__(self, event, current, events):
        self.event = copy.deepcopy(event)
        self.current = copy.deepcopy(current)
        self.events = copy.deepcopy(events)
        self.comments = []
        self.next_comment = 501
        self.posts = 0

    def pages(self, rows):
        return [copy.deepcopy(rows)]

    def call(self, *args):
        endpoint = next(
            (arg for arg in args if isinstance(arg, str) and arg.startswith("repos/")),
            "",
        )
        event_prefix = (
            "/events?per_page=%s&page=" % recovery.HISTORY_PAGE_SIZE
        )
        comment_prefix = (
            "/comments?per_page=%s&page=" % recovery.HISTORY_PAGE_SIZE
        )
        if args[0] == "api" and event_prefix in endpoint:
            page = int(endpoint.rsplit("=", 1)[1])
            rows = self.events[page - 1] if page <= len(self.events) else []
            return copy.deepcopy(rows)
        if args[0] == "api" and comment_prefix in endpoint:
            page = int(endpoint.rsplit("=", 1)[1])
            start = (page - 1) * recovery.HISTORY_PAGE_SIZE
            end = start + recovery.HISTORY_PAGE_SIZE
            return copy.deepcopy(self.comments[start:end])
        if args[:3] == ("api", "--method", "POST"):
            body = next(arg[5:] for arg in args if arg.startswith("body="))
            comment = {
                "id": self.next_comment,
                "body": body,
                "user": {"login": "github-actions[bot]"},
            }
            self.next_comment += 1
            self.posts += 1
            self.comments.append(comment)
            self.current["updated_at"] = T3
            return copy.deepcopy(comment)
        if args[0] == "api" and "/issues/comments/" in endpoint:
            comment_id = int(endpoint.rsplit("/", 1)[1])
            return copy.deepcopy(
                next(row for row in self.comments if row["id"] == comment_id)
            )
        if args[0] == "api" and endpoint.endswith("/issues/77"):
            return copy.deepcopy(self.current)
        raise AssertionError("unexpected gh call %r" % (args,))


def read_outputs(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        values[key] = value
    return values


def run_claim(fake, event_path, output_path):
    saved_gh = recovery._gh_json
    old_output = os.environ.get("GITHUB_OUTPUT")
    recovery._gh_json = fake.call
    os.environ["GITHUB_OUTPUT"] = output_path
    try:
        code = recovery.claim(
            SimpleNamespace(
                event_file=event_path,
                repo_slug="owner/wheelhouse",
                issue=77,
                sender="owner",
                authorized="true",
            )
        )
    finally:
        recovery._gh_json = saved_gh
        if old_output is None:
            os.environ.pop("GITHUB_OUTPUT", None)
        else:
            os.environ["GITHUB_OUTPUT"] = old_output
    return code, read_outputs(output_path)


def test_durable_claim_and_revalidation():
    event, current, events = fixture()
    fake = FakeGitHub(event, current, events)
    with tempfile.TemporaryDirectory() as temp:
        event_path = Path(temp) / "event.json"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        first_output = str(Path(temp) / "first.out")
        Path(first_output).write_text("", encoding="utf-8")
        code, first = run_claim(fake, str(event_path), first_output)
        check(
            "claim: accepted event is durably claimed once",
            code == 0
            and first.get("required") == "true"
            and first.get("admitted") == "true"
            and fake.posts == 1,
        )

        replay_output = str(Path(temp) / "replay.out")
        Path(replay_output).write_text("", encoding="utf-8")
        code, replay = run_claim(fake, str(event_path), replay_output)
        check(
            "claim: duplicate delivery is replay-denied without another write",
            code == 0
            and replay.get("required") == "true"
            and replay.get("admitted") == "false"
            and replay.get("reason") == "claim.replay"
            and fake.posts == 1,
        )

        fake.current["labels"].append({"name": "processing"})
        saved_gh = recovery._gh_json
        old_output = os.environ.get("GITHUB_OUTPUT")
        allowed_output = str(Path(temp) / "allowed.out")
        Path(allowed_output).write_text("", encoding="utf-8")
        recovery._gh_json = fake.call
        os.environ["GITHUB_OUTPUT"] = allowed_output
        try:
            recovery.revalidate(
                SimpleNamespace(
                    event_file=str(event_path),
                    repo_slug="owner/wheelhouse",
                    issue=77,
                    sender="owner",
                    authorized="true",
                    event_key=first["event_key"],
                    processing="required",
                )
            )
        finally:
            recovery._gh_json = saved_gh
            if old_output is None:
                os.environ.pop("GITHUB_OUTPUT", None)
            else:
                os.environ["GITHUB_OUTPUT"] = old_output
        allowed = read_outputs(allowed_output)
        check(
            "revalidate: exact processing claim remains action-eligible",
            allowed.get("allowed") == "true",
        )

        fake.events[0].extend(
            [
                {
                    "id": 104,
                    "event": "labeled",
                    "created_at": T3,
                    "actor": {"login": "owner"},
                    "label": {"name": "decision:merge"},
                },
                {
                    "id": 105,
                    "event": "unlabeled",
                    "created_at": T3,
                    "actor": {"login": "owner"},
                    "label": {"name": "decision:merge"},
                },
            ]
        )
        denied_output = str(Path(temp) / "denied.out")
        Path(denied_output).write_text("", encoding="utf-8")
        recovery._gh_json = fake.call
        os.environ["GITHUB_OUTPUT"] = denied_output
        try:
            recovery.revalidate(
                SimpleNamespace(
                    event_file=str(event_path),
                    repo_slug="owner/wheelhouse",
                    issue=77,
                    sender="owner",
                    authorized="true",
                    event_key=first["event_key"],
                    processing="required",
                )
            )
        finally:
            recovery._gh_json = saved_gh
            if old_output is None:
                os.environ.pop("GITHUB_OUTPUT", None)
            else:
                os.environ["GITHUB_OUTPUT"] = old_output
        check(
            "revalidate: later explicit removal cancels recovered action",
            read_outputs(denied_output).get("allowed") == "false",
        )

    old = FakeGitHub(event, dict(current, updated_at=T3), events)
    with tempfile.TemporaryDirectory() as temp:
        event_path = Path(temp) / "event.json"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        output_path = Path(temp) / "old.out"
        output_path.write_text("", encoding="utf-8")
        _, values = run_claim(old, str(event_path), str(output_path))
    check(
        "claim: old projection evidence cannot authorize recovery",
        values.get("required") == "true"
        and values.get("admitted") == "false"
        and values.get("reason") == "projection.not_current"
        and old.posts == 0,
    )


def test_bounded_complete_history():
    event, current, events = fixture()
    complete = FakeGitHub(event, current, events)
    complete.events = [
        [{} for _ in range(recovery.HISTORY_PAGE_SIZE)],
        [{}],
    ]
    complete.comments = [
        {} for _ in range(recovery.HISTORY_PAGE_SIZE + 1)
    ]
    saved_gh = recovery._gh_json
    recovery._gh_json = complete.call
    try:
        _, event_pages, comment_pages = recovery._read_world(
            "owner/wheelhouse", 77
        )
    finally:
        recovery._gh_json = saved_gh
    check(
        "history: short terminal pages prove bounded completeness",
        [len(page) for page in event_pages]
        == [recovery.HISTORY_PAGE_SIZE, 1]
        and [len(page) for page in comment_pages]
        == [recovery.HISTORY_PAGE_SIZE, 1],
    )

    denied = []
    for resource in ("events", "comments"):
        bounded = FakeGitHub(event, current, events)
        if resource == "events":
            bounded.events = [
                [{} for _ in range(recovery.HISTORY_PAGE_SIZE)]
                for _ in range(recovery.MAX_HISTORY_PAGES)
            ]
        else:
            bounded.comments = [
                {}
                for _ in range(
                    recovery.HISTORY_PAGE_SIZE * recovery.MAX_HISTORY_PAGES
                )
            ]
        recovery._gh_json = bounded.call
        try:
            recovery._read_world("owner/wheelhouse", 77)
        except recovery.RecoveryError:
            denied.append(True)
        else:
            denied.append(False)
        finally:
            recovery._gh_json = saved_gh
    oversized = FakeGitHub(event, current, events)
    oversized.events = [
        [{} for _ in range(recovery.HISTORY_PAGE_SIZE + 1)]
    ]
    recovery._gh_json = oversized.call
    try:
        recovery._read_world("owner/wheelhouse", 77)
    except recovery.RecoveryError:
        denied.append(True)
    else:
        denied.append(False)
    finally:
        recovery._gh_json = saved_gh
    check(
        "history: page caps and malformed oversized pages fail closed",
        all(denied),
    )


def test_workflow_composition():
    workflow = (
        ROOT / ".github" / "workflows" / "decision-handler.yml"
    ).read_text(encoding="utf-8")
    check(
        "workflow: recovery is authorized, event-bound, claimed, and revalidated",
        "steps.gate.outputs.authorized == 'true'" in workflow
        and "github.event.action == 'labeled'" in workflow
        and "decision_label_recovery.py claim" in workflow
        and "decision_label_recovery.py revalidate" in workflow
        and "--event-file \"$GITHUB_EVENT_PATH\"" in workflow
        and "--processing required" in workflow
        and "--processing forbidden" in workflow,
    )
    check(
        "workflow: consuming and investigate actions require recovered-event revalidation",
        "steps.decision-label-revalidate.outputs.allowed == 'true'" in workflow
        and "needs.handle.outputs.decision_label_recovery_admitted != 'true'"
        in workflow
        and '$trigger_comments + (if $label_recovery == "true" then 1 else 0 end)'
        in workflow,
    )


def main():
    test_pure_admission_matrix()
    test_durable_claim_and_revalidation()
    test_bounded_complete_history()
    test_workflow_composition()
    if FAILURES:
        raise SystemExit(
            "%d decision-label recovery failure(s): %s"
            % (len(FAILURES), ", ".join(FAILURES))
        )
    print("\nall decision-label recovery tests passed")


if __name__ == "__main__":
    main()
