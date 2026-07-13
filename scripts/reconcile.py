#!/usr/bin/env python3
"""
Wheelhouse - backstop reconciler.

The safety net behind the event-driven `ingest` path. Given a fresh scan of the
fleet (scan.json) and the current open cards in THIS repo (cards.json), it:

  * opens a decision card for any worklist item that has no open card,
    reads that freshly-created card back by the issue number returned from
    `upsert_card`, and queues its first eligible auto-triage attempt in the same
    pass,
  * refreshes an OPEN `needs-decision` card in place when its target's material
    state changed (head_sha/compliance/tests/kind/priority/options), when its
    render version is stale, or when a held card should be published because
    auto triage is no longer eligible - so the queue reflects current state,
    not just the snapshot taken when the card was first created,
  * reflects target activity with a hidden state-only card body edit when the
    target's `updated_at` is newer than the card's `activity_reflected_at` and
    no full refresh or auto-triage queued write is already editing the body, and
  * queues lightweight automatic triage for eligible pure pending pr-review or
    issue-triage cards whose current revision lacks a `triaged_sha` cache
    (`pending-triage` held cards still count as pure pending), and
  * closes any open card whose underlying PR/issue is no longer open, and closes
    pure pending cards whose open target no longer needs a maintainer decision -
    so the queue self-heals even if a dispatch was lost.
    This also consumes old scan-built cards for owner/maintainer/bot-authored
    targets after the author filter removes them from the current worklist, and
    for conflicted PR-review targets after the scan moves them to needs-rebase.

Both card operations run against THIS repo via the ambient GH_TOKEN, which the
workflow sets to the default GITHUB_TOKEN (card activity must not re-trigger the
handler).

Usage:
  reconcile.py scan.json cards.json [automerge.json]

When automerge.json is present, its read-only `criteria` snapshot is attached to
matching PR-review items before rendering. Missing or historical files degrade
to explicit unavailable UI rows and never affect routing or acting.

cards.json is an array of open issue rows with number, body, labels, title, and
updated_at.
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
        "updated_at": render_card.card_updated_at(card),
    }


# Kept as a thin alias: reconcile.py historically owned this check, and
# render_card.py now needs the same signal (to decide whether a brand-new
# card is created HELD), so it is the shared single source of truth.
auto_triage_has_token = render_card.auto_triage_has_token


def maybe_queue_auto_triage(item, row, has_token, owner=""):
    """Queue lightweight advisory triage when this card revision lacks a cache.

    The card is marked queued before dispatch so a failed workflow still counts
    as this revision's one attempt. Only pure needs-decision pr-review and
    issue-triage cards qualify.

    If the workflow dispatch itself fails (the queued-cache write already
    landed, so a later scan would never retry this revision - see
    `render_card.mark_triage_queued`), a HELD card is published immediately
    with a note instead of being left held indefinitely: fail-open (see
    AGENTS.md "Held cards") must not depend on a dispatch that never actually
    started.
    """
    if not row:
        return False
    if not render_card.should_auto_triage(
        item, row.get("state"), row.get("labels"), has_token
    ):
        return False
    revision = render_card.triage_revision(item)
    try:
        if not render_card.mark_triage_queued(row["number"], item, row.get("body", "")):
            return False
    except Exception as e:
        print(
            "::warning::failed to queue auto triage for card #%s (%s#%s): %s"
            % (row.get("number"), item.get("repo"), item.get("number"), str(e)[:160])
        )
        return False
    try:
        render_card.dispatch_triage_workflow(row["number"], item)
    except Exception as e:
        print(
            "::warning::failed to dispatch auto triage for card #%s (%s#%s): %s "
            "- publishing the card so it is not left held indefinitely"
            % (row.get("number"), item.get("repo"), item.get("number"), str(e)[:160])
        )
        render_card.publish_dispatch_failure(
            row["number"],
            revision,
            "Auto triage could not be started: %s" % str(e)[:160],
            owner=owner,
        )
        return False
    print(
        "queued auto triage for %s#%s on card #%s"
        % (item["repo"], item["number"], row["number"])
    )
    return True


def main():
    if len(sys.argv) not in (3, 4):
        sys.exit("usage: reconcile.py scan.json cards.json [automerge.json]")
    scan = load(sys.argv[1])
    cards = load(sys.argv[2])
    criteria_payload = load(sys.argv[3]) if len(sys.argv) == 4 else {}

    repos = scan.get("repos", {})
    items = scan.get("items", [])
    criteria_index = {}
    for entry in (
        criteria_payload.get("criteria", [])
        if isinstance(criteria_payload, dict)
        else []
    ):
        if not isinstance(entry, dict) or not isinstance(entry.get("criteria"), list):
            continue
        try:
            entry_number = int(entry.get("number") or 0)
        except (TypeError, ValueError):
            continue
        key = (str(entry.get("repo") or ""), entry_number)
        if not key[0] or not key[1] or key in criteria_index:
            continue
        criteria_index[key] = entry

    def attach_automerge_criteria(item):
        if item.get("kind") != "pr-review":
            return item
        key = (str(item.get("repo") or ""), int(item.get("number") or 0))
        entry = criteria_index.get(key)
        if not entry or str(entry.get("head_sha") or "") != str(
            item.get("head_sha") or ""
        ):
            return item
        enriched = dict(item)
        enriched[render_card.AUTOMERGE_CRITERIA_FIELD] = entry["criteria"]
        return enriched

    items = [attach_automerge_criteria(item) for item in items]
    for repo_result in repos.values():
        if not isinstance(repo_result, dict):
            continue
        repo_result["ci_wait_refresh_items"] = [
            attach_automerge_criteria(item)
            for item in (repo_result.get("ci_wait_refresh_items") or [])
        ]

    # Index existing open cards by their target (repo, number) from the state block.
    existing = {}  # (repo, number) -> existing card row
    cards_with_state = []  # existing card rows with parsed state
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
            "updated_at": card.get("updated_at", ""),
        }
        existing[key] = row
        cards_with_state.append(row)

    worklist_keys = {(item["repo"], int(item["number"])) for item in items}

    # 1) For each scanned worklist item, create a card if none exists, else
    #    refresh it in place when its target materially changed, its card
    #    render_version is stale, or a held card should now be published. If no
    #    full refresh is needed, reflect newer target activity with a hidden
    #    state-only edit. Items only come from ok:true repos (build_repo returns
    #    no items for a failed scan), so this path never refreshes or
    #    activity-stamps a card for a repo whose state is unknown.
    created = 0
    refreshed = 0
    activity_reflected = 0
    triage_queued = 0
    has_triage_token = auto_triage_has_token()
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    for item in items:
        key = (item["repo"], int(item["number"]))
        ex = existing.get(key)
        current_for_triage = None
        if ex is None:
            try:
                # Read the fresh card back BY NUMBER (current_card ->
                # get_card), never via find_card's label-filtered listing:
                # that listing is not read-after-write consistent right after
                # `gh issue create`, so it would silently miss the card just
                # created and skip queuing its first auto-triage attempt.
                # `has_token` also decides whether an eligible new card is
                # created HELD (see AGENTS.md "Held cards") - the same signal
                # that gates whether triage below actually gets queued.
                number = render_card.upsert_card(item, has_token=has_triage_token)
                created += 1
                current_for_triage = (
                    current_card({"number": number}) if number else None
                )
            except Exception as e:  # one bad item must not abort the whole pass
                print(
                    "::warning::failed to create card for %s#%s: %s"
                    % (item["repo"], item["number"], str(e)[:160])
                )
            if maybe_queue_auto_triage(
                item, current_for_triage, has_triage_token, owner=owner
            ):
                triage_queued += 1
            continue
        # Card exists: refresh only a pure needs-decision card whose target
        # materially changed, whose stored render_version is behind current,
        # or whose held state no longer has a completion path. If no full
        # refresh or triage queued write is needed, a newer target updated_at can
        # get one hidden state-only activity reflection. A card mid-decision
        # (processing/resolved/blocked), or with no trigger at all, is left
        # completely untouched. `upsert_card` re-checks both guards before it edits.
        refreshable = render_card.is_refreshable(ex["labels"])
        needs_full_refresh = refreshable and render_card.refresh_needed(
            item, ex["state"], has_triage_token
        )
        if needs_full_refresh:
            try:
                current = current_card(ex)
                current_for_triage = current
                if current is not None and render_card.is_refreshable(
                    current["labels"]
                ):
                    still_stale = render_card.refresh_needed(
                        item, current["state"], has_triage_token
                    )
                    if still_stale:
                        render_card.upsert_card(
                            item, existing=current, has_token=has_triage_token
                        )
                        refreshed += 1
                        current_for_triage = current_card(current)
            except Exception as e:
                print(
                    "::warning::failed to refresh card #%s for %s#%s: %s"
                    % (ex["number"], item["repo"], item["number"], str(e)[:160])
                )
        elif refreshable and render_card.should_auto_triage(
            item, ex["state"], ex["labels"], has_triage_token
        ):
            current_for_triage = current_card(ex)
        elif render_card.activity_reflection_needed(
            item, ex["state"], ex["labels"], card_updated_at=ex.get("updated_at", "")
        ):
            try:
                current = current_card(ex)
                current_for_triage = current
                if (
                    current is not None
                    and render_card.is_refreshable(current["labels"])
                    and not render_card.refresh_needed(
                        item, current["state"], has_triage_token
                    )
                    and not render_card.should_auto_triage(
                        item, current["state"], current["labels"], has_triage_token
                    )
                    and render_card.activity_reflection_needed(
                        item,
                        current["state"],
                        current["labels"],
                        card_updated_at=current.get("updated_at", ""),
                    )
                ):
                    if render_card.reflect_activity(
                        current["number"],
                        item,
                        current.get("body", ""),
                        card_updated_at=current.get("updated_at", ""),
                    ):
                        activity_reflected += 1
                        current_for_triage = current_card(current)
            except Exception as e:
                print(
                    "::warning::failed to reflect activity on card #%s for %s#%s: %s"
                    % (ex["number"], item["repo"], item["number"], str(e)[:160])
                )
        if current_for_triage is None and render_card.should_auto_triage(
            item, ex["state"], ex["labels"], has_triage_token
        ):
            current_for_triage = current_card(ex)
        if maybe_queue_auto_triage(
            item, current_for_triage, has_triage_token, owner=owner
        ):
            triage_queued += 1

    # 1b) Anti-masquerade for the approve/wait window. A PR whose fork CI was just
    #     auto-approved this scan, or whose approved checks are still running, emits
    #     NO worklist item while it awaits terminal checks - so its existing
    #     pr-review card would keep displaying the pre-rebase head's (now
    #     superseded) state, e.g. a stale merge-ready/green that masquerades as
    #     current. When the scan OBSERVES the head has moved, refresh that existing
    #     card in place to the new head's honest pending state. This NEVER creates a
    #     card (creation still defers until checks are terminal), only refreshes a
    #     same-kind pure needs-decision card, and never queues triage for this
    #     transient revision. Frozen-from-consumption is handled in the close loop
    #     below via `ci_wait_pr_numbers`.
    antimasq_refreshed = 0
    for repo_name, r in repos.items():
        if not r or not r.get("ok") or r.get("truncated"):
            continue
        for item in r.get("ci_wait_refresh_items", []) or []:
            key = (item["repo"], int(item["number"]))
            ex = existing.get(key)
            if ex is None:
                continue  # no existing card -> defer creation until checks terminal
            if ex["state"].get("kind") != item.get("kind"):
                continue  # only refresh a same-kind (pr-review) card in place
            if not render_card.is_refreshable(ex["labels"]):
                continue
            if not render_card.material_changed(item, ex["state"]):
                continue  # card already reflects the new head -> no churn
            try:
                current = current_card(ex)
                if (
                    current is not None
                    and render_card.is_refreshable(current["labels"])
                    and current["state"].get("kind") == item.get("kind")
                    and render_card.material_changed(item, current["state"])
                ):
                    render_card.upsert_card(
                        item, existing=current, has_token=has_triage_token
                    )
                    antimasq_refreshed += 1
            except Exception as e:
                print(
                    "::error::failed anti-masquerade refresh for card #%s "
                    "(%s#%s): %s - card left FROZEN at the stale head "
                    "(not consumed, not actable until re-checked) and retried "
                    "on the next scan"
                    % (ex["number"], item["repo"], item["number"], str(e)[:160])
                )

    # 2) Close cards whose target is no longer open, and pure pending cards whose
    #    open target no longer appears in the current maintainer worklist (for
    #    example author-excluded targets or conflicted PRs now waiting on
    #    contributor rebase). Skip repos that failed to scan (ok:false) - we don't
    #    know their state.
    closed = 0
    for ex in cards_with_state:
        card_number = ex["number"]
        state = ex["state"]
        if state.get("automerge_audit_pending") or state.get("automerge_audit_intent"):
            continue
        repo = state.get("repo")
        r = repos.get(repo)
        if not r or not r.get("ok"):
            continue
        if r.get("truncated"):
            continue
        number = int(state.get("number", 0))
        kind = state.get("kind", "pr-review")
        if kind in PR_KINDS and number in set(r.get("indeterminate_pr_numbers", [])):
            # This PR's mergeability was unreadable this scan (UNKNOWN did not
            # settle), so the scan cannot say whether it belongs in the worklist.
            # Freeze the card exactly as-is: an UNKNOWN reading must never
            # create, close, or consume a card. Same fail-safe family as the
            # ok:false / truncated repo skips above (act only on known state) -
            # NOT the separate, out-of-scope K-consecutive-absence soft-close
            # hysteresis.
            continue
        if kind in PR_KINDS and number in set(r.get("ci_wait_pr_numbers", [])):
            # This PR's fork CI was auto-approved this scan (or its approved
            # checks are still running), so it emitted no worklist item WHILE it
            # awaits terminal checks. Freeze the card exactly like the
            # indeterminate case: never consume a card merely because its target
            # is mid-approval/CI-wait (approve -> wait-for-terminal -> classify
            # continuity). Step 1b already refreshed its display to the new head;
            # a later scan, once checks are terminal, reclassifies it into a real
            # bucket and either refreshes it via a worklist item or self-heals it.
            # Terminal checks always release the freeze, so it can never wedge a
            # card permanently.
            continue
        open_set = set(
            r.get("open_pr_numbers", [])
            if kind in PR_KINDS
            else r.get("open_issue_numbers", [])
        )
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
            open_set = set(
                r.get("open_pr_numbers", [])
                if kind in PR_KINDS
                else r.get("open_issue_numbers", [])
            )
            current_key = (repo, number)
            if number not in open_set or current_key in worklist_keys:
                continue
            if not render_card.is_refreshable(current["labels"]):
                continue
            msg = (
                "Self-healed by the scheduled backstop: %s#%s no longer needs "
                "a maintainer decision in the current scan - consuming this "
                "card." % (repo, number)
            )
        else:
            current = current_card(ex)
            if current is None:
                continue
            if current["state"].get("automerge_audit_pending") or current["state"].get(
                "automerge_audit_intent"
            ):
                continue
            card_number = current["number"]
            msg = (
                "Self-healed by the scheduled backstop: %s#%s is no longer open "
                "(merged/closed) - consuming this card." % (repo, number)
            )
        try:
            render_card.close_card(card_number, msg)
            closed += 1
        except Exception as e:
            print(
                "::warning::failed to close card #%s: %s" % (card_number, str(e)[:160])
            )

    print(
        "reconcile: %d card(s) created, %d refreshed, %d anti-masquerade "
        "refreshed, %d activity reflected, %d auto-triage queued, %d card(s) closed"
        % (
            created,
            refreshed,
            antimasq_refreshed,
            activity_reflected,
            triage_queued,
            closed,
        )
    )


if __name__ == "__main__":
    main()
