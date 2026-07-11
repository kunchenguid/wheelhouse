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
  G1  a trusted pure pending pr-review card is claimed from the merge-ready scan worklist
  G2  the PR touches none of the deterministic unconditional exclusions
  G3  the author has >= 1 previously merged PR in the same repo (non-bot human)
  G4  compliance + tests green (worst-wins, already encoded by merge-ready),
      live mergeable == MERGEABLE, live merge state CLEAN
  G5  blast radius: <= 20 changed files AND <= 1000 total changed lines
  G6  fresh structured verdict for the current head SHA: eligible A/B/C class,
      aligns with the base VISION.md, no ineligible existing/default behavior
      change, recommends merge (class C also strictly opt-in + default off)
  G7  immediately re-check the card, head SHA, base SHA, default-branch VISION.md,
      mergeability, clean state, escape hatch, and configured check contexts,
      then do_merge
Plus a per-PR `wheelhouse:no-auto-merge` escape hatch, global/per-repo switches
(default OFF), a durable audit ledger, and a resolved decision record.

There are DELIBERATELY no open-PR file-overlap gate and no per-contributor /
per-scan rate caps (captain override); their absence is asserted by the tests.

Four CLI phases run as separate workflow steps so each uses the right token:

  auto_merge.py claim <scan.json> <cards.json>
      Under GITHUB_TOKEN. Reclaim stale claims, then claim only stable pure
      pending cards that could be eligible. Writes the handoff file from
      $WHEELHOUSE_AUTOMERGE_CLAIMS (default automerge-claims.json).

  auto_merge.py validate <claims.json>
      Under GITHUB_TOKEN. Re-read every claimed card and discard or release a
      claim if a trusted owner/maintainer decision, comment, or card change
      appeared. Writes $WHEELHOUSE_AUTOMERGE_VALIDATED_CLAIMS (default
      automerge-valid-claims.json).

  auto_merge.py act <scan.json> <validated-claims.json>
      Under FLEET_TOKEN. Identify merge-ready pr-review candidates from the scan,
      join the validated persisted behavior verdict from the card bodies, run G0-G7, and
      call do_merge for the ones that qualify. Writes a machine-readable results
      file (path from $WHEELHOUSE_AUTOMERGE_RESULTS, default automerge.json) and
      one ::notice::/::warning:: audit line per candidate. Uses the separate
      default card token only to persist an audit intent before merging.

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
AUDIT_PENDING_FIELD = "automerge_audit_pending"
AUDIT_INTENT_FIELD = "automerge_audit_intent"
# Keep the stored history bounded so the ledger body cannot grow without limit.
LEDGER_ENTRY_CAP = 200
LEDGER_MAX_BODY_BYTES = 60000
_LEDGER_RE = re.compile(
    r"<!--\s*%s:\s*(\{.*?\})\s*-->" % re.escape(LEDGER_MARKER), re.S
)
_GIT_OBJECT_ID_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

_LIVE_STATUS_GQL = (
    """
query($owner:String!, $name:String!, $number:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      headRefOid
      commits(last:1) { nodes { commit { statusCheckRollup {
        state
        contexts(first:%d) { totalCount pageInfo { hasNextPage } nodes {
          __typename
          ... on CheckRun { name conclusion status }
          ... on StatusContext { context state }
        }}
      }}}}
    }
  }
}
"""
    % core.STATUS_CONTEXTS_PAGE_SIZE
)


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
    return subprocess.run(["gh", "api", path], capture_output=True, text=True)


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


def live_check_status(owner, repo, number, head_sha, repo_cfg):
    try:
        data = core._gh_graphql_data(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                "query=" + _LIVE_STATUS_GQL,
                "-f",
                "owner=" + owner,
                "-f",
                "name=" + repo,
                "-F",
                "number=%s" % number,
            ]
        )
        pr = data["data"]["repository"]["pullRequest"]
        if not isinstance(pr, dict):
            return (False, "could not re-read PR check status")
        if str(pr.get("headRefOid") or "") != str(head_sha or ""):
            return (False, "head moved while re-reading check status")
        commits = pr.get("commits") or {}
        commit_nodes = commits.get("nodes") if isinstance(commits, dict) else None
        commit = (
            commit_nodes[0].get("commit")
            if isinstance(commit_nodes, list) and commit_nodes
            else None
        )
        rollup = commit.get("statusCheckRollup") if isinstance(commit, dict) else None
        contexts = rollup.get("contexts") if isinstance(rollup, dict) else None
        page_info = contexts.get("pageInfo") if isinstance(contexts, dict) else None
        context_nodes = contexts.get("nodes") if isinstance(contexts, dict) else None
        total_count = contexts.get("totalCount") if isinstance(contexts, dict) else None
        if (
            not isinstance(page_info, dict)
            or page_info.get("hasNextPage") is not False
            or not isinstance(context_nodes, list)
            or not isinstance(total_count, int)
            or total_count != len(context_nodes)
        ):
            return (False, "configured check contexts are incomplete")
        comp, tests, _, _ = core.check_status(pr, repo_cfg)
    except (KeyError, TypeError, RuntimeError, ValueError) as error:
        return (False, "could not re-read configured checks: %s" % str(error)[:160])
    if comp not in ("pass", "n/a") or tests != "green":
        return (False, "configured checks are comp=%s tests=%s" % (comp, tests))
    return (True, "comp=%s tests=%s" % (comp, tests))


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
    if not _GIT_OBJECT_ID_RE.fullmatch(base_sha) or not _GIT_OBJECT_ID_RE.fullmatch(
        head_sha
    ):
        return ([], False, False)
    try:
        comparison = core.gh_rest(
            "/repos/%s/compare/%s...%s" % (slug, base_sha, head_sha)
        )
    except RuntimeError:
        return ([], False, False)
    if not isinstance(comparison, dict) or not isinstance(
        comparison.get("files"), list
    ):
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


def _card_comment_count(card):
    comments = (card or {}).get("comments")
    if isinstance(comments, list):
        return len(comments)
    if isinstance(comments, int) and not isinstance(comments, bool) and comments >= 0:
        return comments
    return None


def _card_author_login(card):
    for key in ("author", "user"):
        author = (card or {}).get(key)
        if isinstance(author, dict):
            author = author.get("login")
        login = str(author or "").strip()
        if login:
            return login
    return ""


def _trusted_card_identity(card, state, labels):
    repo = str((state or {}).get("repo") or "").strip()
    number = str((state or {}).get("number") or "").strip()
    required = {
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


def _trusted_card(card, state, labels):
    return _trusted_card_identity(card, state, labels) and "needs-decision" in labels


def _card_is_claimed(labels):
    names = set(labels or ())
    return {"needs-decision", "processing", AUTO_MERGE_CLAIM_LABEL}.issubset(
        names
    ) and names.isdisjoint({"resolved", "blocked"})


def _card_has_pending_decision(labels):
    return any(str(label).startswith("decision:") for label in labels or ())


def _selected_card_option(body):
    return bool(re.search(r"(?m)^\s*[-*]\s+\[[xX]\].*<!--\s*opt:[^>]+-->", body or ""))


_NATURAL_HOLD_OR_CLOSE_RE = re.compile(
    r"""(?ix)
    \b(?:please|kindly)\s+(?:hold|close)\b
    |\b(?:hold(?:ing)?|pause|wait)\s+(?:off|on|this|it|the\s+(?:pr|pull\s+request|card|merge))\b
    |\b(?:close|decline)\s+(?:this|it|the\s+(?:pr|pull\s+request|card))\b
    |\b(?:do\s+not|don't|dont|never)\s+(?:auto[-\s]*)?merge\b
    |\b(?:stop|cancel|block)\s+(?:the\s+)?(?:auto[-\s]*)?merge\b
    |\b(?:handle|take\s+care\s+of)\s+(?:this|it)\s+manually\b
    """
)


def _trusted_decider_logins():
    try:
        owner = str(core.get_owner() or "").strip()
        maintainers = core.maintainers()
    except (Exception, SystemExit):
        return None
    logins = {owner.casefold()} if owner else set()
    if not isinstance(maintainers, (set, frozenset, list, tuple)):
        return None
    logins.update(str(login).strip().casefold() for login in maintainers if login)
    return logins or None


def _card_has_pending_owner_action(card):
    comments = (card or {}).get("comments")
    if not isinstance(comments, list):
        return (True, "card comment contents are unavailable")
    state = core.parse_state_block((card or {}).get("body") or "") or {}
    allowed = apply_decision.ALLOWED.get(state.get("kind"))
    if not isinstance(allowed, set):
        return (True, "card decision kind is unavailable")
    trusted_logins = _trusted_decider_logins()
    if not trusted_logins:
        return (True, "trusted owner/maintainer identities are unavailable")
    for comment in comments:
        if not isinstance(comment, dict):
            return (True, "card comment contents are malformed")
        author = _card_author_login(comment)
        if not author:
            return (True, "card comment author is unavailable")
        if author.casefold() not in trusted_logins:
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            return (True, "trusted owner/maintainer comment is unreadable")
        action, _ = apply_decision.parse_slash(body, allowed)
        if action or _NATURAL_HOLD_OR_CLOSE_RE.search(body):
            return (True, "a trusted owner/maintainer action is pending")
    return (False, "")


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
            "comment_count": _card_comment_count(card),
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
        return hold(
            "G3 author %s is a bot/maintainer, not a returning contributor" % author
        )
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


def _pending_audit_record(state, card_issue=None):
    return _audit_state_record(
        state, AUDIT_PENDING_FIELD, card_issue, require_merge_commit=True
    )


def _audit_intent_record(state, card_issue=None):
    return _audit_state_record(
        state, AUDIT_INTENT_FIELD, card_issue, require_merge_commit=False
    )


def _audit_state_record(state, field, card_issue=None, require_merge_commit=False):
    state = state if isinstance(state, dict) else {}
    record = state.get(field)
    if not isinstance(record, dict):
        return None
    if card_issue is not None and str(record.get("card_issue") or "") != str(
        card_issue
    ):
        return None
    if str(record.get("repo") or "") != str(state.get("repo") or ""):
        return None
    if str(record.get("number") or "") != str(state.get("number") or ""):
        return None
    if str(record.get("head_sha") or "") != str(state.get("head_sha") or ""):
        return None
    if not _ledger_entry_identity(record):
        return None
    merge_commit = str(record.get("merge_commit") or "")
    if require_merge_commit and not _GIT_OBJECT_ID_RE.fullmatch(merge_commit):
        return None
    if not require_merge_commit and merge_commit:
        return None
    return record


def _audit_state_is_protected(state, card_issue=None):
    return bool(
        _pending_audit_record(state, card_issue)
        or _audit_intent_record(state, card_issue)
    )


def _with_card_token(card_token, operation):
    if not str(card_token or "").strip():
        raise RuntimeError("default card token is unavailable")
    original_token = os.environ.get("GH_TOKEN")
    try:
        os.environ["GH_TOKEN"] = card_token
        return operation()
    finally:
        if original_token is None:
            os.environ.pop("GH_TOKEN", None)
        else:
            os.environ["GH_TOKEN"] = original_token


def stage_audit_intent(expected_card, record, card_token):
    card_issue = record.get("card_issue")
    if not card_issue:
        raise RuntimeError("auto-merge audit intent has no card issue")
    card = _read_card_with_card_token(card_issue, card_token)
    current = _card_index([card]).get(
        (str(record.get("repo") or ""), str(record.get("number") or ""))
    )
    matches, reason = _current_claim_matches(
        expected_card,
        card,
        str(record.get("repo") or ""),
        str(record.get("number") or ""),
    )
    if not matches or not current:
        raise RuntimeError("could not stage audit intent: %s" % reason)
    state = current.get("state") or {}
    existing = _audit_intent_record(state, card_issue)
    if existing:
        if _ledger_entry_identity(existing) != _ledger_entry_identity(record):
            raise RuntimeError("card #%s has a different audit intent" % card_issue)
        return current
    if _pending_audit_record(state, card_issue):
        raise RuntimeError("card #%s already has a pending audit" % card_issue)
    new_state = dict(state)
    new_state[AUDIT_INTENT_FIELD] = record
    _with_card_token(
        card_token,
        lambda: render_card._edit_issue_body(
            card_issue,
            render_card._replace_state_block(card.get("body") or "", new_state),
        ),
    )
    staged_card = _read_card_with_card_token(card_issue, card_token)
    staged = _card_index([staged_card]).get(
        (str(record.get("repo") or ""), str(record.get("number") or ""))
    )
    if not staged or not _audit_intent_record(staged.get("state"), card_issue):
        raise RuntimeError("could not confirm audit intent on card #%s" % card_issue)
    return staged


def recover_stale_card_claims(cards):
    recovered = []
    for entry in _card_index(cards).values():
        if not _card_is_claimed(entry.get("labels") or set()):
            continue
        number = entry.get("issue")
        if not number:
            continue
        if _audit_state_is_protected(entry.get("state"), number):
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
                and not _audit_state_is_protected(current_entry.get("state"), number)
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
    expected_comment_count = expected.get("comment_count")
    current_comment_count = current_entry.get("comment_count")
    if expected_comment_count is None or current_comment_count is None:
        return (False, "card comment activity is unavailable")
    if current_comment_count != expected_comment_count:
        return (False, "card comment activity changed after the claim")
    owner_action, owner_action_reason = _card_has_pending_owner_action(current)
    if owner_action:
        return (False, owner_action_reason)
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
    for (repo, number), expected in index.items():
        if not _card_is_claimed(
            expected.get("labels") or set()
        ) or not _audit_intent_record(expected.get("state"), expected.get("issue")):
            continue
        try:
            current = render_card.get_card(expected["issue"])
            matches, _ = _current_claim_matches(expected, current, repo, number)
            if matches:
                claimed.append(current)
        except Exception as e:
            print(
                "::warning::wheelhouse auto-merge could not preserve audit intent #%s: %s"
                % (expected["issue"], str(e)[:160]),
                file=sys.stderr,
            )
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
                or current_entry.get("comment_count") != expected.get("comment_count")
                or current_entry.get("comment_count") is None
                or _card_has_pending_decision(current_entry["labels"])
                or _selected_card_option(current.get("body"))
                or _card_has_pending_owner_action(current)[0]
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
                or claimed_entry.get("comment_count")
                != current_entry.get("comment_count")
                or not _card_is_claimed(claimed_entry["labels"])
                or _card_has_pending_decision(claimed_entry["labels"])
                or _selected_card_option(claimed_card.get("body"))
                or _card_has_pending_owner_action(claimed_card)[0]
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
    _write_claim_handoff(out_path, claimed, "claims")
    print("wheelhouse auto-merge: %d card claim(s)" % len(claimed))


def validate_claimed_cards(cards):
    validated = []
    for (repo, number), expected in _card_index(cards).items():
        if not _card_is_claimed(expected.get("labels") or set()):
            continue
        issue = expected.get("issue")
        try:
            current = render_card.get_card(issue)
            current_matches, _ = _current_claim_matches(expected, current, repo, number)
            if current_matches:
                validated.append(current)
                continue
            current_entry = _card_index([current]).get((repo, number))
            if current_entry and _audit_state_is_protected(
                current_entry.get("state"), issue
            ):
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
    _write_claim_handoff(out_path, validated, "validated claims")
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


def final_auto_merge_guard(expected_card, repo, number, card_token):
    def guard(pr):
        if core.NO_AUTO_MERGE_LABEL in _pr_label_names(pr):
            return (False, "escape hatch label appeared before merging")
        current_card = _read_card_with_card_token(
            expected_card.get("issue"), card_token
        )
        if current_card is None and not str(card_token or "").strip():
            return (False, "default card token is unavailable")
        card_ok, card_reason = _current_claim_matches(
            expected_card, current_card, repo, str(number)
        )
        if not card_ok:
            return (False, "card claim changed: %s" % card_reason)
        return (True, "")

    return guard


def act_merge(
    owner,
    repo,
    number,
    head_sha,
    vision_sha,
    base_sha,
    expected_card,
    card_token,
    repo_cfg,
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
    mc_ok, mc_reason = mergeable_clean(pr)
    if not mc_ok:
        return ("held", "final re-check: %s" % mc_reason, "")
    checks_ok, checks_reason = live_check_status(
        owner, repo, number, head_sha, repo_cfg
    )
    if not checks_ok:
        return ("held", "final re-check: %s" % checks_reason, "")

    message, terminal, merge_commit = apply_decision.do_merge(
        owner,
        repo,
        number,
        head_sha,
        return_merge_commit=True,
        expected_base_sha=base_sha,
        require_clean_merge_state=True,
        auto_merge_guard=final_auto_merge_guard(
            expected_card, repo, number, card_token
        ),
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
    if terminal in ("blocked", "retryable"):
        return ("held", message, "")
    return ("error", message, "")


# --------------------------------------------------------------------------- #
# act CLI (FLEET_TOKEN)
# --------------------------------------------------------------------------- #
def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _audit_record(result, merge_commit="", merged_at="", detail=""):
    return {
        "repo": result["repo"],
        "number": result["number"],
        "card_issue": result["card_issue"],
        "head_sha": result["head_sha"],
        "merge_commit": merge_commit,
        "merged_at": merged_at,
        "contributor": result["audit"].get("contributor", ""),
        "contributor_proof": result["audit"].get("contributor_proof", ""),
        "vision_sha": result["audit"].get("vision_sha", ""),
        "behavior_class": result["audit"].get("behavior_class", ""),
        "behavior_verdict": result["audit"].get("behavior_verdict", {}),
        "gates": result["gates"],
        "detail": detail,
    }


def closed_audit_intent_entries(card_token):
    slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not slug or "/" not in slug:
        raise RuntimeError("cards repository is unavailable for audit recovery")
    cards = _with_card_token(
        card_token,
        lambda: core._flatten_paginated_comments(
            core.gh_rest(
                "repos/%s/issues?state=closed&per_page=100" % slug,
                paginate=True,
                slurp=True,
            )
        ),
    )
    entries = {}
    duplicate_keys = set()
    for card in cards:
        if not isinstance(card, dict) or "pull_request" in card:
            continue
        if render_card.issue_is_open(card):
            continue
        labels = _card_label_names(card)
        state = core.parse_state_block(card.get("body") or "") or {}
        if not _trusted_card_identity(card, state, labels):
            continue
        intent = _audit_intent_record(state, card.get("number"))
        if not intent:
            continue
        key = (str(state.get("repo") or ""), str(state.get("number") or ""))
        if key in entries:
            duplicate_keys.add(key)
            continue
        entries[key] = {
            "issue": card.get("number"),
            "state": state,
            "labels": labels,
        }
    for key in duplicate_keys:
        entries.pop(key, None)
    return entries


def _closed_intent_audit_record(intent, pr):
    merged_head = str(((pr or {}).get("head") or {}).get("sha") or "")
    if merged_head != str(intent.get("head_sha") or ""):
        return None
    merge_commit = str((pr or {}).get("merge_commit_sha") or "")
    if not _GIT_OBJECT_ID_RE.fullmatch(merge_commit):
        return None
    record = dict(intent)
    record["merge_commit"] = merge_commit
    record["merged_at"] = str((pr or {}).get("merged_at") or _now())
    record["detail"] = "ledger backfilled from a closed-card audit intent"
    record["_closed_intent_recovery"] = True
    return record


def recover_audit_intents(owner, index, closed_intents=None):
    merges = []
    releases = []
    holds = []
    ambiguous = []
    recovered = set()
    entries = [(False, key, entry) for key, entry in index.items()]
    entries.extend((True, key, entry) for key, entry in (closed_intents or {}).items())
    for closed_card, (repo, number), entry in entries:
        intent = _audit_intent_record(entry.get("state"), entry.get("issue"))
        if not intent or (
            not closed_card and not _card_is_claimed(entry.get("labels") or set())
        ):
            continue
        pr = live_pr("%s/%s" % (owner, repo), number)
        if pr is None:
            holds.append(
                {
                    "repo": repo,
                    "number": number,
                    "hold_reason": "audit recovery could not read target PR",
                }
            )
            continue
        if pr.get("merged"):
            if closed_card:
                record = _closed_intent_audit_record(intent, pr)
                if record:
                    merges.append(record)
                    recovered.add((repo, number))
                    continue
                reason = "could not confirm merged target for closed-card audit intent"
                holds.append({"repo": repo, "number": number, "hold_reason": reason})
                ambiguous.append("%s#%s: %s" % (repo, number, reason))
                continue
            reason = "audit outcome is ambiguous after target merge"
            holds.append({"repo": repo, "number": number, "hold_reason": reason})
            ambiguous.append("%s#%s: %s" % (repo, number, reason))
            continue
        if closed_card:
            holds.append(
                {
                    "repo": repo,
                    "number": number,
                    "hold_reason": "closed-card audit intent target is not merged",
                }
            )
            continue
        releases.append({"card_issue": entry["issue"]})
        recovered.add((repo, number))
    return merges, releases, holds, ambiguous, recovered


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
    recovery_discovery_errors = []
    try:
        closed_intents = closed_audit_intent_entries(card_token)
    except Exception as error:
        closed_intents = {}
        recovery_discovery_errors.append(
            "closed-card audit intent discovery failed: %s" % str(error)[:160]
        )
    (
        recovered_merges,
        recovered_releases,
        recovery_holds,
        recovery_ambiguous,
        recovered_keys,
    ) = recover_audit_intents(owner, index, closed_intents)
    ambiguous_outcomes = recovery_discovery_errors + list(recovery_ambiguous)
    merges.extend(recovered_merges)
    releases = list(recovered_releases)
    holds.extend(recovery_holds)
    protected_issues = {
        entry["issue"]
        for entry in index.values()
        if _audit_state_is_protected(entry.get("state"), entry.get("issue"))
    }
    releases.extend(
        {"card_issue": entry["issue"]}
        for entry in index.values()
        if _card_is_claimed(entry.get("labels"))
        and entry["issue"] not in protected_issues
    )
    for item in (scan or {}).get("items") or []:
        if item.get("kind") != "pr-review" or item.get("bucket") != "merge-ready":
            continue
        repo = item["repo"]
        number = str(item["number"])
        if (repo, number) in recovered_keys:
            continue
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
        indeterminate = ((scan.get("repos") or {}).get(repo) or {}).get(
            "indeterminate_pr_numbers"
        ) or []
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
        intent = _audit_record(result)
        try:
            staged_card = stage_audit_intent(card_entry, intent, card_token)
        except Exception as e:
            reason = "could not stage audit intent: %s" % str(e)[:160]
            _warn(repo, number, reason)
            holds.append({"repo": repo, "number": number, "hold_reason": reason})
            continue
        releases = [
            release
            for release in releases
            if release.get("card_issue") != result["card_issue"]
        ]
        try:
            outcome, detail, merge_commit = act_merge(
                owner,
                repo,
                item["number"],
                result["head_sha"],
                result["audit"]["vision_sha"],
                result["base_sha"],
                staged_card,
                card_token,
                repo_cfg,
            )
        except Exception as e:  # noqa: BLE001 - a merge hiccup must not crash
            outcome, detail, merge_commit = (
                "error",
                "act raised: %s" % str(e)[:160],
                "",
            )
        if outcome == "post-merge-error":
            confirmed = live_pr("%s/%s" % (owner, repo), number)
            confirmed_commit = str((confirmed or {}).get("merge_commit_sha") or "")
            if (
                confirmed
                and confirmed.get("merged")
                and _GIT_OBJECT_ID_RE.fullmatch(confirmed_commit)
            ):
                outcome = "merged"
                detail = "%s; merge commit re-read from target" % detail
                merge_commit = confirmed_commit
        if outcome == "merged":
            record = _audit_record(
                result,
                merge_commit=merge_commit,
                merged_at=_now(),
                detail=detail,
            )
            try:
                stage_pending_audit_with_card_token(record, card_token)
            except Exception as e:
                post_merge_errors.append(
                    "%s#%s: could not stage completed audit: %s"
                    % (repo, number, str(e)[:160])
                )
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
                {
                    "repo": repo,
                    "number": number,
                    "hold_reason": "%s: %s" % (outcome, detail),
                }
            )
            if outcome == "post-merge-error":
                post_merge_errors.append("%s#%s: %s" % (repo, number, detail))
            elif outcome == "error":
                ambiguous_outcomes.append("%s#%s: %s" % (repo, number, detail))
            elif outcome == "held":
                releases.append({"card_issue": result["card_issue"]})

    return {
        "generated_at": _now(),
        "owner": owner,
        "merges": merges,
        "holds": holds,
        "releases": releases,
        "post_merge_errors": post_merge_errors,
        "ambiguous_outcomes": ambiguous_outcomes,
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
            "::error::wheelhouse auto-merge could not write results: %s" % str(e)[:160],
            file=sys.stderr,
        )
        raise RuntimeError("could not write auto-merge results") from e
    print(
        "wheelhouse auto-merge: %d merged, %d held"
        % (len(payload["merges"]), len(payload["holds"]))
    )
    audit_errors = list(payload.get("post_merge_errors") or []) + list(
        payload.get("ambiguous_outcomes") or []
    )
    if audit_errors:
        for error in audit_errors:
            print(
                "::error::wheelhouse auto-merge audit handoff failed: %s" % error,
                file=sys.stderr,
            )
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
        str(entry.get(field) or "") for field in ("repo", "number", "head_sha")
    )
    return values if all(values) else None


def render_ledger_body(entries, updated_at=""):
    """Render the ledger issue body: a short human summary of recent merges plus
    the hidden machine-readable marker carrying every stored entry."""
    entries = entries if isinstance(entries, list) else []
    lines = [
        "Automated ledger of Wheelhouse scan-time auto-merges - do not edit by hand.",
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
    lines.append(
        "Wheelhouse never auto-reverts; revert the merge commit above "
        "if this merge was not wanted."
    )
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
        return False
    if not record.get("_audit_finalized"):
        try:
            current = render_card.get_card(card)
            state = core.parse_state_block((current or {}).get("body") or "") or {}
            if _audit_state_is_protected(state, card):
                print(
                    "::warning::wheelhouse auto-merge retained card #%s for audit recovery"
                    % card,
                    file=sys.stderr,
                )
                return False
        except Exception as e:
            print(
                "::warning::wheelhouse auto-merge could not verify card #%s before release: %s"
                % (card, str(e)[:200]),
                file=sys.stderr,
            )
            return False
    try:
        _release_card_claim(card)
        return True
    except Exception as e:
        print(
            "::warning::wheelhouse auto-merge could not release card #%s: %s"
            % (card, str(e)[:200]),
            file=sys.stderr,
        )
        return False


def stage_pending_audit(record):
    card_issue = record.get("card_issue")
    if not card_issue:
        raise RuntimeError("auto-merge audit record has no card issue")
    card = render_card.get_card(card_issue)
    if not card:
        raise RuntimeError("could not read card #%s for pending audit" % card_issue)
    state = core.parse_state_block(card.get("body") or "")
    if not state:
        raise RuntimeError("card #%s has no state for pending audit" % card_issue)
    existing = _pending_audit_record(state, card_issue)
    if existing:
        if _ledger_entry_identity(existing) != _ledger_entry_identity(record):
            raise RuntimeError("card #%s has a different pending audit" % card_issue)
        return existing
    intent = _audit_intent_record(state, card_issue)
    if intent and _ledger_entry_identity(intent) != _ledger_entry_identity(record):
        raise RuntimeError("card #%s has a different audit intent" % card_issue)
    new_state = dict(state)
    new_state.pop(AUDIT_INTENT_FIELD, None)
    new_state[AUDIT_PENDING_FIELD] = record
    render_card._edit_issue_body(
        card_issue,
        render_card._replace_state_block(card.get("body") or "", new_state),
    )
    return record


def stage_pending_audit_with_card_token(record, card_token):
    return _with_card_token(
        card_token,
        lambda: stage_pending_audit(record),
    )


def _closed_intent_recovery(record):
    return isinstance(record, dict) and record.get("_closed_intent_recovery") is True


def clear_closed_audit_intent(record):
    card_issue = record.get("card_issue")
    if not card_issue:
        raise RuntimeError("closed-card audit recovery has no card issue")
    card = render_card.get_card(card_issue)
    if not card or render_card.issue_is_open(card):
        raise RuntimeError(
            "could not read closed card #%s for audit recovery" % card_issue
        )
    state = core.parse_state_block(card.get("body") or "") or {}
    intent = _audit_intent_record(state, card_issue)
    if not intent or _ledger_entry_identity(intent) != _ledger_entry_identity(record):
        raise RuntimeError(
            "closed card #%s no longer has the expected audit intent" % card_issue
        )
    new_state = dict(state)
    new_state.pop(AUDIT_INTENT_FIELD, None)
    render_card._edit_issue_body(
        card_issue,
        render_card._replace_state_block(card.get("body") or "", new_state),
    )


def clear_audit_intent(card_issue):
    card = render_card.get_card(card_issue)
    if not card or not render_card.issue_is_open(card):
        raise RuntimeError(
            "could not read open card #%s to clear audit intent" % card_issue
        )
    state = core.parse_state_block(card.get("body") or "")
    if not state:
        raise RuntimeError("card #%s has no state to clear audit intent" % card_issue)
    if not _audit_intent_record(state, card_issue):
        return False
    if _pending_audit_record(state, card_issue):
        raise RuntimeError("card #%s has a pending audit" % card_issue)
    new_state = dict(state)
    new_state.pop(AUDIT_INTENT_FIELD, None)
    render_card._edit_issue_body(
        card_issue,
        render_card._replace_state_block(card.get("body") or "", new_state),
    )
    return True


def pending_audit_records():
    slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not slug or "/" not in slug:
        return []
    cards = core._flatten_paginated_comments(
        core.gh_rest(
            "repos/%s/issues?state=all&labels=%s&per_page=100"
            % (slug, core.quote(AUTO_MERGE_CLAIM_LABEL)),
            paginate=True,
            slurp=True,
        )
    )
    records = []
    seen = set()
    for card in cards:
        if not isinstance(card, dict) or "pull_request" in card:
            continue
        labels = _card_label_names(card)
        state = core.parse_state_block(card.get("body") or "") or {}
        record = _pending_audit_record(state, card.get("number"))
        if (
            not record
            or not _trusted_card_identity(card, state, labels)
            or AUTO_MERGE_CLAIM_LABEL not in labels
        ):
            continue
        identity = _ledger_entry_identity(record)
        if record and identity and identity not in seen:
            records.append(record)
            seen.add(identity)
    return records


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
    errors = []
    try:
        pending = pending_audit_records()
    except Exception as error:
        pending = []
        errors.append(error)
    protected_cards = {
        record.get("card_issue")
        for record in records
        if isinstance(record, dict) and record.get("card_issue")
    }
    protected_cards.update(
        record.get("card_issue")
        for record in pending
        if isinstance(record, dict) and record.get("card_issue")
    )
    known = {
        _ledger_entry_identity(record)
        for record in pending
        if _ledger_entry_identity(record)
    }
    staged = list(pending)
    for record in records:
        if not isinstance(record, dict):
            errors.append(RuntimeError("invalid auto-merge audit record"))
            continue
        identity = _ledger_entry_identity(record)
        if not identity:
            errors.append(RuntimeError("invalid auto-merge audit identity"))
            continue
        if identity in known:
            continue
        try:
            staged.append(
                record
                if _closed_intent_recovery(record)
                else stage_pending_audit(record)
            )
            known.add(identity)
            protected_cards.add(record.get("card_issue"))
        except Exception as error:
            errors.append(error)
    if not staged and not releases:
        if errors:
            raise RuntimeError("wheelhouse auto-merge audit record failed") from errors[
                0
            ]
        print("wheelhouse auto-merge record: no auto-merges to record")
        return
    ledger_written = not staged
    if staged:
        try:
            append_to_ledger(staged)
            ledger_written = True
        except Exception as error:
            errors.append(error)
    resolved_cards = set()
    if ledger_written:
        for record in staged:
            protected_cards.add(record.get("card_issue"))
            try:
                if _closed_intent_recovery(record):
                    clear_closed_audit_intent(record)
                    continue
                resolve_card(record)
                resolved_cards.add(record.get("card_issue"))
                release_card_claim(dict(record, _audit_finalized=True))
            except Exception as error:
                errors.append(error)
    for record in releases:
        if (
            record.get("card_issue") not in resolved_cards
            and record.get("card_issue") not in protected_cards
        ):
            if handoff_valid:
                try:
                    clear_audit_intent(record.get("card_issue"))
                except Exception as error:
                    errors.append(error)
                    continue
            if release_card_claim(record) is False:
                errors.append(
                    RuntimeError(
                        "could not safely release auto-merge claim #%s"
                        % record.get("card_issue")
                    )
                )
    if errors:
        raise RuntimeError("wheelhouse auto-merge audit record failed") from errors[0]
    print("wheelhouse auto-merge record: recorded %d auto-merge(s)" % len(staged))


# --------------------------------------------------------------------------- #
def _write_json_atomically(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(
        prefix=".automerge-", suffix=".json", dir=directory
    )
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


def _write_claim_handoff(path, cards, name):
    try:
        _write_json_atomically(path, cards)
        return
    except Exception as error:
        release_errors = []
        released = []
        seen = set()
        for card in cards:
            number = card.get("number") if isinstance(card, dict) else None
            if not number or number in seen:
                continue
            seen.add(number)
            try:
                _release_card_claim(number)
                released.append(str(number))
            except Exception as release_error:
                release_errors.append("#%s: %s" % (number, str(release_error)[:120]))
        detail = "could not write %s: %s" % (name, str(error)[:160])
        if released:
            detail += "; released claims on %s" % ", ".join("#%s" % n for n in released)
        if release_errors:
            detail += "; claim release failures: %s" % "; ".join(release_errors)
        print("::error::wheelhouse auto-merge %s" % detail, file=sys.stderr)
        raise RuntimeError("wheelhouse auto-merge %s handoff failed" % name) from error


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
