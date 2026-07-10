#!/usr/bin/env python3
"""
Wheelhouse - scan-time auto-merge (V1).

A merge-ready pr-review PR is merged automatically ONLY as a strict subset of
the manual merge gate: every deterministic gate must pass AND one fresh,
structured, fail-closed behavior verdict for the current head SHA must assign an
eligible A/B/C behavior class and recommend merge. Any missing, stale, malformed,
uncertain, or unreadable input HOLDS the PR for normal human review. This mirrors
the scan-time fork-CI auto-approve architecture (`ci_safety` /
`_auto_approve_or_card` in wheelhouse_core.py) and reuses the existing
`do_merge` acting path unchanged. See AGENTS.md "Auto-merge".

Every auto-merge requires ALL of (see the numbered contract in AGENTS.md):
  G0  repo `auto_merge: true` AND a committed VISION.md on its DEFAULT branch
  G1  the candidate is a merge-ready pr-review decision (from the scan worklist)
  G2  the PR touches none of the deterministic unconditional exclusions
  G3  the author has >= 1 previously merged PR in the same repo (non-bot human)
  G4  compliance + tests green (worst-wins, already encoded by merge-ready),
      live mergeable == MERGEABLE, live merge state CLEAN
  G5  blast radius: <= 20 changed files AND <= 1000 total changed lines
  G6  fresh structured verdict for the current head SHA: eligible A/B/C class,
      aligns with the base VISION.md, no ineligible existing/default behavior
      change, recommends merge (class C also strictly opt-in + default off)
  G7  immediately re-check head SHA + mergeability + clean state, then do_merge
Plus a per-PR `wheelhouse:no-auto-merge` escape hatch, global/per-repo switches
(default OFF), a durable audit ledger, and a resolved decision record.

There are DELIBERATELY no open-PR file-overlap gate and no per-contributor /
per-scan rate caps (captain override); their absence is asserted by the tests.

Two CLIs, run as separate workflow steps so each uses the right token:

  auto_merge.py act <scan.json> <cards.json>
      Under FLEET_TOKEN. Identify merge-ready pr-review candidates from the scan,
      join the persisted behavior verdict from the card bodies, run G0-G7, and
      call do_merge for the ones that qualify. Writes a machine-readable results
      file (path from $WHEELHOUSE_AUTOMERGE_RESULTS, default automerge.json) and
      one ::notice::/::warning:: audit line per candidate. It performs NO
      THIS-repo card writes (token discipline) - those are left to `record`.

  auto_merge.py record <results.json> [validated-claims.json]
      Under GITHUB_TOKEN. Append each auto-merge to the durable ledger issue in
      THIS repo and resolve each merged PR's decision card with an audit record
      of why it qualified. Audit writes retry transient failures and report
      unrecoverable errors after the merge. When the result handoff is missing,
      the optional validated-claims file releases claims under the default token.

Owner is derived from $GITHUB_REPOSITORY_OWNER. Cross-repo reads and the merge
itself use the ambient GH_TOKEN (FLEET_TOKEN in the act step); the ledger and
card writes in `record` use the default GITHUB_TOKEN.
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wheelhouse_core as core  # noqa: E402
import render_card  # noqa: E402
import apply_decision  # noqa: E402

# Blast-radius caps (captain-fixed). Both are inclusive maxima.
MAX_CHANGED_FILES = 20
MAX_CHANGED_LINES = 1000
MAX_VISION_BYTES = 40000

# The eligible behavior classes (captain-fixed):
#   A = no product behavior change
#   B = narrow corrective bug fix restoring intended behavior
#   C = new feature strictly opt-in and disabled by default
# Any change to existing/default behavior that is not one of these is ineligible.
ELIGIBLE_BEHAVIOR_CLASSES = ("A", "B", "C")
CARD_AUTOMATION_AUTHOR = "github-actions[bot]"
AUTO_MERGE_CLAIM_LABEL = "wheelhouse:auto-merge-claim"
AUDIT_WRITE_MAX_ATTEMPTS = 3
AUDIT_WRITE_BACKOFF_SECONDS = 0.25
_audit_sleep = time.sleep

# Durable audit ledger (mirrors the scan-health ledger: a dedicated CLOSED issue
# in THIS cards repo carrying a hidden marker; state lives in GitHub, not disk).
LEDGER_MARKER = "wheelhouse-auto-merge-log"
LEDGER_LABEL = "wheelhouse:auto-merge-log"
LEDGER_TITLE = "Wheelhouse auto-merge log (automated)"
# Keep the stored history bounded so the ledger body cannot grow without limit.
LEDGER_ENTRY_CAP = 200
LEDGER_MAX_BODY_BYTES = 60000
_LEDGER_RE = re.compile(
    r"<!--\s*%s:\s*(\{.*?\})\s*-->" % re.escape(LEDGER_MARKER), re.S
)
_GIT_OBJECT_ID_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


# --------------------------------------------------------------------------- #
# pure verdict / blast-radius logic (fail-closed)
# --------------------------------------------------------------------------- #
def normalize_behavior_class(value):
    """Map a model-supplied class token to one of A/B/C, else '' (ineligible)."""
    text = str(value or "").strip().upper()
    return text if text in ELIGIBLE_BEHAVIOR_CLASSES else ""


def verdict_eligible(verdict):
    """Given a persisted `automerge_verdict` dict, decide whether it clears the
    behavior gate. Returns (ok, behavior_class, reason). Fail-closed: any
    missing field, wrong type, or disqualifying value holds.

    Fields (each defaulting to its disqualifying value if absent):
      behavior_class                        one of A/B/C, else ineligible
      aligns_with_vision            (bool)  must be True
      changes_existing_or_default_behavior (bool) must be False
      recommend_merge               (bool)  must be True
      optin_default_off             (bool)  class C only: must be True
    """
    if not isinstance(verdict, dict):
        return (False, "", "no structured behavior verdict")
    cls = normalize_behavior_class(verdict.get("behavior_class"))
    if not cls:
        return (
            False,
            "",
            "behavior class %r is not an eligible A/B/C class"
            % (verdict.get("behavior_class"),),
        )
    if verdict.get("aligns_with_vision") is not True:
        return (False, cls, "verdict does not confirm alignment with VISION.md")
    if verdict.get("changes_existing_or_default_behavior") is not False:
        return (
            False,
            cls,
            "verdict does not rule out an ineligible existing/default behavior change",
        )
    if verdict.get("recommend_merge") is not True:
        return (False, cls, "verdict does not recommend merge")
    if cls == "C" and verdict.get("optin_default_off") is not True:
        return (
            False,
            cls,
            "class C but verdict does not confirm strictly opt-in and default off",
        )
    return (True, cls, "eligible class %s, aligns with vision, recommends merge" % cls)


def blast_radius_ok(changed_files, additions, deletions):
    """(ok, reason) for the file / total-line caps. Fail-closed on unusable
    numbers (a missing count must never read as 'small')."""
    try:
        files = int(changed_files)
        adds = int(additions)
        dels = int(deletions)
    except (TypeError, ValueError):
        return (False, "changed-file / line counts unavailable")
    if files < 0 or adds < 0 or dels < 0:
        return (False, "changed-file / line counts unavailable")
    total = adds + dels
    if files > MAX_CHANGED_FILES:
        return (False, "%d changed files > cap %d" % (files, MAX_CHANGED_FILES))
    if total > MAX_CHANGED_LINES:
        return (False, "%d changed lines > cap %d" % (total, MAX_CHANGED_LINES))
    return (True, "%d files / %d lines within caps" % (files, total))


def _pr_author_login(pr):
    return core._author_login(((pr or {}).get("user") or {}))


def _pr_author_is_provably_human(pr):
    author = (pr or {}).get("user")
    if not isinstance(author, dict) or core._author_is_bot(author):
        return False
    return core._author_typename(author).casefold() == "user" and bool(
        core._author_login(author)
    )


def _pr_label_names(pr):
    names = set()
    for label in (pr or {}).get("labels") or []:
        if isinstance(label, dict) and label.get("name"):
            names.add(str(label["name"]))
        elif isinstance(label, str):
            names.add(label)
    return names


def auto_merge_triage_available():
    return os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", "").lower() == "true"


# --------------------------------------------------------------------------- #
# live target reads (FLEET_TOKEN) - thin wrappers so tests can stub them
# --------------------------------------------------------------------------- #
def _gh_api(path):
    return subprocess.run(
        ["gh", "api", path], capture_output=True, text=True
    )


def vision_on_default_branch(slug):
    """Read VISION.md from the target's DEFAULT branch (base), never the PR head
    (the self-authorization guard). Returns (present, blob_sha). Fail-closed:
    any 404 / read / decode error returns (False, '').

    The GitHub contents API defaults to the repo's default branch when no `?ref`
    is given, which is exactly the base-branch-only read we require."""
    r = _gh_api("/repos/%s/contents/VISION.md" % slug)
    if r.returncode != 0:
        return (False, "")
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return (False, "")
    if not isinstance(data, dict) or data.get("type") != "file":
        return (False, "")
    sha = str(data.get("sha") or "").strip()
    size = data.get("size")
    content = data.get("content")
    if (
        not sha
        or type(size) is not int
        or size <= 0
        or size > MAX_VISION_BYTES
        or data.get("encoding") != "base64"
        or not isinstance(content, str)
    ):
        return (False, "")
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", content), validate=True)
        text = raw.decode("utf-8")
    except (ValueError, TypeError, UnicodeDecodeError):
        return (False, "")
    if len(raw) != size or not text.strip():
        return (False, "")
    return (True, sha)


def has_prior_merged_pr(slug, author):
    """True if `author` has at least one previously merged PR in `slug` (the
    captain-fixed returning-contributor definition: one prior same-repo merge, no
    revert/quality inspection). Fail-closed False on any read error or blank
    author."""
    author = str(author or "").strip()
    if not author:
        return False
    r = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "-R",
            slug,
            "--state",
            "merged",
            "--author",
            author,
            "--limit",
            "1",
            "--json",
            "number",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return False
    try:
        arr = json.loads(r.stdout or "[]")
    except ValueError:
        return False
    return isinstance(arr, list) and len(arr) >= 1


def live_pr(slug, number):
    """The live REST PR object, or None on read failure. Carries head.sha,
    mergeable, mergeable_state, additions, deletions, changed_files, user,
    labels, state, merged, merge_commit_sha - everything G4/G5/G7 need."""
    try:
        return core.gh_rest("/repos/%s/pulls/%s" % (slug, number))
    except RuntimeError:
        return None


def mergeable_clean(pr):
    """(ok, reason): the live merge state is provably clean to merge NOW.

    Requires `mergeable == True` AND `mergeable_state == 'clean'`, the REST twins
    of GraphQL `mergeable == MERGEABLE` / `mergeStateStatus == CLEAN`. `clean`
    already encodes required checks + required reviews + up-to-date, so
    dirty/blocked/behind/unstable/draft/unknown/null all fail closed. GitHub
    computes these lazily, so a null read (base just moved) correctly holds."""
    if not isinstance(pr, dict):
        return (False, "no live PR data")
    if pr.get("mergeable") is not True:
        return (False, "live mergeable is %r (need MERGEABLE)" % pr.get("mergeable"))
    state = str(pr.get("mergeable_state") or "").strip().lower()
    if state != "clean":
        return (False, "live merge state is %r (need CLEAN)" % (state or "<none>"))
    return (True, "MERGEABLE and CLEAN")


def immutable_compare_files(slug, base_sha, head_sha, expected_count):
    base_sha = str(base_sha or "").strip()
    head_sha = str(head_sha or "").strip()
    if not _GIT_OBJECT_ID_RE.fullmatch(base_sha) or not _GIT_OBJECT_ID_RE.fullmatch(head_sha):
        return ([], False, False)
    try:
        comparison = core.gh_rest(
            "/repos/%s/compare/%s...%s" % (slug, base_sha, head_sha)
        )
    except RuntimeError:
        return ([], False, False)
    if not isinstance(comparison, dict) or not isinstance(comparison.get("files"), list):
        return ([], False, False)
    files = []
    entry_count = 0
    for changed in comparison["files"]:
        if not isinstance(changed, dict):
            return ([], False, False)
        filename = str(changed.get("filename") or "").strip()
        if not filename:
            return ([], False, False)
        files.append(filename)
        entry_count += 1
        if "previous_filename" in changed:
            previous_filename = changed.get("previous_filename")
            if not isinstance(previous_filename, str):
                return ([], False, False)
            previous_filename = previous_filename.strip()
            if not previous_filename:
                return ([], False, False)
            files.append(previous_filename)
    try:
        count = int(expected_count)
    except (TypeError, ValueError):
        return ([], False, False)
    complete = count >= 0 and entry_count == count
    return (files, True, complete)


# --------------------------------------------------------------------------- #
# candidate evaluation (G0-G6) - deterministic, fail-closed
# --------------------------------------------------------------------------- #
def _card_label_names(card):
    names = set()
    for label in (card or {}).get("labels") or []:
        if isinstance(label, dict) and label.get("name"):
            names.add(str(label["name"]))
        elif isinstance(label, str):
            names.add(label)
    return names


def _card_author_login(card):
    author = (card or {}).get("author")
    if isinstance(author, dict):
        author = author.get("login")
    return str(author or "").strip()


def _trusted_card(card, state, labels):
    repo = str((state or {}).get("repo") or "").strip()
    number = str((state or {}).get("number") or "").strip()
    required = {
        "needs-decision",
        "repo:%s" % repo,
        "kind:pr-review",
        "target:%s-%s" % (repo, number),
    }
    return (
        _card_author_login(card) == CARD_AUTOMATION_AUTHOR
        and bool(repo)
        and bool(number)
        and required.issubset(labels)
        and any(label.startswith("priority:") for label in labels)
    )


def _card_is_claimed(labels):
    names = set(labels or ())
    return (
        {"needs-decision", "processing", AUTO_MERGE_CLAIM_LABEL}.issubset(names)
        and names.isdisjoint({"resolved", "blocked"})
    )


def _card_has_pending_decision(labels):
    return any(str(label).startswith("decision:") for label in labels or ())


def _selected_card_option(body):
    return bool(
        re.search(r"(?m)^\s*[-*]\s+\[[xX]\].*<!--\s*opt:[^>]+-->", body or "")
    )


def _fresh_verdict_for_head(state, head_sha):
    state = state if isinstance(state, dict) else {}
    head_sha = str(head_sha or "")
    if not head_sha:
        return (False, "", "current head SHA is unavailable")
    if state.get("triage_status") != "succeeded":
        return (False, "", "no successful auto-triage verdict on the card")
    if str(state.get("triaged_sha") or "") != head_sha:
        return (False, "", "behavior verdict is stale (not for the current head SHA)")
    if str(state.get("head_sha") or "") != head_sha:
        return (False, "", "card head SHA is not current")
    recommendation = state.get("triage_recommendation")
    action = (
        render_card.normalize_recommendation_action(recommendation.get("action"))
        if isinstance(recommendation, dict)
        else ""
    )
    if action != "merge":
        return (
            False,
            "",
            "top-level triage recommendation is not an explicit merge",
        )
    return verdict_eligible(state.get("automerge_verdict"))


def _card_index(cards):
    """Map (target_repo, target_number) -> {issue, state, labels} for every
    pr-review card, so a scan worklist item can find its persisted behavior
    verdict. `cards` is the cards.json list ({number, body, labels, ...})."""
    index = {}
    duplicate_keys = set()
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        state = core.parse_state_block(card.get("body") or "") or {}
        if state.get("kind") != "pr-review":
            continue
        repo = str(state.get("repo") or "").strip()
        number = str(state.get("number") or "").strip()
        if not repo or not number:
            continue
        labels = _card_label_names(card)
        if not _trusted_card(card, state, labels):
            continue
        key = (repo, number)
        if key in index:
            duplicate_keys.add(key)
            continue
        index[key] = {
            "issue": card.get("number"),
            "state": state,
            "labels": labels,
            "body": card.get("body") or "",
            "updated_at": render_card.card_updated_at(card),
        }
    for key in duplicate_keys:
        index.pop(key, None)
    return index


def _repo_result_ok(scan, repo):
    """(ok, reason): the repo scanned cleanly this pass. Never act on an
    ok:false, truncated, or absent repo - state is incomplete (same freeze
    invariant reconcile uses)."""
    result = ((scan or {}).get("repos") or {}).get(repo)
    if not isinstance(result, dict):
        return (False, "repo %s absent from scan results" % repo)
    if not result.get("ok"):
        return (False, "repo %s did not scan cleanly (ok:false)" % repo)
    if result.get("truncated"):
        return (False, "repo %s scan was truncated (incomplete state)" % repo)
    return (True, "")


def evaluate_candidate(
    owner,
    item,
    card_entry,
    repo_cfg,
    global_auto_merge,
    maintainer_logins,
):
    """Run every deterministic gate for one merge-ready pr-review scan item and
    return a structured result. Does NOT merge - see `act_on_scan`.

    Returns a dict: {eligible, hold_reason, gates{...}, audit{...},
    head_sha, card_issue, slug}. `eligible` True means the caller may proceed to
    the G7 live re-check + do_merge.
    """
    repo = item["repo"]
    number = str(item["number"])
    slug = "%s/%s" % (owner, repo)
    head_sha = str(item.get("head_sha") or "")
    result = {
        "repo": repo,
        "number": number,
        "slug": slug,
        "head_sha": head_sha,
        "card_issue": (card_entry or {}).get("issue"),
        "eligible": False,
        "hold_reason": "",
        "gates": {},
        "audit": {},
    }

    def hold(reason):
        result["hold_reason"] = reason
        return result

    # G0a: repo opted in.
    if not core._auto_merge_enabled(repo_cfg, global_auto_merge):
        return hold("G0 auto_merge not enabled for %s" % repo)
    if not auto_merge_triage_available():
        return hold("G6 CLAUDE_CODE_OAUTH_TOKEN is unavailable")

    # G1: a persisted pr-review card with a fresh, successful behavior verdict.
    if not card_entry:
        return hold("G1 no pr-review decision card found for %s#%s" % (repo, number))
    state = card_entry.get("state") or {}
    if state.get("held"):
        return hold("G1 card is still held (auto-triage has not published it)")
    if not _card_is_claimed(card_entry.get("labels") or set()):
        return hold("G1 card is not a current auto-merge claim")
    v_ok, behavior_class, v_reason = _fresh_verdict_for_head(state, head_sha)
    # G6 is a free/cheap check on already-persisted state, so run it before the
    # cached VISION read and the live target reads below (an ineligible fresh
    # verdict holds without spending any API calls).
    if not v_ok:
        return hold("G6 %s" % v_reason)
    verdict = state.get("automerge_verdict")

    vision_present, vision_sha = vision_on_default_branch(slug)
    if not vision_present:
        return hold("G0 no committed VISION.md on %s default branch" % repo)
    if str((verdict or {}).get("vision_sha") or "") != vision_sha:
        return hold("G6 behavior verdict is not for the current VISION.md revision")
    result["audit"]["vision_sha"] = vision_sha

    # Gather live PR state once (used by G2/G3/G4/G5); a final fresh re-read
    # happens at act time (G7) immediately before merging.
    pr = live_pr(slug, number)
    if pr is None:
        return hold("G4 could not read live PR %s#%s" % (repo, number))
    if pr.get("merged"):
        return hold("PR %s#%s already merged" % (repo, number))
    if str(pr.get("state") or "").lower() != "open":
        return hold("PR %s#%s is not open" % (repo, number))

    # Per-PR escape hatch.
    if core.NO_AUTO_MERGE_LABEL in _pr_label_names(pr):
        return hold("escape hatch label %s present" % core.NO_AUTO_MERGE_LABEL)

    # Live head re-check vs the scan/verdict revision.
    live_head = str((pr.get("head") or {}).get("sha") or "")
    if not live_head or live_head != head_sha:
        return hold(
            "head moved since scan (scan %s, live %s)"
            % (head_sha[:8] or "<none>", live_head[:8] or "<none>")
        )
    base_sha = str((pr.get("base") or {}).get("sha") or "")
    if not _GIT_OBJECT_ID_RE.fullmatch(base_sha):
        return hold("G2 live PR base SHA is unavailable")
    verdict_base_sha = str((verdict or {}).get("base_sha") or "")
    if not _GIT_OBJECT_ID_RE.fullmatch(verdict_base_sha):
        return hold("G6 behavior verdict is not bound to a base SHA")
    if verdict_base_sha != base_sha:
        return hold("G6 behavior verdict is not for the current base SHA")
    result["base_sha"] = base_sha

    # G3: returning contributor (non-bot human, >= 1 prior same-repo merge).
    author = _pr_author_login(pr)
    if not author:
        return hold("G3 PR author unknown")
    if not _pr_author_is_provably_human(pr) or author.casefold() in maintainer_logins:
        return hold("G3 author %s is a bot/maintainer, not a returning contributor"
                    % author)
    if not has_prior_merged_pr(slug, author):
        return hold("G3 author %s has no prior merged PR in %s" % (author, repo))
    result["audit"]["contributor"] = author
    result["audit"]["contributor_proof"] = "has >=1 prior merged PR in %s" % repo
    result["gates"]["returning_contributor"] = True

    # G2: unconditional file exclusions (fail closed if the list is unreadable).
    files, files_ok, complete = immutable_compare_files(
        slug, base_sha, head_sha, pr.get("changed_files")
    )
    if not files_ok or not complete:
        return hold("G2 could not list all changed files (failing closed)")
    exclusions = core._auto_merge_exclusions(files)
    if exclusions:
        return hold("G2 touches excluded path(s): %s" % ", ".join(exclusions[:5]))
    result["gates"]["exclusions"] = "none"

    # G5: blast radius.
    br_ok, br_reason = blast_radius_ok(
        pr.get("changed_files"), pr.get("additions"), pr.get("deletions")
    )
    if not br_ok:
        return hold("G5 blast radius: %s" % br_reason)
    result["gates"]["blast_radius"] = br_reason

    # G4: live mergeability + clean merge state.
    mc_ok, mc_reason = mergeable_clean(pr)
    if not mc_ok:
        return hold("G4 %s" % mc_reason)
    result["gates"]["mergeable_clean"] = mc_reason
    result["gates"]["compliance_tests"] = "comp=%s tests=%s (merge-ready)" % (
        item.get("comp"),
        item.get("tests"),
    )

    # G6 (already validated above as v_ok): record it for the audit trail.
    result["gates"]["behavior_verdict"] = v_reason
    result["audit"]["behavior_class"] = behavior_class
    result["audit"]["behavior_verdict"] = verdict

    result["eligible"] = True
    return result


def _release_card_claim(number):
    result = render_card._gh(
        [
            "issue",
            "edit",
            str(number),
            "--remove-label",
            "processing",
            "--remove-label",
            AUTO_MERGE_CLAIM_LABEL,
        ],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "could not release auto-merge claim: %s"
            % str(getattr(result, "stderr", "") or "gh error").strip()
        )


def recover_stale_card_claims(cards):
    recovered = []
    for entry in _card_index(cards).values():
        if not _card_is_claimed(entry.get("labels") or set()):
            continue
        number = entry.get("issue")
        if not number:
            continue
        try:
            current = render_card.get_card(number)
            current_entry = _card_index([current]).get(
                (
                    str((entry.get("state") or {}).get("repo") or ""),
                    str((entry.get("state") or {}).get("number") or ""),
                )
            )
            if (
                current_entry
                and render_card.issue_is_open(current)
                and _card_is_claimed(current_entry.get("labels") or set())
            ):
                _release_card_claim(number)
                recovered.append(number)
        except Exception as e:
            print(
                "::warning::wheelhouse auto-merge could not recover stale claim #%s: %s"
                % (number, str(e)[:160]),
                file=sys.stderr,
            )
    return recovered


def _current_claim_matches(expected, current, repo, number):
    if not current or not render_card.issue_is_open(current):
        return (False, "card is no longer open")
    current_entry = _card_index([current]).get((repo, number))
    if not current_entry:
        return (False, "card is no longer a trusted pr-review card")
    expected_updated_at = str(expected.get("updated_at") or "")
    current_updated_at = str(current_entry.get("updated_at") or "")
    if not expected_updated_at or not current_updated_at:
        return (False, "card updatedAt is unavailable")
    if current_updated_at != expected_updated_at:
        return (False, "card changed after the claim")
    if current_entry["body"] != expected.get("body", ""):
        return (False, "card body changed")
    if current_entry["state"] != expected.get("state"):
        return (False, "card state changed")
    if not _card_is_claimed(current_entry["labels"]):
        return (False, "card claim is no longer current")
    if _card_has_pending_decision(current_entry["labels"]):
        return (False, "a pending owner decision label is present")
    if _selected_card_option(current_entry["body"]):
        return (False, "an owner selected a card option")
    return (True, "")


def claim_cards(scan, cards):
    cfg = core.load_config()
    global_auto_merge = cfg["auto_merge"]
    index = _card_index(cards)
    claimed = []
    recover_stale_card_claims(cards)
    if not auto_merge_triage_available():
        return claimed
    for item in (scan or {}).get("items") or []:
        if item.get("kind") != "pr-review" or item.get("bucket") != "merge-ready":
            continue
        repo = item.get("repo")
        number = str(item.get("number") or "")
        repo_cfg = (cfg["repos"] or {}).get(repo, {})
        if not core._auto_merge_enabled(repo_cfg, global_auto_merge):
            continue
        expected = index.get((repo, number))
        if not expected:
            continue
        try:
            current = render_card.get_card(expected["issue"])
            current_entry = _card_index([current]).get((repo, number))
            verdict_ok, _, _ = _fresh_verdict_for_head(
                (current_entry or {}).get("state"), item.get("head_sha")
            )
            if (
                not current_entry
                or not render_card.issue_is_open(current)
                or not render_card.is_refreshable(current_entry["labels"])
                or current_entry["state"] != expected["state"]
                or current_entry.get("updated_at") != expected.get("updated_at")
                or _card_has_pending_decision(current_entry["labels"])
                or _selected_card_option(current.get("body"))
                or not verdict_ok
            ):
                continue
            render_card.ensure_labels(["processing", AUTO_MERGE_CLAIM_LABEL])
            claim = render_card._gh(
                [
                    "issue",
                    "edit",
                    str(expected["issue"]),
                    "--add-label",
                    "processing",
                    "--add-label",
                    AUTO_MERGE_CLAIM_LABEL,
                ],
                check=False,
            )
            if claim.returncode != 0:
                continue
            claimed_card = render_card.get_card(expected["issue"])
            claimed_entry = _card_index([claimed_card]).get((repo, number))
            claimed_verdict_ok, _, _ = _fresh_verdict_for_head(
                (claimed_entry or {}).get("state"), item.get("head_sha")
            )
            if (
                not claimed_entry
                or not render_card.issue_is_open(claimed_card)
                or claimed_entry["state"] != expected["state"]
                or not _card_is_claimed(claimed_entry["labels"])
                or _card_has_pending_decision(claimed_entry["labels"])
                or _selected_card_option(claimed_card.get("body"))
                or not claimed_verdict_ok
            ):
                _release_card_claim(expected["issue"])
                continue
            claimed.append(claimed_card)
        except Exception as e:
            print(
                "::warning::wheelhouse auto-merge could not claim %s#%s: %s"
                % (repo, number, str(e)[:160]),
                file=sys.stderr,
            )
    return claimed


def cmd_claim(scan_path, cards_path):
    scan = _load_json(scan_path, {})
    cards = _load_json(cards_path, [])
    if not isinstance(cards, list):
        cards = []
    claimed = claim_cards(scan, cards)
    out_path = os.environ.get("WHEELHOUSE_AUTOMERGE_CLAIMS", "automerge-claims.json")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(claimed, f, indent=2)
    except OSError as e:
        print(
            "::warning::wheelhouse auto-merge could not write claims: %s"
            % str(e)[:160],
            file=sys.stderr,
        )
    print("wheelhouse auto-merge: %d card claim(s)" % len(claimed))


def validate_claimed_cards(cards):
    validated = []
    for (repo, number), expected in _card_index(cards).items():
        if not _card_is_claimed(expected.get("labels") or set()):
            continue
        issue = expected.get("issue")
        try:
            current = render_card.get_card(issue)
            current_matches, _ = _current_claim_matches(
                expected, current, repo, number
            )
            if current_matches:
                validated.append(current)
                continue
            _release_card_claim(issue)
        except Exception as e:
            print(
                "::warning::wheelhouse auto-merge could not validate claim #%s: %s"
                % (issue, str(e)[:160]),
                file=sys.stderr,
            )
    return validated


def cmd_validate(cards_path):
    cards = _load_json(cards_path, [])
    if not isinstance(cards, list):
        cards = []
    validated = validate_claimed_cards(cards)
    out_path = os.environ.get(
        "WHEELHOUSE_AUTOMERGE_VALIDATED_CLAIMS", "automerge-valid-claims.json"
    )
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(validated, f, indent=2)
    except OSError as e:
        print(
            "::warning::wheelhouse auto-merge could not write validated claims: %s"
            % str(e)[:160],
            file=sys.stderr,
        )
    print("wheelhouse auto-merge: %d validated claim(s)" % len(validated))


# --------------------------------------------------------------------------- #
# G7: act (live re-check immediately before merging, then do_merge)
# --------------------------------------------------------------------------- #
def _read_card_with_card_token(number, card_token):
    if not str(card_token or "").strip():
        return None
    original_token = os.environ.get("GH_TOKEN")
    try:
        os.environ["GH_TOKEN"] = card_token
        return render_card.get_card(number)
    finally:
        if original_token is None:
            os.environ.pop("GH_TOKEN", None)
        else:
            os.environ["GH_TOKEN"] = original_token


def act_merge(
    owner, repo, number, head_sha, vision_sha, base_sha, expected_card, card_token
):
    """G7. Immediately re-read head SHA + mergeability + clean merge state, then
    call the existing do_merge (which does its own head re-check and runs on the
    ambient FLEET_TOKEN with the unchanged owner-safety / thank-you model).

    Returns (outcome, detail, merge_commit) where outcome in
    'merged' / 'held' / 'error'."""
    slug = "%s/%s" % (owner, repo)
    vision_present, live_vision_sha = vision_on_default_branch(slug)
    if not vision_present:
        return ("held", "VISION.md disappeared before acting", "")
    if live_vision_sha != vision_sha:
        return ("held", "VISION.md changed before acting", "")
    pr = live_pr(slug, number)
    if pr is None:
        return ("held", "could not re-read PR before merging", "")
    if pr.get("merged") or str(pr.get("state") or "").lower() != "open":
        return ("held", "PR left the open merge-ready state before acting", "")
    live_head = str((pr.get("head") or {}).get("sha") or "")
    if not live_head or live_head != head_sha:
        return ("held", "head moved immediately before acting", "")
    live_base = str((pr.get("base") or {}).get("sha") or "")
    if not live_base or live_base != base_sha:
        return ("held", "base changed immediately before acting", "")
    if core.NO_AUTO_MERGE_LABEL in _pr_label_names(pr):
        return ("held", "escape hatch label appeared before acting", "")
    mc_ok, mc_reason = mergeable_clean(pr)
    if not mc_ok:
        return ("held", "final re-check: %s" % mc_reason, "")
    current_card = _read_card_with_card_token(expected_card.get("issue"), card_token)
    if current_card is None and not str(card_token or "").strip():
        return ("held", "final card re-check: default card token is unavailable", "")
    card_ok, card_reason = _current_claim_matches(
        expected_card, current_card, repo, str(number)
    )
    if not card_ok:
        return ("held", "final card re-check: %s" % card_reason, "")

    message, terminal, merge_commit = apply_decision.do_merge(
        owner, repo, number, head_sha, return_merge_commit=True
    )
    if terminal == "resolved" and message.startswith("Merged "):
        merge_commit = str(merge_commit or "").strip()
        if not _GIT_OBJECT_ID_RE.fullmatch(merge_commit):
            return (
                "post-merge-error",
                "merge endpoint did not return a merge commit SHA for audit",
                "",
            )
        return ("merged", message, merge_commit)
    if terminal == "resolved":
        # do_merge saw already-merged / not-open (a race) - not our merge.
        return ("held", message, "")
    return ("error", message, "")


# --------------------------------------------------------------------------- #
# act CLI (FLEET_TOKEN)
# --------------------------------------------------------------------------- #
def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def act_on_scan(scan, cards):
    """Evaluate every merge-ready pr-review candidate and merge the ones that
    qualify. Returns the results payload (also written to disk by the CLI).
    Emits exactly one ::notice:: (merged / no candidate action) or ::warning::
    (held / error) per candidate, mirroring `_auto_approve_or_card`."""
    owner = core.get_owner()
    cfg = core.load_config()
    global_auto_merge = cfg["auto_merge"]
    maintainer_logins = {m.casefold() for m in core.maintainers()}
    card_token = os.environ.get("WHEELHOUSE_CARD_TOKEN", "")
    index = _card_index(cards)
    merges = []
    holds = []
    post_merge_errors = []
    releases = [
        {"card_issue": entry["issue"]}
        for entry in index.values()
        if _card_is_claimed(entry.get("labels"))
    ]
    for item in (scan or {}).get("items") or []:
        if item.get("kind") != "pr-review" or item.get("bucket") != "merge-ready":
            continue
        repo = item["repo"]
        number = str(item["number"])
        repo_cfg = (cfg["repos"] or {}).get(repo, {})
        # SILENTLY skip a repo that never opted into auto-merge (the default for
        # the whole fleet): it is an ordinary merge-ready card, not an auto-merge
        # candidate, so it must not spam the scan log with a hold warning. Audit
        # notices/warnings below are reserved for opted-in repos, where "why
        # didn't this auto-merge?" is a real question.
        if not core._auto_merge_enabled(repo_cfg, global_auto_merge):
            continue
        ok_repo, ok_reason = _repo_result_ok(scan, repo)
        if not ok_repo:
            _warn(repo, number, ok_reason)
            holds.append({"repo": repo, "number": number, "hold_reason": ok_reason})
            continue
        indeterminate = (
            ((scan.get("repos") or {}).get(repo) or {}).get("indeterminate_pr_numbers")
            or []
        )
        if item["number"] in indeterminate:
            reason = "mergeability indeterminate this scan (frozen)"
            _warn(repo, number, reason)
            holds.append({"repo": repo, "number": number, "hold_reason": reason})
            continue
        card_entry = index.get((repo, number))
        # Fail CLOSED on any unexpected error evaluating or acting on one
        # candidate: hold it and keep scanning, never crash the scheduled
        # backstop over a single API hiccup.
        try:
            result = evaluate_candidate(
                owner,
                item,
                card_entry,
                repo_cfg,
                global_auto_merge,
                maintainer_logins,
            )
        except Exception as e:  # noqa: BLE001 - fail-closed on any surprise
            reason = "evaluation raised: %s" % str(e)[:160]
            _warn(repo, number, reason)
            holds.append({"repo": repo, "number": number, "hold_reason": reason})
            continue
        if not result["eligible"]:
            _warn(repo, number, result["hold_reason"])
            holds.append(
                {
                    "repo": repo,
                    "number": number,
                    "hold_reason": result["hold_reason"],
                }
            )
            continue
        try:
            outcome, detail, merge_commit = act_merge(
                owner,
                repo,
                item["number"],
                result["head_sha"],
                result["audit"]["vision_sha"],
                result["base_sha"],
                card_entry,
                card_token,
            )
        except Exception as e:  # noqa: BLE001 - a merge hiccup must not crash
            outcome, detail, merge_commit = ("error", "act raised: %s" % str(e)[:160], "")
        if outcome == "merged":
            record = {
                "repo": repo,
                "number": number,
                "card_issue": result["card_issue"],
                "head_sha": result["head_sha"],
                "merge_commit": merge_commit,
                "merged_at": _now(),
                "contributor": result["audit"].get("contributor", ""),
                "contributor_proof": result["audit"].get("contributor_proof", ""),
                "vision_sha": result["audit"].get("vision_sha", ""),
                "behavior_class": result["audit"].get("behavior_class", ""),
                "behavior_verdict": result["audit"].get("behavior_verdict", {}),
                "gates": result["gates"],
                "detail": detail,
            }
            merges.append(record)
            print(
                "::notice::wheelhouse auto-merge merged %s#%s (%s) commit %s: "
                "class %s, %s"
                % (
                    repo,
                    number,
                    result["head_sha"][:8],
                    (merge_commit or "?")[:8],
                    record["behavior_class"],
                    result["audit"].get("contributor_proof", ""),
                ),
                file=sys.stderr,
            )
        else:
            _warn(repo, number, "%s (%s)" % (detail, outcome))
            holds.append(
                {"repo": repo, "number": number, "hold_reason": "%s: %s"
                 % (outcome, detail)}
            )
            if outcome == "post-merge-error":
                post_merge_errors.append("%s#%s: %s" % (repo, number, detail))

    return {
        "generated_at": _now(),
        "owner": owner,
        "merges": merges,
        "holds": holds,
        "releases": releases,
        "post_merge_errors": post_merge_errors,
    }


def _warn(repo, number, reason):
    print(
        "::warning::wheelhouse auto-merge held %s#%s: %s"
        % (repo, number, core._workflow_command_text(reason)),
        file=sys.stderr,
    )


def cmd_act(scan_path, cards_path):
    scan = _load_json(scan_path, {})
    cards = _load_json(cards_path, [])
    if not isinstance(cards, list):
        cards = []
    payload = act_on_scan(scan, cards)
    out_path = os.environ.get("WHEELHOUSE_AUTOMERGE_RESULTS", "automerge.json")
    try:
        _write_json_atomically(out_path, payload)
    except Exception as e:
        print(
            "::error::wheelhouse auto-merge could not write results: %s"
            % str(e)[:160],
            file=sys.stderr,
        )
        raise RuntimeError("could not write auto-merge results") from e
    print(
        "wheelhouse auto-merge: %d merged, %d held"
        % (len(payload["merges"]), len(payload["holds"]))
    )
    if payload.get("post_merge_errors"):
        for error in payload["post_merge_errors"]:
            print("::error::wheelhouse auto-merge audit handoff failed: %s" % error, file=sys.stderr)
        raise RuntimeError("could not record one or more completed auto-merges")


# --------------------------------------------------------------------------- #
# durable audit ledger (mirrors scan-health) + resolved decision record
# --------------------------------------------------------------------------- #
def parse_ledger(body):
    """The persisted list of auto-merge entries, or [] for a missing/unparseable
    ledger."""
    if not body:
        return []
    m = _LEDGER_RE.search(body)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except (ValueError, TypeError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def _ledger_entry(record):
    """The compact, durable audit row for one auto-merge."""
    verdict = record.get("behavior_verdict")
    return {
        "merged_at": record.get("merged_at", ""),
        "repo": record.get("repo", ""),
        "number": record.get("number", ""),
        "card": record.get("card_issue"),
        "contributor": record.get("contributor", ""),
        "contributor_proof": record.get("contributor_proof", ""),
        "head_sha": record.get("head_sha", ""),
        "vision_sha": record.get("vision_sha", ""),
        "behavior_class": record.get("behavior_class", ""),
        "behavior_verdict": verdict if isinstance(verdict, dict) else {},
        "merge_commit": record.get("merge_commit", ""),
        "gates": record.get("gates", {}),
    }


def append_ledger_entries(
    prev,
    records,
    cap=LEDGER_ENTRY_CAP,
    max_body_bytes=LEDGER_MAX_BODY_BYTES,
    updated_at="",
):
    """Pure ledger update: previous entries + this run's records, newest last,
    capped to the most recent `cap`."""
    prev = prev if isinstance(prev, list) else []
    combined = list(prev)
    known = {
        _ledger_entry_identity(entry)
        for entry in combined
        if _ledger_entry_identity(entry)
    }
    for record in records or []:
        entry = _ledger_entry(record)
        identity = _ledger_entry_identity(entry)
        if identity and identity in known:
            continue
        combined.append(entry)
        if identity:
            known.add(identity)
    if cap and len(combined) > cap:
        combined = combined[-cap:]
    while (
        combined
        and len(render_ledger_body(combined, updated_at).encode("utf-8"))
        > max_body_bytes
    ):
        combined.pop(0)
    return combined


def _ledger_entry_identity(entry):
    if not isinstance(entry, dict):
        return None
    values = tuple(
        str(entry.get(field) or "")
        for field in ("repo", "number", "head_sha")
    )
    return values if all(values) else None


def render_ledger_body(entries, updated_at=""):
    """Render the ledger issue body: a short human summary of recent merges plus
    the hidden machine-readable marker carrying every stored entry."""
    entries = entries if isinstance(entries, list) else []
    lines = [
        "Automated ledger of Wheelhouse scan-time auto-merges - do not edit by "
        "hand.",
        "",
        "Each row is one PR merged automatically as a strict subset of the manual "
        "merge gate, with the contributor trust proof, head SHA, base VISION.md "
        "SHA, behavior class, and merge commit that qualified it.",
        "",
    ]
    if entries:
        lines.append("Most recent auto-merges:")
        for e in reversed(entries[-20:]):
            lines.append(
                "- `%s` %s#%s by %s - class %s, head `%s`, vision `%s`, commit `%s` (%s)"
                % (
                    e.get("merged_at", ""),
                    e.get("repo", ""),
                    e.get("number", ""),
                    e.get("contributor", "?"),
                    e.get("behavior_class", "?"),
                    str(e.get("head_sha", ""))[:8],
                    str(e.get("vision_sha", ""))[:8],
                    str(e.get("merge_commit", ""))[:8],
                    e.get("contributor_proof", ""),
                )
            )
    else:
        lines.append("No auto-merges recorded yet.")
    lines.append("")
    lines.append(
        "<!-- %s: %s -->"
        % (
            LEDGER_MARKER,
            json.dumps(
                {"updated_at": updated_at or "", "entries": entries},
                separators=(",", ":"),
            ),
        )
    )
    return "\n".join(lines)


def _find_ledger_issue(slug):
    path = "repos/%s/issues?state=all&labels=%s&per_page=100" % (
        slug,
        core.quote(LEDGER_LABEL),
    )
    issues = core._flatten_paginated_comments(
        core.gh_rest(path, paginate=True, slurp=True)
    )
    for it in issues:
        if not isinstance(it, dict) or "pull_request" in it:
            continue
        if _LEDGER_RE.search(it.get("body") or ""):
            return it
    return None


def _create_ledger_issue(slug, body):
    core._ensure_repo_label(slug, LEDGER_LABEL)
    r = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            "repos/%s/issues" % slug,
            "-f",
            "title=" + LEDGER_TITLE,
            "-f",
            "body=" + body,
            "-f",
            "labels[]=" + LEDGER_LABEL,
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            "create auto-merge ledger issue failed: %s"
            % (r.stderr.strip() or "gh error")
        )
    issue = json.loads(r.stdout)
    number = issue.get("number")
    if number:
        core.gh_rest(
            "repos/%s/issues/%s" % (slug, number),
            method="PATCH",
            fields={"state": "closed"},
        )
    return issue


def _is_transient_audit_error(error):
    text = str(error or "")
    return core._is_transient_stderr(text) or bool(
        re.search(r"(?:^|\\D)(?:408|409|429|500|502|503|504)(?:\\D|$)", text)
    )


def _retry_audit_write(operation, description):
    for attempt in range(1, AUDIT_WRITE_MAX_ATTEMPTS + 1):
        try:
            return operation()
        except SystemExit:
            raise
        except Exception as error:
            if attempt < AUDIT_WRITE_MAX_ATTEMPTS and _is_transient_audit_error(error):
                _audit_sleep(AUDIT_WRITE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            print(
                "::error::wheelhouse auto-merge %s failed: %s"
                % (description, str(error)[:200]),
                file=sys.stderr,
            )
            raise RuntimeError("%s failed" % description) from error


def append_to_ledger(records):
    """Persist this run's auto-merges into the durable ledger issue in THIS repo."""
    if not records:
        return

    def write():
        slug = core._this_repo_slug()
        issue = _find_ledger_issue(slug)
        prev = parse_ledger(issue.get("body") if issue else None)
        updated_at = _now()
        entries = append_ledger_entries(prev, records, updated_at=updated_at)
        body = render_ledger_body(entries, updated_at)
        if issue and issue.get("number"):
            core.gh_rest(
                "repos/%s/issues/%s" % (slug, issue["number"]),
                method="PATCH",
                fields={"body": body, "state": "closed"},
            )
        else:
            _create_ledger_issue(slug, body)

    _retry_audit_write(write, "ledger update")


def audit_comment(record):
    """The resolved-decision-record comment posted on the merged PR's card, so
    the owner sees each automatic merge and why it qualified."""
    verdict = record.get("behavior_verdict") or {}
    lines = [
        "Auto-merged %s#%s as a strict subset of the manual merge gate."
        % (record.get("repo", ""), record.get("number", "")),
        "",
        "- Contributor: %s (%s)"
        % (
            record.get("contributor", "?"),
            record.get("contributor_proof", "prior same-repo merge"),
        ),
        "- Head SHA: `%s`" % record.get("head_sha", ""),
        "- Base VISION.md SHA: `%s`" % record.get("vision_sha", ""),
        "- Behavior class: %s" % record.get("behavior_class", "?"),
        "- Merge commit: `%s`" % record.get("merge_commit", ""),
        "- Behavior verdict: `%s`"
        % json.dumps(verdict, separators=(",", ":"), sort_keys=True),
    ]
    gates = record.get("gates") or {}
    if gates:
        lines.append("- Gates: %s" % json.dumps(gates, separators=(",", ":")))
    lines.append("")
    lines.append("Wheelhouse never auto-reverts; revert the merge commit above "
                 "if this merge was not wanted.")
    return "\n".join(lines)


def _strict_audited_close_card(number, message, close_issue=True):
    core._ensure_repo_label(core._this_repo_slug(), "resolved")
    render_card._gh(["issue", "comment", str(number), "--body", message])
    render_card._gh(
        [
            "issue",
            "edit",
            str(number),
            "--add-label",
            "resolved",
            "--remove-label",
            "needs-decision",
        ]
    )
    if close_issue:
        render_card._gh(["issue", "close", str(number)])


def resolve_card(record):
    """Leave a resolved decision record on the merged PR's card (GITHUB_TOKEN)."""
    card = record.get("card_issue")
    if not card:
        return

    def close():
        current = render_card.get_card(card)
        if current is None:
            raise RuntimeError("could not read card #%s for audit" % card)
        _strict_audited_close_card(
            card,
            audit_comment(record),
            close_issue=render_card.issue_is_open(current),
        )

    _retry_audit_write(close, "resolved-card audit #%s" % card)


def release_card_claim(record):
    card = record.get("card_issue")
    if not card:
        return
    try:
        _release_card_claim(card)
    except Exception as e:
        print(
            "::warning::wheelhouse auto-merge could not release card #%s: %s"
            % (card, str(e)[:200]),
            file=sys.stderr,
        )


def _fallback_claim_releases(path):
    claims = _load_json(path, [])
    if not isinstance(claims, list):
        return []
    return [
        {"card_issue": card.get("number")}
        for card in claims
        if isinstance(card, dict) and card.get("number")
    ]


def cmd_record(results_path, validated_claims_path=None):
    payload = _load_json(results_path, None)
    handoff_valid = isinstance(payload, dict) and all(
        isinstance(payload.get(key, []), list) for key in ("merges", "releases")
    )
    if handoff_valid:
        records = payload.get("merges") or []
        releases = payload.get("releases") or []
    else:
        records = []
        releases = []
        if validated_claims_path:
            releases = _fallback_claim_releases(validated_claims_path)
    if not records and not releases:
        print("wheelhouse auto-merge record: no auto-merges to record")
        return
    errors = []
    try:
        append_to_ledger(records)
    except Exception as error:
        errors.append(error)
    for record in records:
        try:
            resolve_card(record)
        except Exception as error:
            errors.append(error)
        release_card_claim(record)
    resolved_cards = {record.get("card_issue") for record in records}
    for record in releases:
        if record.get("card_issue") not in resolved_cards:
            release_card_claim(record)
    if errors:
        raise RuntimeError("wheelhouse auto-merge audit record failed") from errors[0]
    print("wheelhouse auto-merge record: recorded %d auto-merge(s)" % len(records))


# --------------------------------------------------------------------------- #
def _write_json_atomically(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(prefix=".automerge-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print(
            "::warning::wheelhouse auto-merge could not read %s: %s"
            % (path, str(e)[:160]),
            file=sys.stderr,
        )
        return default


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "claim":
        cmd_claim(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 3 and sys.argv[1] == "validate":
        cmd_validate(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "act":
        cmd_act(sys.argv[2], sys.argv[3])
    elif len(sys.argv) in (3, 4) and sys.argv[1] == "record":
        cmd_record(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
