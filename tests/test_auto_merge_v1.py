#!/usr/bin/env python3
"""
Unit-exercise Wheelhouse scan-time auto-merge (V1) with NO network and NO writes
to any target repository.

Run: python tests/test_auto_merge_v1.py   (needs PyYAML; no network)

Auto-merge is a strict subset of the manual merge gate: a merge-ready pr-review
PR is merged automatically ONLY when every deterministic gate passes AND a fresh
structured behavior verdict for the current head SHA assigns an eligible A/B/C
class and recommends merge. Any missing/stale/malformed/uncertain/unreadable
input HOLDS for human review. These tests cover, end-to-end through the
`act_on_scan` orchestrator with every gh call stubbed:

  * the config + exclusion helpers (`_auto_merge_enabled`, `_auto_merge_exclusions`);
  * the pure behavior-verdict gate (A/B/C eligibility, class-C opt-in/default-off,
    malformed/stale/absent verdicts, fail-closed defaults);
  * the blast-radius caps at the exact 20-file and 1000-line boundaries;
  * every deterministic gate G0-G6 (repo opt-in, base-branch VISION.md presence,
    returning contributor, unconditional file exclusions incl. VISION.md
    self-authorization, live green+CLEAN mergeability, blast radius) via
    representative live-card fixtures walked through PASS and HOLD outcomes;
  * the G7 live head + merge-state re-check immediately before acting;
  * the per-PR `wheelhouse:no-auto-merge` escape hatch and the global/per-repo
    kill switches;
  * the durable audit ledger (parse/append/render + cap) and the resolved
    decision record, plus the per-merge ::notice::/::warning:: audit lines;
  * base-branch-ONLY VISION.md reads (never the PR head);
  * the DELIBERATE ABSENCE of an open-PR file-overlap gate and of any
    per-contributor / per-scan rate cap (captain override).
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)
import wheelhouse_core as core  # noqa: E402
import render_card  # noqa: E402
import apply_decision  # noqa: E402
import auto_merge as am  # noqa: E402

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


ELIGIBLE_A = {
    "behavior_class": "A",
    "aligns_with_vision": True,
    "changes_existing_or_default_behavior": False,
    "recommend_merge": True,
    "vision_sha": "vsha",
}
ELIGIBLE_B = dict(ELIGIBLE_A, behavior_class="B")
ELIGIBLE_C = dict(
    ELIGIBLE_A, behavior_class="C", optin_default_off=True
)


# --------------------------------------------------------------------------- #
# fixture world: a controllable stub of every live read + the merge act
# --------------------------------------------------------------------------- #
def make_pr(
    head="h1" * 20,
    mergeable=True,
    mergeable_state="clean",
    additions=10,
    deletions=10,
    changed_files=2,
    author="alice",
    author_type="User",
    labels=None,
    merged=False,
    state="open",
    merge_commit_sha="mc" * 20,
):
    return {
        "head": {"sha": head},
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
        "additions": additions,
        "deletions": deletions,
        "changed_files": changed_files,
        "user": {"login": author, "type": author_type},
        "labels": [{"name": n} for n in (labels or [])],
        "merged": merged,
        "state": state,
        "merge_commit_sha": merge_commit_sha,
    }


def make_card(
    card_issue,
    repo,
    number,
    head,
    triage_status="succeeded",
    automerge_verdict=None,
    held=False,
    kind="pr-review",
    triage_recommendation="merge",
    labels=None,
    author=am.CARD_AUTOMATION_AUTHOR,
):
    state = {
        "repo": repo,
        "number": number,
        "kind": kind,
        "head_sha": head,
        "triaged_sha": head,
        "triage_status": triage_status,
    }
    if held:
        state["held"] = True
    if automerge_verdict is not None:
        state["automerge_verdict"] = automerge_verdict
    if triage_recommendation:
        state["triage_recommendation"] = {
            "action": triage_recommendation,
            "reason": "",
        }
    body = "Card\n\n<!-- wheelhouse-state: %s -->" % json.dumps(state)
    if labels is None:
        labels = [
            "needs-decision",
            "repo:%s" % repo,
            "kind:%s" % kind,
            "priority:med",
            "target:%s-%s" % (repo, number),
        ]
        if held:
            labels.append("pending-triage")
        else:
            labels.extend(["processing", am.AUTO_MERGE_CLAIM_LABEL])
    return {
        "number": card_issue,
        "body": body,
        "labels": [{"name": n} for n in labels],
        "author": author,
        "updatedAt": "2026-07-10T00:00:00Z",
    }


def make_item(repo, number, head, comp="pass", tests="green", bucket="merge-ready"):
    return {
        "repo": repo,
        "number": number,
        "kind": "pr-review",
        "bucket": bucket,
        "head_sha": head,
        "comp": comp,
        "tests": tests,
    }


class World:
    def __init__(self):
        self.owner = "owner"
        self.maintainers = {"owner"}
        self.global_auto_merge = True
        self.repos = {}  # repo -> repo_cfg dict
        self.vision = {}  # repo -> (present, sha)
        self.vision_seq = {}
        self.merged_authors = {}  # (slug, author) -> bool
        self.pr_seq = {}  # (slug, str(number)) -> [pr, ...]
        self.files = {}  # (slug, str(number)) -> (files, ok, complete)
        self.do_merge_calls = []
        self.do_merge_returns = {}  # (repo, number) -> (message, terminal)

    def set_pr(self, slug, number, prs):
        self.pr_seq[(slug, str(number))] = prs if isinstance(prs, list) else [prs]

    def live_pr(self, slug, number):
        seq = self.pr_seq.get((slug, str(number)))
        if not seq:
            return None
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def vision_on_default_branch(self, slug):
        repo = slug.split("/")[-1]
        seq = self.vision_seq.get(repo)
        if seq:
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return self.vision.get(repo, (False, ""))

    def do_merge(self, owner, repo, number, head):
        self.do_merge_calls.append((owner, repo, number, head))
        return self.do_merge_returns.get(
            (repo, str(number)), ("Merged %s#%s (squash)." % (repo, number), "resolved")
        )


def run_act(world, items, cards, has_token=True):
    """Install stubs, run act_on_scan, capture stderr, restore. Returns
    (payload, stderr_text)."""
    saved = {
        "vision": am.vision_on_default_branch,
        "prior": am.has_prior_merged_pr,
        "live": am.live_pr,
        "files": core._list_pr_files,
        "domerge": apply_decision.do_merge,
        "get_card": render_card.get_card,
        "cfg": core.load_config,
        "maint": core.maintainers,
        "owner": core.get_owner,
        "token": os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN"),
    }
    am.vision_on_default_branch = world.vision_on_default_branch
    am.has_prior_merged_pr = lambda slug, author: world.merged_authors.get(
        (slug, author), False
    )
    am.live_pr = world.live_pr
    core._list_pr_files = lambda slug, pr, expected=None: world.files.get(
        (slug, str(pr)), ([], True, True)
    )
    apply_decision.do_merge = world.do_merge
    core.load_config = lambda: {
        "auto_merge": world.global_auto_merge,
        "repos": world.repos,
    }
    core.maintainers = lambda: set(world.maintainers)
    core.get_owner = lambda: world.owner
    cards_by_number = {str(card["number"]): card for card in cards}

    def get_card(number):
        sequence = getattr(world, "card_seq", {}).get(str(number))
        if sequence:
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]
        return cards_by_number.get(str(number))

    render_card.get_card = get_card
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true" if has_token else "false"
    scan = {
        "repos": {
            it["repo"]: world.repos_scan.get(it["repo"], {"ok": True})
            for it in items
        }
        if hasattr(world, "repos_scan")
        else {it["repo"]: {"ok": True} for it in items},
        "items": items,
    }
    buf = io.StringIO()
    try:
        with redirect_stderr(buf):
            payload = am.act_on_scan(scan, cards)
    finally:
        am.vision_on_default_branch = saved["vision"]
        am.has_prior_merged_pr = saved["prior"]
        am.live_pr = saved["live"]
        core._list_pr_files = saved["files"]
        apply_decision.do_merge = saved["domerge"]
        render_card.get_card = saved["get_card"]
        core.load_config = saved["cfg"]
        core.maintainers = saved["maint"]
        core.get_owner = saved["owner"]
        if saved["token"] is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = saved["token"]
    return payload, buf.getvalue()


def default_world(head="h1" * 20, verdict=None, repo="fmt", number=5):
    """A world where everything passes; individual tests knock out one gate."""
    w = World()
    slug = "owner/%s" % repo
    w.repos = {repo: {"auto_merge": True}}
    w.vision = {repo: (True, "vsha")}
    w.merged_authors = {(slug, "alice"): True}
    w.files[(slug, str(number))] = (["src/a.py", "README.md"], True, True)
    w.set_pr(slug, number, make_pr(head=head))
    cards = [make_card(101, repo, number, head, automerge_verdict=verdict or ELIGIBLE_A)]
    items = [make_item(repo, number, head)]
    return w, items, cards


# --------------------------------------------------------------------------- #
# config + exclusion helpers
# --------------------------------------------------------------------------- #
def test_auto_merge_enabled_default_off_and_overrides():
    check("config: absent -> default off", core._auto_merge_enabled({}, False) is False)
    check(
        "config: per-repo true overrides global false",
        core._auto_merge_enabled({"auto_merge": True}, False) is True,
    )
    check(
        "config: per-repo false is the one-repo kill switch",
        core._auto_merge_enabled({"auto_merge": False}, True) is False,
    )
    check(
        "config: absent falls through to global",
        core._auto_merge_enabled({}, True) is True,
    )


def test_exclusions_cover_every_category():
    cases = {
        "workflow-action": ".github/workflows/ci.yml",
        "governance": ".github/CODEOWNERS",
        "dependency": "package-lock.json",
        "release": "release-please-config.json",
        "security": "SECURITY.md",
        "authentication": "lib/auth/session.go",
        "billing": "app/billing/stripe.py",
        "migration": "db/migrate/001_init.rb",
        "persistence": "db/schema.sql",
        "install-bootstrap": "Dockerfile",
        "public-default": "config/app.yml",
        "vision": "VISION.md",
    }
    for category, path in cases.items():
        hits = core._auto_merge_exclusions([path])
        check(
            "exclusion: %s (%s) held" % (category, path),
            any(h.startswith(category + ":") for h in hits),
        )
    check(
        "exclusion: ordinary source/docs/tests are NOT excluded",
        core._auto_merge_exclusions(["src/x.py", "docs/g.md", "tests/test_x.py"]) == [],
    )
    check(
        "exclusion: action.yml at any depth is a pwn-request hold",
        core._auto_merge_exclusions(["nested/action.yaml"]),
    )
    for path, category in (
        ("src/auth.py", "authentication"),
        ("src/authentication.py", "authentication"),
        ("src/permissions.py", "authentication"),
        ("src/permission.ts", "authentication"),
        ("src/iam.py", "authentication"),
        ("src/rbac.go", "authentication"),
        ("src/acl.rs", "authentication"),
        ("src/security.py", "security"),
        ("pom.xml", "dependency"),
        ("build.gradle", "dependency"),
        ("build.gradle.kts", "dependency"),
        ("settings.gradle", "dependency"),
        ("settings.gradle.kts", "dependency"),
        (".gitmodules", "dependency"),
        ("scripts/migrate.py", "migration"),
        ("scripts/migrate_data.py", "migration"),
        ("tools/migrate.py", "migration"),
    ):
        check(
            "exclusion: component filename %s is held" % path,
            any(
                hit.startswith(category + ":")
                for hit in core._auto_merge_exclusions([path])
            ),
        )


# --------------------------------------------------------------------------- #
# pure behavior-verdict gate: A/B/C, class-C opt-in, malformed/stale/absent
# --------------------------------------------------------------------------- #
def test_verdict_classes_ABC():
    for label, v in (("A", ELIGIBLE_A), ("B", ELIGIBLE_B), ("C", ELIGIBLE_C)):
        ok, cls, _ = am.verdict_eligible(v)
        check("verdict: class %s eligible" % label, ok is True and cls == label)


def test_verdict_class_C_requires_optin_default_off():
    ok, cls, reason = am.verdict_eligible(
        dict(ELIGIBLE_A, behavior_class="C")  # no optin_default_off
    )
    check("verdict: class C w/o opt-in held", ok is False and cls == "C")
    check("verdict: class C w/o opt-in reason", "opt-in" in reason)


def test_verdict_ineligible_and_fail_closed_defaults():
    ok, cls, _ = am.verdict_eligible(dict(ELIGIBLE_A, behavior_class="D"))
    check("verdict: non-ABC class held", ok is False and cls == "")
    ok, _, _ = am.verdict_eligible(dict(ELIGIBLE_A, aligns_with_vision=False))
    check("verdict: not-aligned held", ok is False)
    ok, _, _ = am.verdict_eligible(
        dict(ELIGIBLE_A, changes_existing_or_default_behavior=True)
    )
    check("verdict: ineligible existing/default behavior change held", ok is False)
    ok, _, _ = am.verdict_eligible(dict(ELIGIBLE_A, recommend_merge=False))
    check("verdict: not-recommended held", ok is False)
    for bad in (None, {}, {"behavior_class": "A"}, "merge", 3):
        ok, _, _ = am.verdict_eligible(bad)
        check("verdict: malformed %r held" % (bad,), ok is False)


def test_verdict_normalization_and_persistence_fail_closed():
    # A missing required boolean means the verdict is never persisted (hold).
    bad = {"behavior_class": "A", "aligns_with_vision": True, "recommend_merge": True}
    check(
        "verdict: normalize drops verdict missing a required field",
        render_card.normalize_automerge_verdict(bad) is None,
    )
    # A well-formed sub-object round-trips with booleans coerced.
    good = render_card.normalize_automerge_verdict(
        {
            "behavior_class": "b",
            "aligns_with_vision": "true",
            "changes_existing_or_default_behavior": "false",
            "recommend_merge": "true",
        }
    )
    check(
        "verdict: normalize coerces + upper-cases class",
        good
        and good["behavior_class"] == "B"
        and good["aligns_with_vision"] is True,
    )


# --------------------------------------------------------------------------- #
# blast-radius caps at the exact boundaries
# --------------------------------------------------------------------------- #
def test_blast_radius_boundaries():
    ok, _ = am.blast_radius_ok(20, 500, 500)
    check("blast: 20 files / 1000 lines EXACTLY -> ok", ok is True)
    ok, _ = am.blast_radius_ok(21, 1, 1)
    check("blast: 21 files -> held", ok is False)
    ok, _ = am.blast_radius_ok(5, 600, 401)
    check("blast: 1001 total lines -> held", ok is False)
    ok, _ = am.blast_radius_ok(1, 1000, 0)
    check("blast: 1000 additions + 0 deletions -> ok", ok is True)
    ok, _ = am.blast_radius_ok(None, 1, 1)
    check("blast: missing file count -> held (fail-closed)", ok is False)


# --------------------------------------------------------------------------- #
# end-to-end PASS path (A/B/C) via act_on_scan
# --------------------------------------------------------------------------- #
def test_happy_path_class_A_merges():
    w, items, cards = default_world()
    payload, err = run_act(w, items, cards)
    check("act: class A merge-ready PR is merged", len(payload["merges"]) == 1)
    check("act: do_merge was called once", len(w.do_merge_calls) == 1)
    m = payload["merges"][0]
    check("act: merge record carries behavior class", m["behavior_class"] == "A")
    check("act: merge record carries contributor proof", bool(m["contributor_proof"]))
    check("act: merge record carries head + vision + commit",
          m["head_sha"] and m["vision_sha"] == "vsha" and m["merge_commit"])
    check("act: ::notice:: audit line emitted", "auto-merge merged" in err)


def test_happy_path_classes_B_and_C():
    for label, v in (("B", ELIGIBLE_B), ("C", ELIGIBLE_C)):
        w, items, cards = default_world(verdict=v)
        payload, _ = run_act(w, items, cards)
        check("act: class %s PR merges" % label, len(payload["merges"]) == 1
              and payload["merges"][0]["behavior_class"] == label)


def test_class_C_without_optin_holds_end_to_end():
    w, items, cards = default_world(verdict=dict(ELIGIBLE_A, behavior_class="C"))
    payload, err = run_act(w, items, cards)
    check("act: class C w/o opt-in holds", not payload["merges"] and payload["holds"])
    check("act: no merge attempted", not w.do_merge_calls)


# --------------------------------------------------------------------------- #
# each deterministic gate holds end-to-end (fail-closed)
# --------------------------------------------------------------------------- #
def _held_reason(payload):
    return payload["holds"][0]["hold_reason"] if payload["holds"] else ""


def test_G0_repo_not_opted_in_is_silently_skipped():
    # A non-opted-in repo is NOT an auto-merge candidate: silent skip (no merge,
    # no hold entry, no ::warning:: spam), not a logged hold.
    w, items, cards = default_world()
    w.repos = {"fmt": {"auto_merge": False}}
    payload, err = run_act(w, items, cards)
    check("G0: per-repo auto_merge off -> silent skip",
          not payload["merges"] and not payload["holds"])
    check("G0: per-repo off -> no merge", not w.do_merge_calls)
    check("G0: per-repo off -> no warning spam", "auto-merge held" not in err)
    # But evaluate_candidate still fails closed on G0a as defense in depth.
    r = am.evaluate_candidate("owner", items[0], {"issue": 1, "state": {}, "labels":
                              {"needs-decision"}}, {"auto_merge": False}, False,
                              set())
    check("G0: evaluate_candidate G0a defense-in-depth", "G0" in r["hold_reason"])


def test_G0_global_off_is_silently_skipped():
    w, items, cards = default_world()
    w.global_auto_merge = False
    w.repos = {"fmt": {}}  # no per-repo override -> global (off)
    payload, _ = run_act(w, items, cards)
    check("G0: global off + no override -> silent skip",
          not payload["merges"] and not payload["holds"] and not w.do_merge_calls)


def test_G0_no_vision_holds():
    w, items, cards = default_world()
    w.vision = {}  # no VISION.md on default branch
    payload, _ = run_act(w, items, cards)
    check("G0: missing VISION.md holds", "VISION.md" in _held_reason(payload))
    check("G0: missing VISION.md -> no merge", not w.do_merge_calls)


def test_G3_non_returning_contributor_holds():
    w, items, cards = default_world()
    w.merged_authors = {}  # author has no prior merged PR
    payload, _ = run_act(w, items, cards)
    check("G3: no prior merged PR holds", "G3" in _held_reason(payload))


def test_G3_bot_and_maintainer_author_hold():
    w, items, cards = default_world(head="hb" * 20)
    slug = "owner/fmt"
    w.set_pr(slug, 5, make_pr(head="hb" * 20, author="dependabot[bot]"))
    w.merged_authors = {(slug, "dependabot[bot]"): True}
    payload, _ = run_act(w, items, cards)
    check("G3: bot author holds", "G3" in _held_reason(payload))

    w_type, items_type, cards_type = default_world(head="bt" * 20)
    w_type.set_pr(
        slug,
        5,
        make_pr(head="bt" * 20, author="automation", author_type="Bot"),
    )
    w_type.merged_authors = {(slug, "automation"): True}
    payload_type, _ = run_act(w_type, items_type, cards_type)
    check("G3: REST Bot type author holds", "G3" in _held_reason(payload_type))

    w2, items2, cards2 = default_world(head="hm" * 20)
    w2.set_pr("owner/fmt", 5, make_pr(head="hm" * 20, author="owner"))
    w2.merged_authors = {("owner/fmt", "owner"): True}
    payload2, _ = run_act(w2, items2, cards2)
    check("G3: maintainer author holds", "G3" in _held_reason(payload2))


def test_G2_excluded_file_holds():
    w, items, cards = default_world()
    w.files[("owner/fmt", "5")] = ([".github/workflows/ci.yml", "src/a.py"], True, True)
    payload, _ = run_act(w, items, cards)
    check("G2: workflow file holds", "G2" in _held_reason(payload)
          and "excluded" in _held_reason(payload))
    check("G2: excluded file -> no merge", not w.do_merge_calls)


def test_G2_vision_self_authorization_excluded():
    w, items, cards = default_world()
    # A PR that edits the very rubric it is judged against must never auto-merge.
    w.files[("owner/fmt", "5")] = (["VISION.md", "src/a.py"], True, True)
    payload, _ = run_act(w, items, cards)
    check("G2: PR editing VISION.md is held (self-authorization guard)",
          "G2" in _held_reason(payload))


def test_G2_unreadable_file_list_fails_closed():
    w, items, cards = default_world()
    w.files[("owner/fmt", "5")] = ([], False, False)  # gh could not list files
    payload, _ = run_act(w, items, cards)
    check("G2: unreadable file list holds", "G2" in _held_reason(payload))


def test_G4_not_mergeable_or_not_clean_holds():
    for label, pr in (
        ("mergeable false", make_pr(head="h4" * 20, mergeable=False)),
        ("state blocked", make_pr(head="h4" * 20, mergeable_state="blocked")),
        ("state behind", make_pr(head="h4" * 20, mergeable_state="behind")),
        ("state unknown", make_pr(head="h4" * 20, mergeable=None,
                                  mergeable_state="unknown")),
    ):
        w, items, cards = default_world(head="h4" * 20)
        w.set_pr("owner/fmt", 5, pr)
        payload, _ = run_act(w, items, cards)
        check("G4: %s holds" % label, "G4" in _held_reason(payload))
        check("G4: %s -> no merge" % label, not w.do_merge_calls)


def test_G5_blast_radius_holds_and_boundary_merges():
    w, items, cards = default_world(head="h5" * 20)
    w.set_pr("owner/fmt", 5, make_pr(head="h5" * 20, changed_files=21))
    payload, _ = run_act(w, items, cards)
    check("G5: 21 files holds", "G5" in _held_reason(payload))

    w2, items2, cards2 = default_world(head="h6" * 20)
    w2.set_pr("owner/fmt", 5, make_pr(head="h6" * 20, additions=600, deletions=401))
    payload2, _ = run_act(w2, items2, cards2)
    check("G5: 1001 lines holds", "G5" in _held_reason(payload2))

    # Exact boundary: 20 files + 1000 lines merges.
    w3, items3, cards3 = default_world(head="h7" * 20)
    w3.set_pr("owner/fmt", 5,
              make_pr(head="h7" * 20, changed_files=20, additions=500, deletions=500))
    payload3, _ = run_act(w3, items3, cards3)
    check("G5: 20 files / 1000 lines boundary merges", len(payload3["merges"]) == 1)


def test_G6_stale_absent_and_held_verdict_hold():
    # Stale: card triaged_sha != current head.
    w, items, cards = default_world(head="cur" * 13 + "x")
    cards[0] = make_card(101, "fmt", 5, "old" * 13 + "y",
                         automerge_verdict=ELIGIBLE_A)
    # item/live head is the current one; card is for the old one.
    payload, _ = run_act(w, items, cards)
    check("G6: stale verdict (head mismatch) holds", "G6" in _held_reason(payload))

    # Absent verdict.
    w2, items2, cards2 = default_world(head="ab" * 20)
    cards2[0] = make_card(101, "fmt", 5, "ab" * 20, automerge_verdict=None)
    payload2, _ = run_act(w2, items2, cards2)
    check("G6: no verdict holds", "G6" in _held_reason(payload2))

    # triage_status not succeeded.
    w3, items3, cards3 = default_world(head="qd" * 20)
    cards3[0] = make_card(101, "fmt", 5, "qd" * 20, triage_status="queued",
                          automerge_verdict=ELIGIBLE_A)
    payload3, _ = run_act(w3, items3, cards3)
    check("G6: queued (not succeeded) verdict holds", "G6" in _held_reason(payload3))

    # Held card (auto-triage not published) never auto-merges.
    w4, items4, cards4 = default_world(head="he" * 20)
    cards4[0] = make_card(101, "fmt", 5, "he" * 20, held=True,
                          automerge_verdict=ELIGIBLE_A)
    payload4, _ = run_act(w4, items4, cards4)
    check("G6: held card holds", "held" in _held_reason(payload4).lower())

    w5, items5, cards5 = default_world(head="rm" * 20)
    cards5[0] = make_card(
        101,
        "fmt",
        5,
        "rm" * 20,
        automerge_verdict=ELIGIBLE_A,
        triage_recommendation="hold",
    )
    payload5, _ = run_act(w5, items5, cards5)
    check(
        "G6: non-merge triage recommendation contradicts verdict and holds",
        "top-level triage recommendation" in _held_reason(payload5),
    )


def test_G1_no_card_holds():
    w, items, cards = default_world()
    payload, _ = run_act(w, items, [])  # no card at all
    check("G1: no decision card holds", "G1" in _held_reason(payload))


def test_G1_non_pure_card_holds():
    # A card already mid-decision (processing/resolved/blocked) must never race
    # the manual/decision-handler path.
    for lbl in ("processing", "resolved", "blocked"):
        w, items, cards = default_world(head="pu" * 20)
        cards[0] = make_card(101, "fmt", 5, "pu" * 20, automerge_verdict=ELIGIBLE_A,
                             labels=["needs-decision", lbl])
        payload, _ = run_act(w, items, cards)
        check("G1: %s card is not pure -> holds" % lbl, "G1" in _held_reason(payload))
        check("G1: %s card -> no merge" % lbl, not w.do_merge_calls)


def test_G1_rejects_untrusted_or_unmanaged_card():
    w, items, cards = default_world()
    cards[0]["author"] = "contributor"
    payload, _ = run_act(w, items, cards)
    check("G1: contributor-authored forged card holds", not payload["merges"])

    w2, items2, cards2 = default_world()
    cards2[0]["labels"] = [{"name": "needs-decision"}]
    payload2, _ = run_act(w2, items2, cards2)
    check("G1: card without managed target labels holds", not payload2["merges"])


def test_claim_rechecks_and_locks_current_card():
    w, items, cards = default_world()
    initial = make_card(
        101,
        "fmt",
        5,
        items[0]["head_sha"],
        automerge_verdict=ELIGIBLE_A,
        labels=[
            "needs-decision",
            "repo:fmt",
            "kind:pr-review",
            "priority:med",
            "target:fmt-5",
        ],
    )
    current = dict(initial)
    current["state"] = "OPEN"
    calls = []
    token = os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN")
    saved = {"get": render_card.get_card, "ensure": render_card.ensure_labels,
             "gh": render_card._gh, "cfg": core.load_config}
    core.load_config = lambda: {"auto_merge": True, "repos": {"fmt": {"auto_merge": True}}}
    render_card.get_card = lambda number: current
    render_card.ensure_labels = lambda labels: calls.append(("ensure", labels))
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true"

    def claim(args, check=False):
        calls.append(("edit", args))
        current["labels"] += [
            {"name": "processing"},
            {"name": am.AUTO_MERGE_CLAIM_LABEL},
        ]
        return type("R", (), {"returncode": 0})()

    render_card._gh = claim
    scan = {"items": items}
    try:
        claims = am.claim_cards(scan, [initial])
        check(
            "claim: current card is re-read and locked",
            len(claims) == 1
            and "processing" in am._card_label_names(claims[0])
            and am.AUTO_MERGE_CLAIM_LABEL in am._card_label_names(claims[0]),
        )
    finally:
        render_card.get_card = saved["get"]
        render_card.ensure_labels = saved["ensure"]
        render_card._gh = saved["gh"]
        core.load_config = saved["cfg"]
        if token is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = token


def test_claim_rejects_changed_decision_card():
    w, items, cards = default_world()
    initial = make_card(
        101,
        "fmt",
        5,
        items[0]["head_sha"],
        automerge_verdict=ELIGIBLE_A,
        labels=[
            "needs-decision",
            "repo:fmt",
            "kind:pr-review",
            "priority:med",
            "target:fmt-5",
        ],
    )
    current = dict(initial)
    current["state"] = "OPEN"
    current["body"] += "\n- [x] Hold <!-- opt:hold -->"
    saved = {"get": render_card.get_card, "cfg": core.load_config}
    token = os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN")
    core.load_config = lambda: {"auto_merge": True, "repos": {"fmt": {"auto_merge": True}}}
    render_card.get_card = lambda number: current
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true"
    try:
        claims = am.claim_cards({"items": items}, [initial])
        check("claim: changed decision card is not claimed", claims == [])
    finally:
        render_card.get_card = saved["get"]
        core.load_config = saved["cfg"]
        if token is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = token


def test_claim_rechecks_owner_selection_after_locking():
    w, items, _ = default_world()
    initial = make_card(
        101,
        "fmt",
        5,
        items[0]["head_sha"],
        automerge_verdict=ELIGIBLE_A,
        labels=[
            "needs-decision",
            "repo:fmt",
            "kind:pr-review",
            "priority:med",
            "target:fmt-5",
        ],
    )
    current = dict(initial, state="OPEN")
    saved = {
        "get": render_card.get_card,
        "ensure": render_card.ensure_labels,
        "gh": render_card._gh,
        "cfg": core.load_config,
    }
    token = os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN")
    core.load_config = lambda: {"auto_merge": True, "repos": {"fmt": {"auto_merge": True}}}
    render_card.get_card = lambda number: current
    render_card.ensure_labels = lambda labels: None

    def edit(args, check=False):
        if "--add-label" in args:
            current["labels"] += [
                {"name": "processing"},
                {"name": am.AUTO_MERGE_CLAIM_LABEL},
            ]
            current["body"] += "\n- [x] Hold <!-- opt:hold -->"
        elif "--remove-label" in args:
            remove = {args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--remove-label"}
            current["labels"] = [label for label in current["labels"]
                                 if label.get("name") not in remove]
        return type("R", (), {"returncode": 0, "stderr": ""})()

    render_card._gh = edit
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true"
    try:
        claims = am.claim_cards({"items": items}, [initial])
        labels = am._card_label_names(current)
        check("claim: post-lock owner selection cancels the claim",
              claims == [] and "processing" not in labels
              and am.AUTO_MERGE_CLAIM_LABEL not in labels)
    finally:
        render_card.get_card = saved["get"]
        render_card.ensure_labels = saved["ensure"]
        render_card._gh = saved["gh"]
        core.load_config = saved["cfg"]
        if token is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = token


def test_claim_requires_fresh_verdict_before_locking_card():
    _, items, _ = default_world()
    initial = make_card(
        101,
        "fmt",
        5,
        items[0]["head_sha"],
        automerge_verdict=None,
        labels=[
            "needs-decision",
            "repo:fmt",
            "kind:pr-review",
            "priority:med",
            "target:fmt-5",
        ],
    )
    current = dict(initial, state="OPEN")
    calls = []
    saved = {
        "get": render_card.get_card,
        "ensure": render_card.ensure_labels,
        "gh": render_card._gh,
        "cfg": core.load_config,
    }
    token = os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN")
    core.load_config = lambda: {"auto_merge": True, "repos": {"fmt": {"auto_merge": True}}}
    render_card.get_card = lambda number: current
    render_card.ensure_labels = lambda labels: calls.append(("ensure", labels))
    render_card._gh = lambda *args, **kwargs: calls.append(("edit", args))
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true"
    try:
        claims = am.claim_cards({"items": items}, [initial])
        check(
            "claim: a successful triage without a verdict leaves no claim churn",
            claims == [] and calls == [],
        )
    finally:
        render_card.get_card = saved["get"]
        render_card.ensure_labels = saved["ensure"]
        render_card._gh = saved["gh"]
        core.load_config = saved["cfg"]
        if token is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = token


def test_validate_claimed_card_rechecks_owner_selection_before_acting():
    claimed = make_card(101, "fmt", 5, "vc" * 20, automerge_verdict=ELIGIBLE_A)
    current = dict(claimed, state="OPEN")
    current["body"] += "\n- [x] Hold <!-- opt:hold -->"
    saved = {"get": render_card.get_card, "gh": render_card._gh}
    render_card.get_card = lambda number: current

    def edit(args, check=False):
        remove = {
            args[i + 1]
            for i, arg in enumerate(args[:-1])
            if arg == "--remove-label"
        }
        current["labels"] = [
            label for label in current["labels"] if label.get("name") not in remove
        ]
        return type("R", (), {"returncode": 0, "stderr": ""})()

    render_card._gh = edit
    try:
        validated = am.validate_claimed_cards([claimed])
        labels = am._card_label_names(current)
        check(
            "validate: a new owner selection releases the claim before acting",
            validated == []
            and "processing" not in labels
            and am.AUTO_MERGE_CLAIM_LABEL not in labels,
        )
    finally:
        render_card.get_card = saved["get"]
        render_card._gh = saved["gh"]


def test_validate_claimed_card_rejects_pending_decisions_and_newer_activity():
    saved = {"get": render_card.get_card, "gh": render_card._gh}
    try:
        for label, current_change in (
            ("newer card activity", lambda card: card.update({"updatedAt": "2026-07-10T00:01:00Z"})),
            (
                "decision label",
                lambda card: card["labels"].append({"name": "decision:hold"}),
            ),
        ):
            claimed = make_card(
                101, "fmt", 5, "vd" * 20, automerge_verdict=ELIGIBLE_A
            )
            current = dict(claimed, state="OPEN", labels=list(claimed["labels"]))
            current_change(current)
            render_card.get_card = lambda number: current

            def edit(args, check=False):
                remove = {
                    args[i + 1]
                    for i, arg in enumerate(args[:-1])
                    if arg == "--remove-label"
                }
                current["labels"] = [
                    value for value in current["labels"] if value.get("name") not in remove
                ]
                return type("R", (), {"returncode": 0, "stderr": ""})()

            render_card._gh = edit
            validated = am.validate_claimed_cards([claimed])
            labels = am._card_label_names(current)
            check(
                "validate: %s releases the claim" % label,
                validated == []
                and "processing" not in labels
                and am.AUTO_MERGE_CLAIM_LABEL not in labels,
            )
    finally:
        render_card.get_card = saved["get"]
        render_card._gh = saved["gh"]


def test_stale_claim_release_failure_is_recovered_on_next_scan():
    stale = make_card(101, "fmt", 5, "st" * 20, automerge_verdict=ELIGIBLE_A)
    current = dict(stale, state="OPEN")
    saved = {"get": render_card.get_card, "gh": render_card._gh}
    attempts = [0]
    render_card.get_card = lambda number: current

    def edit(args, check=False):
        attempts[0] += 1
        if attempts[0] == 1:
            return type("R", (), {"returncode": 1, "stderr": "transient failure"})()
        remove = {args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--remove-label"}
        current["labels"] = [label for label in current["labels"]
                             if label.get("name") not in remove]
        return type("R", (), {"returncode": 0, "stderr": ""})()

    render_card._gh = edit
    try:
        failed = False
        try:
            am._release_card_claim(101)
        except RuntimeError:
            failed = True
        recovered = am.recover_stale_card_claims([stale])
        labels = am._card_label_names(current)
        check("claim: release failure is surfaced", failed)
        check("claim: next scan releases the stale claim",
              recovered == [101] and "processing" not in labels
              and am.AUTO_MERGE_CLAIM_LABEL not in labels)
    finally:
        render_card.get_card = saved["get"]
        render_card._gh = saved["gh"]


# --------------------------------------------------------------------------- #
# G7: live head + merge-state re-check immediately before acting
# --------------------------------------------------------------------------- #
def test_G7_head_moved_before_acting_holds():
    w, items, cards = default_world(head="hh" * 20)
    good = make_pr(head="hh" * 20)
    moved = make_pr(head="ZZ" * 20)  # head advanced between gate read and act
    w.set_pr("owner/fmt", 5, [good, moved])
    payload, _ = run_act(w, items, cards)
    check("G7: head moved before acting holds", not payload["merges"])
    check("G7: head moved -> no merge call", not w.do_merge_calls)


def test_G7_escape_hatch_appears_before_acting_holds():
    w, items, cards = default_world(head="ee" * 20)
    good = make_pr(head="ee" * 20)
    tagged = make_pr(head="ee" * 20, labels=[core.NO_AUTO_MERGE_LABEL])
    w.set_pr("owner/fmt", 5, [good, tagged])
    payload, _ = run_act(w, items, cards)
    check("G7: escape hatch appearing before acting holds", not payload["merges"])


def test_G7_card_changed_before_acting_holds_and_releases_claim():
    w, items, cards = default_world(head="cc" * 20)
    changed = dict(cards[0])
    changed["body"] += "\n- [x] Hold <!-- opt:hold -->"
    w.card_seq = {"101": [changed]}
    payload, _ = run_act(w, items, cards)
    check("G7: card changed before acting holds", not payload["merges"])
    check("G7: card change before acting does not call merge", not w.do_merge_calls)
    check(
        "G7: card change is queued for default-token claim release",
        payload["releases"] == [{"card_issue": 101}],
    )


def test_G7_card_activity_after_claim_holds_and_releases_claim():
    w, items, cards = default_world(head="ua" * 20)
    changed = dict(cards[0], updatedAt="2026-07-10T00:01:00Z")
    w.card_seq = {"101": [changed]}
    payload, _ = run_act(w, items, cards)
    check("G7: later card activity holds", not payload["merges"])
    check("G7: later card activity does not call merge", not w.do_merge_calls)
    check(
        "G7: later card activity is queued for claim release",
        payload["releases"] == [{"card_issue": 101}],
    )


def test_vision_is_rechecked_per_candidate_and_before_acting():
    w, items, cards = default_world(head="vv" * 20)
    w.vision_seq = {"fmt": [(True, "vsha"), (True, "new-vision")]}
    payload, _ = run_act(w, items, cards)
    check("G7: changed VISION.md before acting holds", not payload["merges"])
    check("G7: changed VISION.md does not call merge", not w.do_merge_calls)

    w2 = World()
    w2.repos = {"fmt": {"auto_merge": True}}
    w2.merged_authors = {("owner/fmt", "alice"): True}
    w2.vision_seq = {"fmt": [(True, "vsha"), (True, "vsha"), (True, "new-vision")]}
    w2.files[("owner/fmt", "5")] = (["src/one.py"], True, True)
    w2.files[("owner/fmt", "6")] = (["src/two.py"], True, True)
    w2.set_pr("owner/fmt", 5, make_pr(head="v5" * 20))
    w2.set_pr("owner/fmt", 6, make_pr(head="v6" * 20))
    items2 = [make_item("fmt", 5, "v5" * 20), make_item("fmt", 6, "v6" * 20)]
    cards2 = [
        make_card(101, "fmt", 5, "v5" * 20, automerge_verdict=ELIGIBLE_A),
        make_card(102, "fmt", 6, "v6" * 20, automerge_verdict=ELIGIBLE_A),
    ]
    payload2, _ = run_act(w2, items2, cards2)
    check(
        "G0: each candidate reads its own current VISION.md revision",
        len(payload2["merges"]) == 1
        and payload2["merges"][0]["number"] == "5"
        and any(
            hold["number"] == "6" and "VISION.md revision" in hold["hold_reason"]
            for hold in payload2["holds"]
        ),
    )


def test_head_moved_scan_vs_live_holds():
    # The scan/verdict head and the live head disagree at gate time.
    w, items, cards = default_world(head="sc" * 20)
    w.set_pr("owner/fmt", 5, make_pr(head="LV" * 20))  # live head differs
    payload, _ = run_act(w, items, cards)
    check("gate: scan/live head mismatch holds", "head moved" in _held_reason(payload))


# --------------------------------------------------------------------------- #
# escape hatch + kill switches
# --------------------------------------------------------------------------- #
def test_escape_hatch_label_holds():
    w, items, cards = default_world(head="lb" * 20)
    w.set_pr("owner/fmt", 5, make_pr(head="lb" * 20, labels=[core.NO_AUTO_MERGE_LABEL]))
    payload, _ = run_act(w, items, cards)
    check("kill: wheelhouse:no-auto-merge label holds",
          "escape hatch" in _held_reason(payload))


def test_G6_token_kill_switch_holds():
    w, items, cards = default_world()
    payload, _ = run_act(w, items, cards, has_token=False)
    check("G6: absent triage token holds persisted verdict", "TOKEN" in _held_reason(payload))


def test_G6_vision_revision_mismatch_holds():
    w, items, cards = default_world(verdict=dict(ELIGIBLE_A, vision_sha="old"))
    payload, _ = run_act(w, items, cards)
    check("G6: stale VISION.md revision holds", "VISION.md revision" in _held_reason(payload))


def test_triage_persists_trusted_vision_revision():
    item = make_item("fmt", 5, "vv" * 20)
    triage = {
        "summary": "A focused change.",
        "product_implications": "No broad behavior change.",
        "recommended_next_step": "merge - narrow and safe.",
        "automerge": ELIGIBLE_A,
    }
    body = render_card.body_with_triage_result(
        render_card.render(item)["body"], item["head_sha"], triage=triage,
        vision_sha="trusted-vision-sha",
    )
    verdict = core.parse_state_block(body).get("automerge_verdict")
    check(
        "triage: persists the trusted VISION.md revision with verdict",
        verdict and verdict.get("vision_sha") == "trusted-vision-sha",
    )


# --------------------------------------------------------------------------- #
# repo-state freeze invariants (ok:false / truncated / indeterminate)
# --------------------------------------------------------------------------- #
def _run_with_scan(world, items, cards, repos_scan):
    world.repos_scan = repos_scan
    return run_act(world, items, cards)


def test_scan_freeze_invariants():
    for label, scan in (
        ("ok:false", {"fmt": {"ok": False}}),
        ("truncated", {"fmt": {"ok": True, "truncated": True}}),
        (
            "indeterminate",
            {"fmt": {"ok": True, "indeterminate_pr_numbers": [5]}},
        ),
    ):
        w, items, cards = default_world(head="fz" * 20)
        payload, _ = _run_with_scan(w, items, cards, scan)
        check("freeze: %s repo/PR never auto-merges" % label, not w.do_merge_calls)
        check("freeze: %s held" % label, bool(payload["holds"]))


# --------------------------------------------------------------------------- #
# G7 do_merge race outcomes (already-merged / not-open / error)
# --------------------------------------------------------------------------- #
def test_do_merge_race_and_error_outcomes():
    w, items, cards = default_world(head="rc" * 20)
    w.do_merge_returns = {("fmt", "5"): ("Target fmt#5 is already merged - nothing "
                                         "to do.", "resolved")}
    payload, _ = run_act(w, items, cards)
    check("act: already-merged race is NOT recorded as our merge",
          not payload["merges"])

    w2, items2, cards2 = default_world(head="er" * 20)
    w2.do_merge_returns = {("fmt", "5"): ("Merge of fmt#5 failed: boom", "error")}
    payload2, err2 = run_act(w2, items2, cards2)
    check("act: do_merge error is a hold, not a recorded merge",
          not payload2["merges"] and payload2["holds"])
    check("act: error emits a ::warning::", "auto-merge held" in err2)


# --------------------------------------------------------------------------- #
# DELIBERATE ABSENCE of an overlap gate and any rate cap (captain override)
# --------------------------------------------------------------------------- #
def test_no_open_pr_overlap_gate():
    # Two open merge-ready PRs whose file sets fully overlap BOTH merge - there
    # is intentionally no open-PR file-overlap gate in V1.
    w = World()
    w.repos = {"fmt": {"auto_merge": True}}
    w.vision = {"fmt": (True, "vsha")}
    w.merged_authors = {("owner/fmt", "alice"): True}
    same_files = (["src/shared.py", "README.md"], True, True)
    w.files[("owner/fmt", "5")] = same_files
    w.files[("owner/fmt", "6")] = same_files
    w.set_pr("owner/fmt", 5, make_pr(head="o5" * 20))
    w.set_pr("owner/fmt", 6, make_pr(head="o6" * 20))
    items = [make_item("fmt", 5, "o5" * 20), make_item("fmt", 6, "o6" * 20)]
    cards = [
        make_card(101, "fmt", 5, "o5" * 20, automerge_verdict=ELIGIBLE_A),
        make_card(102, "fmt", 6, "o6" * 20, automerge_verdict=ELIGIBLE_A),
    ]
    payload, _ = run_act(w, items, cards)
    check("absence: overlapping-file PRs BOTH auto-merge (no overlap gate)",
          len(payload["merges"]) == 2)
    check("absence: no overlap helper exists in auto_merge",
          not any("overlap" in n for n in dir(am)))


def test_no_rate_cap_same_contributor_or_scan():
    # The SAME contributor's several PRs in ONE scan all merge - no per-
    # contributor daily cap and no per-scan cap exist in V1.
    w = World()
    w.repos = {"fmt": {"auto_merge": True}}
    w.vision = {"fmt": (True, "vsha")}
    w.merged_authors = {("owner/fmt", "alice"): True}
    items, cards = [], []
    for i, n in enumerate((5, 6, 7, 8)):
        head = ("r%d" % n) * 20
        w.files[("owner/fmt", str(n))] = (["src/f%d.py" % n], True, True)
        w.set_pr("owner/fmt", n, make_pr(head=head, author="alice"))
        items.append(make_item("fmt", n, head))
        cards.append(make_card(100 + i, "fmt", n, head, automerge_verdict=ELIGIBLE_A))
    payload, _ = run_act(w, items, cards)
    check("absence: 4 PRs from one contributor in one scan all merge",
          len(payload["merges"]) == 4)
    check("absence: no rate/cap helper exists in auto_merge",
          not any(("cap" in n or "rate" in n) for n in dir(am)))


# --------------------------------------------------------------------------- #
# base-branch-ONLY VISION.md read (never the PR head)
# --------------------------------------------------------------------------- #
def test_vision_read_is_base_branch_only():
    captured = {}

    def fake_gh_api(path):
        captured["path"] = path
        payload = {
            "type": "file",
            "sha": "visionsha",
            "size": len(b"Our vision."),
            "encoding": "base64",
            "content": __import__("base64").b64encode(b"Our vision.").decode(),
        }
        return type("R", (), {"returncode": 0, "stdout": json.dumps(payload)})()

    save = am._gh_api
    am._gh_api = fake_gh_api
    try:
        present, sha = am.vision_on_default_branch("owner/fmt")
    finally:
        am._gh_api = save
    check("vision: present read from default branch", present is True and sha == "visionsha")
    check(
        "vision: read uses the contents API with NO ?ref (default branch = base)",
        captured["path"] == "/repos/owner/fmt/contents/VISION.md"
        and "ref=" not in captured["path"]
        and "head" not in captured["path"],
    )


def test_vision_absent_and_empty_fail_closed():
    def gh_404(path):
        return type("R", (), {"returncode": 1, "stdout": ""})()

    def gh_empty(path):
        payload = {"type": "file", "sha": "s", "encoding": "base64",
                   "size": 3,
                   "content": __import__("base64").b64encode(b"   ").decode()}
        return type("R", (), {"returncode": 0, "stdout": json.dumps(payload)})()

    save = am._gh_api
    try:
        am._gh_api = gh_404
        check("vision: 404 -> not present", am.vision_on_default_branch("o/r") == (False, ""))
        am._gh_api = gh_empty
        check("vision: blank VISION.md -> not present (fail-closed)",
              am.vision_on_default_branch("o/r") == (False, ""))
    finally:
        am._gh_api = save


def test_vision_oversized_or_incomplete_fails_closed():
    saved = am._gh_api

    def content(payload):
        return lambda path: type("R", (), {"returncode": 0, "stdout": json.dumps(payload)})()

    try:
        oversized = b"x" * (am.MAX_VISION_BYTES + 1)
        am._gh_api = content(
            {
                "type": "file",
                "sha": "oversized",
                "size": len(oversized),
                "encoding": "base64",
                "content": __import__("base64").b64encode(oversized).decode(),
            }
        )
        check(
            "vision: oversized policy is unavailable for auto-merge",
            am.vision_on_default_branch("o/r") == (False, ""),
        )
        am._gh_api = content(
            {
                "type": "file",
                "sha": "incomplete",
                "size": 10,
                "encoding": "base64",
                "content": __import__("base64").b64encode(b"short").decode(),
            }
        )
        check(
            "vision: incomplete policy is unavailable for auto-merge",
            am.vision_on_default_branch("o/r") == (False, ""),
        )
    finally:
        am._gh_api = saved


# --------------------------------------------------------------------------- #
# audit trail: ledger (pure) + resolved decision record + record CLI
# --------------------------------------------------------------------------- #
def _merge_record(number=5, card=101):
    return {
        "repo": "fmt",
        "number": str(number),
        "card_issue": card,
        "head_sha": "h" * 40,
        "merge_commit": "c" * 40,
        "merged_at": "2026-07-10T00:00:00Z",
        "contributor": "alice",
        "contributor_proof": "has >=1 prior merged PR in fmt",
        "vision_sha": "vsha",
        "behavior_class": "A",
        "behavior_verdict": ELIGIBLE_A,
        "gates": {"blast_radius": "2 files / 20 lines within caps"},
    }


def test_ledger_parse_append_render_and_cap():
    entries = am.append_ledger_entries([], [_merge_record(5), _merge_record(6, 102)])
    body = am.render_ledger_body(entries, "2026-07-10T00:00:00Z")
    parsed = am.parse_ledger(body)
    check("ledger: two entries round-trip", len(parsed) == 2)
    check("ledger: entry carries all audit fields",
          parsed[0]["contributor"] == "alice"
          and parsed[0]["head_sha"]
          and parsed[0]["vision_sha"] == "vsha"
          and parsed[0]["behavior_class"] == "A"
          and parsed[0]["merge_commit"])
    check("ledger: human summary names the merged PR", "fmt#5" in body)
    check("ledger: missing/blank ledger parses to []",
          am.parse_ledger("") == [] and am.parse_ledger("no marker here") == [])
    # Cap keeps only the most recent entries.
    many = [_merge_record(n) for n in range(300)]
    capped = am.append_ledger_entries([], many, cap=am.LEDGER_ENTRY_CAP)
    check(
        "ledger: stored history is bounded by count and serialized size",
        capped
        and len(capped) <= am.LEDGER_ENTRY_CAP
        and capped[-1]["number"] == "299"
        and len(am.render_ledger_body(capped).encode("utf-8"))
        <= am.LEDGER_MAX_BODY_BYTES,
    )
    oversized = []
    for n in range(12):
        record = _merge_record(n)
        record["gates"] = {"detail": "x" * 20000}
        oversized.append(record)
    byte_capped = am.append_ledger_entries([], oversized)
    byte_body = am.render_ledger_body(byte_capped, "2026-07-10T00:00:00Z")
    check(
        "ledger: serialized body stays within the GitHub-safe byte cap",
        len(byte_body.encode("utf-8")) <= am.LEDGER_MAX_BODY_BYTES,
    )
    check(
        "ledger: byte cap trims oldest records first",
        byte_capped
        and byte_capped[-1]["number"] == "11"
        and len(byte_capped) < len(oversized),
    )
    duplicate = am.append_ledger_entries(entries, [_merge_record(5)])
    check(
        "ledger: retrying an already-persisted merge does not duplicate it",
        len(duplicate) == len(entries),
    )


def test_audit_comment_explains_why_it_qualified():
    text = am.audit_comment(_merge_record())
    for token in ("Auto-merged fmt#5", "alice", "Behavior class: A",
                  "Merge commit", "VISION.md SHA", "never auto-reverts"):
        check("audit-comment: mentions %r" % token, token in text)


def test_record_cli_resolves_card_and_appends_ledger():
    calls = {"ledger": [], "resolved": [], "released": []}
    saved = {
        "ledger": am.append_to_ledger,
        "release": am.release_card_claim,
        "get": render_card.get_card,
        "close": am._strict_audited_close_card,
    }
    am.append_to_ledger = lambda records: calls["ledger"].extend(records)
    am.release_card_claim = lambda record: calls["released"].append(record["card_issue"])
    render_card.get_card = lambda n: {"state": "OPEN", "labels": []}
    am._strict_audited_close_card = lambda n, msg, close_issue=True: calls["resolved"].append(
        (n, close_issue)
    )
    tmp = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "am_results_%d.json" % os.getpid()
    )
    try:
        with open(tmp, "w") as f:
            json.dump({"merges": [_merge_record()]}, f)
        am.cmd_record(tmp)
        check("record: ledger got the merge", len(calls["ledger"]) == 1)
        check("record: card resolved+closed", calls["resolved"] == [(101, True)])
        check("record: merged card claim is released", calls["released"] == [101])
    finally:
        os.unlink(tmp)
        am.append_to_ledger = saved["ledger"]
        am.release_card_claim = saved["release"]
        render_card.get_card = saved["get"]
        am._strict_audited_close_card = saved["close"]


def test_audit_writes_retry_transient_failures_and_surface_unrecoverable_ones():
    saved = {
        "this_repo": core._this_repo_slug,
        "find": am._find_ledger_issue,
        "rest": core.gh_rest,
        "get": render_card.get_card,
        "ensure": core._ensure_repo_label,
        "gh": render_card._gh,
        "sleep": am._audit_sleep,
    }
    ledger_attempts = [0]
    card_attempts = [0]
    am._audit_sleep = lambda seconds: None
    core._this_repo_slug = lambda: "owner/wheelhouse"
    am._find_ledger_issue = lambda slug: {"number": 77, "body": ""}

    def patch_ledger(*args, **kwargs):
        ledger_attempts[0] += 1
        if ledger_attempts[0] == 1:
            raise RuntimeError("HTTP 503 service unavailable")
        return {}

    core.gh_rest = patch_ledger
    render_card.get_card = lambda number: {"state": "OPEN"}
    core._ensure_repo_label = lambda *args, **kwargs: None

    def gh(args, check=True):
        if args[:2] == ["issue", "comment"]:
            card_attempts[0] += 1
        if args[:2] == ["issue", "comment"] and card_attempts[0] == 1:
            raise RuntimeError("HTTP 502 bad gateway")
        return type("R", (), {"returncode": 0, "stderr": ""})()

    render_card._gh = gh
    try:
        am.append_to_ledger([_merge_record()])
        am.resolve_card(_merge_record())
        check("audit: ledger write retries a transient failure", ledger_attempts == [2])
        check("audit: card resolution retries a transient failure", card_attempts == [2])

        core.gh_rest = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("HTTP 422 validation failed")
        )
        stderr = io.StringIO()
        failed = False
        with redirect_stderr(stderr):
            try:
                am.append_to_ledger([_merge_record()])
            except RuntimeError:
                failed = True
        check(
            "audit: unrecoverable ledger write emits an error and fails",
            failed and "::error::" in stderr.getvalue(),
        )
    finally:
        core._this_repo_slug = saved["this_repo"]
        am._find_ledger_issue = saved["find"]
        core.gh_rest = saved["rest"]
        render_card.get_card = saved["get"]
        core._ensure_repo_label = saved["ensure"]
        render_card._gh = saved["gh"]
        am._audit_sleep = saved["sleep"]


def test_resolve_card_errors_fail_the_record_path():
    saved_get, saved_close = render_card.get_card, am._strict_audited_close_card
    closed = []
    am._strict_audited_close_card = lambda n, m, close_issue=True: closed.append(
        (n, close_issue)
    )
    try:
        render_card.get_card = lambda n: {"state": "CLOSED"}
        am.resolve_card(_merge_record())
        check(
            "record: closed card still receives its audit record without re-close",
            closed == [(101, False)],
        )
        am.resolve_card(dict(_merge_record(), card_issue=None))
        check("record: no card issue -> no-op", closed == [(101, False)])

        def boom(n):
            raise RuntimeError("gh down")

        render_card.get_card = boom
        failed = False
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            try:
                am.resolve_card(_merge_record())
            except RuntimeError:
                failed = True
        check(
            "record: card audit failure is surfaced",
            failed and closed == [(101, False)] and "::error::" in stderr.getvalue(),
        )
    finally:
        render_card.get_card = saved_get
        am._strict_audited_close_card = saved_close


def test_strict_audited_close_propagates_all_card_write_failures():
    saved = {
        "this_repo": core._this_repo_slug,
        "ensure": core._ensure_repo_label,
        "gh": render_card._gh,
        "get": render_card.get_card,
        "sleep": am._audit_sleep,
    }
    calls = []
    core._this_repo_slug = lambda: "owner/wheelhouse"
    core._ensure_repo_label = lambda *args: calls.append(("label", args))
    render_card.get_card = lambda number: {"state": "OPEN"}
    am._audit_sleep = lambda seconds: None

    def gh(args, check=True):
        calls.append(("gh", args, check))
        if args[:2] == ["issue", "close"]:
            raise RuntimeError("HTTP 422 close failed")
        return type("R", (), {"returncode": 0, "stderr": ""})()

    render_card._gh = gh
    try:
        failed = False
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            try:
                am.resolve_card(_merge_record())
            except RuntimeError:
                failed = True
        command_calls = [entry for entry in calls if entry[0] == "gh"]
        check(
            "audit: strict close runs comment, label update, and issue close",
            [entry[1][:2] for entry in command_calls[:3]]
            == [["issue", "comment"], ["issue", "edit"], ["issue", "close"]],
        )
        check(
            "audit: strict close failures emit an error and fail",
            failed and "::error::" in stderr.getvalue(),
        )
    finally:
        core._this_repo_slug = saved["this_repo"]
        core._ensure_repo_label = saved["ensure"]
        render_card._gh = saved["gh"]
        render_card.get_card = saved["get"]
        am._audit_sleep = saved["sleep"]


def test_atomic_results_handoff_fails_loudly_and_releases_claims_by_fallback():
    saved = {
        "act": am.act_on_scan,
        "write": am._write_json_atomically,
        "release": am.release_card_claim,
        "ledger": am.append_to_ledger,
        "result_env": os.environ.get("WHEELHOUSE_AUTOMERGE_RESULTS"),
    }
    payload = {"merges": [_merge_record()], "holds": [], "releases": []}
    calls = {"released": [], "ledger": []}
    am.act_on_scan = lambda scan, cards: payload
    am.release_card_claim = lambda record: calls["released"].append(record["card_issue"])
    am.append_to_ledger = lambda records: calls["ledger"].extend(records)
    try:
        with tempfile.TemporaryDirectory() as directory:
            scan_path = os.path.join(directory, "scan.json")
            cards_path = os.path.join(directory, "cards.json")
            results_path = os.path.join(directory, "automerge.json")
            claims_path = os.path.join(directory, "automerge-valid-claims.json")
            for path, data in ((scan_path, {}), (cards_path, []), (claims_path, [{"number": 101}])):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            os.environ["WHEELHOUSE_AUTOMERGE_RESULTS"] = results_path
            try:
                am.cmd_act(scan_path, cards_path)
                with open(results_path, encoding="utf-8") as f:
                    recorded = json.load(f)
                check("handoff: atomic results file contains the complete payload", recorded == payload)
                check(
                    "handoff: atomic write leaves no temporary result file",
                    not any(name.startswith(".automerge-") for name in os.listdir(directory)),
                )

                am._write_json_atomically = lambda path, data: (_ for _ in ()).throw(
                    OSError("disk full")
                )
                failed = False
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    try:
                        am.cmd_act(scan_path, cards_path)
                    except RuntimeError:
                        failed = True
                check(
                    "handoff: write failure emits an error and fails the act step",
                    failed and "::error::" in stderr.getvalue(),
                )
                os.unlink(results_path)
                with redirect_stderr(io.StringIO()):
                    am.cmd_record(results_path, claims_path)
                check(
                    "handoff: missing results release validated card claims",
                    calls["released"] == [101],
                )
                check(
                    "handoff: missing results do not fabricate ledger entries",
                    calls["ledger"] == [],
                )
            finally:
                if saved["result_env"] is None:
                    os.environ.pop("WHEELHOUSE_AUTOMERGE_RESULTS", None)
                else:
                    os.environ["WHEELHOUSE_AUTOMERGE_RESULTS"] = saved["result_env"]
    finally:
        am.act_on_scan = saved["act"]
        am._write_json_atomically = saved["write"]
        am.release_card_claim = saved["release"]
        am.append_to_ledger = saved["ledger"]


# --------------------------------------------------------------------------- #
# non-candidates: only merge-ready pr-review items are considered
# --------------------------------------------------------------------------- #
def test_only_merge_ready_pr_review_items_are_candidates():
    w, items, cards = default_world(head="nc" * 20)
    items[0]["bucket"] = "review-needed"  # not merge-ready
    payload, _ = run_act(w, items, cards)
    check("scope: non-merge-ready item is not a candidate",
          not payload["merges"] and not payload["holds"])

    w2, items2, cards2 = default_world(head="ci" * 20)
    items2[0]["kind"] = "ci-approval"
    payload2, _ = run_act(w2, items2, cards2)
    check("scope: ci-approval item is not a candidate",
          not payload2["merges"] and not payload2["holds"])


# --------------------------------------------------------------------------- #
# workflow wiring (offline YAML/script inspection) - token discipline + VISION
# --------------------------------------------------------------------------- #
_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_scan_backstop_wiring_and_token_discipline():
    import yaml

    doc = yaml.safe_load(_read(".github/workflows/scan-backstop.yml"))
    steps = doc["jobs"]["reconcile"]["steps"]
    by_name = {s.get("name"): s for s in steps if isinstance(s, dict)}
    listed = by_name.get("List open cards")
    claim = by_name.get("Claim auto-merge decision cards")
    validate = by_name.get("Validate auto-merge decision cards")
    act = by_name.get("Auto-merge eligible PRs")
    rec = by_name.get("Record auto-merges")
    check("wiring: auto-merge act step exists", act is not None)
    check("wiring: card list records card author provenance",
          listed and "author: (.user.login" in listed.get("run", ""))
    check("wiring: current cards are claimed before auto-merge", claim is not None)
    check("wiring: record step exists", rec is not None)
    check(
        "wiring: the MERGE runs on FLEET_TOKEN (cross-repo write)",
        act and "FLEET_TOKEN" in (act.get("env") or {}).get("GH_TOKEN", ""),
    )
    check(
        "wiring: claim runs under github.token before the FLEET_TOKEN act",
        claim
        and "github.token" in (claim.get("env") or {}).get("GH_TOKEN", "")
        and "auto_merge.py claim scan.json cards.json" in claim.get("run", ""),
    )
    check(
        "wiring: claimed cards are revalidated under github.token before acting",
        validate
        and "github.token" in (validate.get("env") or {}).get("GH_TOKEN", "")
        and "auto_merge.py validate automerge-claims.json" in validate.get("run", ""),
    )
    check(
        "wiring: act consumes only final validated claims",
        act
        and "auto_merge.py act scan.json automerge-valid-claims.json"
        in act.get("run", ""),
    )
    check(
        "wiring: absent triage token disables both claim and act",
        claim
        and act
        and "CLAUDE_CODE_OAUTH_TOKEN != ''" in (claim.get("env") or {}).get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", "")
        and "CLAUDE_CODE_OAUTH_TOKEN != ''" in (act.get("env") or {}).get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", ""),
    )
    check(
        "wiring: the AUDIT record runs on github.token (this-repo bookkeeping)",
        rec and "github.token" in (rec.get("env") or {}).get("GH_TOKEN", ""),
    )
    check(
        "wiring: missing act results release validated claims under github.token",
        rec
        and rec.get("if") == "always()"
        and "record automerge.json automerge-valid-claims.json" in rec.get("run", ""),
    )
    check(
        "wiring: record does NOT get FLEET_TOKEN",
        rec and "FLEET_TOKEN" not in json.dumps(rec.get("env") or {}),
    )
    # The merge must happen before reconcile self-heal-closes anything and the
    # audit resolve must be the last write.
    order = [s.get("name") for s in steps if isinstance(s, dict)]
    check(
        "wiring: order is act -> reconcile -> record",
        order.index("Claim auto-merge decision cards")
        < order.index("Validate auto-merge decision cards")
        < order.index("Auto-merge eligible PRs")
        < order.index("Reconcile the queue")
        < order.index("Record auto-merges"),
    )
    handler = yaml.safe_load(_read(".github/workflows/decision-handler.yml"))
    handler_steps = handler["jobs"]["handle"]["steps"]
    handler_by_name = {s.get("name") or s.get("id"): s for s in handler_steps
                       if isinstance(s, dict)}
    current = handler_by_name.get("current-card")
    check(
        "wiring: manual decisions share the auto-merge concurrency lock",
        handler.get("concurrency", {}).get("group") == "wheelhouse-backstop",
    )
    check(
        "wiring: shared lock queues manual decisions instead of replacing them",
        doc.get("concurrency", {}).get("queue") == "max"
        and handler.get("concurrency", {}).get("queue") == "max",
    )
    check(
        "wiring: manual target actions re-read claim state before execution",
        current
        and "wheelhouse:auto-merge-claim" in current.get("run", "")
        and "steps.current-card.outputs.allowed == 'true'" in json.dumps(handler_steps),
    )


def test_triage_reads_vision_from_base_only_and_asks_verdict():
    text = _read(".github/workflows/triage.yml")
    check(
        "triage: VISION.md is read via the contents API",
        "contents/VISION.md" in text,
    )
    check(
        "triage: VISION.md read passes NO ?ref (default branch = base, never head)",
        "contents/VISION.md?ref=" not in text and "VISION.md?" not in text,
    )
    check(
        "triage: asks for the A/B/C behavior_class verdict when VISION present",
        '"behavior_class"' in text and '"optin_default_off"' in text,
    )
    check(
        "triage: verdict is gated on VISION_PRESENT (base VISION.md exists)",
        "VISION_PRESENT" in text,
    )
    check(
        "triage: the VISION policy is labeled TRUSTED owner-authored (not head)",
        "TRUSTED owner-authored policy" in text,
    )
    check(
        "triage: binds verdict storage to the fetched VISION.md SHA",
        "vision_sha=$VISION_SHA" in text and "--vision-sha \"$VISION_SHA\"" in text,
    )
    check(
        "triage: incomplete diffs suppress auto-merge verdict storage",
        'if [ "$VISION_PRESENT" = "true" ] && [ "$DIFF_COMPLETE" = "true" ]; then'
        in text
        and "AUTOMERGE_VERDICT_AVAILABLE=true" in text,
    )
    check(
        "triage: oversized or incomplete VISION.md is unavailable for auto-merge",
        "VISION_SIZE=" in text
        and '"$VISION_SIZE" -le "$vision_limit_bytes"' in text
        and '"$vision_bytes" = "$VISION_SIZE"' in text
        and 'head -c "$vision_limit_bytes" > vision.md' not in text,
    )
    check(
        "triage: binary diff input suppresses auto-merge verdict storage",
        "Binary files .+ differ" in text
        and "GIT binary patch" in text
        and '"$diff_size" -eq 0' in text
        and "binary data unavailable for auto-merge assessment" in text,
    )


# --------------------------------------------------------------------------- #
def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if _failures:
        print("FAILURES: %d" % len(_failures))
        for f in _failures:
            print("  - " + f)
        sys.exit(1)
    print("all auto-merge V1 tests passed")


if __name__ == "__main__":
    main()
