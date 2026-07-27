#!/usr/bin/env python3
"""Bounded display-only migration for incomplete-observation cards.

Every check is offline: no network call, no live card or target is read or
mutated, and no model runs. The cohort fixtures in
`tests/fixtures/frozen_observation_cards.json` are the EXACT production bodies of
the five open `no-mistakes` PR-review cards that the `CARD_RENDER_VERSION`
11 -> 12 canonical-recommendation migration correctly refused, captured
2026-07-27T07:36Z after scan-backstop run 30244348404:

  #531  -> no-mistakes#450, retired deterministic `### Recommended action`
  #1392 -> no-mistakes#491, retired advisory `Recommended next step`
  #1562 -> no-mistakes#542, retired advisory `Recommended next step`
  #1585 -> no-mistakes#547, retired advisory `Recommended next step`
  #1594 -> no-mistakes#549, retired advisory `Recommended next step`

All five are stored at `render_version: 8`, so they had already missed three
earlier display migrations for the same reason.
"""

import copy
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import presentation_migration as pm  # noqa: E402
import render_card as rc  # noqa: E402

FAILURES = []
COHORT = json.loads(
    (ROOT / "tests" / "fixtures" / "frozen_observation_cards.json").read_text(
        encoding="utf-8"
    )
)
CARD_NUMBERS = sorted(int(key) for key in COHORT)


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def fixture(number):
    return copy.deepcopy(COHORT[str(number)])


def card_of(number, **overrides):
    """The `get_card`-shaped object for one fixture."""
    row = fixture(number)
    card = {
        "number": row["number"],
        "title": "[%s] fixture" % row["target"]["repo"],
        "body": row["body"],
        "labels": [{"name": name} for name in row["labels"]],
        "state": "OPEN",
        "updatedAt": "2026-07-27T07:36:00Z",
        "author": {"login": rc.GET_CARD_AUTOMATION_AUTHOR},
        "url": row["url"],
    }
    card.update(overrides)
    return card


class World:
    """In-memory GitHub boundary: `get_card`, target head, and body writes."""

    def __init__(self, numbers=None, head_override=None):
        self.cards = {n: card_of(n) for n in (numbers or CARD_NUMBERS)}
        self.writes = []
        self.head_override = head_override or {}

    def get_card(self, number):
        card = self.cards.get(int(number))
        return copy.deepcopy(card) if card else None

    def live_head(self, state):
        target = "%s#%s" % ((state or {}).get("repo"), (state or {}).get("number"))
        if target in self.head_override:
            return self.head_override[target]
        return str((state or {}).get("head_sha") or "")

    def edit(self, number, body_path_or_body):
        raise AssertionError("unexpected raw write")

    def install(self):
        self._saved = (pm.render_card.get_card, pm.live_head, rc._gh)
        pm.render_card.get_card = self.get_card
        pm.live_head = self.live_head

        def gh(args, check=True):
            if args[:2] == ["issue", "edit"]:
                number = int(args[2])
                path = args[args.index("--body-file") + 1]
                body = Path(path).read_text(encoding="utf-8")
                self.cards[number]["body"] = body
                self.writes.append((number, body))

                class R:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return R()
            raise AssertionError("unexpected gh call: %s" % args)

        rc._gh = gh
        return self

    def restore(self):
        pm.render_card.get_card, pm.live_head, rc._gh = self._saved


# --------------------------------------------------------------------------- #
# The exact production cohort
# --------------------------------------------------------------------------- #
def test_cohort_fixtures_are_the_frozen_production_shapes():
    check("cohort: exactly five cards are pinned", CARD_NUMBERS == [531, 1392, 1562, 1585, 1594])
    for number in CARD_NUMBERS:
        row = fixture(number)
        state = rc._unique_state_block(row["body"])
        check(
            "cohort #%s: pr-review card frozen at render_version 8" % number,
            state["kind"] == "pr-review" and state["render_version"] == 8,
        )
        check(
            "cohort #%s: still shows retired recommendation presentation" % number,
            tuple(rc.legacy_recommendation_presentation(row["body"]))
            == tuple(row["surfaces"]),
        )
        check(
            "cohort #%s: the whole-card writer cannot reach it" % number,
            state.get("bucket") == "ci-state-unknown"
            and "review_observation" not in state
            and state.get("projection_owner") is None,
        )
    check(
        "cohort: both retired surfaces are represented",
        {tuple(fixture(n)["surfaces"]) for n in CARD_NUMBERS}
        == {("deterministic-section",), ("advisory-next-step",)},
    )


def test_migration_is_deletions_only_and_preserves_authority():
    for number in CARD_NUMBERS:
        before = fixture(number)["body"]
        after = rc.presentation_migration_body(before)
        ok, reason = rc.presentation_migration_verify(before, after)
        check("migrate #%s: verification passes (%s)" % (number, reason or "ok"), ok)
        check(
            "migrate #%s: retired presentation is gone" % number,
            rc.legacy_recommendation_presentation(after) == (),
        )
        check(
            "migrate #%s: hidden state block is byte-identical" % number,
            rc._STATE_BLOCK_RE.search(before).group(0)
            == rc._STATE_BLOCK_RE.search(after).group(0),
        )
        check(
            "migrate #%s: render_version is NOT advanced" % number,
            rc._unique_state_block(after)["render_version"] == 8,
        )
        check(
            "migrate #%s: the card stays render-stale for the real migration" % number,
            rc.render_stale(rc._unique_state_block(after)),
        )
        added = set(after.split("\n")) - set(before.split("\n"))
        check(
            "migrate #%s: not one line is added" % number,
            not [line for line in added if line.strip()],
        )
        removed = [
            line
            for line in set(before.split("\n")) - set(after.split("\n"))
            if line.strip()
        ]
        check(
            "migrate #%s: every removed line is allowlisted" % number,
            removed
            and set(removed) <= rc.presentation_removable_lines(before),
        )
        for marker in (
            "### Situation",
            "### Auto-merge criteria",
            rc.DECISION_START,
            rc.DECISION_END,
            rc.TRIAGE_START,
            rc.TRIAGE_END,
            "<!-- opt:",
        ):
            check(
                "migrate #%s: %s preserved" % (number, marker[:24]),
                before.count(marker) == after.count(marker),
            )
        check(
            "migrate #%s: idempotent (second pass is a no-op)" % number,
            rc.presentation_migration_body(after) == after,
        )


def test_verification_fails_closed():
    before = fixture(1392)["body"]
    good = rc.presentation_migration_body(before)

    check(
        "fail-closed: an unchanged body is refused",
        rc.presentation_migration_verify(before, before)
        == (False, "no retired recommendation presentation to remove"),
    )
    added = good.replace("### Situation", "### Situation\n- Injected: yes", 1)
    check(
        "fail-closed: an added line is refused",
        rc.presentation_migration_verify(before, added)[0] is False,
    )
    blank_added = good.replace("### Situation\n", "### Situation\n\n", 1)
    check(
        "fail-closed: an added blank line is refused",
        rc.presentation_migration_verify(before, blank_added)[0] is False,
    )
    edited = good.replace("- Compliance: `unknown`", "- Compliance: `pass`", 1)
    check(
        "fail-closed: a modified observation-derived fact is refused",
        edited == good or rc.presentation_migration_verify(before, edited)[0] is False,
    )
    state = rc._unique_state_block(good)
    bumped = dict(state)
    bumped["render_version"] = rc.CARD_RENDER_VERSION
    check(
        "fail-closed: advancing render_version is refused",
        rc.presentation_migration_verify(
            before, rc._replace_state_block(good, bumped)
        )[0]
        is False,
    )
    tampered = dict(state)
    tampered["triage_recommendation"] = {"action": "merge", "reason": "forged"}
    check(
        "fail-closed: injecting authority state is refused",
        rc.presentation_migration_verify(
            before, rc._replace_state_block(good, tampered)
        )[0]
        is False,
    )
    dropped = good.replace("- [ ] Merge it <!-- opt:merge -->\n", "", 1)
    check(
        "fail-closed: removing a decision option is refused",
        rc.presentation_migration_verify(before, dropped)[0] is False,
    )
    next_step = next(
        line
        for line in before.splitlines()
        if line.startswith(rc.PRESENTATION_NEXT_STEP_PREFIX)
    )
    duplicate_before = before.replace(
        "### Situation\n", "### Situation\n%s\n" % next_step, 1
    )
    duplicate_good = rc.presentation_migration_body(duplicate_before)
    duplicate_removed = duplicate_good.replace(next_step + "\n", "", 1)
    check(
        "fail-closed: an identical line outside its approved span is preserved",
        next_step in duplicate_good
        and rc.presentation_migration_verify(
            duplicate_before, duplicate_removed
        )[0]
        is False,
    )
    nosection = "no state block at all"
    check(
        "fail-closed: a body without a state block is refused",
        rc.presentation_migration_verify(before, nosection)[0] is False,
    )
    check(
        "fail-closed: the verified writer refuses an unverified body",
        _raises(lambda: rc.edit_presentation_only_body(1392, before, added)),
    )


def test_canonical_recommendation_survives_legacy_bullet_migration():
    before = fixture(1392)["body"]
    with_canonical = rc._set_recommendation_section(
        before,
        {"action": "merge", "reason": "Current admitted assessment."},
    )
    canonical_before = rc._RECOMMENDATION_SECTION_RE.search(with_canonical).group(0)
    after = rc.presentation_migration_body(with_canonical)
    canonical_after = rc._RECOMMENDATION_SECTION_RE.search(after).group(0)
    check(
        "canonical: only the advisory next-step surface is classified",
        rc.legacy_recommendation_presentation(with_canonical)
        == (rc.LEGACY_ADVISORY_NEXT_STEP,),
    )
    check(
        "canonical: admitted recommendation is byte-identical",
        canonical_after == canonical_before,
    )
    check(
        "canonical: migration remains verified",
        rc.presentation_migration_verify(with_canonical, after)[0] is True,
    )


def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


# --------------------------------------------------------------------------- #
# Cohort preflight and bounded apply
# --------------------------------------------------------------------------- #
def test_dry_run_writes_nothing_and_plans_the_whole_cohort():
    world = World().install()
    try:
        report = pm.run(CARD_NUMBERS, apply_changes=False)
    finally:
        world.restore()
    check("dry-run: outcome is dry-run", report["outcome"] == "dry-run")
    check("dry-run: no card was written", world.writes == [])
    check(
        "dry-run: every cohort member is planned for migration",
        sorted(row["card"] for row in report["migrate"]) == CARD_NUMBERS
        and not report["blocked"]
        and not report["noop"],
    )
    check(
        "dry-run: the plan reports the exact removed lines",
        all(row["removed_lines"] for row in report["migrate"]),
    )


def test_apply_migrates_the_cohort_and_is_idempotent():
    world = World().install()
    try:
        report = pm.run(CARD_NUMBERS, apply_changes=True)
        second = pm.run(CARD_NUMBERS, apply_changes=True)
    finally:
        world.restore()
    check("apply: outcome is applied", report["outcome"] == "applied")
    check("apply: exactly five body writes", len(world.writes) == 5)
    check(
        "apply: every result verified after write",
        all(r["applied"] and r["verified"] for r in report["results"]),
    )
    for number in CARD_NUMBERS:
        body = world.cards[number]["body"]
        check(
            "apply #%s: card is clean and still render-stale" % number,
            rc.legacy_recommendation_presentation(body) == ()
            and rc._unique_state_block(body)["render_version"] == 8,
        )
    check(
        "apply: a repeated run writes nothing and reports noop",
        len(world.writes) == 5
        and second["outcome"] == "noop"
        and sorted(r["card"] for r in second["noop"]) == CARD_NUMBERS,
    )


def test_one_ambiguous_member_denies_the_entire_run():
    for label, world in (
        (
            "moved target head",
            World(head_override={"no-mistakes#491": "f" * 40}),
        ),
        (
            "decision in flight",
            World(),
        ),
        (
            "human-authored card",
            World(),
        ),
        (
            "closed card",
            World(),
        ),
    ):
        if label == "decision in flight":
            world.cards[1562]["labels"].append({"name": "processing"})
        if label == "human-authored card":
            world.cards[1585]["author"] = {"login": "kunchenguid"}
        if label == "closed card":
            world.cards[1594]["state"] = "CLOSED"
        world.install()
        try:
            report = pm.run(CARD_NUMBERS, apply_changes=True)
        finally:
            world.restore()
        check(
            "atomic: %s denies the whole run before any write" % label,
            report["outcome"] == "denied"
            and world.writes == []
            and len(report["blocked"]) == 1,
        )


def test_later_member_change_denies_before_first_write():
    world = World().install()
    original_plan = pm.plan

    def plan_then_mutate(cohort):
        rows = original_plan(cohort)
        world.cards[1594]["labels"].append({"name": "processing"})
        return rows

    pm.plan = plan_then_mutate
    try:
        report = pm.run(CARD_NUMBERS, apply_changes=True)
    finally:
        pm.plan = original_plan
        world.restore()
    check(
        "atomic: a later member changing after planning denies before writes",
        report["outcome"] == "denied"
        and world.writes == []
        and [row["card"] for row in report["blocked"]] == [1594],
    )


def test_out_of_scope_cards_are_never_migrated():
    world = World()
    # An issue-triage card and a card with no retired surface both refuse.
    world.cards[531]["body"] = world.cards[531]["body"].replace(
        '"kind":"pr-review"', '"kind":"issue-triage"', 1
    )
    world.cards[1392]["body"] = rc.presentation_migration_body(
        world.cards[1392]["body"]
    )
    world.install()
    try:
        report = pm.run([531, 1392], apply_changes=True)
    finally:
        world.restore()
    check(
        "scope: a non-pr-review card blocks and nothing is written",
        report["outcome"] == "denied" and world.writes == [],
    )
    check(
        "scope: an already-clean card is a noop, never a rewrite",
        any(row["card"] == 1392 and row["action"] == "noop" for row in report["noop"]),
    )


def test_cohort_selector_is_bounded_and_fails_closed():
    check("selector: parses an exact cohort", pm.parse_cohort("531,1392") == [531, 1392])
    for bad in ("", "  ", "531,abc", "531,531", "0", "-1", ",".join(str(n) for n in range(1, 40))):
        check(
            "selector: refuses %r" % (bad[:24],),
            _raises(lambda bad=bad: pm.parse_cohort(bad)),
        )


def test_no_target_or_label_writes_are_possible():
    source = (ROOT / "scripts" / "presentation_migration.py").read_text(encoding="utf-8")
    for forbidden in (
        "--add-label",
        "--remove-label",
        "--title",
        "issue close",
        "issue comment",
        "pulls/%d/merge",
        "workflow run",
        "FLEET_TOKEN",
    ):
        check(
            "boundary: module never uses %r" % forbidden,
            forbidden not in source,
        )
    check(
        "boundary: the module owns no body write of its own",
        "edit_presentation_only_body" in source
        and "--body-file" not in source,
    )
    writer = (ROOT / "scripts" / "render_card.py").read_text(encoding="utf-8")
    check(
        "boundary: the pr-review authoritative-writer rule is unchanged",
        "pr-review projection bypassed the authoritative writer" in writer,
    )
    check(
        "boundary: the whole-card observation guard is unchanged",
        "defer card refresh for %s: current PR observation " in writer,
    )


def main():
    prior = os.environ.get("GITHUB_REPOSITORY_OWNER")
    os.environ["GITHUB_REPOSITORY_OWNER"] = "kunchenguid"
    try:
        test_cohort_fixtures_are_the_frozen_production_shapes()
        test_migration_is_deletions_only_and_preserves_authority()
        test_verification_fails_closed()
        test_canonical_recommendation_survives_legacy_bullet_migration()
        test_dry_run_writes_nothing_and_plans_the_whole_cohort()
        test_apply_migrates_the_cohort_and_is_idempotent()
        test_one_ambiguous_member_denies_the_entire_run()
        test_later_member_change_denies_before_first_write()
        test_out_of_scope_cards_are_never_migrated()
        test_cohort_selector_is_bounded_and_fails_closed()
        test_no_target_or_label_writes_are_possible()
    finally:
        if prior is None:
            os.environ.pop("GITHUB_REPOSITORY_OWNER", None)
        else:
            os.environ["GITHUB_REPOSITORY_OWNER"] = prior
    if FAILURES:
        print("\n%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        sys.exit(1)
    print("\nall presentation-migration tests passed")


if __name__ == "__main__":
    main()
