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
    pure pending cards whose open target no longer needs a maintainer decision
    only after two consecutive conclusive scans - so the queue self-heals even
    if a dispatch was lost without churning on a one-scan bucket transition.
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
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wheelhouse_core as core  # noqa: E402
import render_card  # noqa: E402

PR_KINDS = {"pr-review", "ci-approval"}


def load(path):
    with open(path) as f:
        return json.load(f)


def load_optional_object(path):
    try:
        value = load(path)
    except (OSError, ValueError, TypeError) as e:
        print(
            "::warning::optional auto-merge criteria handoff is unavailable: %s"
            % str(e)[:160]
        )
        return {}
    return value if isinstance(value, dict) else {}


def _comment_count(value):
    if isinstance(value, list):
        return len(value)
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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
        "comments": _comment_count(card.get("comments")),
    }


def _label_names(labels):
    return {
        label if isinstance(label, str) else label.get("name", "")
        for label in (labels or [])
    }


def _matches_snapshot(current, snapshot):
    """Whether the live card still matches the scan-start card snapshot.

    Every reconcile state mutation and close is preceded by this live-card
    comparison. A checkbox edit, owner comment, label transition to processing,
    or any other card update after cards.json was listed defers maintenance to a
    later scan instead of racing the owner's action.
    """
    return bool(
        current
        and snapshot
        and int(current.get("number") or 0) == int(snapshot.get("number") or 0)
        and current.get("body", "") == snapshot.get("body", "")
        and _label_names(current.get("labels")) == _label_names(snapshot.get("labels"))
        and current.get("updated_at", "") == snapshot.get("updated_at", "")
        and _comment_count(current.get("comments"))
        == _comment_count(snapshot.get("comments"))
    )


def _matches_expected_write(current, before, expected_body):
    """Verify our state-only write landed and no owner/handler label raced it."""
    return bool(
        current
        and before
        and int(current.get("number") or 0) == int(before.get("number") or 0)
        and current.get("body", "") == expected_body
        and _label_names(current.get("labels")) == _label_names(before.get("labels"))
        and _comment_count(current.get("comments"))
        == _comment_count(before.get("comments"))
    )


def _soft_close_timestamp():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _reconcile_run_number():
    value = os.environ.get("GITHUB_RUN_NUMBER", "")
    if not value.isdigit():
        return 0
    number = int(value)
    return number if 1 <= number <= 9_007_199_254_740_991 else 0


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
    reconcile_run_number = _reconcile_run_number()
    criteria_payload = load_optional_object(sys.argv[3]) if len(sys.argv) == 4 else {}

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
            "comments": _comment_count(card.get("comments")),
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
        maintained_this_pass = False
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
                if (
                    current is not None
                    and _matches_snapshot(current, ex)
                    and render_card.is_refreshable(current["labels"])
                ):
                    still_stale = render_card.refresh_needed(
                        item, current["state"], has_triage_token
                    )
                    if still_stale:
                        render_card.upsert_card(
                            item,
                            existing=current,
                            has_token=has_triage_token,
                            expected_existing=current,
                        )
                        refreshed += 1
                        maintained_this_pass = True
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
                    and _matches_snapshot(current, ex)
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
                        maintained_this_pass = True
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
        if (
            current_for_triage is not None
            and not maintained_this_pass
            and not _matches_snapshot(current_for_triage, ex)
        ):
            # Do not queue any card-state mutation when owner/card activity
            # changed since the scan snapshot. A card we just refreshed is read
            # back immediately and is the sole expected mismatch.
            current_for_triage = None
        if maybe_queue_auto_triage(
            item, current_for_triage, has_triage_token, owner=owner
        ):
            triage_queued += 1

        # A conclusive worklist return resets soft-close hysteresis. Full
        # refresh, activity reflection, and triage-queue writes clear it in the
        # body they already write. If none of those paths wrote, perform one
        # state-only clear after a fresh card read. Exact snapshot matching keeps
        # this from racing a checkbox edit, owner comment, or processing label.
        if render_card.reconcile_absence_needs_clear(ex.get("body", "")):
            try:
                current = current_card(ex)
                if (
                    current is not None
                    and render_card.reconcile_absence_needs_clear(
                        current.get("body", "")
                    )
                    and _matches_snapshot(current, ex)
                    and render_card.is_refreshable(current.get("labels"))
                    and (
                        current["state"].get("repo"),
                        int(current["state"].get("number", 0)),
                    )
                    == key
                ):
                    render_card.clear_reconcile_absence(
                        current["number"], current.get("body", "")
                    )
            except Exception as e:
                print(
                    "::warning::failed to clear reconcile absence state on card "
                    "#%s for %s#%s: %s"
                    % (ex["number"], item["repo"], item["number"], str(e)[:160])
                )

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
                    and _matches_snapshot(current, ex)
                    and render_card.is_refreshable(current["labels"])
                    and current["state"].get("kind") == item.get("kind")
                    and render_card.material_changed(item, current["state"])
                ):
                    render_card.upsert_card(
                        item,
                        existing=current,
                        has_token=has_triage_token,
                        preserve_reconcile_absence=True,
                        expected_existing=current,
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

    # 2) Hard-close cards whose target is definitively no longer open. For an
    #    authoritatively still-open target that is outside the maintainer
    #    worklist, require two consecutive complete, conclusive scans before the
    #    existing soft-close path runs. Failed/truncated/UNKNOWN/CI-wait scans
    #    remain freezes: they neither increment nor reset this hidden state.
    closed = 0
    for ex in cards_with_state:
        state = ex["state"]
        if state.get("automerge_audit_pending") or state.get("automerge_audit_intent"):
            continue
        repo = state.get("repo")
        r = repos.get(repo)
        if not r or not r.get("ok") or r.get("truncated"):
            continue
        number = int(state.get("number", 0))
        kind = state.get("kind", "pr-review")
        if kind in PR_KINDS and number in set(r.get("indeterminate_pr_numbers", [])):
            # UNKNOWN did not settle, so membership is not authoritative.
            continue
        if kind in PR_KINDS and number in set(r.get("ci_wait_pr_numbers", [])):
            # Fork CI approval/running is another non-membership freeze.
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
            if not _matches_snapshot(current, ex):
                continue
            state = current["state"]
            repo = state.get("repo")
            number = int(state.get("number", 0))
            kind = state.get("kind", "pr-review")
            r = repos.get(repo)
            if not r or not r.get("ok") or r.get("truncated"):
                continue
            if kind in PR_KINDS and number in set(
                r.get("indeterminate_pr_numbers", [])
            ):
                continue
            if kind in PR_KINDS and number in set(r.get("ci_wait_pr_numbers", [])):
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

            count = render_card.reconcile_absence_count(current.get("body", ""))
            absence_run_number = render_card.reconcile_absence_run_number(
                current.get("body", "")
            )
            if not reconcile_run_number:
                continue
            expected_body = current.get("body", "")
            if count == 0:
                try:
                    render_card.update_reconcile_absence(
                        current["number"],
                        current.get("body", ""),
                        1,
                        run_number=reconcile_run_number,
                    )
                except Exception as e:
                    print(
                        "::warning::failed to record first reconcile absence on "
                        "card #%s: %s" % (current["number"], str(e)[:160])
                    )
                continue
            if count == 1 and absence_run_number != reconcile_run_number - 1:
                try:
                    render_card.update_reconcile_absence(
                        current["number"],
                        current.get("body", ""),
                        1,
                        run_number=reconcile_run_number,
                    )
                except Exception as e:
                    print(
                        "::warning::failed to restart reconcile absence on card "
                        "#%s: %s" % (current["number"], str(e)[:160])
                    )
                continue
            if count == 1:
                closed_at = _soft_close_timestamp()
                expected_body = render_card.body_with_reconcile_absence(
                    current.get("body", ""),
                    render_card.RECONCILE_ABSENCE_THRESHOLD,
                    run_number=reconcile_run_number,
                    closed_at=closed_at,
                )
                if expected_body == current.get("body", ""):
                    continue
                try:
                    if not render_card.update_reconcile_absence(
                        current["number"],
                        current.get("body", ""),
                        render_card.RECONCILE_ABSENCE_THRESHOLD,
                        run_number=reconcile_run_number,
                        closed_at=closed_at,
                    ):
                        continue
                except Exception as e:
                    print(
                        "::warning::failed to persist reconcile soft-close "
                        "provenance on card #%s: %s"
                        % (current["number"], str(e)[:160])
                    )
                    continue
                latest = current_card(current)
                if not _matches_expected_write(latest, current, expected_body):
                    continue
                current = latest
            elif count != render_card.RECONCILE_ABSENCE_THRESHOLD:
                continue
            elif absence_run_number not in (
                reconcile_run_number,
                reconcile_run_number - 1,
            ):
                try:
                    render_card.update_reconcile_absence(
                        current["number"],
                        current.get("body", ""),
                        1,
                        run_number=reconcile_run_number,
                    )
                except Exception as e:
                    print(
                        "::warning::failed to restart reconcile absence on card "
                        "#%s: %s" % (current["number"], str(e)[:160])
                    )
                continue

            # Re-read and validate the exact threshold/provenance state
            # immediately before closing. A failed close can safely retry on a
            # later qualifying scan because count 2 is bounded and fully formed.
            if (
                render_card.reconcile_absence_count(current.get("body", ""))
                != render_card.RECONCILE_ABSENCE_THRESHOLD
                or not render_card.reconcile_soft_close_provenance(
                    current.get("body", "")
                )
                or not render_card.is_refreshable(current.get("labels"))
            ):
                continue
            card_number = current["number"]
            msg = (
                "Self-healed by the scheduled backstop: %s#%s no longer needs "
                "a maintainer decision in the current scan - consuming this "
                "card." % (repo, number)
            )
        else:
            # Definitive target closure bypasses hysteresis, including blocked
            # or processing cards. The live snapshot comparison only defers when
            # an owner/handler transition raced this scan.
            current = current_card(ex)
            if not _matches_snapshot(current, ex):
                continue
            state = current["state"]
            if state.get("automerge_audit_pending") or state.get(
                "automerge_audit_intent"
            ):
                continue
            repo = state.get("repo")
            number = int(state.get("number", 0))
            kind = state.get("kind", "pr-review")
            r = repos.get(repo)
            if not r or not r.get("ok") or r.get("truncated"):
                continue
            live_open_set = set(
                r.get("open_pr_numbers", [])
                if kind in PR_KINDS
                else r.get("open_issue_numbers", [])
            )
            if number in live_open_set:
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
