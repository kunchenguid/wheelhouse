#!/usr/bin/env python3
"""
Unit-exercise reconcile routing with NO network.

Run: python tests/test_reconcile.py
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import reconcile  # noqa: E402

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def labels(*names):
    return [{"name": n} for n in names]


def body_state(repo="wheelhouse", number=42, kind="pr-review",
               head_sha="oldsha", comp="pass", tests="green", priority="med"):
    state = {
        "repo": repo,
        "number": number,
        "kind": kind,
        "head_sha": head_sha,
        "options": ["merge", "close", "hold"],
        "comp": comp,
        "tests": tests,
        "priority": priority,
    }
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
        "url": "https://github.com/kunchenguid/wheelhouse/pull/42",
        "summary": "compliance=pass tests=green",
        "recommendation": "Merge - compliance and tests are green.",
        "priority": "med",
    }
    item.update(overrides)
    return item


def run_reconcile(scan, cards, current_cards=None):
    calls = {"upsert": [], "close": []}
    current_by_number = {
        c["number"]: c
        for c in (cards if current_cards is None else current_cards)
    }

    def fake_upsert(item, existing=None):
        calls["upsert"].append({"item": item, "existing": existing})

    def fake_close(number, message, label="resolved"):
        calls["close"].append({"number": number, "message": message, "label": label})

    def fake_get_card(number):
        return current_by_number.get(int(number))

    old_argv = sys.argv[:]
    old_upsert = reconcile.render_card.upsert_card
    old_close = reconcile.render_card.close_card
    old_get_card = reconcile.render_card.get_card
    reconcile.render_card.upsert_card = fake_upsert
    reconcile.render_card.close_card = fake_close
    reconcile.render_card.get_card = fake_get_card
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
    return calls


def scan_payload(items=None, open_pr_numbers=None, ok=True):
    return {
        "repos": {
            "wheelhouse": {
                "ok": ok,
                "open_pr_numbers": [42] if open_pr_numbers is None else open_pr_numbers,
                "open_issue_numbers": [],
            }
        },
        "items": [] if items is None else items,
    }


def card(labels_):
    return {
        "number": 7,
        "body": body_state(),
        "labels": labels_,
        "title": "[wheelhouse#42] Ready PR",
    }


def test_refresh_uses_known_card_when_target_label_missing():
    calls = run_reconcile(
        scan_payload(items=[work_item(priority="high")]),
        [card(labels("needs-decision", "repo:wheelhouse", "kind:pr-review", "priority:med"))],
    )
    check("reconcile: refresh called once", len(calls["upsert"]) == 1)
    existing = calls["upsert"][0]["existing"] if calls["upsert"] else None
    check("reconcile: refresh receives known card row",
          existing is not None and existing["number"] == 7)
    check("reconcile: known row has missing target label",
          existing is not None and all(label.get("name") != "target:wheelhouse-42"
                                       for label in existing["labels"]))
    check("reconcile: no close for refreshed worklist item", calls["close"] == [])


def test_refresh_uses_current_labels_before_upsert():
    snapshot = card(labels("needs-decision", "repo:wheelhouse", "kind:pr-review",
                           "priority:med", "target:wheelhouse-42"))
    current = card(labels("needs-decision", "processing", "repo:wheelhouse",
                          "kind:pr-review", "priority:med", "target:wheelhouse-42"))
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
        [card(labels("needs-decision", "repo:wheelhouse", "kind:pr-review",
                     "priority:med", "target:wheelhouse-42"))],
    )
    check("reconcile: no upsert for item outside worklist", calls["upsert"] == [])
    check("reconcile: pure pending card outside worklist closed",
          len(calls["close"]) == 1 and calls["close"][0]["number"] == 7)
    check("reconcile: close message says no maintainer decision needed",
          "no longer needs a maintainer decision" in calls["close"][0]["message"])


def test_open_target_that_left_worklist_uses_current_labels_before_close():
    snapshot = card(labels("needs-decision", "repo:wheelhouse", "kind:pr-review",
                           "priority:med", "target:wheelhouse-42"))
    current = card(labels("needs-decision", "processing", "repo:wheelhouse",
                          "kind:pr-review", "priority:med", "target:wheelhouse-42"))
    calls = run_reconcile(
        scan_payload(items=[]),
        [snapshot],
        current_cards=[current],
    )
    check("reconcile: processing current card outside worklist is not closed",
          calls["close"] == [])


def test_open_target_without_needs_decision_is_left_alone():
    calls = run_reconcile(
        scan_payload(items=[]),
        [card(labels("repo:wheelhouse", "kind:pr-review",
                     "priority:med", "target:wheelhouse-42"))],
    )
    check("reconcile: card missing needs-decision is not closed", calls["close"] == [])
    check("reconcile: card missing needs-decision is not upserted", calls["upsert"] == [])


def main():
    test_refresh_uses_known_card_when_target_label_missing()
    test_refresh_uses_current_labels_before_upsert()
    test_open_target_that_left_worklist_is_consumed()
    test_open_target_that_left_worklist_uses_current_labels_before_close()
    test_open_target_without_needs_decision_is_left_alone()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all reconcile tests passed")


if __name__ == "__main__":
    main()
