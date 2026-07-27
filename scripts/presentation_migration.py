#!/usr/bin/env python3
"""Bounded display-only migration for cards the observation-bound writer cannot reach.

A PR-review card body is an observation-bound projection, so `upsert_card`
refuses to re-render one whose current target observation is incomplete
(`defer card refresh ...: current PR observation is unavailable`). That
whole-card guard is correct and is NOT touched here. Its side effect is that a
card parked in `ci-state-unknown` is excluded from every display-only renderer
migration for as long as it stays unobservable: the five `no-mistakes` cards the
`CARD_RENDER_VERSION` 11 -> 12 canonical-recommendation migration left behind had
already missed 8 -> 9, 9 -> 10, and 10 -> 11 exactly the same way.

This closes that gap and nothing else. The default migration REMOVES retired
recommendation presentation - the deterministic check-derived `### Recommended
action` section and the action-bearing `Recommended next step` bullet inside
`### Triage` - from an explicit operator-supplied cohort. The
`stale-accept-recommendation` migration is a separate deletion-only mode: it
removes exactly the renderer-inserted Accept checkbox only when the shipped
`accept_recommendation_available` gate is false for the card's parsed state.

Boundaries, enforced in code rather than by convention (the pure transform,
diff allowlist, invariant check, and the single verified writer all live in
`render_card.py` beside the rule they except):

  * Deletions only. The allowlist rejects any added or modified line, so this
    can never fabricate Situation, target facts, criteria, or a recommendation.
  * The hidden state block must be byte-identical, preserving hidden authority
    state and every observation-derived field exactly.
  * `render_version` is deliberately NOT advanced: the observation-bound
    migration did not run, so the card stays render-stale and a later complete
    observation still performs the ordinary full renderer migration.
  * Title, labels, options, target, and model cache are never written; the only
    write is one issue-body edit under the ambient default card token.
  * Every member is preflighted, including an exact live target-head match,
    before any write, and one ambiguous member denies the entire run.
  * Dry-run is the default; writing requires an explicit `--apply`.
  * Idempotent by content: a card with no retired surface is a no-op.

It never hand-edits outside those transformations, never clears valid card data,
never replays model triage, and never touches a target pull request.
"""

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import render_card  # noqa: E402
import wheelhouse_core as core  # noqa: E402

SCHEMA = "wheelhouse.presentation-migration/v1"
MAX_COHORT = 25
MIGRATION_RETIRED_RECOMMENDATION = "retired-recommendation"
MIGRATION_STALE_ACCEPT = "stale-accept-recommendation"
MIGRATIONS = frozenset(
    {MIGRATION_RETIRED_RECOMMENDATION, MIGRATION_STALE_ACCEPT}
)


def _event(kind, **fields):
    """Structured, content-free audit line on stderr (stdout carries the report)."""
    payload = {"schema": SCHEMA, "event": kind}
    payload.update(fields)
    print(
        "wheelhouse presentation-migration %s" % json.dumps(payload, sort_keys=True),
        file=sys.stderr,
    )


def _author_login(card):
    author = (card or {}).get("author")
    if isinstance(author, dict):
        author = author.get("login")
    return str(author or "").strip()


def _admission_fingerprint(card, target_head):
    payload = json.dumps(
        {"card": card, "target_head": target_head},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def live_head(state):
    """Exact live head SHA for the card's OPEN target PR, else ''. Read-only."""
    repo = str((state or {}).get("repo") or "")
    number = (state or {}).get("number")
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    if not repo or not isinstance(number, int) or not owner:
        return ""
    try:
        pr = core.gh_rest("/repos/%s/%s/pulls/%d" % (owner, repo, number))
    except Exception:
        return ""
    if not isinstance(pr, dict) or pr.get("state") != "open" or pr.get("merged_at"):
        return ""
    head = pr.get("head")
    return str(head.get("sha") or "") if isinstance(head, dict) else ""


def inspect_card(number, card, target_head, migration=MIGRATION_RETIRED_RECOMMENDATION):
    """Classify one cohort member. Pure given its inputs; no writes."""
    if migration not in MIGRATIONS:
        raise ValueError("unknown presentation migration: %s" % migration)
    row = {
        "card": int(number),
        "action": "skip",
        "reason": "",
        "url": "",
        "target": "",
        "migration": migration,
        "_admission_fingerprint": _admission_fingerprint(card, target_head),
    }
    if not isinstance(card, dict) or not card.get("body"):
        row["reason"] = "card is unreadable"
        return row
    row["url"] = card.get("url") or ""
    if not render_card.issue_is_open(card):
        row["reason"] = "card is not open"
        return row
    author = _author_login(card)
    if author not in (
        render_card.CARD_AUTOMATION_AUTHOR,
        render_card.GET_CARD_AUTOMATION_AUTHOR,
    ):
        row["reason"] = "card is not machine-created"
        return row
    labels = card.get("labels")
    names = render_card._label_names(labels)
    if "needs-decision" not in names:
        row["reason"] = "card is not a pending decision"
        return row
    if not render_card.is_refreshable(labels):
        row["reason"] = "decision in flight (not pure needs-decision)"
        return row
    state = render_card._unique_state_block(card.get("body") or "")
    if not state:
        row["reason"] = "card state block is missing or ambiguous"
        return row
    if state.get("kind") != "pr-review":
        row["reason"] = "card is not pr-review"
        return row
    row["target"] = "%s#%s" % (state.get("repo"), state.get("number"))
    row["render_version"] = state.get("render_version", 0)
    card_head = str(state.get("head_sha") or "")
    if not card_head:
        row["reason"] = "card records no head SHA"
        return row
    if not target_head:
        row["reason"] = "live target head is unavailable or target is not open"
        return row
    if target_head != card_head:
        row["reason"] = "target head moved (card %s, live %s)" % (
            card_head[:12],
            target_head[:12],
        )
        return row

    body = card["body"]
    if migration == MIGRATION_STALE_ACCEPT:
        if render_card.accept_recommendation_available(state):
            row["action"] = "noop"
            row["reason"] = "Accept recommendation is currently authorized"
            return row
        if not render_card.accept_recommendation_checkbox_present(body):
            row["action"] = "noop"
            row["reason"] = "no stale Accept recommendation checkbox"
            return row
        migrated = render_card.accept_recommendation_migration_body(body, state)
        ok, reason = render_card.accept_recommendation_migration_verify(
            body, migrated
        )
    else:
        surfaces = render_card.legacy_recommendation_presentation(body)
        if not surfaces:
            row["action"] = "noop"
            row["reason"] = "already free of retired recommendation presentation"
            return row
        row["surfaces"] = list(surfaces)
        migrated = render_card.presentation_migration_body(body)
        ok, reason = render_card.presentation_migration_verify(body, migrated)
    if not ok:
        row["reason"] = reason
        return row
    removed = set(body.split("\n")) - set(migrated.split("\n"))
    row["removed_lines"] = sorted(line[:120] for line in removed if line.strip())
    row["action"] = "migrate"
    return row


def plan(cohort, migration=MIGRATION_RETIRED_RECOMMENDATION):
    """Read-only plan for the exact cohort. No writes, no target mutations."""
    if migration not in MIGRATIONS:
        raise ValueError("unknown presentation migration: %s" % migration)
    rows = []
    for number in cohort:
        card = render_card.get_card(number)
        state = render_card._unique_state_block((card or {}).get("body") or "")
        rows.append(inspect_card(number, card, live_head(state), migration))
    return rows


def _public_rows(rows):
    return [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in rows
    ]


def admit_plan(rows, migration=MIGRATION_RETIRED_RECOMMENDATION):
    """Second-read the complete cohort before its first write."""
    admitted = []
    blocked = []
    for planned in rows:
        number = planned["card"]
        card = render_card.get_card(number)
        state = render_card._unique_state_block((card or {}).get("body") or "")
        current = inspect_card(number, card, live_head(state), migration)
        if (
            current["action"] != planned["action"]
            or current["_admission_fingerprint"]
            != planned["_admission_fingerprint"]
        ):
            if current["action"] != "skip":
                current["action"] = "skip"
                current["reason"] = "card changed after planning"
            blocked.append(current)
            continue
        admitted.append(current)
    return admitted, blocked


def apply_plan(rows, migration=MIGRATION_RETIRED_RECOMMENDATION):
    """Write the verified plan. Each card is re-read and re-verified first."""
    results = []
    for row in rows:
        number = row["card"]
        card = render_card.get_card(number)
        body = (card or {}).get("body") or ""
        state = render_card._unique_state_block(body)
        recheck = inspect_card(number, card, live_head(state), migration)
        if recheck["action"] != "migrate":
            _event("skipped", card=number, reason=recheck["reason"])
            results.append(
                dict(
                    recheck,
                    applied=False,
                    reason=recheck["reason"] or "card changed before write",
                )
            )
            continue
        if migration == MIGRATION_STALE_ACCEPT:
            migrated = render_card.accept_recommendation_migration_body(body, state)
            render_card.edit_accept_recommendation_only_body(number, body, migrated)
        else:
            migrated = render_card.presentation_migration_body(body)
            render_card.edit_presentation_only_body(number, body, migrated)
        after = render_card.get_card(number)
        verified = ((after or {}).get("body") or "") == migrated
        _event(
            "committed" if verified else "unverified",
            card=number,
            removed=len(recheck.get("removed_lines") or []),
            verified=verified,
        )
        if not verified:
            raise RuntimeError(
                "presentation migration for card #%s did not verify after write"
                % number
            )
        results.append(dict(recheck, applied=True, verified=True))
    return results


def parse_cohort(raw):
    """Parse the exact `--cards N,N` selector. Fails closed on anything odd."""
    parts = [part.strip() for part in str(raw or "").split(",") if part.strip()]
    if not parts:
        raise ValueError("an explicit --cards cohort is required")
    numbers = []
    for part in parts:
        if not part.isdigit() or int(part) <= 0:
            raise ValueError("cohort entry %r is not an issue number" % part)
        numbers.append(int(part))
    if len(set(numbers)) != len(numbers):
        raise ValueError("cohort contains duplicate cards")
    if len(numbers) > MAX_COHORT:
        raise ValueError("cohort exceeds the bounded maximum of %d" % MAX_COHORT)
    return numbers


def run(
    cohort,
    apply_changes=False,
    migration=MIGRATION_RETIRED_RECOMMENDATION,
):
    """Plan, then optionally apply. Returns the structured report."""
    if migration not in MIGRATIONS:
        raise ValueError("unknown presentation migration: %s" % migration)
    _event(
        "planning",
        cards=cohort,
        apply=bool(apply_changes),
        migration=migration,
    )
    rows = (
        plan(cohort)
        if migration == MIGRATION_RETIRED_RECOMMENDATION
        else plan(cohort, migration)
    )
    migrate = [row for row in rows if row["action"] == "migrate"]
    noop = [row for row in rows if row["action"] == "noop"]
    blocked = [row for row in rows if row["action"] == "skip"]
    report = {
        "schema": SCHEMA,
        "migration": migration,
        "mode": "apply" if apply_changes else "dry-run",
        "requested": len(cohort),
        "migrate": _public_rows(migrate),
        "noop": _public_rows(noop),
        "blocked": _public_rows(blocked),
    }
    if blocked:
        # Atomic: one ambiguous member denies the whole run before any write.
        report["outcome"] = "denied"
        _event("denied-run", blocked=[row["card"] for row in blocked])
        return report
    if not migrate:
        # Content-based idempotency: nothing retired remains, so there is
        # nothing to write in either mode.
        report["outcome"] = "noop"
        return report
    if not apply_changes:
        report["outcome"] = "dry-run"
        return report
    admitted, admission_blocked = admit_plan(rows, migration)
    if admission_blocked:
        report["blocked"] = _public_rows(admission_blocked)
        report["outcome"] = "denied"
        _event(
            "denied-run",
            blocked=[row["card"] for row in admission_blocked],
        )
        return report
    admitted_migrate = [row for row in admitted if row["action"] == "migrate"]
    report["results"] = _public_rows(apply_plan(admitted_migrate, migration))
    report["outcome"] = "applied"
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cards", required=True, help="exact cohort, e.g. 531,1392")
    ap.add_argument(
        "--migration",
        choices=sorted(MIGRATIONS),
        default=MIGRATION_RETIRED_RECOMMENDATION,
        help="bounded presentation correction to plan (default: retired-recommendation)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="perform the bounded body edits (default is dry-run)",
    )
    args = ap.parse_args()
    report = run(
        parse_cohort(args.cards),
        apply_changes=args.apply,
        migration=args.migration,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["outcome"] == "denied":
        sys.exit(3)


if __name__ == "__main__":
    main()
