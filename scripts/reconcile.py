#!/usr/bin/env python3
"""
Wheelhouse - backstop reconciler.

The safety net behind the event-driven `ingest` path. Given a fresh scan of the
fleet (scan.json) and the current open cards in THIS repo (cards.json), it:

  * opens a decision card for any worklist item that has no open card,
  * refreshes an OPEN `needs-decision` card in place when its target's material
    state changed (head_sha/compliance/tests/kind/priority/options) - so the queue
    reflects current state, not just the snapshot taken when the card was first
    created - and leaves materially-unchanged cards completely untouched, and
  * closes any open card whose underlying PR/issue is no longer open, and closes
    pure pending cards whose open target no longer needs a maintainer decision -
    so the queue self-heals even if a dispatch was lost.
    This also consumes old scan-built cards for owner/maintainer/bot-authored
    targets after the author filter removes them from the current worklist.

Both card operations run against THIS repo via the ambient GH_TOKEN, which the
workflow sets to the default GITHUB_TOKEN (card activity must not re-trigger the
handler).

Usage:
  reconcile.py scan.json cards.json

cards.json is `gh issue list --state open --json number,body,labels,title`.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wheelhouse_core as core  # noqa: E402
import render_card  # noqa: E402

PR_KINDS = {"pr-review", "ci-approval"}


def load(path):
    with open(path) as f:
        return json.load(f)


def current_card(row):
    card = render_card.get_card(row["number"])
    if not card or not render_card.issue_is_open(card):
        return None
    state = core.parse_state_block(card.get("body", ""))
    if not state:
        return None
    return {
        "number": card["number"],
        "body": card.get("body", ""),
        "state": state,
        "labels": card.get("labels", []),
    }


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: reconcile.py scan.json cards.json")
    scan = load(sys.argv[1])
    cards = load(sys.argv[2])

    repos = scan.get("repos", {})
    items = scan.get("items", [])

    # Index existing open cards by their target (repo, number) from the state block.
    existing = {}            # (repo, number) -> existing card row
    cards_with_state = []    # existing card rows with parsed state
    for card in cards:
        state = core.parse_state_block(card.get("body", ""))
        if not state:
            continue  # a manually-created issue with no card state; leave it alone
        key = (state.get("repo"), int(state.get("number", 0)))
        row = {
            "number": card["number"],
            "body": card.get("body", ""),
            "state": state,
            "labels": card.get("labels", []),
        }
        existing[key] = row
        cards_with_state.append(row)

    worklist_keys = {(item["repo"], int(item["number"])) for item in items}

    # 1) For each scanned worklist item, create a card if none exists, else
    #    refresh it in place when its target materially changed. Items only come
    #    from ok:true repos (build_repo returns no items for a failed scan), so
    #    this path never refreshes a card for a repo whose state is unknown.
    created = 0
    refreshed = 0
    for item in items:
        key = (item["repo"], int(item["number"]))
        ex = existing.get(key)
        if ex is None:
            try:
                render_card.upsert_card(item)
                created += 1
            except Exception as e:  # one bad item must not abort the whole pass
                print("::warning::failed to create card for %s#%s: %s"
                      % (item["repo"], item["number"], str(e)[:160]))
            continue
        # Card exists: refresh only a pure needs-decision card whose target
        # materially changed. A card mid-decision (processing/resolved/blocked)
        # or with no material change is left completely untouched (no edit, no
        # comment). `upsert_card` re-checks both guards before it edits.
        if not render_card.is_refreshable(ex["labels"]):
            continue
        if not render_card.material_changed(item, ex["state"]):
            continue
        try:
            current = current_card(ex)
            if current is None:
                continue
            if not render_card.is_refreshable(current["labels"]):
                continue
            if not render_card.material_changed(item, current["state"]):
                continue
            render_card.upsert_card(item, existing=current)
            refreshed += 1
        except Exception as e:
            print("::warning::failed to refresh card #%s for %s#%s: %s"
                  % (ex["number"], item["repo"], item["number"], str(e)[:160]))

    # 2) Close cards whose target is no longer open, and pure pending cards whose
    #    open target no longer appears in the current maintainer worklist. Skip
    #    repos that failed to scan (ok:false) - we don't know their state.
    closed = 0
    for ex in cards_with_state:
        card_number = ex["number"]
        state = ex["state"]
        repo = state.get("repo")
        r = repos.get(repo)
        if not r or not r.get("ok"):
            continue
        number = int(state.get("number", 0))
        kind = state.get("kind", "pr-review")
        open_set = set(r.get("open_pr_numbers", []) if kind in PR_KINDS
                       else r.get("open_issue_numbers", []))
        if number in open_set:
            key = (repo, number)
            if key in worklist_keys or not render_card.is_refreshable(ex["labels"]):
                continue
            current = current_card(ex)
            if current is None:
                continue
            state = current["state"]
            repo = state.get("repo")
            number = int(state.get("number", 0))
            kind = state.get("kind", "pr-review")
            r = repos.get(repo)
            if not r or not r.get("ok"):
                continue
            open_set = set(r.get("open_pr_numbers", []) if kind in PR_KINDS
                           else r.get("open_issue_numbers", []))
            current_key = (repo, number)
            if number not in open_set or current_key in worklist_keys:
                continue
            if not render_card.is_refreshable(current["labels"]):
                continue
            msg = ("Self-healed by the scheduled backstop: %s#%s no longer needs "
                   "a maintainer decision in the current scan - consuming this "
                   "card." % (repo, number))
        else:
            msg = ("Self-healed by the scheduled backstop: %s#%s is no longer open "
                   "(merged/closed) - consuming this card." % (repo, number))
        try:
            render_card.close_card(card_number, msg)
            closed += 1
        except Exception as e:
            print("::warning::failed to close card #%s: %s" % (card_number, str(e)[:160]))

    print("reconcile: %d card(s) created, %d refreshed, %d card(s) closed"
          % (created, refreshed, closed))


if __name__ == "__main__":
    main()
