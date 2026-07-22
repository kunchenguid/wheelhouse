#!/usr/bin/env python3
"""
Wheelhouse - deterministic brain (ported from the local OSS-triage machinery).

Runs inside GitHub Actions. A GraphQL query plus pagination fetches every open
PR/issue with compliance + test status + mergeability, classifies each
deterministically, and emits a worklist of items that need the maintainer's
decision. The scan excludes known owner, configured maintainer, and bot authors
from that worklist while failing open when author metadata is missing. Also
carries the security-gated CI approval (the fork-CI / pwn-request HOLD) and the
scan-time auto-approval of provably-safe fork-CI runs (so only
contributor-authored risky or uncertain ones raise a card, excluded-author
failures log suppressed-card, and verified no-pending runs emit no stale card).
The auto path logs exactly one stderr workflow-command line per CI-approval
candidate it handles, so approvals, no-pending results, approve failures, and
fail-closed verdicts are visible in the scan-backstop run log. Conflicted
PR-review candidates leave the maintainer worklist as needs-rebase, with one
contributor rebase nudge per head SHA.
Approval verifies each awaiting run against the target PR: populated
workflow_run.pull_requests must name that PR, while fork-originated empty
associations must match the PR head SHA and branch.
Every verified current-head pending run is approved, including same-workflow
duplicates that GitHub still exposes as independently actionable.
Incomplete PR, issue, or closing-reference pagination is reported as a warning
and marks the repo result as truncated, so reconcile will not self-heal close
cards from an incomplete view of the repo.

This is the GHA port of `data/triage/triage.py`. What the Actions model
replaces has been dropped: the local single-flight lock (-> Actions
`concurrency`), the lavish board and nudge-ledger (-> issues/labels/comments as
state), per-repo `owner` (-> derived from github.repository_owner).

Usage:
  wheelhouse_core.py scan [repo] [--cards cards.json]  scan configured repos -> JSON worklist; may auto-approve safe fork CI, nudge conflicted PR-review candidates, run stale pending-contributor cleanup, and log outcomes
  wheelhouse_core.py scan-health <scan.json>  update the persisted per-repo consecutive-failure ledger; ::error:: + non-zero exit when a repo is dark past the threshold (uses default GITHUB_TOKEN)
  wheelhouse_core.py approve-ci <repo> <pr>   security-gated fork-CI approval (exit 4 = HOLD)
  wheelhouse_core.py checks <repo>        list distinct check names on a repo's PRs (onboarding)
  wheelhouse_core.py authorized           print true/false: is $SENDER allowed to drive decisions?
  wheelhouse_core.py nl-decisions-enabled print true/false: is nl_decisions on in config?
  wheelhouse_core.py auto-triage-enabled <repo> print true/false for one configured repo (pr-review)
  wheelhouse_core.py auto-triage-issues-enabled <repo> print true/false for one configured repo (issue-triage)
  wheelhouse_core.py thank-on-merge-enabled <repo> print true/false for one configured repo
  wheelhouse_core.py auto-merge-enabled <repo> print true/false: is scan-time auto_merge on for one configured repo?
  wheelhouse_core.py state <field>        print one field of the state block in $ISSUE_BODY
  wheelhouse_core.py repos                list configured repos

Owner is derived from $GITHUB_REPOSITORY_OWNER (or --owner). Cross-repo reads
and fork-CI approvals use the ambient GH_TOKEN (set to FLEET_TOKEN by the
calling workflow step).
"""

import base64
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

try:
    import yaml
except ImportError:  # pragma: no cover - workflows `pip install pyyaml` first
    yaml = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Config search order: repo root, then .github/.
CONFIG_CANDIDATES = [
    os.path.join(ROOT, "wheelhouse.config.yml"),
    os.path.join(ROOT, "wheelhouse.config.yaml"),
    os.path.join(ROOT, ".github", "wheelhouse.config.yml"),
    os.path.join(ROOT, ".github", "wheelhouse.config.yaml"),
]

# Page sizes are kept deliberately small. A single oversized page (100 open PRs,
# each fanning out into deeply nested statusCheckRollup / labels /
# closingIssuesReferences sub-connections) pushes GitHub's GraphQL resolver past
# its complexity/timeout budget and returns HTTP 502/504 - which, without retry,
# blanks the whole repo (ok:false). Small pages resolve well inside budget, and
# the existing cursor pagination (`_page_open_prs` / `_page_open_issues`) plus
# per-page retry (`_gh_graphql_data`) carry the rest reliably. statusCheckRollup
# `contexts` is intentionally left large because silently truncating check
# contexts could hide a failing gate and manufacture a false-green card (the
# card #392 lesson); with the PR page cut to PR_PAGE_SIZE the rollup fan-out is
# already small enough to stay within budget.
PR_PAGE_SIZE = 30
ISSUE_PAGE_SIZE = 50
PR_LABELS_PAGE_SIZE = 20
CLOSING_REFS_PAGE_SIZE = 20
STATUS_CONTEXTS_PAGE_SIZE = 100
ACTION_REQUIRED_RUN_LIMIT = 30

_PR_NODE_FIELDS = """
        number title isDraft updatedAt changedFiles isCrossRepository mergeable
        author { login __typename }
        headRefName headRefOid baseRefName baseRefOid
        headRepository { name owner { login } }
        baseRepository { name owner { login } }
        labels(first:%d){ totalCount pageInfo { hasNextPage } nodes{ name } }
        closingIssuesReferences(first:%d){ totalCount pageInfo { hasNextPage endCursor } nodes{ number } }
        commits(last:1){ nodes{ commit{ statusCheckRollup{
          state
          contexts(first:%d){ nodes{
            __typename
            ... on CheckRun { name conclusion status }
            ... on StatusContext { context state }
          }}
        }}}}
""" % (
    PR_LABELS_PAGE_SIZE,
    CLOSING_REFS_PAGE_SIZE,
    STATUS_CONTEXTS_PAGE_SIZE,
)

GQL = """
query($owner:String!, $name:String!) {
  repository(owner:$owner, name:$name) {
    defaultBranchRef { name }
    pullRequests(states:OPEN, first:%d, orderBy:{field:UPDATED_AT, direction:DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {%s}
    }
    issues(states:OPEN, first:%d, orderBy:{field:UPDATED_AT, direction:DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { number title updatedAt author{login __typename} labels(first:20){nodes{name}} }
    }
  }
}
""" % (
    PR_PAGE_SIZE,
    _PR_NODE_FIELDS,
    ISSUE_PAGE_SIZE,
)

PRS_PAGE_GQL = """
query($owner:String!, $name:String!, $after:String!) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:OPEN, first:%d, after:$after, orderBy:{field:UPDATED_AT, direction:DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {%s}
    }
  }
}
""" % (
    PR_PAGE_SIZE,
    _PR_NODE_FIELDS,
)

ISSUES_PAGE_GQL = (
    """
query($owner:String!, $name:String!, $after:String!) {
  repository(owner:$owner, name:$name) {
    issues(states:OPEN, first:%d, after:$after, orderBy:{field:UPDATED_AT, direction:DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { number title updatedAt author{login __typename} labels(first:20){nodes{name}} }
    }
  }
}
"""
    % ISSUE_PAGE_SIZE
)

CLOSING_REFS_PAGE_GQL = (
    """
query($owner:String!, $name:String!, $number:Int!, $after:String!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      closingIssuesReferences(first:%d, after:$after) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes { number }
      }
    }
  }
}
"""
    % CLOSING_REFS_PAGE_SIZE
)

OPEN_PR_CLOSING_REFS_GQL = """
query($owner:String!, $name:String!, $after:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:OPEN, first:%d, after:$after, orderBy:{field:UPDATED_AT, direction:DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        closingIssuesReferences(first:%d) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes { number }
        }
      }
    }
  }
}
""" % (PR_PAGE_SIZE, CLOSING_REFS_PAGE_SIZE)

PR_MERGEABLE_GQL = """
query($owner:String!, $name:String!, $number:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) { mergeable }
  }
}
"""

PR_USER_CONTENT_EDITS_GQL = """
query($owner:String!, $name:String!, $number:Int!, $after:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      userContentEdits(first:100, after:$after) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes { editedAt editor { login __typename } }
      }
    }
  }
}
"""

# Buckets that need the maintainer's call vs. ones waiting on the contributor.
NEEDS_MAINTAINER = {"merge-ready", "needs-ci-approval", "review-needed"}
# (waiting-on-contributor: needs-reraise, needs-rebase, fix-tests, draft, ci-running)
REBASE_NUDGE_MARKER_PREFIX = "wheelhouse-rebase-nudge"
PENDING_CONTRIBUTOR_LABEL = "wheelhouse:pending-contributor-action"
PENDING_CONTRIBUTOR_KEEP_OPEN_LABEL = "wheelhouse:keep-open"
# Per-PR escape hatch: applying this label to a TARGET PR exempts it from
# scan-time auto-merge (see auto_merge.py). It never affects the manual merge
# path or any other decision - it only forces the auto-merge gate to hold.
NO_AUTO_MERGE_LABEL = "wheelhouse:no-auto-merge"
PENDING_CONTRIBUTOR_MARKER_PREFIX = "wheelhouse-pending-contributor-action"
PENDING_CONTRIBUTOR_REMINDER_PREFIX = "wheelhouse-pending-contributor-reminder"
PENDING_CONTRIBUTOR_CLOSE_PREFIX = "wheelhouse-pending-contributor-close"
PENDING_CONTRIBUTOR_ASK_KINDS_PR = {"request-changes", "needs-rebase"}
PENDING_CONTRIBUTOR_TIMELINE_LIMIT = 1000
_PENDING_CONTRIBUTOR_TARGETS_UNSET = object()

# Decision-card "kind" per PR bucket.
PR_KIND = {
    "merge-ready": "pr-review",
    "review-needed": "pr-review",
    "needs-ci-approval": "ci-approval",
}

PRIORITY = {
    "merge-ready": "med",
    "needs-ci-approval": "med",
    "review-needed": "low",
    "issue-triage": "low",
}

TRIAGE_ATTEMPT_CAP_DEFAULT = 2
TRIAGE_ATTEMPT_CAP_MIN = 1
TRIAGE_ATTEMPT_CAP_MAX = 5
TRIAGE_DAILY_CEILING_DEFAULT = 1200
TRIAGE_DAILY_CEILING_MIN = 1
TRIAGE_DAILY_CEILING_MAX = 2000


# --------------------------------------------------------------------------- #
# config + owner
# --------------------------------------------------------------------------- #
def config_path():
    for p in CONFIG_CANDIDATES:
        if os.path.exists(p):
            return p
    sys.exit("no wheelhouse.config.yml found (looked in repo root and .github/)")


def _invalid_bounded_int(key, fallback, scope="global"):
    print(
        "::error::wheelhouse config: %s %s is invalid; failing closed to %s"
        % (scope, key, fallback),
        file=sys.stderr,
    )
    return fallback


def _bounded_config_int(value, key, minimum, maximum, fallback, scope="global"):
    """Validate one spend-relevant integer without bool/int coercion.

    YAML booleans are Python integers, so the explicit bool rejection is
    load-bearing. Invalid values always return the caller's fail-closed value
    and emit a GitHub Actions error annotation on stderr.
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        return _invalid_bounded_int(key, fallback, scope)
    return value


def _triage_attempt_cap(repo_cfg, global_default):
    """Effective queued-attempt cap for one repository.

    The global and per-repository values both use the approved [1, 5] range.
    A malformed override fails closed to one rather than falling back to a
    potentially larger global value.
    """
    if "triage_attempt_cap_per_revision" not in (repo_cfg or {}):
        return _bounded_config_int(
            global_default,
            "triage_attempt_cap_per_revision",
            TRIAGE_ATTEMPT_CAP_MIN,
            TRIAGE_ATTEMPT_CAP_MAX,
            1,
        )
    name = str((repo_cfg or {}).get("name") or "?")
    return _bounded_config_int(
        repo_cfg.get("triage_attempt_cap_per_revision"),
        "triage_attempt_cap_per_revision",
        TRIAGE_ATTEMPT_CAP_MIN,
        TRIAGE_ATTEMPT_CAP_MAX,
        1,
        scope="repo %s" % name,
    )


def load_config():
    if yaml is None:
        sys.exit("PyYAML is required (pip install pyyaml)")
    with open(config_path()) as f:
        cfg = yaml.safe_load(f) or {}
    repos = cfg.get("repos") or []
    global_attempt_cap = (
        TRIAGE_ATTEMPT_CAP_DEFAULT
        if "triage_attempt_cap_per_revision" not in cfg
        else _bounded_config_int(
            cfg.get("triage_attempt_cap_per_revision"),
            "triage_attempt_cap_per_revision",
            TRIAGE_ATTEMPT_CAP_MIN,
            TRIAGE_ATTEMPT_CAP_MAX,
            1,
        )
    )
    daily_ceiling = (
        TRIAGE_DAILY_CEILING_DEFAULT
        if "triage_daily_ceiling" not in cfg
        else _bounded_config_int(
            cfg.get("triage_daily_ceiling"),
            "triage_daily_ceiling",
            TRIAGE_DAILY_CEILING_MIN,
            TRIAGE_DAILY_CEILING_MAX,
            0,
        )
    )
    by_name = {}
    triage_attempt_caps = {}
    for r in repos:
        if isinstance(r, dict) and r.get("name"):
            triage_attempt_caps[r["name"]] = _triage_attempt_cap(r, global_attempt_cap)
            if "triage_daily_ceiling" in r:
                # The daily budget is one fleet-wide ledger. A repository-level
                # value would falsely imply an independent budget and is
                # therefore an invalid ceiling configuration, not an override.
                daily_ceiling = _invalid_bounded_int(
                    "triage_daily_ceiling",
                    0,
                    scope="repo %s (per-repo overrides are not supported)"
                    % r.get("name"),
                )
            # Preserve the repository entry byte-for-byte in the public config
            # mapping. Effective typed caps live in the parallel map so callers
            # that depend on raw repo policy still see exactly what was loaded.
            by_name[r["name"]] = r
    return {
        "repos": by_name,
        "maintainer": (cfg.get("maintainer") or "").strip(),
        "nl_decisions": bool(cfg.get("nl_decisions", False)),
        "card_issues": bool(cfg.get("card_issues", False)),
        # Security-relevant DEFAULT ON (opt-out): when the key is absent a fresh
        # fork still gets scan-time auto-approval of provably-safe fork-CI runs.
        # Set false to restore the click-to-approve-everything behavior.
        "auto_approve_ci": bool(cfg.get("auto_approve_ci", True)),
        # Scan-time auto-merge is DEFAULT OFF (opt-in). A merge is irreversible
        # and higher-stakes than a fork-CI approval, so a repo never auto-merges
        # until the owner opts in globally or per repo AND commits a VISION.md on
        # the target's default branch (see auto_merge.py / AGENTS.md "Auto-merge").
        "auto_merge": cfg.get("auto_merge", False) is True,
        # Advisory LLM triage is DEFAULT ON when the Claude token exists. The
        # flag is only a spend-control opt-out; absence keeps fresh forks useful.
        "auto_triage": bool(cfg.get("auto_triage", True)),
        # Same idea, but for issue-triage cards. Independent of `auto_triage`:
        # either can be toggled off without affecting the other.
        "auto_triage_issues": bool(cfg.get("auto_triage_issues", True)),
        # Spend guards apply to every automatic triage queueing path. The cap
        # is per card-kind source revision and may be lowered/raised per repo;
        # the daily ceiling is one global UTC budget with no repo override.
        "triage_attempt_cap_per_revision": global_attempt_cap,
        "triage_attempt_caps": triage_attempt_caps,
        "triage_daily_ceiling": daily_ceiling,
        # Contributor-etiquette DEFAULT ON: a successful merge posts a short,
        # friendly @-mention thank-you on the contributor's PR. Set false (globally
        # or per-repo) to restore silent merges.
        "thank_on_merge": bool(cfg.get("thank_on_merge", True)),
        # Optional custom wording (an `{author}` placeholder is substituted with
        # the contributor's bare login; include `@{author}` in the template when
        # you want a mention); empty/absent means "use the built-in default".
        # A per-repo `thank_on_merge_message` override takes precedence.
        "thank_on_merge_message": str(cfg.get("thank_on_merge_message") or "").strip(),
        # Stale pending-contributor cleanup is DEFAULT OFF. A fresh fork should
        # never auto-close target PRs until the owner opts in globally or per repo.
        "pending_contributor_cleanup": bool(
            cfg.get("pending_contributor_cleanup", False)
        ),
        "pending_contributor_cleanup_days": cfg.get(
            "pending_contributor_cleanup_days", 14
        ),
        "pending_contributor_reminder_days": cfg.get(
            "pending_contributor_reminder_days", 10
        ),
        "pending_contributor_cleanup_targets": cfg.get(
            "pending_contributor_cleanup_targets", ["pr"]
        ),
    }


def get_owner():
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    if not owner:
        sys.exit("owner not set (GITHUB_REPOSITORY_OWNER missing)")
    return owner


# --------------------------------------------------------------------------- #
# gh wrappers (ambient GH_TOKEN, set per-step by the workflow)
# --------------------------------------------------------------------------- #
# GitHub's GraphQL endpoint intermittently 5xxes on the fleet scan (a heavy
# per-PR query under scheduled-run load). A single un-retried failure blanks the
# whole repo for the scan (ok:false), which - because reconcile refuses to touch
# an ok:false repo - freezes every card for that repo until a later scan happens
# to succeed. So every gh-graphql call goes through `_gh_graphql_data`, which
# retries transient 5xx/timeout failures with exponential backoff + jitter before
# giving up. A genuine (non-transient) error, or exhaustion of the retries, still
# raises exactly as before, so the existing fail-safe semantics (build_repo ->
# ok:false, page helpers -> truncated) are preserved - retry never fabricates
# completeness, it only survives a hiccup.
GRAPHQL_MAX_ATTEMPTS = 4
GRAPHQL_BACKOFF_BASE = 0.5  # seconds; grows 0.5 -> 1 -> 2 (+ jitter)
GRAPHQL_BACKOFF_CAP = 8.0

# gh surfaces transport-level HTTP errors on stderr with a non-zero exit.
_TRANSIENT_STDERR_MARKERS = (
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "bad gateway",
    "gateway timeout",
    "service unavailable",
    "internal server error",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "connection reset",
    "connection refused",
    "was submitted too quickly",  # secondary rate limit - back off and retry
    "eof occurred",
    "tls handshake",
)

# A GraphQL query timeout can also arrive as an HTTP 200 with an `errors` array
# ("Something went wrong while executing your query. This may be the result of a
# timeout..."). That is transient too and worth retrying.
_TRANSIENT_GQL_ERROR_MARKERS = (
    "something went wrong while executing your query",
    "timeout",
    "timed out",
    "please try again",
)

# Indirected so tests can stub sleeping without wall-clock delay.
_sleep = time.sleep


def _text_has_marker(text, markers):
    low = (text or "").lower()
    return any(m in low for m in markers)


def _is_transient_stderr(stderr):
    return _text_has_marker(stderr, _TRANSIENT_STDERR_MARKERS)


def _is_transient_gql_errors(errors):
    try:
        text = json.dumps(errors)
    except (TypeError, ValueError):
        text = str(errors)
    return _text_has_marker(text, _TRANSIENT_GQL_ERROR_MARKERS)


def _graphql_backoff_delay(attempt):
    """Exponential backoff (capped) plus jitter for the `attempt`-th try (1-based)."""
    delay = min(GRAPHQL_BACKOFF_BASE * (2 ** (attempt - 1)), GRAPHQL_BACKOFF_CAP)
    return delay + random.uniform(0.0, GRAPHQL_BACKOFF_BASE)


def _gh_graphql_data(args):
    """Run a `gh api graphql` invocation, returning the parsed `data` dict.

    Retries `GRAPHQL_MAX_ATTEMPTS` times on transient 5xx/timeout failures (bad
    gateway, gateway timeout, service unavailable, connection resets, secondary
    rate limits, and GraphQL query-timeout `errors`) with exponential backoff +
    jitter. A non-transient failure raises immediately; exhausting the retries
    re-raises the last error - callers keep their existing fail-safe behavior."""
    last_err = None
    for attempt in range(1, GRAPHQL_MAX_ATTEMPTS + 1):
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            stderr = r.stderr.strip()
            last_err = RuntimeError(stderr or "gh graphql failed")
            if attempt < GRAPHQL_MAX_ATTEMPTS and _is_transient_stderr(stderr):
                _sleep(_graphql_backoff_delay(attempt))
                continue
            raise last_err
        try:
            data = json.loads(r.stdout)
        except (ValueError, TypeError) as e:
            # A truncated/garbled body can accompany a flaky gateway that still
            # exits 0; treat an unparseable response as transient.
            last_err = RuntimeError(
                "gh graphql returned unparseable output: %s" % (str(e)[:120])
            )
            if attempt < GRAPHQL_MAX_ATTEMPTS:
                _sleep(_graphql_backoff_delay(attempt))
                continue
            raise last_err
        if data.get("errors"):
            last_err = RuntimeError(json.dumps(data["errors"]))
            if attempt < GRAPHQL_MAX_ATTEMPTS and _is_transient_gql_errors(
                data["errors"]
            ):
                _sleep(_graphql_backoff_delay(attempt))
                continue
            raise last_err
        return data
    raise last_err  # pragma: no cover - loop always returns or raises above


def gh_graphql(owner, name):
    data = _gh_graphql_data(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=" + GQL,
            "-f",
            "owner=" + owner,
            "-f",
            "name=" + name,
        ]
    )
    return data["data"]["repository"]


def gh_graphql_pr_page(owner, name, after):
    data = _gh_graphql_data(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=" + PRS_PAGE_GQL,
            "-f",
            "owner=" + owner,
            "-f",
            "name=" + name,
            "-f",
            "after=" + after,
        ]
    )
    return data["data"]["repository"]["pullRequests"]


def gh_graphql_issue_page(owner, name, after):
    data = _gh_graphql_data(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=" + ISSUES_PAGE_GQL,
            "-f",
            "owner=" + owner,
            "-f",
            "name=" + name,
            "-f",
            "after=" + after,
        ]
    )
    return data["data"]["repository"]["issues"]


def gh_graphql_closing_refs_page(owner, name, number, after):
    data = _gh_graphql_data(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=" + CLOSING_REFS_PAGE_GQL,
            "-f",
            "owner=" + owner,
            "-f",
            "name=" + name,
            "-F",
            "number=%s" % number,
            "-f",
            "after=" + after,
        ]
    )
    pr = data["data"]["repository"]["pullRequest"]
    if not pr:
        raise RuntimeError("pull request #%s not found" % number)
    return pr["closingIssuesReferences"]


def gh_graphql_open_pr_closing_refs_page(owner, name, after=None):
    args = [
        "gh",
        "api",
        "graphql",
        "-f",
        "query=" + OPEN_PR_CLOSING_REFS_GQL,
        "-f",
        "owner=" + owner,
        "-f",
        "name=" + name,
    ]
    if after is not None:
        args.extend(["-f", "after=" + after])
    data = _gh_graphql_data(args)
    repository = data["data"]["repository"]
    if not repository:
        raise RuntimeError("repository %s/%s not found" % (owner, name))
    return repository["pullRequests"]


def gh_graphql_pr_mergeable(owner, name, number):
    """Read just `mergeable` for one PR. Fetching a single PR forces GitHub to
    compute mergeability (the bulk list often returns UNKNOWN under load), so this
    is the targeted re-read used to settle a merge-ready candidate."""
    data = _gh_graphql_data(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=" + PR_MERGEABLE_GQL,
            "-f",
            "owner=" + owner,
            "-f",
            "name=" + name,
            "-F",
            "number=%s" % number,
        ]
    )
    pr = data["data"]["repository"]["pullRequest"]
    if not pr:
        raise RuntimeError("pull request #%s not found" % number)
    return pr.get("mergeable")


def gh_graphql_pr_user_content_edits_page(owner, name, number, after=None):
    args = [
        "gh",
        "api",
        "graphql",
        "-f",
        "query=" + PR_USER_CONTENT_EDITS_GQL,
        "-f",
        "owner=" + owner,
        "-f",
        "name=" + name,
        "-F",
        "number=%s" % number,
    ]
    if after is not None:
        args.extend(["-f", "after=" + after])
    data = _gh_graphql_data(args)
    pr = data["data"]["repository"]["pullRequest"]
    if not pr:
        raise RuntimeError("pull request #%s not found" % number)
    return pr["userContentEdits"]


def _page_open_prs(owner, name, first_page):
    nodes = list(first_page.get("nodes") or [])
    page_info = first_page.get("pageInfo") or {}
    if not page_info:
        return nodes, first_page.get("totalCount", len(nodes)) <= len(nodes)
    seen_cursors = set()
    while page_info.get("hasNextPage"):
        cursor = page_info.get("endCursor")
        if not cursor or cursor in seen_cursors:
            raise RuntimeError("PR pagination did not advance")
        seen_cursors.add(cursor)
        page = gh_graphql_pr_page(owner, name, cursor)
        nodes.extend(page.get("nodes") or [])
        page_info = page.get("pageInfo") or {}
        if not page_info:
            return nodes, page.get("totalCount", len(nodes)) <= len(nodes)
    return nodes, True


def _page_open_issues(owner, name, first_page):
    nodes = list(first_page.get("nodes") or [])
    page_info = first_page.get("pageInfo") or {}
    if not page_info:
        return nodes, first_page.get("totalCount", len(nodes)) <= len(nodes)
    seen_cursors = set()
    while page_info.get("hasNextPage"):
        cursor = page_info.get("endCursor")
        if not cursor or cursor in seen_cursors:
            raise RuntimeError("issue pagination did not advance")
        seen_cursors.add(cursor)
        page = gh_graphql_issue_page(owner, name, cursor)
        nodes.extend(page.get("nodes") or [])
        page_info = page.get("pageInfo") or {}
        if not page_info:
            return nodes, page.get("totalCount", len(nodes)) <= len(nodes)
    return nodes, True


def _closing_issue_numbers(owner, name, pr):
    pr_number = pr.get("number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        raise RuntimeError("closing issue PR number is unavailable")
    first_page = pr.get("closingIssuesReferences")
    if not isinstance(first_page, dict):
        raise RuntimeError("closing issue references are unavailable")
    total_count = first_page.get("totalCount")
    page_info = first_page.get("pageInfo")
    first_nodes = first_page.get("nodes")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
        or not isinstance(first_nodes, list)
        or not isinstance(page_info, dict)
        or not isinstance(page_info.get("hasNextPage"), bool)
    ):
        raise RuntimeError("closing issue reference page is incomplete")
    nodes = list(first_nodes)
    seen_cursors = set()
    while page_info.get("hasNextPage"):
        cursor = page_info.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise RuntimeError("closing issue pagination did not advance")
        seen_cursors.add(cursor)
        page = gh_graphql_closing_refs_page(owner, name, pr_number, cursor)
        if not isinstance(page, dict) or page.get("totalCount") != total_count:
            raise RuntimeError("closing issue reference pagination changed shape")
        page_nodes = page.get("nodes")
        page_info = page.get("pageInfo")
        if (
            not isinstance(page_nodes, list)
            or not isinstance(page_info, dict)
            or not isinstance(page_info.get("hasNextPage"), bool)
        ):
            raise RuntimeError("closing issue reference page is incomplete")
        nodes.extend(page_nodes)
    numbers = []
    seen_numbers = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise RuntimeError("closing issue reference node is incomplete")
        number = node.get("number")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or number in seen_numbers
        ):
            raise RuntimeError("closing issue reference node is invalid")
        seen_numbers.add(number)
        numbers.append(number)
    return numbers, len(numbers) == total_count


def _closing_map(prs):
    closing = {}
    for pr in prs:
        for number in pr.get("closes") or []:
            closing.setdefault(number, []).append(pr["number"])
    return closing


def _open_pr_closing_refs(owner, name):
    """Read every open PR and its first page of closing references strictly."""
    first_page = gh_graphql_open_pr_closing_refs_page(owner, name)
    if not isinstance(first_page, dict):
        raise RuntimeError("open PR closing-reference read returned unexpected data")
    total_count = first_page.get("totalCount")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
    ):
        raise RuntimeError("open PR closing-reference total is unavailable")
    first_nodes = first_page.get("nodes")
    if not isinstance(first_nodes, list):
        raise RuntimeError("open PR closing-reference page is incomplete")
    nodes = list(first_nodes)
    page_info = first_page.get("pageInfo")
    if not isinstance(page_info, dict) or not isinstance(
        page_info.get("hasNextPage"), bool
    ):
        raise RuntimeError("open PR closing-reference pagination is unavailable")
    seen_cursors = set()
    while page_info["hasNextPage"]:
        cursor = page_info.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise RuntimeError("open PR closing-reference pagination did not advance")
        seen_cursors.add(cursor)
        page = gh_graphql_open_pr_closing_refs_page(owner, name, cursor)
        if not isinstance(page, dict) or page.get("totalCount") != total_count:
            raise RuntimeError("open PR closing-reference pagination changed shape")
        page_nodes = page.get("nodes")
        next_page_info = page.get("pageInfo")
        if not isinstance(page_nodes, list) or not isinstance(next_page_info, dict):
            raise RuntimeError("open PR closing-reference page is incomplete")
        if not isinstance(next_page_info.get("hasNextPage"), bool):
            raise RuntimeError("open PR closing-reference pagination is incomplete")
        nodes.extend(page_nodes)
        page_info = next_page_info
    if len(nodes) != total_count:
        raise RuntimeError(
            "open PR closing-reference read is incomplete (%d of %d)"
            % (len(nodes), total_count)
        )
    return nodes


def same_closing_issue_overlap(owner, name, number):
    """Return (read_complete, existing overlap note) for one open PR.

    The note is produced by the same `_closing_map` / `_overlap_note` path used
    by the fleet scan. Any malformed, incomplete, duplicated, or raced read is
    reported as incomplete so an acting caller can fail closed.
    """
    try:
        if isinstance(number, bool):
            return (False, "")
        target_number = int(number)
        if target_number < 1 or str(target_number) != str(number):
            return (False, "")
        prs = _open_pr_closing_refs(owner, name)
        enriched = []
        seen_prs = set()
        target_seen = 0
        target_closes = None
        for pr in prs:
            if not isinstance(pr, dict):
                return (False, "")
            pr_number = pr.get("number")
            if (
                isinstance(pr_number, bool)
                or not isinstance(pr_number, int)
                or pr_number < 1
                or pr_number in seen_prs
            ):
                return (False, "")
            seen_prs.add(pr_number)
            closing_refs = pr.get("closingIssuesReferences")
            if not isinstance(closing_refs, dict):
                return (False, "")
            closing_total = closing_refs.get("totalCount")
            closing_nodes = closing_refs.get("nodes")
            closing_page_info = closing_refs.get("pageInfo")
            if (
                isinstance(closing_total, bool)
                or not isinstance(closing_total, int)
                or closing_total < 0
                or not isinstance(closing_nodes, list)
                or not isinstance(closing_page_info, dict)
                or not isinstance(closing_page_info.get("hasNextPage"), bool)
            ):
                return (False, "")
            closes, complete = _closing_issue_numbers(owner, name, pr)
            if (
                not complete
                or not isinstance(closes, list)
                or len(closes) != closing_total
            ):
                return (False, "")
            if any(
                isinstance(issue, bool) or not isinstance(issue, int) or issue < 1
                for issue in closes
            ) or len(set(closes)) != len(closes):
                return (False, "")
            enriched.append({"number": pr_number, "closes": closes})
            if pr_number == target_number:
                target_seen += 1
                target_closes = closes
        if target_seen != 1 or target_closes is None:
            return (False, "")
        closing = _closing_map(enriched)
        return (
            True,
            _overlap_note(target_number, target_closes, closing, set()),
        )
    except Exception:  # noqa: BLE001 - every unreadable shape must fail closed
        return (False, "")


def _dedupe_numbered_nodes(nodes):
    seen = set()
    out = []
    for node in nodes:
        number = node.get("number")
        if number in seen:
            continue
        seen.add(number)
        out.append(node)
    return out


def gh_rest(path, method=None, fields=None, jq=None, paginate=False, slurp=False):
    cmd = ["gh", "api"]
    if method:
        cmd += ["--method", method]
    if paginate:
        cmd += ["--paginate"]
    if slurp:
        cmd += ["--slurp"]
    cmd += [path]
    for k, v in (fields or {}).items():
        cmd += ["-f", "%s=%s" % (k, v)]
    if jq:
        cmd += ["--jq", jq]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("gh api %s failed: %s" % (path, r.stderr.strip()))
    out = r.stdout.strip()
    if not out:
        return None
    if jq:
        return out
    return json.loads(out) if out[:1] in ("{", "[") else out


# --------------------------------------------------------------------------- #
# classification (ported)
# --------------------------------------------------------------------------- #
def _check_run_outcome(conclusion, status):
    """Map one CheckRun to pass/fail/pending/cancelled.

    CANCELLED is a distinct outcome so same-name aggregation can ignore a
    concurrency-cancelled duplicate only when a completed SUCCESS exists for
    that equivalent check on the current head (card #1537). Cancelled-only
    evidence never becomes pass.
    """
    concl = (conclusion or "").upper()
    st = (status or "").upper()
    done = st == "COMPLETED" or st == ""
    if not done:
        return "pending"
    if concl == "SUCCESS":
        return "pass"
    if concl == "CANCELLED":
        return "cancelled"
    if concl in ("FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"):
        return "fail"
    # Unknown/neutral completed conclusions wait rather than invent pass.
    return "pending"


def _status_context_outcome(state):
    st = (state or "").upper()
    if st == "SUCCESS":
        return "pass"
    if st == "PENDING":
        return "pending"
    if st in ("FAILURE", "ERROR"):
        return "fail"
    return "pending"


def _reduce_check_outcomes(outcomes):
    """Reduce outcomes for one equivalent check identity on the current head.

    Order-independent. Substantive fail wins; pending waits; a completed SUCCESS
    makes CANCELLED siblings ignorable; cancelled-only is fail (never pass).
    """
    if not outcomes:
        return "pending"
    if "fail" in outcomes:
        return "fail"
    if "pending" in outcomes:
        return "pending"
    if "pass" in outcomes:
        return "pass"
    if "cancelled" in outcomes:
        return "fail"
    return "pending"


def _rollup_failure_only_ignorable_cancels(context_nodes):
    """True when every non-pass rollup context is a CANCELLED CheckRun whose
    same-name equivalent also has a completed SUCCESS on this head.

    Used to avoid re-poisoning compliance when GitHub's aggregate rollup is
    FAILURE solely from concurrency-cancelled duplicates (card #1537). Any
    unaccounted failure, missing success sibling, pending run, or StatusContext
    non-success keeps the fail-safe backstop active.
    """
    by_name = {}
    for c in context_nodes or []:
        if not isinstance(c, dict):
            return False
        if c.get("__typename") == "CheckRun":
            name = c.get("name") or ""
            by_name.setdefault(name, []).append(
                _check_run_outcome(c.get("conclusion"), c.get("status"))
            )
        elif c.get("__typename") == "StatusContext" or "context" in c:
            # Status contexts have no CANCELLED twin; any non-pass is unaccounted.
            if _status_context_outcome(c.get("state")) != "pass":
                return False
        else:
            return False
    if not by_name:
        return False
    saw_ignorable_cancel = False
    for outcomes in by_name.values():
        reduced = _reduce_check_outcomes(outcomes)
        # Every name-group must reduce to pass (success, or success + cancelled).
        if reduced != "pass":
            return False
        if "pass" not in outcomes:
            return False
        if any(o not in ("pass", "cancelled") for o in outcomes):
            return False
        if "cancelled" in outcomes:
            saw_ignorable_cancel = True
    # Pure all-SUCCESS with rollup FAILURE still fails closed (unaccounted check).
    # Only excuse when at least one CANCELLED sibling was actually observed.
    return saw_ignorable_cancel


def check_status(pr, cfg):
    """Return (compliance, tests, ci_present, names).

    compliance in pass/fail/pending/missing/n/a/none; tests in green/fail/pending/none.

    Matching compliance (and same-name test) contexts aggregate by equivalent
    check identity on the current head: substantive FAILURE/TIMED_OUT/
    ACTION_REQUIRED/STARTUP_FAILURE still fail, pending still waits, and a
    CANCELLED concurrency duplicate is ignored only when a completed SUCCESS
    for that same check name exists. Cancelled-only evidence never becomes
    pass. A GitHub rollup FAILURE/ERROR still clamps an otherwise pass/n/a
    compliance read to fail, except when every non-pass rollup context is an
    ignorable CANCELLED sibling of a proven SUCCESS (card #1537).
    """
    commits = pr["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    if not rollup or not rollup["contexts"]["nodes"]:
        return ("none", "none", False, [])
    comp_name = cfg.get("compliance_check")
    patterns = cfg.get("test_check_patterns", []) or []
    # Group by exact check name / status context so only truly equivalent
    # duplicates (same head, same name) can cancel each other out. Different
    # matrix legs (e.g. ubuntu vs macos) keep distinct names and stay independent.
    comp_by_name = {}
    test_by_name = {}
    names = []
    for c in rollup["contexts"]["nodes"]:
        if c["__typename"] == "CheckRun":
            name = c.get("name") or ""
            names.append(name)
            outcome = _check_run_outcome(c.get("conclusion"), c.get("status"))
            if comp_name and name == comp_name:
                comp_by_name.setdefault(name, []).append(outcome)
            elif any(p in name for p in patterns):
                test_by_name.setdefault(name, []).append(outcome)
        else:  # StatusContext
            ctx = c.get("context") or ""
            names.append(ctx)
            outcome = _status_context_outcome(c.get("state"))
            if comp_name and ctx == comp_name:
                comp_by_name.setdefault(ctx, []).append(outcome)
            elif any(p in ctx for p in patterns):
                test_by_name.setdefault(ctx, []).append(outcome)
    # Reduce each equivalent-name group, then worst-wins across groups.
    # Scalar last-write-wins would silently pick API order (card #392).
    if not comp_name:
        compliance = "n/a"
    elif not comp_by_name:
        compliance = "missing"
    else:
        comp_results = [
            _reduce_check_outcomes(outcomes) for outcomes in comp_by_name.values()
        ]
        if "fail" in comp_results:
            compliance = "fail"
        elif "pending" in comp_results:
            compliance = "pending"
        else:
            compliance = "pass"
    if not test_by_name:
        tstate = "none"
    else:
        test_results = [
            _reduce_check_outcomes(outcomes) for outcomes in test_by_name.values()
        ]
        if "fail" in test_results:
            tstate = "fail"
        elif "pending" in test_results:
            tstate = "pending"
        else:
            tstate = "green"
    # Fail-toward-safe backstop: GitHub's authoritative rollup state is
    # FAILURE/ERROR. Never let compliance come out pass/n/a when an unaccounted
    # failure may exist - deliberately conservative even for untracked checks.
    # Exception: every non-pass context is an ignorable CANCELLED sibling of a
    # proven same-name SUCCESS on this head (concurrency cancel-in-progress).
    if rollup.get("state") in ("FAILURE", "ERROR") and compliance in ("pass", "n/a"):
        if not _rollup_failure_only_ignorable_cancels(rollup["contexts"]["nodes"]):
            compliance = "fail"
    return (compliance, tstate, True, names)


def _repo_identity(repo):
    if not isinstance(repo, dict):
        return None
    owner = repo.get("owner")
    owner_login = owner.get("login") if isinstance(owner, dict) else None
    name = repo.get("name")
    if not owner_login or not name:
        return None
    return (str(owner_login).lower(), str(name).lower())


def _pr_is_cross_repo(pr):
    """Return True/False when the PR source repo is known, else None.

    Fork-CI approval is meaningful only for cross-repo PRs. Missing/deleted head
    repository metadata is treated as unknown so the scan can fail safe instead
    of silently closing or approving the wrong target.
    """
    direct = pr.get("isCrossRepository")
    if isinstance(direct, bool):
        return direct
    head = _repo_identity(pr.get("headRepository"))
    base = _repo_identity(pr.get("baseRepository"))
    if head is None or base is None:
        return None
    return head != base


def _mergeable_is_conflicting(mergeable):
    return str(mergeable or "").strip().upper() == "CONFLICTING"


def _mergeable_is_mergeable(mergeable):
    return str(mergeable or "").strip().upper() == "MERGEABLE"


def _mergeable_is_unknown(mergeable):
    """GitHub's GraphQL `mergeable` is a non-null enum on a valid response:
    MERGEABLE / CONFLICTING / UNKNOWN. UNKNOWN is the EXPECTED PENDING STATE
    (GitHub is still computing it, typically right after a base push) - never an
    answer. A missing/None value is not something the bulk list produces for an
    open PR, so it is left to classify's existing fail-open rather than triggering
    a poll."""
    return str(mergeable or "").strip().upper() == "UNKNOWN"


def _mergeable_is_conclusive(mergeable):
    """Only MERGEABLE and CONFLICTING are authoritative answers."""
    return _mergeable_is_mergeable(mergeable) or _mergeable_is_conflicting(mergeable)


# Sentinel bucket for a PR whose mergeability could not be settled this scan.
# It is deliberately NOT in NEEDS_MAINTAINER, PR_KIND, or PRIORITY, and is NOT
# `needs-rebase`, so build_repo emits no worklist item and no rebase nudge for
# it: an UNKNOWN reading must never create, close, consume, or nudge. The PR is
# frozen (its number is reported in `indeterminate_pr_numbers`) until a later
# scan reads a conclusive value.
MERGEABILITY_PENDING = "mergeability-pending"

# UNKNOWN-mergeable poll budget. GitHub computes PR mergeability LAZILY: a push
# to the base branch invalidates every open PR's cached mergeability to UNKNOWN,
# and NOTHING recomputes it until the PR is queried - the first query returns
# UNKNOWN and merely TRIGGERS the async compute, which settles to
# MERGEABLE/CONFLICTING within seconds-to-a-minute (confirmed live: a repo went
# 32 UNKNOWN -> 0 within ~2 min of being queried; matches GitHub's documented
# REST "mergeable is null until computed, poll until non-null" contract). So a
# single-PR re-read with short backoff both TRIGGERS and then CATCHES the settled
# value. If the scan is the only regular requester, an un-polled UNKNOWN would
# make a statically-conflicting PR flip in/out of the worklist once per base push
# (the lavish-axi#111 duplicate-card oscillation).
MERGEABLE_SETTLE_READS = 4
MERGEABLE_SETTLE_BASE = 1.5  # seconds; backoff grows 1.5 -> 3 -> 6 (capped)
MERGEABLE_SETTLE_CAP = 6.0
_MERGEABLE_SETTLEMENT_UNSET = object()


def _settle_mergeables(owner, name, numbers):
    values = {number: None for number in dict.fromkeys(numbers)}
    errors = {}
    pending = list(values)
    for i in range(MERGEABLE_SETTLE_READS):
        next_round = []
        for number in pending:
            try:
                value = gh_graphql_pr_mergeable(owner, name, number)
            except Exception as e:
                errors[number] = str(e)
                next_round.append(number)
                continue
            values[number] = value
            if _mergeable_is_conclusive(value):
                errors.pop(number, None)
                continue
            next_round.append(number)
        pending = next_round
        if pending and i + 1 < MERGEABLE_SETTLE_READS:
            _sleep(min(MERGEABLE_SETTLE_BASE * (2**i), MERGEABLE_SETTLE_CAP))
    return values, errors


def _settle_mergeable(owner, name, number):
    values, _ = _settle_mergeables(owner, name, [number])
    return values[number]


def _settlement_failure_result(
    slug,
    name,
    prs,
    issues,
    numbers,
    settled_mergeables,
    settlement_errors,
    truncated,
    indeterminate_numbers=(),
):
    if not settlement_errors:
        return None
    indeterminate = set(indeterminate_numbers)
    indeterminate.update(
        number
        for number in numbers
        if not _mergeable_is_conclusive(settled_mergeables.get(number))
    )
    failed_numbers = sorted(settlement_errors)
    failed_reason = settlement_errors[failed_numbers[0]]
    return {
        "name": name,
        "ok": False,
        "warning": (
            "%s scan failed: mergeability settlement query failed for "
            "PR(s) %s: %s"
            % (
                slug,
                ", ".join("#%s" % number for number in failed_numbers),
                failed_reason[:160],
            )
        ),
        "open_pr_numbers": [pr["number"] for pr in prs],
        "open_issue_numbers": [it["number"] for it in issues],
        "indeterminate_pr_numbers": sorted(indeterminate),
        "truncated": truncated,
    }


def _resolve_pr_bucket(
    owner,
    name,
    pr,
    draft,
    comp,
    tests,
    ci,
    cross_repo,
    settled_mergeable=_MERGEABLE_SETTLEMENT_UNSET,
):
    """Classify a PR, treating an UNKNOWN mergeable as an expected pending state.

    `classify` fails open to a worklist bucket when mergeability is UNKNOWN or
    missing. Left alone, that lets a statically-conflicting PR whose mergeability
    was just invalidated by a base push (see MERGEABLE_SETTLE_READS) read as
    `merge-ready` or `review-needed` and flip INTO the worklist for one scan -
    minting a duplicate card that the next scan (once GitHub settles it to
    CONFLICTING) soft-closes again. So when the fast path lands on either bucket
    and mergeability is not authoritatively MERGEABLE, poll the single PR to
    settle it, then re-classify: CONFLICTING -> `needs-rebase` (out, nudged), a
    non-conflicting value -> the original worklist bucket (in). If it still can't
    be settled within the budget, return `MERGEABILITY_PENDING` so
    build_repo FREEZES the PR: an UNKNOWN reading must never flip worklist
    membership (in stays in, out stays out; no card created/closed/consumed)."""
    mergeable = pr.get("mergeable")
    bucket = classify(draft, comp, tests, ci, cross_repo, mergeable)
    # UNKNOWN threatens membership for every worklist bucket that a conflicting
    # value would rewrite to needs-rebase. Only an explicit UNKNOWN is polled - a
    # missing value keeps classify's fail-open.
    if bucket not in ("merge-ready", "review-needed") or not _mergeable_is_unknown(
        mergeable
    ):
        return bucket
    settled = (
        _settle_mergeable(owner, name, pr["number"])
        if settled_mergeable is _MERGEABLE_SETTLEMENT_UNSET
        else settled_mergeable
    )
    if _mergeable_is_conclusive(settled):
        return classify(draft, comp, tests, ci, cross_repo, settled)
    return MERGEABILITY_PENDING


def _with_mergeability(bucket, mergeable):
    if bucket in ("merge-ready", "review-needed") and _mergeable_is_conflicting(
        mergeable
    ):
        return "needs-rebase"
    return bucket


def classify(draft, comp, tests, ci, cross_repo=True, mergeable=None):
    """Return the PR routing bucket.

    Only an authoritative GraphQL `mergeable=CONFLICTING` rewrites PR-review
    buckets (`merge-ready` / `review-needed`) to waiting-on-contributor
    `needs-rebase`. UNKNOWN or missing mergeability fails open, and fork
    `needs-ci-approval` routing is independent of mergeability.
    """
    if draft:
        return "draft"
    if not ci:
        if cross_repo is False:
            return _with_mergeability("review-needed", mergeable)
        return "needs-ci-approval"
    if comp == "fail":
        return "needs-reraise"
    if comp == "pending":
        return "ci-running"
    if comp in ("pass", "n/a"):
        if tests == "green":
            return _with_mergeability("merge-ready", mergeable)
        if tests == "fail":
            return "fix-tests"
        if tests == "pending":
            return "ci-running"
        if tests == "none":
            return _with_mergeability(
                "review-needed", mergeable
            )  # compliant but no test signal - look before trusting
    return _with_mergeability(
        "review-needed", mergeable
    )  # comp missing-but-ci-present, or anything unmodeled


def config_warning(repo, comp, names):
    """Catch the most dangerous misconfig: a gate-like check exists but
    compliance_check is unset/wrong, which would silently show non-compliant
    PRs as merge-ready."""
    if comp and comp not in names:
        return (
            "compliance_check %r not seen in any PR check on %s - misconfigured? "
            "(run: checks %s)" % (comp, repo, repo)
        )
    if not comp:
        # Generic, owner-agnostic gate-like check name heuristics.
        gate_terms = (
            "must be raised",
            "policy",
            "dco",
            "cla",
            "sign-off",
            "signoff",
            "contribut",
            "compliance",
            "required",
        )
        gateish = [n for n in names if any(t in n.lower() for t in gate_terms)]
        if gateish:
            return (
                "no compliance_check set on %s but a gate-like check exists (%r) - "
                "non-compliant PRs may show as merge-ready" % (repo, gateish[0])
            )
    return None


# --------------------------------------------------------------------------- #
# worklist item rendering helpers
# --------------------------------------------------------------------------- #
def _overlap_note(number, closes, dup_clusters, addressed):
    notes = []
    for issue in closes:
        sibs = dup_clusters.get(issue)
        if sibs and len(sibs) > 1:
            others = [n for n in sibs if n != number]
            if others:
                notes.append(
                    "overlaps PR(s) %s (all close issue #%d)"
                    % (", ".join("#%d" % n for n in sorted(others)), issue)
                )
    return "; ".join(notes)


def _recommendation(bucket):
    return {
        "merge-ready": "Merge - compliance and tests are green.",
        "review-needed": "Review before merge - compliant but the test signal is missing/unclear.",
        "needs-ci-approval": "Approve CI to get a test signal (security-gated; held automatically if the PR touches CI/action files).",
        "issue-triage": "Triage - open issue with no linked PR yet.",
    }.get(bucket, "Needs your call.")


def _auto_approve_enabled(repo_cfg, global_default):
    """Effective auto_approve_ci for one repo: the per-repo `auto_approve_ci`
    override if set, else the global flag (which itself defaults to True). A
    cheap, portable escape hatch - a single repo can opt out without flipping the
    fleet-wide default."""
    v = repo_cfg.get("auto_approve_ci")
    return global_default if v is None else bool(v)


def _auto_merge_enabled(repo_cfg, global_default):
    """Effective auto_merge for one repo: the per-repo `auto_merge` override if
    set, else the global flag.

    Unlike auto_approve_ci this is DEFAULT OFF in code - a merge is irreversible,
    so a repo must opt in globally or with a per-repo override (and additionally
    commit a VISION.md on the target's default branch) before scan-time
    auto-merge can act. A per-repo `auto_merge: false` is the portable one-repo
    kill switch even when the fleet-wide default is on."""
    v = repo_cfg.get("auto_merge")
    return global_default is True if v is None else v is True


def _default_branch_vision_sha(slug):
    try:
        data = gh_rest("/repos/%s/contents/VISION.md" % slug)
    except RuntimeError:
        return ""
    sha = data.get("sha") if isinstance(data, dict) else ""
    return str(sha or "") if isinstance(sha, str) else ""


def _auto_triage_enabled(repo_cfg, global_default):
    """Effective auto_triage for one repo, mirroring auto_approve_ci.

    Default ON keeps a fresh fork useful once CLAUDE_CODE_OAUTH_TOKEN is present;
    the global or per-repo false value is a token-spend opt-out. This governs
    pr-review cards ONLY - see `_auto_triage_issues_enabled` for issue-triage.
    """
    v = repo_cfg.get("auto_triage")
    return global_default if v is None else bool(v)


def _auto_triage_issues_enabled(repo_cfg, global_default):
    """Effective auto_triage_issues for one repo, mirroring _auto_triage_enabled.

    Independent of `auto_triage`: this governs issue-triage cards ONLY, so
    toggling either flag (globally or per-repo) never affects the other.
    """
    v = repo_cfg.get("auto_triage_issues")
    return global_default if v is None else bool(v)


def _thank_on_merge_enabled(repo_cfg, global_default):
    """Effective thank_on_merge for one repo, mirroring auto_approve_ci.

    Default ON is the intended fresh-fork etiquette; the global or per-repo
    false value opts out of the post-merge thank-you comment entirely."""
    v = repo_cfg.get("thank_on_merge")
    return global_default if v is None else bool(v)


def _thank_on_merge_message(repo_cfg, global_message):
    """Effective thank_on_merge_message for one repo: the per-repo override if
    it is a non-blank string, else the global config message (which may itself
    be blank - the caller falls back to the built-in default in that case)."""
    v = repo_cfg.get("thank_on_merge_message")
    if isinstance(v, str) and v.strip():
        return v.strip()
    return global_message


def _pending_contributor_cleanup_enabled(repo_cfg, global_default):
    """Effective pending_contributor_cleanup for one repo.

    Unlike auto triage and thank-on-merge, this is default OFF in code. A repo
    must opt in globally or with a per-repo override before scan-time cleanup can
    remind or close a target PR.
    """
    v = repo_cfg.get("pending_contributor_cleanup")
    return global_default if v is None else bool(v)


def _pending_contributor_cleanup_days(repo_cfg, global_default):
    v = repo_cfg.get("pending_contributor_cleanup_days")
    try:
        n = int(global_default if v is None else v)
    except (TypeError, ValueError):
        n = 14
    return max(1, n)


def _pending_contributor_reminder_days(repo_cfg, global_default):
    v = repo_cfg.get("pending_contributor_reminder_days")
    try:
        n = int(global_default if v is None else v)
    except (TypeError, ValueError):
        n = 10
    return max(1, n)


def _pending_contributor_cleanup_targets(repo_cfg, global_default):
    raw = (
        repo_cfg.get("pending_contributor_cleanup_targets")
        if "pending_contributor_cleanup_targets" in repo_cfg
        else global_default
    )
    if raw is None:
        return set()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(x).strip().casefold() for x in raw if str(x).strip()}


def _author_login(author):
    if not isinstance(author, dict):
        return ""
    return str(author.get("login") or "").strip()


def _author_typename(author):
    if not isinstance(author, dict):
        return ""
    return str(author.get("__typename") or author.get("type") or "").strip()


def _author_is_bot(author):
    typename = _author_typename(author)
    login = _author_login(author)
    return typename.casefold() == "bot" or login.casefold().endswith("[bot]")


def _author_excluded_from_queue(author, maintainer_logins):
    """Return true only when authorship is known to be owner/maintainer or bot.

    Missing author metadata fails open so a real contributor is not silently
    dropped from the maintainer's worklist.
    """
    if _author_is_bot(author):
        return True
    login = _author_login(author)
    return bool(login and login.casefold() in maintainer_logins)


def _display_list(values, limit=10):
    items = [str(v) for v in (values or [])]
    if len(items) <= limit:
        return ", ".join(items)
    return "%s (+%d more; %d total)" % (
        ", ".join(items[:limit]),
        len(items) - limit,
        len(items),
    )


def _workflow_command_text(value):
    return re.sub(r"[\r\n]+", " ", str(value))


def _rebase_nudge_marker(head_sha):
    return "<!-- %s:%s -->" % (
        REBASE_NUDGE_MARKER_PREFIX,
        str(head_sha or "").strip(),
    )


def _flatten_paginated_comments(data):
    if not isinstance(data, list):
        return []
    if data and all(isinstance(page, list) for page in data):
        comments = []
        for page in data:
            comments.extend(page)
        return comments
    return data


def _trusted_ask_author(author, maintainer_logins):
    login = _author_login(author).casefold()
    trusted = {str(item).casefold() for item in (maintainer_logins or [])}
    return bool(login and login in trusted)


def _has_rebase_nudge(comments, head_sha, maintainer_logins):
    marker = _rebase_nudge_marker(head_sha)
    for comment in _flatten_paginated_comments(comments):
        if not isinstance(comment, dict):
            continue
        if marker in str(comment.get("body") or "") and _trusted_ask_author(
            _event_author(comment), maintainer_logins
        ):
            return True
    return False


_PENDING_CONTRIBUTOR_RE = re.compile(
    r"<!--\s*%s:\s*(\{.*?\})\s*-->" % re.escape(PENDING_CONTRIBUTOR_MARKER_PREFIX),
    re.S,
)
_PENDING_CONTRIBUTOR_REMINDER_RE = re.compile(
    r"<!--\s*%s:\s*(\{.*?\})\s*-->" % re.escape(PENDING_CONTRIBUTOR_REMINDER_PREFIX),
    re.S,
)
_PENDING_CONTRIBUTOR_CLOSE_RE = re.compile(
    r"<!--\s*%s:\s*(\{.*?\})\s*-->" % re.escape(PENDING_CONTRIBUTOR_CLOSE_PREFIX),
    re.S,
)
_REBASE_NUDGE_RE = re.compile(
    r"<!--\s*%s\s*:\s*([^>]*)-->" % re.escape(REBASE_NUDGE_MARKER_PREFIX)
)


def _parse_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_time(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pending_contributor_marker(record):
    return "<!-- %s: %s -->" % (
        PENDING_CONTRIBUTOR_MARKER_PREFIX,
        json.dumps(record, separators=(",", ":"), sort_keys=True),
    )


def _pending_contributor_reminder_marker(ask_id, reminded_at):
    payload = {"version": 1, "ask_id": ask_id, "reminded_at": reminded_at}
    return "<!-- %s: %s -->" % (
        PENDING_CONTRIBUTOR_REMINDER_PREFIX,
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
    )


def _pending_contributor_close_marker(ask_id, closed_at):
    payload = {"version": 1, "ask_id": ask_id, "closed_at": closed_at}
    return "<!-- %s: %s -->" % (
        PENDING_CONTRIBUTOR_CLOSE_PREFIX,
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
    )


def _pending_record(
    repo,
    number,
    ask_kind,
    asked_at,
    head_sha,
    target_author,
    asked_by,
    source_id,
):
    if ask_kind not in PENDING_CONTRIBUTOR_ASK_KINDS_PR:
        raise RuntimeError("unknown pending contributor ask kind: %s" % ask_kind)
    asked_dt = _parse_time(asked_at)
    if asked_dt is None:
        raise RuntimeError("pending contributor ask has no provable timestamp")
    if not str(head_sha or "").strip():
        raise RuntimeError("pending contributor ask has no provable head SHA")
    if not str(source_id or "").strip():
        raise RuntimeError("pending contributor ask has no source id")
    source = str(source_id).strip()
    ask_id = "%s:%s:%s" % (ask_kind, str(head_sha).strip(), source)
    return {
        "version": 1,
        "ask_id": ask_id,
        "ask_kind": ask_kind,
        "asked_at": _format_time(asked_dt),
        "asked_by": str(asked_by or "").strip(),
        "target_author": str(target_author or "").strip(),
        "head_sha": str(head_sha or "").strip(),
        "repo": str(repo or "").strip(),
        "number": int(number),
        "source_id": source,
        "reminded_at": None,
    }


def _ensure_repo_label(slug, name, color="fbca04", description="Managed by Wheelhouse"):
    try:
        gh_rest(
            "/repos/%s/labels" % slug,
            method="POST",
            fields={"name": name, "color": color, "description": description},
        )
    except RuntimeError as e:
        msg = str(e).lower()
        if (
            "already_exists" not in msg
            and "already exists" not in msg
            and "422" not in msg
        ):
            raise


def _add_target_label(slug, number, label):
    _ensure_repo_label(slug, label)
    gh_rest(
        "/repos/%s/issues/%s/labels" % (slug, number),
        method="POST",
        fields={"labels[]": label},
    )


def _remove_target_label(slug, number, label):
    try:
        gh_rest(
            "/repos/%s/issues/%s/labels/%s" % (slug, number, quote(label, safe="")),
            method="DELETE",
        )
    except RuntimeError as e:
        msg = str(e).lower()
        if "404" not in msg and "not found" not in msg:
            raise


def arm_pending_contributor_action(
    owner,
    repo,
    number,
    ask_kind,
    asked_at,
    head_sha,
    target_author,
    asked_by=None,
    source_id=None,
):
    """Persist a deterministic target-side pending-contributor ask.

    This writes a hidden marker comment plus the active target label. Callers use
    it only after a provable ask was created; failures raise so the caller can
    report that cleanup was not armed without undoing the original ask.
    """
    slug = "%s/%s" % (owner, repo)
    record = _pending_record(
        repo,
        number,
        ask_kind,
        asked_at,
        head_sha,
        target_author,
        asked_by or owner,
        source_id,
    )
    gh_rest(
        "/repos/%s/issues/%s/comments" % (slug, number),
        method="POST",
        fields={"body": _pending_contributor_marker(record)},
    )
    _add_target_label(slug, number, PENDING_CONTRIBUTOR_LABEL)
    return record


def _rebase_nudge_body(repo, number, head_sha):
    marker = _rebase_nudge_marker(head_sha)
    short = str(head_sha or "").strip()[:8] or "current head"
    # Contributor-facing only: plain language, no product name, no queue jargon.
    # Visible automation disclosure is required - FLEET_TOKEN posts as a human
    # login, so without it the nudge reads as a personal maintainer message.
    # See AGENTS.md "Contributor-facing copy". Mechanics (hidden marker below)
    # stay load-bearing for fire-once-per-head-SHA idempotence.
    return (
        "Automated reminder: thanks for the PR! This branch currently has a "
        "merge conflict with the base branch.\n\n"
        "When you get a chance, please rebase onto (or merge) the latest base "
        "branch, resolve the conflict, and push. After that, checks will "
        "re-run and the PR will get looked at again.\n\n"
        "<sub>Noted for %s#%s at `%s`.</sub>\n"
        "%s" % (repo, number, short, marker)
    )


def _patch_comment_body(slug, comment_id, body):
    gh_rest(
        "/repos/%s/issues/comments/%s" % (slug, comment_id),
        method="PATCH",
        fields={"body": body},
    )


def _post_rebase_nudge_if_needed(
    slug, repo, number, head_sha, maintainer_logins, arm_cleanup=False
):
    comments = gh_rest(
        "/repos/%s/issues/%s/comments?per_page=100" % (slug, number),
        paginate=True,
        slurp=True,
    )
    if _has_rebase_nudge(comments, head_sha, maintainer_logins):
        return False
    body = _rebase_nudge_body(repo, number, head_sha)
    posted = gh_rest(
        "/repos/%s/issues/%s/comments" % (slug, number),
        method="POST",
        fields={"body": body},
    )
    comment_id = (posted or {}).get("id") if isinstance(posted, dict) else None
    created_at = (posted or {}).get("created_at") if isinstance(posted, dict) else None
    if arm_cleanup and comment_id and created_at:
        record = _pending_record(
            repo,
            number,
            "needs-rebase",
            created_at,
            head_sha,
            "",
            "",
            comment_id,
        )
        _patch_comment_body(
            slug,
            comment_id,
            body + "\n" + _pending_contributor_marker(record),
        )
        _add_target_label(slug, number, PENDING_CONTRIBUTOR_LABEL)
    elif arm_cleanup:
        print(
            "::warning::wheelhouse rebase-nudge could not arm stale cleanup %s#%s: missing comment timestamp"
            % (repo, number),
            file=sys.stderr,
        )
    return True


def _maybe_nudge_rebase(slug, repo, pr, maintainer_logins, arm_cleanup=False):
    try:
        posted = _post_rebase_nudge_if_needed(
            slug,
            repo,
            pr["number"],
            pr.get("head_sha"),
            maintainer_logins,
            arm_cleanup=arm_cleanup,
        )
    except Exception as e:
        print(
            "::warning::wheelhouse rebase-nudge failed %s#%s: %s"
            % (repo, pr["number"], _workflow_command_text(str(e)[:160])),
            file=sys.stderr,
        )
        return
    if posted:
        print(
            "::notice::wheelhouse rebase-nudge posted %s#%s for %s"
            % (
                repo,
                pr["number"],
                _workflow_command_text(str(pr.get("head_sha") or "")[:12]),
            ),
            file=sys.stderr,
        )


def _label_names_from_nodes(nodes):
    names = []
    for label in nodes or []:
        if isinstance(label, dict) and label.get("name"):
            names.append(str(label["name"]))
        elif isinstance(label, str):
            names.append(label)
    return names


def _label_names_from_issue(issue):
    return _label_names_from_nodes((issue or {}).get("labels") or [])


def _label_connection_truncated(labels):
    if not isinstance(labels, dict):
        return False
    nodes = labels.get("nodes") or []
    try:
        total = int(labels.get("totalCount"))
    except (TypeError, ValueError):
        total = None
    if total is not None:
        return total > len(nodes)
    return bool((labels.get("pageInfo") or {}).get("hasNextPage"))


def _is_non_maintainer_human(author, maintainer_logins):
    if not isinstance(author, dict):
        return None
    if _author_is_bot(author):
        return False
    login = _author_login(author)
    if not login:
        return None
    return login.casefold() not in maintainer_logins


def _flatten_pages(data):
    return _flatten_paginated_comments(data)


def _read_paginated_list(path):
    data = gh_rest(path, paginate=True, slurp=True)
    values = _flatten_pages(data)
    if not isinstance(values, list):
        raise RuntimeError("paginated endpoint returned unexpected data")
    if len(values) >= PENDING_CONTRIBUTOR_TIMELINE_LIMIT:
        raise RuntimeError("paginated endpoint reached safety limit")
    return values


def _read_pr_user_content_edits(slug, number):
    owner, name = slug.split("/", 1)
    values = []
    cursor = None
    seen_cursors = set()
    while True:
        page = gh_graphql_pr_user_content_edits_page(owner, name, number, cursor)
        if not isinstance(page, dict):
            raise RuntimeError("PR edit history returned unexpected data")
        nodes = page.get("nodes") or []
        if not isinstance(nodes, list):
            raise RuntimeError("PR edit history returned unexpected nodes")
        values.extend(nodes)
        if len(values) >= PENDING_CONTRIBUTOR_TIMELINE_LIMIT:
            raise RuntimeError("PR edit history reached safety limit")
        page_info = page.get("pageInfo")
        if not isinstance(page_info, dict):
            raise RuntimeError("PR edit history pagination missing")
        if not page_info.get("hasNextPage"):
            try:
                total = int(page.get("totalCount", len(values)))
            except (TypeError, ValueError):
                raise RuntimeError("PR edit history total missing")
            if total > len(values):
                raise RuntimeError("PR edit history incomplete")
            return values
        cursor = page_info.get("endCursor")
        if not cursor or cursor in seen_cursors:
            raise RuntimeError("PR edit history pagination did not advance")
        seen_cursors.add(cursor)


def _authoritative_review_submitted_at(slug, number, review_id):
    """Re-read one PR review by id to recover its authoritative `submitted_at`.

    The list endpoints occasionally omit a usable timestamp on a review (and on
    the `reviewed` timeline event that mirrors it). Re-reading the single review
    by id is the same documented fallback `apply_decision.do_request_changes`
    already uses when arming. Returns the recovered ISO timestamp, or None so the
    caller fails open rather than crashing the whole scan on one missing field.
    """
    try:
        review = gh_rest("/repos/%s/pulls/%s/reviews/%s" % (slug, number, review_id))
    except Exception:
        return None
    if not isinstance(review, dict):
        return None
    submitted_at = review.get("submitted_at")
    return submitted_at if str(submitted_at or "").strip() else None


def _backfill_missing_review_times(slug, number, reviews, timeline):
    """Recover a usable timestamp for any review or `reviewed` timeline event that
    lacks one, via re-read-by-id, so a provable ask is never discarded over one
    missing field and the review's time is still attributed to the target's
    activity. Mutates the dicts in place; every recovery is best-effort and fails
    open (a still-missing timestamp is handled by the downstream fail-open
    checks)."""
    reread_cache = {}

    def reread(review_id):
        key = str(review_id)
        if key not in reread_cache:
            reread_cache[key] = _authoritative_review_submitted_at(
                slug, number, review_id
            )
        return reread_cache[key]

    for review in reviews:
        if not isinstance(review, dict):
            continue
        if _item_time(review, "submitted_at", "created_at", "updated_at") is not None:
            continue
        review_id = review.get("id")
        if review_id is None:
            continue
        recovered = reread(review_id)
        if recovered:
            review["submitted_at"] = recovered

    for event in timeline:
        if not isinstance(event, dict):
            continue
        if str(event.get("event") or "") != "reviewed":
            continue
        if _timeline_event_time(event, "reviewed") is not None:
            continue
        review_id = event.get("id")
        if review_id is None:
            continue
        recovered = reread(review_id)
        if recovered:
            event["submitted_at"] = recovered


def _read_pr_cleanup_state(slug, number):
    issue = gh_rest("/repos/%s/issues/%s" % (slug, number))
    pr = gh_rest("/repos/%s/pulls/%s" % (slug, number))
    comments = _read_paginated_list(
        "/repos/%s/issues/%s/comments?per_page=100" % (slug, number)
    )
    reviews = _read_paginated_list(
        "/repos/%s/pulls/%s/reviews?per_page=100" % (slug, number)
    )
    review_comments = _read_paginated_list(
        "/repos/%s/pulls/%s/comments?per_page=100" % (slug, number)
    )
    timeline = _read_paginated_list(
        "/repos/%s/issues/%s/timeline?per_page=100" % (slug, number)
    )
    # Recover any review/`reviewed`-event timestamp the list endpoints dropped so
    # a provable ask isn't discarded and the review's time is attributable.
    _backfill_missing_review_times(slug, number, reviews, timeline)
    body_edits = _read_pr_user_content_edits(slug, number)
    return {
        "issue": issue,
        "pr": pr,
        "comments": comments,
        "reviews": reviews,
        "review_comments": review_comments,
        "timeline": timeline,
        "body_edits": body_edits,
    }


def _parse_pending_markers(body):
    records = []
    for match in _PENDING_CONTRIBUTOR_RE.finditer(str(body or "")):
        try:
            record = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _valid_pending_record(record, repo, number):
    if not isinstance(record, dict):
        return None
    if int(record.get("version") or 0) != 1:
        return None
    if str(record.get("repo") or "") != str(repo):
        return None
    try:
        if int(record.get("number")) != int(number):
            return None
    except (TypeError, ValueError):
        return None
    if record.get("ask_kind") not in PENDING_CONTRIBUTOR_ASK_KINDS_PR:
        return None
    if not str(record.get("ask_id") or "").strip():
        return None
    if _parse_time(record.get("asked_at")) is None:
        return None
    if not str(record.get("head_sha") or "").strip():
        return None
    return record


def _records_from_sources(repo, number, comments, reviews, maintainer_logins):
    records = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        if not _trusted_ask_author(_event_author(comment), maintainer_logins):
            continue
        for record in _parse_pending_markers(comment.get("body")):
            record = dict(record)
            record.setdefault("marker_source", "comment")
            if comment.get("id") is not None:
                record.setdefault("marker_comment_id", str(comment.get("id")))
            valid = _valid_pending_record(record, repo, number)
            if valid:
                records.append(valid)
    for review in reviews:
        if not isinstance(review, dict):
            continue
        if not _trusted_ask_author(_event_author(review), maintainer_logins):
            continue
        for record in _parse_pending_markers(review.get("body")):
            record = dict(record)
            record.setdefault("marker_source", "review")
            if review.get("id") is not None:
                record.setdefault("marker_review_id", str(review.get("id")))
            valid = _valid_pending_record(record, repo, number)
            if valid:
                records.append(valid)
    return records


def _legacy_rebase_record(repo, number, head_sha, comments, maintainer_logins):
    records = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        if not _trusted_ask_author(_event_author(comment), maintainer_logins):
            continue
        heads = [
            match.group(1).strip()
            for match in _REBASE_NUDGE_RE.finditer(str(comment.get("body") or ""))
        ]
        if head_sha is not None:
            heads = [head for head in heads if head == str(head_sha or "").strip()]
        heads = [head for head in heads if head]
        if not heads:
            continue
        created_at = comment.get("created_at")
        asked_dt = _parse_time(created_at)
        comment_id = comment.get("id")
        if asked_dt is None or comment_id is None:
            continue
        for head in heads:
            records.append(
                {
                    "version": 1,
                    "ask_id": "needs-rebase:%s:%s" % (head, comment_id),
                    "ask_kind": "needs-rebase",
                    "asked_at": _format_time(asked_dt),
                    "asked_by": "",
                    "target_author": "",
                    "head_sha": str(head or ""),
                    "repo": str(repo or ""),
                    "number": int(number),
                    "source_id": str(comment_id),
                    "legacy": True,
                }
            )
    if not records:
        return None
    return max(records, key=lambda r: _parse_time(r["asked_at"]))


def _newest_active_pending_record(
    repo,
    number,
    head_sha,
    comments,
    reviews,
    has_pending_label,
    allow_legacy_rebase,
    maintainer_logins,
    active_ask_kinds=None,
):
    records = _records_from_sources(repo, number, comments, reviews, maintainer_logins)
    if active_ask_kinds is not None:
        active = {str(kind) for kind in active_ask_kinds}
        records = [r for r in records if r.get("ask_kind") in active]
    if has_pending_label and records:
        return max(records, key=lambda r: _parse_time(r["asked_at"]))
    if allow_legacy_rebase:
        return _legacy_rebase_record(
            repo, number, head_sha, comments, maintainer_logins
        )
    return None


def _same_time(a, b):
    da = _parse_time(a)
    db = _parse_time(b)
    return da is not None and db is not None and da == db


def _prove_pending_ask(record, comments, reviews, maintainer_logins):
    kind = record.get("ask_kind")
    source = str(record.get("source_id") or "")
    if kind == "needs-rebase":
        marker = _rebase_nudge_marker(record.get("head_sha"))
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            if str(comment.get("id")) != source:
                continue
            if not _trusted_ask_author(_event_author(comment), maintainer_logins):
                return False
            return marker in str(comment.get("body") or "") and _same_time(
                comment.get("created_at"), record.get("asked_at")
            )
        return False
    if kind == "request-changes":
        for review in reviews:
            if not isinstance(review, dict):
                continue
            if str(review.get("id")) != source:
                continue
            if not _trusted_ask_author(_event_author(review), maintainer_logins):
                return False
            state = str(review.get("state") or "").upper()
            return state in {"CHANGES_REQUESTED", "REQUEST_CHANGES"} and _same_time(
                review.get("submitted_at"), record.get("asked_at")
            )
        return False
    return False


def _event_author(item):
    if not isinstance(item, dict):
        return None
    for key in ("user", "author", "actor"):
        author = item.get(key)
        if isinstance(author, dict):
            return author
    return None


def _item_time(item, *keys):
    for key in keys:
        dt = _parse_time((item or {}).get(key))
        if dt is not None:
            return dt
    return None


def _item_times(item, *keys):
    times = []
    for key in keys:
        dt = _parse_time((item or {}).get(key))
        if dt is not None:
            times.append(dt)
    return times


def _latest_item_time(item, *keys):
    times = _item_times(item, *keys)
    return max(times) if times else None


def _commit_event_time(event):
    dt = _item_time(event, "created_at", "updated_at")
    if dt is not None:
        return dt
    for key in ("author", "committer"):
        author = event.get(key)
        if not isinstance(author, dict):
            continue
        dt = _item_time(author, "date")
        if dt is not None:
            return dt
    return None


def _timeline_event_time(event, event_name):
    if event_name == "committed":
        return _commit_event_time(event)
    # A `reviewed` timeline event carries its timestamp in `submitted_at`, not
    # `created_at` (GitHub's timeline shape). Reading only `created_at` made every
    # PR with a review fail open as "reviewed event missing timestamp", so the
    # already-nudged backlog never reached the reminder-then-close clock. Read
    # both keys so a review recorded in the timeline is never discarded and its
    # time still feeds the attributable-activity set; every other event type only
    # carries `created_at`, so the extra key is a harmless fallback.
    return _item_time(event, "created_at", "submitted_at")


def _timeline_event_authors(event, event_name):
    if event_name != "committed":
        actor = _event_author(event)
        return [actor] if actor is not None else []
    authors = []
    for key in ("actor", "user"):
        actor = event.get(key)
        if isinstance(actor, dict):
            authors.append(actor)
    return authors


def _timeline_event_has_contributor_actor(event, event_name, maintainer_logins):
    authors = _timeline_event_authors(event, event_name)
    if event_name == "committed" and not authors:
        return True
    if not authors:
        raise RuntimeError("%s event author missing or ambiguous" % event_name)
    ambiguous = False
    for actor in authors:
        is_contributor = _is_non_maintainer_human(actor, maintainer_logins)
        if is_contributor is True:
            return True
        if is_contributor is None:
            ambiguous = True
    if ambiguous:
        raise RuntimeError("%s event author missing or ambiguous" % event_name)
    return False


def _known_target_activity_times(state):
    times = {"issue": set(), "pr": set()}

    def absorb(targets, dt):
        if dt is None:
            return
        for target in targets:
            times[target].add(dt)

    def absorb_item(targets, item, *keys):
        for dt in _item_times(item, *keys):
            absorb(targets, dt)

    for comment in state["comments"]:
        absorb_item(
            ("issue", "pr"), comment, "created_at", "submitted_at", "updated_at"
        )
    for review in state["reviews"]:
        absorb_item(("issue", "pr"), review, "submitted_at", "created_at", "updated_at")
    for comment in state["review_comments"]:
        absorb_item(
            ("issue", "pr"), comment, "created_at", "submitted_at", "updated_at"
        )
    for edit in state.get("body_edits", []):
        if not isinstance(edit, dict):
            raise RuntimeError("PR body edit returned unexpected data")
        absorb_item(("issue", "pr"), edit, "editedAt", "edited_at")
    for event in state["timeline"]:
        if not isinstance(event, dict):
            raise RuntimeError("timeline returned unexpected event")
        event_name = str(event.get("event") or "")
        absorb(("issue", "pr"), _timeline_event_time(event, event_name))
        absorb_item(("issue", "pr"), event, "updated_at")
    return times


def _ensure_no_unaccounted_target_update(state, asked_dt):
    known = _known_target_activity_times(state)
    for target_name in ("issue", "pr"):
        dt = _item_time(state.get(target_name), "updated_at")
        if dt is None:
            raise RuntimeError("%s missing updated_at" % target_name)
        if dt > asked_dt and dt not in known[target_name]:
            raise RuntimeError(
                "%s updated after ask without attributable activity" % target_name
            )


def _has_qualifying_contributor_activity(state, asked_dt, maintainer_logins):
    for comment in state["comments"]:
        dt = _latest_item_time(comment, "created_at", "submitted_at", "updated_at")
        if dt is None:
            raise RuntimeError("comment missing timestamp")
        if dt <= asked_dt:
            continue
        actor = _event_author(comment)
        is_contributor = _is_non_maintainer_human(actor, maintainer_logins)
        if is_contributor is None:
            raise RuntimeError("comment author missing or ambiguous")
        if is_contributor:
            return True

    for review in state["reviews"]:
        dt = _latest_item_time(review, "submitted_at", "created_at", "updated_at")
        if dt is None:
            raise RuntimeError("review missing timestamp")
        if dt <= asked_dt:
            continue
        actor = _event_author(review)
        is_contributor = _is_non_maintainer_human(actor, maintainer_logins)
        if is_contributor is None:
            raise RuntimeError("review author missing or ambiguous")
        if is_contributor:
            return True

    for comment in state["review_comments"]:
        dt = _latest_item_time(comment, "created_at", "submitted_at", "updated_at")
        if dt is None:
            raise RuntimeError("review comment missing timestamp")
        if dt <= asked_dt:
            continue
        actor = _event_author(comment)
        is_contributor = _is_non_maintainer_human(actor, maintainer_logins)
        if is_contributor is None:
            raise RuntimeError("review comment author missing or ambiguous")
        if is_contributor:
            return True

    for edit in state.get("body_edits", []):
        if not isinstance(edit, dict):
            raise RuntimeError("PR body edit returned unexpected data")
        dt = _item_time(edit, "editedAt", "edited_at")
        if dt is None:
            raise RuntimeError("PR body edit missing timestamp")
        if dt <= asked_dt:
            continue
        editor = (edit or {}).get("editor")
        is_contributor = _is_non_maintainer_human(editor, maintainer_logins)
        if is_contributor is None:
            raise RuntimeError("PR body edit editor missing or ambiguous")
        if is_contributor:
            return True

    for event in state["timeline"]:
        if not isinstance(event, dict):
            raise RuntimeError("timeline returned unexpected event")
        event_name = str(event.get("event") or "")
        dt = _timeline_event_time(event, event_name)
        if dt is None:
            raise RuntimeError("%s event missing timestamp" % event_name)
        if dt <= asked_dt:
            continue
        if _timeline_event_has_contributor_actor(event, event_name, maintainer_logins):
            return True

    _ensure_no_unaccounted_target_update(state, asked_dt)
    return False


def _has_reminder(comments, ask_id, maintainer_logins, asked_dt):
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        if not _trusted_ask_author(_event_author(comment), maintainer_logins):
            continue
        comment_dt = _item_time(comment, "created_at")
        if comment_dt is None or comment_dt <= asked_dt:
            continue
        for match in _PENDING_CONTRIBUTOR_REMINDER_RE.finditer(
            str(comment.get("body") or "")
        ):
            try:
                payload = json.loads(match.group(1))
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            reminded_dt = _parse_time(payload.get("reminded_at"))
            if (
                payload.get("ask_id") == ask_id
                and reminded_dt is not None
                and reminded_dt > asked_dt
            ):
                return True
    return False


def _has_close_attempt(comments, record, maintainer_logins, asked_dt):
    ask_id = record.get("ask_id")
    legacy_body = _pending_close_body(record)
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        if not _trusted_ask_author(_event_author(comment), maintainer_logins):
            continue
        comment_dt = _item_time(comment, "created_at")
        if comment_dt is None or comment_dt <= asked_dt:
            continue
        body = str(comment.get("body") or "")
        if body.strip() == legacy_body.strip():
            return True
        for match in _PENDING_CONTRIBUTOR_CLOSE_RE.finditer(body):
            try:
                payload = json.loads(match.group(1))
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            closed_dt = _parse_time(payload.get("closed_at"))
            if (
                payload.get("ask_id") == ask_id
                and closed_dt is not None
                and closed_dt > asked_dt
            ):
                return True
    return False


def _pending_reminder_body(record, reminded_at):
    marker = _pending_contributor_reminder_marker(record["ask_id"], reminded_at)
    if record.get("ask_kind") == "needs-rebase":
        # Same visible automation disclosure as the initial rebase nudge - this
        # is an automated follow-up to that ask, not a personal maintainer ping.
        text = (
            "Automated reminder: this PR still looks blocked on a rebase or merge conflict fix.\n\n"
            "If you are still interested, please rebase onto the current base branch, "
            "resolve the conflict, and push.\n\n"
            "If I do not hear back, I may close this as inactive."
        )
    else:
        text = (
            "Quick reminder: this is still waiting on your update.\n\n"
            "If you are still interested, please push the requested changes or leave a quick comment.\n\n"
            "If I do not hear back, I may close this as inactive."
        )
    return text + "\n\n" + marker


def _pending_close_body(record, closed_at=None):
    asked = _parse_time(record.get("asked_at"))
    asked_date = asked.strftime("%Y-%m-%d") if asked else str(record.get("asked_at"))
    if record.get("ask_kind") == "needs-rebase":
        why = (
            "I am closing this because it has been waiting on a rebase or merge-conflict "
            "fix since %s, and I have not seen a comment or push since then."
            % asked_date
        )
    else:
        why = (
            "I am closing this because I requested changes on %s, and I have not seen "
            "a comment or push since then." % asked_date
        )
    body = (
        why
        + "\n\n"
        + "If you still want to keep working on this, please reopen it or open a new PR and mention this one.\n\n"
        + "Happy to take another look when there is an update."
    )
    if closed_at:
        body += "\n\n" + _pending_contributor_close_marker(record["ask_id"], closed_at)
    return body


def _post_pending_reminder(slug, number, record, now):
    gh_rest(
        "/repos/%s/issues/%s/comments" % (slug, number),
        method="POST",
        fields={"body": _pending_reminder_body(record, _format_time(now))},
    )


def _patch_pending_target_closed(slug, number):
    gh_rest(
        "/repos/%s/issues/%s" % (slug, number),
        method="PATCH",
        fields={"state": "closed"},
    )


def _close_pending_target(slug, number, record, now):
    gh_rest(
        "/repos/%s/issues/%s/comments" % (slug, number),
        method="POST",
        fields={"body": _pending_close_body(record, _format_time(now))},
    )
    _patch_pending_target_closed(slug, number)


def _pr_conflicting_for_cleanup(pr):
    """Whether a scanned PR may have an eligible rebase-nudge cleanup record.

    `needs-rebase` is always eligible for proof lookup.
    The ci-noop exception is eligible only for a cross-repo
    `needs-ci-approval` PR whose scan-time mergeability is conclusively
    `CONFLICTING`.
    The caller must still prove the nudge itself before it can act.
    """
    if pr.get("bucket") == "needs-rebase":
        return True
    return _is_ci_noop_cleanup_candidate(pr) and _mergeable_is_conflicting(
        pr.get("mergeable")
    )


def _is_ci_noop_cleanup_candidate(pr):
    """Identify the fork ci-noop route without treating it as an ask or action.

    A later conflict check and a provable rebase-nudge record are both required
    before stale cleanup can do anything for this fast CI-approval lane.
    """
    return pr.get("bucket") == "needs-ci-approval" and pr.get("cross_repo") is True


def _is_ci_approval_cleanup_lane(pr):
    return pr.get("kind") == "ci-approval" or pr.get("bucket") == "needs-ci-approval"


def _ci_noop_conflict_is_current(owner, repo, pr):
    """Re-check ci-noop mergeability immediately before a cleanup write.

    Standard rebase cleanup already routes from a current `needs-rebase` scan
    bucket.
    The ci-noop exception must instead fail closed unless a fresh re-read is
    still conclusively `CONFLICTING`.
    """
    if not _is_ci_noop_cleanup_candidate(pr):
        return True
    return _mergeable_is_conflicting(_settle_mergeable(owner, repo, pr["number"]))


def _active_pending_ask_kinds(pr):
    if _is_ci_approval_cleanup_lane(pr):
        # Conflicting fork ci-noop: only a proven rebase nudge may remind/close.
        if _pr_conflicting_for_cleanup(pr):
            return {"needs-rebase"}
        # Non-conflicting CI-approval is clear-only for answered request-changes
        # (head move / contributor activity). It never reminds or closes.
        return {"request-changes"}
    kinds = {"request-changes"}
    if _pr_conflicting_for_cleanup(pr):
        kinds.add("needs-rebase")
    return kinds


def _ci_approval_clear_only(pr):
    """Non-conflicting CI-approval: label clear only, never remind/close."""
    return _is_ci_approval_cleanup_lane(pr) and not _pr_conflicting_for_cleanup(pr)


def _sweep_pending_pr(
    owner,
    repo,
    pr,
    maintainer_logins,
    reminder_days,
    cleanup_days,
    now,
):
    slug = "%s/%s" % (owner, repo)
    number = pr["number"]
    state = _read_pr_cleanup_state(slug, number)
    issue = state["issue"] or {}
    current_pr = state["pr"] or {}

    if issue.get("state") != "open" or current_pr.get("state") != "open":
        return "skip"

    labels = set(_label_names_from_issue(issue))
    if PENDING_CONTRIBUTOR_KEEP_OPEN_LABEL in labels:
        return "skip"
    has_pending_label = PENDING_CONTRIBUTOR_LABEL in labels

    target_author = _event_author(issue) or _event_author(current_pr)
    author_kind = _is_non_maintainer_human(target_author, maintainer_logins)
    if author_kind is not True:
        return "skip"

    head_sha = str(
        ((current_pr.get("head") or {}).get("sha")) or pr.get("head_sha") or ""
    )
    if not head_sha:
        return "skip"

    active_ask_kinds = _active_pending_ask_kinds(pr)
    record = _newest_active_pending_record(
        repo,
        number,
        head_sha,
        state["comments"],
        state["reviews"],
        has_pending_label,
        allow_legacy_rebase=_pr_conflicting_for_cleanup(pr),
        maintainer_logins=maintainer_logins,
        active_ask_kinds=active_ask_kinds,
    )
    if not record:
        if has_pending_label and "needs-rebase" not in active_ask_kinds:
            records = _records_from_sources(
                repo, number, state["comments"], state["reviews"], maintainer_logins
            )
            active_records = [
                r for r in records if r.get("ask_kind") in active_ask_kinds
            ]
            stale_rebase_records = [
                r for r in records if r.get("ask_kind") == "needs-rebase"
            ]
            legacy_rebase_record = _legacy_rebase_record(
                repo, number, None, state["comments"], maintainer_logins
            )
            if (stale_rebase_records or legacy_rebase_record) and not active_records:
                _remove_target_label(slug, number, PENDING_CONTRIBUTOR_LABEL)
                return "activity"
        return "skip"

    asked_dt = _parse_time(record.get("asked_at"))
    if asked_dt is None:
        return "skip"
    if str(record.get("head_sha") or "") != head_sha:
        if has_pending_label:
            _remove_target_label(slug, number, PENDING_CONTRIBUTOR_LABEL)
        return "activity"
    if not _prove_pending_ask(
        record, state["comments"], state["reviews"], maintainer_logins
    ):
        return "skip"

    if _has_qualifying_contributor_activity(state, asked_dt, maintainer_logins):
        if has_pending_label:
            _remove_target_label(slug, number, PENDING_CONTRIBUTOR_LABEL)
        return "activity"

    # Non-conflicting CI-approval is a fast security gate, not a stale-contributor
    # close lane. After head-move / activity clears above, never remind or close
    # here - even with a still-bound request-changes marker (card #1537).
    if _ci_approval_clear_only(pr):
        return "skip"

    reminder_at = asked_dt + timedelta(days=reminder_days)
    close_at = asked_dt + timedelta(days=cleanup_days)
    reminded = _has_reminder(
        state["comments"], record["ask_id"], maintainer_logins, asked_dt
    )

    action_due = now >= close_at or (now >= reminder_at and not reminded)
    if not action_due:
        return "skip"
    if not _ci_noop_conflict_is_current(owner, repo, pr):
        return "skip"

    if now >= close_at:
        if not reminded:
            _post_pending_reminder(slug, number, record, now)
            if not has_pending_label:
                _add_target_label(slug, number, PENDING_CONTRIBUTOR_LABEL)
            return "reminded"
        if _has_close_attempt(state["comments"], record, maintainer_logins, asked_dt):
            _patch_pending_target_closed(slug, number)
        else:
            _close_pending_target(slug, number, record, now)
        return "closed"
    if now >= reminder_at and not reminded:
        _post_pending_reminder(slug, number, record, now)
        if not has_pending_label:
            _add_target_label(slug, number, PENDING_CONTRIBUTOR_LABEL)
        return "reminded"
    return "skip"


def sweep_pending_contributor_actions(
    owner,
    repo_cfg,
    prs,
    maintainer_logins,
    enabled=False,
    reminder_days=10,
    cleanup_days=14,
    targets=_PENDING_CONTRIBUTOR_TARGETS_UNSET,
    now=None,
):
    effective_targets = _pending_contributor_cleanup_targets(
        {},
        ["pr"] if targets is _PENDING_CONTRIBUTOR_TARGETS_UNSET else targets,
    )
    if not enabled or "pr" not in effective_targets:
        return set()
    name = repo_cfg["name"]
    now = now or datetime.now(timezone.utc)
    closed = set()
    for pr in prs:
        if pr.get("author_excluded"):
            continue
        conflicting = _pr_conflicting_for_cleanup(pr)
        is_ci_approval = _is_ci_approval_cleanup_lane(pr)
        has_pending = PENDING_CONTRIBUTOR_LABEL in set(pr.get("labels") or [])
        # ci-approval is a fast security gate, not a stale-contributor close lane.
        # The narrow ci-noop exception requires both a proven rebase nudge and a
        # conclusively conflicting cross-repo PR.
        # Non-conflicting ci-approval is clear-only: enter only when a pending
        # label (or truncated labels) is present so answered request-changes can
        # drop the stale label after a head move / contributor activity, without
        # ever reminding or closing (card #1537).
        if is_ci_approval and not conflicting:
            if not has_pending and not pr.get("labels_truncated"):
                continue
        maybe_pending = (
            pr.get("bucket") == "needs-rebase"
            or conflicting
            or has_pending
            or pr.get("labels_truncated")
        )
        if not maybe_pending:
            continue
        try:
            outcome = _sweep_pending_pr(
                owner,
                name,
                pr,
                maintainer_logins,
                reminder_days,
                cleanup_days,
                now,
            )
        except Exception as e:
            print(
                "::warning::wheelhouse pending-contributor cleanup skipped %s#%s: %s"
                % (name, pr.get("number"), _workflow_command_text(str(e)[:160])),
                file=sys.stderr,
            )
            continue
        if outcome == "closed":
            closed.add(pr["number"])
            print(
                "::notice::wheelhouse pending-contributor cleanup closed %s#%s"
                % (name, pr["number"]),
                file=sys.stderr,
            )
        elif outcome == "reminded":
            print(
                "::notice::wheelhouse pending-contributor cleanup reminded %s#%s"
                % (name, pr["number"]),
                file=sys.stderr,
            )
    return closed


def _non_default_base_posture(base_ref, default_branch):
    base = str(base_ref or "").strip()
    default = str(default_branch or "").strip()
    if base and default and base == default:
        return None
    return {
        "pr_target": True,
        "exploit": False,
        "error": True,
        "non_default_base": True,
        "base_ref": base,
        "default_branch": default,
    }


def _ci_safety_note(verdict):
    """A human warning for a ci-approval CARD (the not-auto-approved path), so
    the maintainer decides with eyes open. Loudest signal first."""
    parts = []
    if verdict.get("non_default_base"):
        base = verdict.get("base_ref") or "<unknown>"
        default = verdict.get("default_branch") or "<unknown>"
        parts.append(
            "This PR targets base branch `%s`, but the repo default is `%s`. "
            "Wheelhouse only auto-checks `pull_request_target` posture on the "
            "default branch, so it fails closed for manual review." % (base, default)
        )
    elif verdict.get("exploit"):
        parts.append(
            "DANGER (pwn-request): a `pull_request_target` workflow on the base branch "
            "checks out this PR's head, so running fork CI could execute attacker-controlled "
            "code with repo secrets. Review the diff with extreme care before approving."
        )
    elif verdict.get("pr_target"):
        parts.append(
            "This repo runs a `pull_request_target` workflow (it executes with repo secrets "
            "and fires automatically, independent of this approval). Approving here only "
            "clears the read-only fork `pull_request` run - review the PR contents before "
            "trusting CI output."
        )
    if verdict.get("risky_files"):
        parts.append(
            "This PR changes CI-execution files (%s); approving would run the PR's OWN "
            "workflow/action code, so it is held for manual review."
            % _display_list(verdict["risky_files"])
        )
    return " ".join(parts)


def _auto_approve_or_card(
    owner, name, pr_number, posture, auto_enabled, changed_files=None
):
    """For one `needs-ci-approval` PR, decide auto-approve vs card.

    Returns (handled, card_note, log_note, approve_status) where:
      * handled=True  -> the run was auto-approved OR there is no pending run to
        approve; emit NO card. `card_note` is unused (None) and `log_note` is
        the audit line for the scan-step `::notice::`.
      * handled=False -> return a card fallback; `card_note` is the safety warning
        to surface on the card body (may be "", left EXACTLY as before), and
        `log_note` is the per-PR outcome line for the scan-step `::warning::`.
      * approve_status is the `approve_ci` status string when an approve was
        attempted (`approved` / `noop` / `hold` / `error`), else None. Callers
        use a `noop` status plus settled CONFLICTING mergeability to post the
        existing rebase nudge before dropping the PR from the worklist - the
        bucket stays `needs-ci-approval` (never rewritten to `needs-rebase`).
    `log_note` ALWAYS carries the `ci_safety` verdict `reason`, plus - when an
    approve was attempted - the `approve_ci` `status` + `message`. That is what
    makes a silent approve failure (`error`/`hold`) impossible to hide in
    the scan log; it is a logging string only (gh stderr/status text, never a
    token) and does NOT change the card body.
    Fails CLOSED: any uncertainty (unsafe verdict, hold, approve error/exception)
    returns the caller-visible fallback outcome."""
    verdict = ci_safety("%s/%s" % (owner, name), str(pr_number), posture, changed_files)
    reason = verdict.get("reason", "")
    if auto_enabled and verdict["safe"]:
        try:
            status, message = approve_ci(
                owner, name, str(pr_number), posture=posture, strict=True
            )
        except Exception as e:  # an approve that throws must fall back to a card
            status, message = ("error", "auto-approve raised: %s" % str(e)[:160])
        if status == "approved":
            return (
                True,
                None,
                "auto-approved (%s): %s" % (reason, message),
                status,
            )
        if status == "noop":
            return (
                True,
                None,
                "verdict safe (%s); approve_ci noop: %s" % (reason, message),
                status,
            )
        # hold / error -> fall through to caller fallback (fail-closed), keeping the why.
        card_note = "auto-approve did not complete (%s: %s)" % (status, message)
        safety_note = _ci_safety_note(verdict)
        if safety_note:
            card_note += "; " + safety_note
        log_note = "verdict safe (%s); approve_ci %s: %s" % (reason, status, message)
        return (False, card_note, log_note, status)
    # Auto-approve disabled, or an unsafe verdict -> caller fallback; no approve attempted.
    log_note = "verdict %s (%s); not auto-approved%s" % (
        "safe" if verdict["safe"] else "unsafe",
        reason,
        "" if auto_enabled else " (auto-approve disabled)",
    )
    return (False, _ci_safety_note(verdict), log_note, None)


def _mergeable_for_ci_noop_nudge(pr, settled_mergeable=_MERGEABLE_SETTLEMENT_UNSET):
    """Conclusive mergeability for a consumed ci-approval noop, or None.

    Only an authoritative CONFLICTING value may trigger the rebase nudge.
    An explicit UNKNOWN uses the caller's batched settlement result; if it
    never settles, return None with no nudge. Settlement errors are handled
    before this helper runs and make the repo result unhealthy. Missing/None
    mergeable keeps classify's fail-open and does not invent a conflict."""
    mergeable = pr.get("mergeable")
    if _mergeable_is_conclusive(mergeable):
        return mergeable
    if not _mergeable_is_unknown(mergeable):
        return None
    if _mergeable_is_conclusive(settled_mergeable):
        return settled_mergeable
    return None


def _ci_wait_refresh_item(slug, name, pr, auto_triage_enabled):
    """A refresh-ONLY pr-review item for a PR that is mid fork-CI approval/run wait.

    When a source PR's fork CI is auto-approved this scan (or its already-approved
    checks are still running), the PR emits NO worklist item while it awaits
    terminal checks (`needs-ci-approval` is consumed by the auto-approve, and
    `ci-running` is not in `NEEDS_MAINTAINER`). An EXISTING pr-review card for
    that PR would therefore keep displaying the pre-rebase head's now-superseded
    state - e.g. a stale `merge-ready`/`tests=green` - masquerading as current.
    reconcile uses this item to REFRESH that existing card in place to the new
    head's honest pending state so it can no longer masquerade after the scan
    observes the head change. It NEVER creates a card from this item (new-card
    creation still defers until checks are terminal) and reconcile never queues
    triage for this transient revision. The `comp`/`tests` carried here are the
    real scanned values at the new head (`none` when CI has not produced a
    signal yet, or `pending` while checks run - NEVER `green`), so the refreshed
    card visibly stops claiming a passed revision."""
    head = pr.get("head_sha", "") or ""
    return {
        "repo": name,
        "number": pr["number"],
        "kind": "pr-review",
        "head_sha": head,
        "updated_at": pr.get("updated_at", "") or "",
        "title": pr["title"],
        "author": pr["author"],
        "bucket": pr["bucket"],
        "comp": pr["comp"],
        "tests": pr["tests"],
        "url": "https://github.com/%s/pull/%d" % (slug, pr["number"]),
        # Temporally honest UX (card #1537): decision controls may already be
        # published from a prior head, but current-head automatic triage is
        # intentionally deferred on this transient ci_wait revision. Never
        # imply a current-head triage result exists; prior-head triage does
        # not apply. Reconcile still never queues triage here.
        "summary": "head moved to %s - fork CI was re-approved and checks are "
        "re-running. Automatic triage for this head is deferred until "
        "checks finish (prior-head triage does not apply)."
        % (head[:8] or "?"),
        "recommendation": "Automatic triage will resume after terminal checks "
        "if this card remains eligible. Prior-head triage does not apply to "
        "the current head. Wait for checks to finish, then re-review.",
        "priority": "low",
        "base_sha": pr.get("base_sha") or "",
        # Item-level flag stays the repo's config for when terminal checks land;
        # the ci_wait refresh path itself never queues triage on this revision.
        "auto_triage": auto_triage_enabled,
    }


def build_repo(
    owner,
    repo_cfg,
    card_issues,
    auto_approve_ci=True,
    auto_triage=True,
    auto_triage_issues=True,
    pending_contributor_cleanup=False,
    pending_contributor_cleanup_days=14,
    pending_contributor_reminder_days=10,
    pending_contributor_cleanup_targets=_PENDING_CONTRIBUTOR_TARGETS_UNSET,
    ci_security_summary_cache=None,
    auto_merge=False,
    triage_attempt_cap_per_revision=TRIAGE_ATTEMPT_CAP_DEFAULT,
):
    """Scan one repo. Returns (repo_result, items).

    Decision cards are for other people's work, so scan-built PR-review and
    issue-triage items skip known owner/maintainer/bot authors. Missing author
    metadata fails open.

    `auto_approve_ci` is the fleet-wide default (config `auto_approve_ci`, itself
    defaulting True); a repo may override it per-repo. `auto_triage` mirrors that
    model for the advisory Claude pass on pr-review cards, and `auto_triage_issues`
    is the INDEPENDENT equivalent for issue-triage cards (its own global/per-repo
    default, never affected by `auto_triage` or vice versa).
    `pending_contributor_cleanup` is the opposite default: OFF unless the owner
    opts in globally or per repo. When enabled for PR targets, this scan may
    remind or close stale PRs only from provable target-side request-changes or
    needs-rebase asks, and every uncertainty fails open. Same-repo PRs with no CI
    signal route to normal review, not CI approval. Unknown fork status keeps a
    manual CI-approval card with no auto-approve attempt for contributor-authored
    PRs and logs a suppressed card for owner/maintainer/bot-authored PRs. When
    enabled, or when the author is excluded from the decision queue, a fork PR
    whose `ci_safety` verdict is provably safe is approved here (in the
    FLEET_TOKEN scan context), or verified as having no pending run, and emits NO
    card; risky/uncertain contributor PRs still become cards while excluded-author
    PRs only log suppressed-card warnings. Before bucket resolution, every
    non-draft known-fork PR gets an exact-head action_required probe so completed
    rollup contexts cannot mask a separate pending suite; a probe error fails the
    repo scan closed before any acting path. Each handled ci-approval PR also
    emits exactly one stderr notice/warning outcome line. When the handled path is a
    verified `approve_ci` noop and settled mergeability is CONFLICTING, the same
    fire-once-per-head rebase nudge used for `needs-rebase` is posted before the
    PR is dropped from the worklist - still no decision card and no bucket rewrite
    (ci-approval stays independent of mergeability for classification).
    `ci_security_summary_cache` is a best-effort, card-side display cache for
    contributor CI-approval HOLD cards. It only avoids redundant read-only
    analysis and never affects safety, routing, approval, or target writes.
    Conflicted PR-review candidates become `needs-rebase`: no decision card is
    emitted, and contributor-authored PRs get at most one rebase nudge per head
    SHA via a hidden comment marker. This runs only on the ok:true success path
    below, so an ok:false repo (early return) is never auto-approved or nudged.
    A merge-ready or review-needed candidate whose bulk `mergeable` reads UNKNOWN
    is polled to a conclusive value (`_resolve_pr_bucket`); if it still cannot be
    settled it is reported in `indeterminate_pr_numbers` and emits no item, so an
    UNKNOWN reading never flips worklist membership (reconcile freezes such a
    card).
    Open PRs, open issues, and PR closing issue references are paginated; if the
    PR or closing-reference scan is incomplete, issue-triage items are withheld
    because Wheelhouse cannot prove which issues are already addressed by PRs."""
    name = repo_cfg["name"]
    slug = "%s/%s" % (owner, name)
    try:
        data = gh_graphql(owner, name)
    except (
        Exception
    ) as e:  # resilient: a missing/unreadable repo does not abort the scan
        # Name the repo in the warning so a dark repo is identifiable straight
        # from the scan log (previously the message carried no repo, and the
        # offending repo had to be inferred from notice ordering).
        return (
            {
                "name": name,
                "ok": False,
                "warning": "%s scan failed: %s" % (slug, str(e)[:200]),
                "open_pr_numbers": [],
                "open_issue_numbers": [],
            },
            [],
        )

    pr_scan_warning = ""
    try:
        prs, pr_scan_complete = _page_open_prs(owner, name, data["pullRequests"])
    except Exception as e:
        prs = list(data["pullRequests"].get("nodes") or [])
        pr_scan_complete = False
        pr_scan_warning = "PR scan incomplete: %s" % str(e)[:160]
    prs = _dedupe_numbered_nodes(prs)
    pr_total = data["pullRequests"].get("totalCount", len(prs))
    pr_truncated = (not pr_scan_complete) or pr_total > len(prs)
    if pr_truncated and not pr_scan_warning:
        pr_scan_warning = "PR scan incomplete: fetched %d of %d open PRs" % (
            len(prs),
            pr_total,
        )
    issue_scan_warning = ""
    try:
        issues, issue_scan_complete = _page_open_issues(owner, name, data["issues"])
    except Exception as e:
        issues = list(data["issues"].get("nodes") or [])
        issue_scan_complete = False
        issue_scan_warning = "issue scan incomplete: %s" % str(e)[:160]
    issues = _dedupe_numbered_nodes(issues)
    issue_total = data["issues"].get("totalCount", len(issues))
    issue_truncated = (not issue_scan_complete) or issue_total > len(issues)
    if issue_truncated and not issue_scan_warning:
        issue_scan_warning = "issue scan incomplete: fetched %d of %d open issues" % (
            len(issues),
            issue_total,
        )
    default_branch = ((data.get("defaultBranchRef") or {}).get("name") or "").strip()
    maintainer_logins = {login.casefold() for login in maintainers()}
    cleanup_enabled = _pending_contributor_cleanup_enabled(
        repo_cfg, pending_contributor_cleanup
    )
    cleanup_targets = _pending_contributor_cleanup_targets(
        repo_cfg,
        ["pr"]
        if pending_contributor_cleanup_targets is _PENDING_CONTRIBUTOR_TARGETS_UNSET
        else pending_contributor_cleanup_targets,
    )
    cleanup_days = _pending_contributor_cleanup_days(
        repo_cfg, pending_contributor_cleanup_days
    )
    reminder_days = _pending_contributor_reminder_days(
        repo_cfg, pending_contributor_reminder_days
    )
    arm_rebase_cleanup = cleanup_enabled and "pr" in cleanup_targets
    all_names = set()
    enriched = []
    closing_scan_complete = True
    closing_scan_warning = ""
    pr_contexts = []
    pending_ci_errors = {}
    settle_numbers = []
    for pr in prs:
        author = pr.get("author") or {}
        comp, tests, ci, names = check_status(pr, repo_cfg)
        all_names.update(names)
        cross_repo = _pr_is_cross_repo(pr)
        pending_ci_approval = False
        # statusCheckRollup only reflects check contexts that already exist. A
        # separate or replayed action_required suite can coexist with completed
        # contexts on the same head, so discover that state independently before
        # bucket selection. Drafts stay excluded by policy, and only a provable
        # fork is queried. The acting path below still re-lists and individually
        # verifies every run against the live PR head before approving anything.
        if not pr["isDraft"] and cross_repo is True:
            pending_runs, pending_error = _list_action_required_runs(
                slug, pr.get("headRefName"), pr.get("headRefOid")
            )
            if pending_error:
                pending_ci_errors[pr["number"]] = pending_error
            else:
                pending_ci_approval = bool(pending_runs)
        bucket = classify(
            pr["isDraft"], comp, tests, ci, cross_repo, pr.get("mergeable")
        )
        if pending_ci_approval:
            bucket = "needs-ci-approval"
        pr_contexts.append(
            (pr, author, comp, tests, ci, cross_repo, pending_ci_approval)
        )
        if bucket in ("merge-ready", "review-needed") and _mergeable_is_unknown(
            pr.get("mergeable")
        ):
            settle_numbers.append(pr["number"])

    if pending_ci_errors:
        failed_numbers = sorted(pending_ci_errors)
        failed_reason = pending_ci_errors[failed_numbers[0]]
        return (
            {
                "name": name,
                "ok": False,
                "warning": (
                    "%s scan failed: action_required run enumeration failed for "
                    "fork PR(s) %s: %s"
                    % (
                        slug,
                        ", ".join("#%s" % number for number in failed_numbers),
                        failed_reason[:160],
                    )
                ),
                "open_pr_numbers": [pr["number"] for pr in prs],
                "open_issue_numbers": [it["number"] for it in issues],
                "truncated": pr_truncated or issue_truncated,
            },
            [],
        )

    settled_mergeables, settlement_errors = _settle_mergeables(
        owner, name, settle_numbers
    )
    settlement_failure = _settlement_failure_result(
        slug,
        name,
        prs,
        issues,
        settle_numbers,
        settled_mergeables,
        settlement_errors,
        pr_truncated or issue_truncated,
    )
    if settlement_failure:
        return (
            settlement_failure,
            [],
        )
    for pr, author, comp, tests, ci, cross_repo, pending_ci_approval in pr_contexts:
        bucket = (
            "needs-ci-approval"
            if pending_ci_approval
            else _resolve_pr_bucket(
                owner,
                name,
                pr,
                pr["isDraft"],
                comp,
                tests,
                ci,
                cross_repo,
                settled_mergeables.get(pr["number"], _MERGEABLE_SETTLEMENT_UNSET),
            )
        )
        author_excluded = _author_excluded_from_queue(author, maintainer_logins)
        try:
            closes, closes_complete = _closing_issue_numbers(owner, name, pr)
        except Exception as e:
            closes = [
                i["number"]
                for i in (pr.get("closingIssuesReferences") or {}).get("nodes", [])
            ]
            closes_complete = False
            if not closing_scan_warning:
                closing_scan_warning = (
                    "PR closing issue scan incomplete: %s" % str(e)[:160]
                )
        if not closes_complete:
            closing_scan_complete = False
            if not closing_scan_warning:
                closing_scan_warning = (
                    "PR closing issue scan incomplete for #%s" % pr["number"]
                )
        enriched.append(
            {
                "number": pr["number"],
                "title": pr["title"],
                "author": _author_login(author) or "?",
                "author_excluded": author_excluded,
                "labels": _label_names_from_nodes(
                    (pr.get("labels") or {}).get("nodes") or []
                ),
                "labels_truncated": _label_connection_truncated(pr.get("labels")),
                "comp": comp,
                "tests": tests,
                "ci": ci,
                "bucket": bucket,
                "closes": closes,
                "head_sha": pr["headRefOid"],
                "updated_at": pr.get("updatedAt", "") or "",
                "changed_files": pr.get("changedFiles"),
                "base_ref": pr.get("baseRefName"),
                "base_sha": pr.get("baseRefOid"),
                "cross_repo": cross_repo,
                # Bulk GraphQL mergeable - used only by the ci-approval noop
                # conflict-nudge path (needs-rebase already resolved via bucket).
                "mergeable": pr.get("mergeable"),
            }
        )
        if bucket == "needs-rebase" and not author_excluded:
            _maybe_nudge_rebase(
                slug, name, enriched[-1], maintainer_logins, arm_rebase_cleanup
            )

    closed_by_cleanup = sweep_pending_contributor_actions(
        owner,
        repo_cfg,
        enriched,
        maintainer_logins,
        enabled=cleanup_enabled,
        reminder_days=reminder_days,
        cleanup_days=cleanup_days,
        targets=cleanup_targets,
    )
    if closed_by_cleanup:
        enriched = [pr for pr in enriched if pr["number"] not in closed_by_cleanup]
        issues = [it for it in issues if it["number"] not in closed_by_cleanup]

    closing = _closing_map(enriched)
    open_issue_numbers = [it["number"] for it in issues]
    addressed = {n for n in closing if n in set(open_issue_numbers)}

    auto_enabled = _auto_approve_enabled(repo_cfg, auto_approve_ci)
    triage_enabled = _auto_triage_enabled(repo_cfg, auto_triage)
    triage_issues_enabled = _auto_triage_issues_enabled(repo_cfg, auto_triage_issues)
    triage_attempt_cap = _triage_attempt_cap(repo_cfg, triage_attempt_cap_per_revision)
    auto_merge_vision_sha = ""
    if _auto_merge_enabled(repo_cfg, auto_merge) and any(
        pr.get("bucket") == "merge-ready" for pr in enriched
    ):
        auto_merge_vision_sha = _default_branch_vision_sha(slug)
    default_posture = None

    items = []
    ci_noop_nudge_candidates = []
    # PRs whose fork CI this scan FRESHLY auto-approved (a new approval that will
    # produce pending checks). Combined below with `ci-running` PRs into the
    # "mid fork-CI approval/run wait" set that reconcile FREEZES (never consume a
    # card merely because its target is awaiting terminal checks) and refreshes
    # (anti-masquerade). A `noop`/verified-empty approve is NOT collected - there
    # is nothing pending to wait for. Author-excluded PRs are excluded so the
    # existing author-filter self-heal is never blocked (they never carry a
    # contributor pr-review card anyway).
    ci_just_approved = []
    for pr in enriched:
        if pr["bucket"] not in NEEDS_MAINTAINER:
            continue
        kind = PR_KIND[pr["bucket"]]
        author_excluded = pr["author_excluded"]
        if author_excluded and kind != "ci-approval":
            continue
        overlap = _overlap_note(pr["number"], pr["closes"], closing, addressed)
        priority = "high" if overlap else PRIORITY.get(pr["bucket"], "low")
        summary = "compliance=%s tests=%s" % (pr["comp"], pr["tests"])
        if overlap:
            summary += "; " + overlap
        item = {
            "repo": name,
            "number": pr["number"],
            "kind": kind,
            "head_sha": pr["head_sha"],
            "updated_at": pr.get("updated_at", "") or "",
            "title": pr["title"],
            "author": pr["author"],
            "bucket": pr["bucket"],
            "comp": pr["comp"],
            "tests": pr["tests"],
            "url": "https://github.com/%s/pull/%d" % (slug, pr["number"]),
            "summary": summary,
            "recommendation": _recommendation(pr["bucket"]),
            "priority": priority,
            # The existing presentation note is also a trusted scan-time
            # auto-merge safety fact. An empty string proves no overlap in this
            # complete scan; a non-empty string is the ambiguity evidence.
            "same_closing_issue_overlap": overlap,
        }
        if kind == "pr-review":
            item["auto_triage"] = triage_enabled
            item["triage_attempt_cap_per_revision"] = triage_attempt_cap
            item["base_sha"] = pr.get("base_sha") or ""
            item["automerge_vision_sha"] = auto_merge_vision_sha

        if kind == "ci-approval":
            if pr.get("cross_repo") is not True:
                card_note = (
                    "Wheelhouse could not determine whether this PR is from a "
                    "fork, so it is leaving fork-CI approval for manual review "
                    "instead of auto-approving or consuming the card."
                )
                print(
                    "::warning::wheelhouse auto-approve %s %s#%s: "
                    "fork status unknown; not auto-approved"
                    % (
                        "suppressed-card" if author_excluded else "carded",
                        name,
                        pr["number"],
                    ),
                    file=sys.stderr,
                )
                if author_excluded:
                    continue
                item["warning"] = card_note
                items.append(item)
                continue
            posture = _non_default_base_posture(pr.get("base_ref"), default_branch)
            if posture is None:
                if default_posture is None:
                    default_posture = repo_pr_target_posture(slug)
                posture = default_posture
            approve_enabled = auto_enabled or author_excluded
            handled, card_note, log_note, approve_status = _auto_approve_or_card(
                owner,
                name,
                pr["number"],
                posture,
                approve_enabled,
                pr.get("changed_files"),
            )
            if handled:
                print(
                    "::notice::%s#%s %s"
                    % (name, pr["number"], _workflow_command_text(log_note)),
                    file=sys.stderr,
                )
                if approve_status == "noop" and not author_excluded:
                    ci_noop_nudge_candidates.append(pr)
                if approve_status == "approved" and not author_excluded:
                    # This scan just approved this fork PR's CI; its checks are now
                    # pending. Freeze/refresh any existing card for it until those
                    # checks reach a terminal result (see `_ci_wait_refresh_item`).
                    ci_just_approved.append(pr["number"])
                continue  # provably safe (or nothing to approve) -> NO card
            # Log exactly one per-PR outcome line so a silent approve failure
            # can never hide in the scan log. The card body itself is unchanged
            # when one is emitted.
            print(
                "::warning::wheelhouse auto-approve %s %s#%s: %s"
                % (
                    "suppressed-card" if author_excluded else "carded",
                    name,
                    pr["number"],
                    _workflow_command_text(log_note),
                ),
                file=sys.stderr,
            )
            if author_excluded:
                continue
            if card_note:  # surface the safety warning on the card body / response
                item["warning"] = card_note
            # Attach the advisory, read-only security summary of the changed
            # workflow/action files so the owner reviews the pwn-request HOLD
            # faster. Presentation only - it never approves or weakens the hold.
            _attach_ci_security_summary(item, slug, pr, ci_security_summary_cache)

        items.append(item)

    ci_noop_unknown_numbers = [
        pr["number"]
        for pr in ci_noop_nudge_candidates
        if _mergeable_is_unknown(pr.get("mergeable"))
    ]
    ci_noop_settled_mergeables = {}
    ci_noop_settlement_errors = {}
    if ci_noop_unknown_numbers:
        ci_noop_settled_mergeables, ci_noop_settlement_errors = _settle_mergeables(
            owner, name, ci_noop_unknown_numbers
        )
    ci_noop_settlement_failure = _settlement_failure_result(
        slug,
        name,
        enriched,
        issues,
        ci_noop_unknown_numbers,
        ci_noop_settled_mergeables,
        ci_noop_settlement_errors,
        pr_truncated or issue_truncated or not closing_scan_complete,
        (pr["number"] for pr in enriched if pr["bucket"] == MERGEABILITY_PENDING),
    )
    if ci_noop_settlement_failure:
        return (ci_noop_settlement_failure, [])
    for pr in ci_noop_nudge_candidates:
        mergeable = _mergeable_for_ci_noop_nudge(
            pr,
            ci_noop_settled_mergeables.get(pr["number"], _MERGEABLE_SETTLEMENT_UNSET),
        )
        if _mergeable_is_conflicting(mergeable):
            _maybe_nudge_rebase(slug, name, pr, maintainer_logins, arm_cleanup=False)

    if card_issues:
        if pr_truncated or not closing_scan_complete:
            if not pr_scan_warning and not closing_scan_warning:
                pr_scan_warning = "PR scan incomplete for issue triage"
        else:
            for it in issues:
                author = it.get("author") or {}
                if _author_excluded_from_queue(author, maintainer_logins):
                    continue
                if it["number"] in addressed:
                    continue  # an open PR is already on it
                items.append(
                    {
                        "repo": name,
                        "number": it["number"],
                        "kind": "issue-triage",
                        "head_sha": "",
                        # Issues have no head SHA, so auto-triage caches against
                        # `updatedAt` instead - it advances on any edit or new
                        # comment and is otherwise stable.
                        "updated_at": it.get("updatedAt", "") or "",
                        "title": it["title"],
                        "author": _author_login(author) or "?",
                        "bucket": "issue-triage",
                        "comp": "n/a",
                        "tests": "n/a",
                        "url": "https://github.com/%s/issues/%d" % (slug, it["number"]),
                        "summary": "open issue, no linked PR",
                        "recommendation": _recommendation("issue-triage"),
                        "priority": PRIORITY["issue-triage"],
                        "auto_triage_issues": triage_issues_enabled,
                        "triage_attempt_cap_per_revision": triage_attempt_cap,
                    }
                )

    warning = "; ".join(
        w
        for w in (
            config_warning(name, repo_cfg.get("compliance_check"), sorted(all_names)),
            pr_scan_warning,
            closing_scan_warning,
            issue_scan_warning,
        )
        if w
    )
    # PRs mid fork-CI approval/run wait this scan: a fresh auto-approve (checks
    # now pending) OR a `ci-running` bucket (approved checks executing, not yet
    # terminal). Both emit no worklist item, so reconcile must NEITHER consume
    # nor refresh-with-stale an existing card for them - it FREEZES the card
    # (approve -> wait-for-terminal -> classify continuity) and refreshes its
    # display to the new head so an old-head card cannot masquerade as current
    # after the scan observes the head change. Author-excluded PRs are omitted so
    # the author-filter self-heal is never blocked. Both keys derive from the one
    # `ci_wait_numbers` source, so they cannot drift.
    enriched_by_number = {p["number"]: p for p in enriched}
    ci_wait_numbers = sorted(
        set(ci_just_approved)
        | {
            p["number"]
            for p in enriched
            if p["bucket"] == "ci-running" and not p["author_excluded"]
        }
    )
    result = {
        "name": name,
        "ok": True,
        "open_pr_numbers": [p["number"] for p in enriched],
        "open_issue_numbers": open_issue_numbers,
        # PRs whose mergeability could not be settled this scan (UNKNOWN did not
        # resolve within the poll budget). They stay in `open_pr_numbers` (still
        # open) but emit no worklist item, and reconcile FREEZES their cards -
        # an UNKNOWN reading must never flip worklist membership or
        # create/close/consume a card.
        "indeterminate_pr_numbers": [
            p["number"] for p in enriched if p["bucket"] == MERGEABILITY_PENDING
        ],
        # Freeze set for the approve/wait window (see above). reconcile skips
        # consuming a PR-kind card whose target number is here, exactly like the
        # indeterminate freeze.
        "ci_wait_pr_numbers": ci_wait_numbers,
        # Anti-masquerade payload: a refresh-ONLY pr-review item per ci_wait PR so
        # reconcile can update an existing stale card to the new head's pending
        # state (never create a card, never queue triage for this revision).
        "ci_wait_refresh_items": [
            _ci_wait_refresh_item(slug, name, enriched_by_number[n], triage_enabled)
            for n in ci_wait_numbers
            if n in enriched_by_number
        ],
        "truncated": pr_truncated or issue_truncated or not closing_scan_complete,
        "warning": warning,
    }
    return (result, items)


# --------------------------------------------------------------------------- #
# state block parsing (shared util)
# --------------------------------------------------------------------------- #
# Cards now WRITE `wheelhouse-state` (see render_card.py), but the legacy
# `triage-state` marker MUST keep parsing: existing open cards in a live machine
# were rendered with it, and they have to stay drivable after the rename. So the
# reader accepts BOTH; only the writer moved to the new name. (When a legacy card
# is next upserted it is re-rendered with the new marker, so the queue migrates
# itself over time.)
_STATE_RE = re.compile(r"<!--\s*(?:wheelhouse|triage)-state:\s*(\{.*?\})\s*-->", re.S)


def parse_state_block(body):
    """Extract the hidden machine-readable state from a decision-card body.

    Accepts the current `wheelhouse-state` marker and the legacy `triage-state`
    marker (back-compat for cards rendered before the rename)."""
    if not body:
        return None
    m = _STATE_RE.search(body)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# cross-repo reference qualification (shared util)
# --------------------------------------------------------------------------- #
# Decision cards live in THIS (cards) repo, but their target is a DIFFERENT
# repo. A bare `#N` in model-generated free text that lands on a card would be
# autolinked by GitHub to an issue/PR in the CARDS repo, not the target - a
# wrong, misleading link. Every surface that renders/posts model free text
# onto a card must rewrite bare refs to `owner/repo#N` via this function
# before the text is displayed. `owner`/`repo` MUST come from deterministic
# card state (`GITHUB_REPOSITORY_OWNER` + the card's `state["repo"]`), never
# from the model's own output - the model only ever supplies `text`.
#
# Match a `#N` only where GitHub itself would autolink it as a same-repo
# reference: at start-of-string, or preceded by a character that is not part
# of an existing `owner/repo#`/`GH-`/word-adjacent-`#` pattern, and not
# followed by another word character (so `#123abc` is left alone). This
# leaves already-qualified `owner/repo#N`, full URLs, markdown-link destination
# URLs, Markdown code, and incidental `#` uses (e.g. a URL fragment
# `page#123`) untouched.
_ISSUE_REF_RE = re.compile(r"(?<![\w/#-])#(\d+)(?!\w)")


def _markdown_link_destination_spans(text):
    spans = []
    i = 0
    while True:
        marker = text.find("](", i)
        if marker < 0:
            return spans
        label_start = text.rfind("[", 0, marker)
        if label_start < 0:
            i = marker + 2
            continue
        start = marker + 2
        depth = 0
        j = start
        while j < len(text):
            ch = text[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    spans.append((start, j))
                    i = j + 1
                    break
                depth -= 1
            j += 1
        else:
            i = marker + 2


def _markdown_reference_link_destination_spans(text):
    spans = []
    pos = 0
    while pos < len(text):
        newline = text.find("\n", pos)
        if newline < 0:
            line_end = len(text)
            next_pos = len(text)
        else:
            line_end = newline
            next_pos = newline + 1
        line = text[pos:line_end]
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if indent <= 3 and stripped.startswith("["):
            close = 1
            while close < len(stripped):
                if stripped[close] == "\\":
                    close += 2
                    continue
                if stripped[close] == "]":
                    break
                close += 1
            if (
                close < len(stripped)
                and close + 1 < len(stripped)
                and stripped[close + 1] == ":"
            ):
                start = pos + indent + close + 2
                while start < line_end and text[start] in " \t":
                    start += 1
                dest_line_end = line_end
                if start >= line_end and newline >= 0:
                    start = next_pos
                    dest_line_newline = text.find("\n", start)
                    if dest_line_newline < 0:
                        dest_line_end = len(text)
                    else:
                        dest_line_end = dest_line_newline
                    while start < dest_line_end and text[start] in " \t":
                        start += 1
                if start < dest_line_end:
                    if text[start] == "<":
                        end = start + 1
                        while end < dest_line_end:
                            if text[end] == "\\":
                                end += 2
                                continue
                            if text[end] == ">":
                                end += 1
                                break
                            end += 1
                    else:
                        end = start
                        while end < dest_line_end and text[end] not in " \t":
                            if text[end] == "\\":
                                end += 2
                            else:
                                end += 1
                    spans.append((start, end))
        pos = next_pos
    return spans


def _markdown_code_span_spans(text):
    spans = []
    i = 0
    while i < len(text):
        if text[i] != "`":
            i += 1
            continue
        start = i
        while i < len(text) and text[i] == "`":
            i += 1
        ticks = i - start
        needle = "`" * ticks
        close = text.find(needle, i)
        while close >= 0:
            before = close > 0 and text[close - 1] == "`"
            after = close + ticks < len(text) and text[close + ticks] == "`"
            if not before and not after:
                spans.append((start, close + ticks))
                i = close + ticks
                break
            close = text.find(needle, close + 1)
        else:
            i = start + ticks
    return spans


def _markdown_fenced_code_spans(text):
    spans = []
    fence = None
    pos = 0
    while pos < len(text):
        newline = text.find("\n", pos)
        if newline < 0:
            line_end = len(text)
        else:
            line_end = newline + 1
        line = text[pos:line_end].rstrip("\r\n")
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if indent <= 3 and stripped:
            char = stripped[0]
            if fence:
                fence_char, fence_len, fence_start = fence
                if char == fence_char:
                    run = 0
                    while run < len(stripped) and stripped[run] == fence_char:
                        run += 1
                    if run >= fence_len and not stripped[run:].strip():
                        spans.append((fence_start, line_end))
                        fence = None
            elif char in ("`", "~"):
                run = 0
                while run < len(stripped) and stripped[run] == char:
                    run += 1
                if run >= 3 and not (char == "`" and "`" in stripped[run:]):
                    fence = (char, run, pos)
        pos = line_end
    if fence:
        spans.append((fence[2], len(text)))
    return spans


def _markdown_indent_width(line):
    width = 0
    for ch in line:
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 4 - (width % 4)
        else:
            break
    return width


def _markdown_indented_code_spans(text):
    spans = []
    pos = 0
    block_start = None
    previous_blank = True
    while pos < len(text):
        newline = text.find("\n", pos)
        if newline < 0:
            line_end = len(text)
            next_pos = len(text)
        else:
            line_end = newline + 1
            next_pos = line_end
        line = text[pos:line_end].rstrip("\r\n")
        blank = not line.strip(" \t")
        indented = not blank and _markdown_indent_width(line) >= 4
        if block_start is not None:
            if not indented and not blank:
                spans.append((block_start, pos))
                block_start = None
            elif indented or blank:
                pos = next_pos
                previous_blank = blank
                continue
        if block_start is None and indented and previous_blank:
            block_start = pos
        previous_blank = blank
        pos = next_pos
    if block_start is not None:
        spans.append((block_start, len(text)))
    return spans


def _merge_spans(spans):
    merged = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _markdown_protected_spans(text):
    return _merge_spans(
        _markdown_link_destination_spans(text)
        + _markdown_reference_link_destination_spans(text)
        + _markdown_code_span_spans(text)
        + _markdown_fenced_code_spans(text)
        + _markdown_indented_code_spans(text)
    )


def qualify_issue_refs(text, owner, repo):
    """Rewrite bare `#N` GitHub-autolink references in `text` to fully
    qualified `owner/repo#N`. Null/empty-safe and idempotent."""
    if not text or not owner or not repo:
        return text or ""
    repl = "%s/%s#\\1" % (owner, repo)
    spans = _markdown_protected_spans(text)
    if not spans:
        return _ISSUE_REF_RE.sub(repl, text)
    qualified = []
    pos = 0
    for start, end in spans:
        qualified.append(_ISSUE_REF_RE.sub(repl, text[pos:start]))
        qualified.append(text[start:end])
        pos = end
    qualified.append(_ISSUE_REF_RE.sub(repl, text[pos:]))
    return "".join(qualified)


# --------------------------------------------------------------------------- #
# security-gated CI approval (ported exit-4 HOLD) + shared safety verdict
# --------------------------------------------------------------------------- #
# `ci_safety` is the ONE security definition. Both the scan-time auto-approve
# path (`build_repo`) and the manual gate (`approve_ci`) consult it, so the auto
# path can never approve something the manual gate would HOLD - it is a strict
# subset. Every read fails CLOSED (unknown -> treated as unsafe).
def _is_not_found(stderr):
    s = (stderr or "").lower()
    return "404" in s or "not found" in s


def _gh_api_capture(path):
    """Raw `gh api <path>` returning the CompletedProcess so the caller can tell
    a 404 (genuinely absent) apart from a read error (must fail closed)."""
    return subprocess.run(["gh", "api", path], capture_output=True, text=True)


def _changed_file_count(value):
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def _list_pr_files(slug, pr, expected_count=None):
    """Return (files, ok, complete). ok/complete=False means the caller fails closed."""
    out = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "/repos/%s/pulls/%s/files" % (slug, pr),
            "--jq",
            ".[].filename",
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return ([], False, False)
    files = [f.strip() for f in out.stdout.splitlines() if f.strip()]
    count = _changed_file_count(expected_count)
    return (files, True, count is not None and len(files) >= count)


def _risky_ci_files(files):
    """Of `files`, the ones whose change makes approving fork CI dangerous:
    approving runs the PR's OWN workflow/action code (the 'pwn request' vector)."""
    risky = []
    for f in files:
        if (
            f.startswith(".github/workflows/")
            or f.startswith(".github/actions/")
            or f.endswith("/action.yml")
            or f.endswith("/action.yaml")
            or f in ("action.yml", "action.yaml")
        ):
            risky.append(f)
    return risky


# GitHub's "List commits on a pull request" REST endpoint returns at most 250
# commits. Card-driven merge must fail closed rather than optimistically merge
# when a larger history cannot be fully inspected for workflow touches.
PR_COMMITS_API_CAP = 250
COMMIT_FILES_API_CAP = 3000


def _workflow_merge_gated_files(files):
    """Of `files`, the ones that require the fine-grained PAT *Workflows* write
    permission to merge via the API.

    Proven: without Workflows write, GitHub returns 403 on
    `PUT .../pulls/{n}/merge` when the PR carries a change under
    `.github/workflows/` - either in the net three-dot diff or in a commit in
    the PR's history (even when the net diff is clean). Callers also pass both
    names of a rename, so a rename into or out of this directory is gated.
    Composite `action.yml`
    and `.github/actions/` paths are Contents-gated, not Workflows-gated, so
    they stay out of this set; `_risky_ci_files` still covers them for fork-CI
    approval safety. Reuse this helper from the card-driven merge path only.
    """
    gated = []
    for f in files or []:
        path = str(f or "")
        if path.startswith(".github/workflows/"):
            gated.append(path)
    return gated


# Auto-merge unconditional file exclusions. A merge-ready PR touching ANY of
# these holds for a human (no LLM involved) - a strict SUPERSET of the
# pwn-request `_risky_ci_files` set, extended into every category the auto-merge
# design marks as never-provably-safe (workflow/action, governance, release,
# dependency/supply-chain, security/auth/credential, billing, migration,
# persistence/schema, install/bootstrap/build entrypoints, public-default
# surfaces, and VISION.md self-authorization). Deliberately generous: a false
# hold is recoverable (the owner just merges manually), a false auto-merge is
# not. Matching is path-segment / suffix based and case-insensitive on the
# filename so it survives odd casing.
_AM_EXCLUDE_SUFFIXES = {
    "dependency": (
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "deno.lock",
        "mix.lock",
        "gradle.lockfile",
        "packages.lock.json",
        "package.resolved",
        "cartfile.resolved",
        "package.swift",
        "go.mod",
        "go.sum",
        "cargo.toml",
        "cargo.lock",
        "pipfile",
        "pipfile.lock",
        "poetry.lock",
        "pyproject.toml",
        "pylock.toml",
        "requirements.txt",
        "uv.lock",
        "gemfile",
        "gemfile.lock",
        "composer.json",
        "composer.lock",
        "gopkg.toml",
        "gopkg.lock",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        ".gitmodules",
    ),
    "release": (
        "release-please-config.json",
        ".release-please-manifest.json",
        "changelog.md",
        ".releaserc",
        ".releaserc.json",
        ".releaserc.yml",
        ".releaserc.yaml",
        ".goreleaser.yml",
        ".goreleaser.yaml",
        "version",
        "version.txt",
    ),
    "security": (
        "security.md",
        ".npmrc",
        ".pypirc",
    ),
    "governance": (
        "codeowners",
        "license",
        "license.md",
        "license.txt",
    ),
    "install-bootstrap": (
        "install.sh",
        "bootstrap.sh",
        "setup.py",
        "setup.cfg",
        "makefile",
    ),
    "vision": ("vision.md",),
}

# Substring signals matched against the whole lowercased path.
_AM_EXCLUDE_PATH_SUBSTRINGS = {
    "security": ("secret", "credential", "password", "private-key", "privatekey"),
    "authentication": ("/auth/", "auth-", "-auth", "oauth", "authn", "authz"),
    "billing": ("billing", "payment", "invoice", "stripe", "subscription", "paywall"),
    "migration": ("migration", "/migrate/", "migrations/", "/alembic/", "flyway"),
}

_AM_EXCLUDE_PATH_COMPONENTS = {
    "security": re.compile(r"(?:^|/)security(?:[._/-]|$)"),
    "authentication": re.compile(
        r"(?:^|/)(?:auth|authentication|authorization|authn|authz|permissions?|iam|rbac|acl|access(?:[._-]?control))(?:[._/-]|$)"
    ),
    "migration": re.compile(
        r"(?:^|/)(?:scripts/)?migrate[^/]*\.(?:py|ts|tsx|js|jsx|mjs|cjs|rb|go|sql)$"
    ),
}

_AM_DEPENDENCY_LOCKFILE_RE = re.compile(
    r"(?:\.lock(?:b|file)?|\.lock\.(?:json|ya?ml|toml|hcl))$"
)
_AM_DEPENDENCY_MANIFEST_RE = re.compile(
    r"(?:requirements|constraints)(?:[-_.][a-z0-9][a-z0-9_.-]*)?\.txt$"
)


def _am_basename(path):
    return path.rsplit("/", 1)[-1]


def _auto_merge_exclusions(files):
    """Return a sorted, de-duplicated list of "category:path" strings for every
    file in `files` that falls in the auto-merge unconditional-exclusion set.
    Empty list means none of the changed files are excluded.

    A PR that touches VISION.md is excluded here (the self-authorization guard)
    on TOP of the base-branch-only read done elsewhere: even reading the base
    copy, a PR editing the rubric it is judged against must never auto-merge."""
    hits = set()
    for raw in files or []:
        f = str(raw or "").strip()
        if not f:
            continue
        low = f.lower()
        base = _am_basename(low)
        # 1) pwn-request CI/action files (reuse the exact existing predicate).
        if _risky_ci_files([f]):
            hits.add("workflow-action:%s" % f)
            continue
        # 2) everything else under .github/ is governance/config surface.
        if low.startswith(".github/"):
            hits.add("governance:%s" % f)
            continue
        # 3) known exact-name / suffix categories.
        matched = False
        for category, names in _AM_EXCLUDE_SUFFIXES.items():
            if base in names or any(low == n or low.endswith("/" + n) for n in names):
                hits.add("%s:%s" % (category, f))
                matched = True
                break
        if matched:
            continue
        if _AM_DEPENDENCY_LOCKFILE_RE.search(
            base
        ) or _AM_DEPENDENCY_MANIFEST_RE.fullmatch(base):
            hits.add("dependency:%s" % f)
            continue
        # 4) dependency directories and other supply-chain / build entrypoints.
        if low.startswith("vendor/") or "/vendor/" in low:
            hits.add("dependency:%s" % f)
            continue
        if base.startswith("dockerfile") or base.startswith("docker-compose"):
            hits.add("install-bootstrap:%s" % f)
            continue
        if base.endswith(".mk") or (base.startswith("setup") and base.endswith(".sh")):
            hits.add("install-bootstrap:%s" % f)
            continue
        if _AM_EXCLUDE_PATH_COMPONENTS["migration"].search(low) or any(
            n in low for n in _AM_EXCLUDE_PATH_SUBSTRINGS["migration"]
        ):
            hits.add("migration:%s" % f)
            continue
        # 5) persistence / schema: raw SQL and schema definition files.
        if (
            low.endswith(".sql")
            or low.endswith(".prisma")
            or base.startswith("schema.")
        ):
            hits.add("persistence:%s" % f)
            continue
        # 6) public-default surfaces: config-schema / defaults files that set
        #    externally observable defaults. Conservative on purpose.
        if (
            base.endswith((".config.js", ".config.ts", ".config.mjs", ".config.cjs"))
            or base
            in ("defaults.yml", "defaults.yaml", "defaults.json", "defaults.toml")
            or low.startswith("config/")
            or "/config/" in low
        ):
            hits.add("public-default:%s" % f)
            continue
        for category, pattern in _AM_EXCLUDE_PATH_COMPONENTS.items():
            if pattern.search(low):
                hits.add("%s:%s" % (category, f))
                break
        else:
            # 7) substring path signals (security/auth/billing/migration).
            for category, needles in _AM_EXCLUDE_PATH_SUBSTRINGS.items():
                if any(n in low for n in needles):
                    hits.add("%s:%s" % (category, f))
                    break
    return sorted(hits)


def _on_triggers(doc):
    """The set of trigger names declared by a parsed workflow doc. Tolerates the
    YAML 1.1 gotcha where the bare key `on:` parses as the boolean True."""
    on = None
    if isinstance(doc, dict):
        if "on" in doc:
            on = doc["on"]
        elif True in doc:  # `on:` parsed as boolean True by PyYAML
            on = doc[True]
    triggers = set()
    if isinstance(on, str):
        triggers.add(on)
    elif isinstance(on, list):
        triggers.update(str(x) for x in on)
    elif isinstance(on, dict):
        triggers.update(str(k) for k in on.keys())
    return triggers


# The supply-chain exploit signature: a workflow that pins a checkout `ref` to
# the PR head. Combined with `pull_request_target` (runs with repo secrets), this
# executes attacker-controlled code with the repo's credentials.
_PR_HEAD_REF_RE = re.compile(
    r"github\.event\.pull_request\.head\.(?:sha|ref)|github\.head_ref"
)


def _is_actions_checkout(name):
    return str(name or "").strip().lower() == "actions/checkout"


def _checks_out_pr_head(doc):
    """True if any job step is an actions/checkout pinning `ref` to the PR head.
    Best-effort but reliable (parses jobs/steps, not free text)."""
    if not isinstance(doc, dict):
        return False
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return False
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            parsed = _parse_uses(step.get("uses"))
            if not parsed or not _is_actions_checkout(parsed[0]):
                continue
            with_ = step.get("with")
            if isinstance(with_, dict) and _PR_HEAD_REF_RE.search(
                str(with_.get("ref") or "")
            ):
                return True
    return False


def _list_workflow_files(slug):
    """Return (paths, status). status in 'ok' (paths listed) / 'none' (no
    .github/workflows dir - genuinely no workflows) / 'error' (read failed - the
    caller must fail closed)."""
    r = _gh_api_capture("/repos/%s/contents/.github/workflows" % slug)
    if r.returncode != 0:
        return ([], "none" if _is_not_found(r.stderr) else "error")
    try:
        entries = json.loads(r.stdout)
    except ValueError:
        return ([], "error")
    if not isinstance(entries, list):  # a file where a dir was expected
        return ([], "none")
    if len(entries) >= 1000:
        return ([], "error")
    paths = []
    for e in entries:
        if isinstance(e, dict) and e.get("type") == "file":
            name = str(e.get("name") or "")
            if name.endswith(".yml") or name.endswith(".yaml"):
                paths.append(e.get("path") or (".github/workflows/" + name))
    return (paths, "ok")


def _fetch_workflow_text(slug, path):
    """Decoded text of one workflow file, or None on any read/decode failure."""
    r = _gh_api_capture("/repos/%s/contents/%s" % (slug, path))
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        return None
    content = data.get("content")
    if content is None:
        return None
    try:
        return base64.b64decode(content).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return None


def repo_pr_target_posture(slug):
    """The source repo's default-branch `pull_request_target` posture, computed
    ONCE per repo (read `.github/workflows/*.yml|*.yaml` via the API; reuse for
    PRs whose base is the repo default branch).

    Returns {pr_target, exploit, error}:
      * pr_target - a default-branch workflow triggers on `pull_request_target`
        (which runs in the repo context WITH secrets).
      * exploit   - one of those workflows also checks out the PR head (the
        pwn-request supply-chain pattern) - flagged loudly, best-effort.
      * error     - a read/parse failure tripped the fail-closed path.
    Fails CLOSED: any unread/unparseable workflow makes pr_target True."""
    if yaml is None:
        return {"pr_target": True, "exploit": False, "error": True}
    paths, status = _list_workflow_files(slug)
    if status == "error":
        return {"pr_target": True, "exploit": False, "error": True}
    if status == "none" or not paths:
        return {"pr_target": False, "exploit": False, "error": False}
    pr_target = False
    exploit = False
    for path in paths:
        text = _fetch_workflow_text(slug, path)
        if text is None:
            return {"pr_target": True, "exploit": False, "error": True}
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            return {"pr_target": True, "exploit": False, "error": True}
        if "pull_request_target" in _on_triggers(doc):
            pr_target = True
            if _checks_out_pr_head(doc):
                exploit = True
    return {"pr_target": pr_target, "exploit": exploit, "error": False}


def ci_safety(slug, pr, repo_posture, changed_files=None):
    """The shared safety verdict for approving a fork PR's awaiting CI run.

    Combines per-PR risky files with the per-repo `pull_request_target` posture
    (`repo_posture`, from `repo_pr_target_posture`, passed in so it is computed
    once per repo, not re-fetched per PR). Returns a dict:
      {safe, error, risky_files, pr_target, exploit, reason}
    `safe` is True only when there are NO risky files, NO pull_request_target
    posture, and NO fail-closed read error - i.e. provably safe to auto-clear."""
    repo_posture = repo_posture or {}
    pr_target = bool(repo_posture.get("pr_target"))
    exploit = bool(repo_posture.get("exploit"))
    posture_error = bool(repo_posture.get("error"))
    non_default_base = bool(repo_posture.get("non_default_base"))
    base_ref = repo_posture.get("base_ref") or ""
    default_branch = repo_posture.get("default_branch") or ""

    files, ok, complete = _list_pr_files(slug, pr, changed_files)
    if not ok:
        risky = ["<could-not-list-files - failing closed>"]
        file_error = True
    elif not complete:
        risky = _risky_ci_files(files) + ["<could-not-list-all-files - failing closed>"]
        file_error = True
    else:
        risky = _risky_ci_files(files)
        file_error = False

    error = file_error or posture_error
    safe = not risky and not pr_target and not error

    if safe:
        reason = "no risky files and no pull_request_target posture"
    else:
        bits = []
        if risky:
            bits.append("touches CI-execution files (%s)" % _display_list(risky))
        if non_default_base:
            bits.append(
                "targets base branch `%s`, not repo default `%s` (failing closed)"
                % (base_ref or "<unknown>", default_branch or "<unknown>")
            )
        elif pr_target:
            bits.append(
                "base branch runs a pull_request_target workflow"
                + (" (workflows unreadable - failing closed)" if posture_error else "")
            )
        if exploit:
            bits.append(
                "a pull_request_target workflow checks out the PR head (pwn-request)"
            )
        reason = "; ".join(bits) or "fail-closed"

    return {
        "safe": safe,
        "error": error,
        "risky_files": risky,
        "pr_target": pr_target,
        "exploit": exploit,
        "reason": reason,
        "non_default_base": non_default_base,
        "base_ref": base_ref,
        "default_branch": default_branch,
    }


def _approve_warning_suffix(verdict):
    """The pull_request_target / exploit warning appended to an approve response
    so the maintainer acts with eyes open (the manual path does NOT block on
    posture - the pull_request_target run fires automatically regardless)."""
    if verdict.get("non_default_base"):
        base = verdict.get("base_ref") or "<unknown>"
        default = verdict.get("default_branch") or "<unknown>"
        return (
            "  NOTE: this PR targets base branch `%s`, but the repo default is `%s`; "
            "Wheelhouse fails closed for non-default bases and this approval only clears "
            "the read-only fork pull_request run." % (base, default)
        )
    if verdict.get("exploit"):
        return (
            "  DANGER: a pull_request_target workflow on the base branch checks out this PR's "
            "head (pwn-request pattern); it runs with repo secrets regardless of this approval - "
            "review the diff before trusting CI."
        )
    if verdict.get("pr_target"):
        return (
            "  NOTE: this repo runs a pull_request_target workflow that executes with repo "
            "secrets and fires automatically regardless of this approval; this approval only "
            "clears the read-only fork pull_request run."
        )
    return ""


# --------------------------------------------------------------------------- #
# CI-approval security summary (advisory, read-only, deterministic).
#
# For a fork PR that touches workflow/action execution files the pwn-request
# HOLD in `approve_ci`/`ci_safety` cards it for manual review, unchanged. This
# builds a focused, deterministic security summary of ONLY those changed
# workflow/action files so the owner can make that SAME manual call faster and
# better-informed. It is presentation/context ONLY:
#   * It never approves CI, never writes to the target, and never touches the
#     hold, the owner gate, the posture logic, or classification.
#   * It reports only structured facts (trigger / permission / secret NAMES,
#     action refs, checkout refs) - never verbatim file lines - so no secret
#     VALUE can leak, and every contributor-derived value is echoed inside an
#     inline-code span at render time.
#   * It fails CLOSED: any read/parse failure yields a "review the diff
#     manually" note and NEVER raises into the scan, so the card still holds.
# --------------------------------------------------------------------------- #
_SECRET_REF_RE = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_-]*)")
_SECRET_BRACKET_REF_RE = re.compile(
    r"secrets\s*\[\s*['\"]([A-Za-z_][A-Za-z0-9_-]*)['\"]\s*\]"
)
_SECRET_DYNAMIC_BRACKET_REF_RE = re.compile(
    r"secrets\s*\[\s*(?!['\"][A-Za-z_][A-Za-z0-9_-]*['\"]\s*\])"
)
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_GITHUB_REPOSITORY_EXPR_RE = re.compile(
    r"^\$\{\{\s*github\.repository\s*\}\}$", re.IGNORECASE
)
CI_SUMMARY_MAX_FILES = 24
CI_SUMMARY_MAX_FLAGS = 48
CI_SUMMARY_MAX_VALUES = 16
CI_SUMMARY_MAX_CHARS = 12000
CI_SECURITY_SUMMARY_VERSION = 1
CI_SECURITY_SUMMARY_HEAD_FIELD = "ci_security_summary_head_sha"
CI_SECURITY_SUMMARY_DIFF_FIELD = "ci_security_summary_diff_revision"
CI_SECURITY_SUMMARY_VERSION_FIELD = "ci_security_summary_version"
CI_SECURITY_SUMMARY_PRESENT_FIELD = "ci_security_summary_present"

# Returned as the summary BODY when the workflow/action changes cannot be
# analyzed deterministically. It keeps the manual review required (fail closed).
CI_SUMMARY_UNANALYZABLE = (
    "Could not analyze the workflow/action changes automatically - review the "
    "diff manually. Approval stays held either way."
)


def _fetch_file_text(slug, path, ref=None):
    """Decoded text of one file at an optional ref, or None on read/decode
    failure. Read-only; used to inspect the PR-head version of a changed
    workflow/action file (the version approving CI would run)."""
    api = "/repos/%s/contents/%s" % (slug, path)
    if ref:
        api += "?ref=%s" % quote(str(ref), safe="")
    r = _gh_api_capture(api)
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        return None
    content = data.get("content")
    if content is None:
        return None
    try:
        return base64.b64decode(content).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return None


def _list_pr_file_changes(slug, pr, expected_count=None):
    """Return (changes, ok, complete) where changes = [{filename, status}, ...].
    ok/complete=False means the caller must fail closed (like `_list_pr_files`)."""
    out = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "/repos/%s/pulls/%s/files" % (slug, pr),
            "--jq",
            ".[] | {filename: .filename, status: .status}",
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return ([], False, False)
    changes = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            return ([], False, False)
        if isinstance(obj, dict) and obj.get("filename"):
            changes.append(
                {
                    "filename": str(obj["filename"]),
                    "status": str(obj.get("status") or ""),
                }
            )
    count = _changed_file_count(expected_count)
    complete = count is not None and len(changes) >= count
    return (changes, True, complete)


def _workflow_steps(doc):
    """Every step dict declared by a workflow (`jobs.*.steps`) or a composite
    action (`runs.steps`), so uses/run detection covers both file kinds."""
    steps = []
    if not isinstance(doc, dict):
        return steps
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job in jobs.values():
            if isinstance(job, dict) and isinstance(job.get("steps"), list):
                steps.extend(s for s in job["steps"] if isinstance(s, dict))
    runs = doc.get("runs")
    if isinstance(runs, dict) and isinstance(runs.get("steps"), list):
        steps.extend(s for s in runs["steps"] if isinstance(s, dict))
    return steps


def _reusable_workflow_uses(doc):
    if not isinstance(doc, dict):
        return []
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []
    return [
        job.get("uses")
        for job in jobs.values()
        if isinstance(job, dict) and job.get("uses") is not None
    ]


def _secret_names(text):
    return sorted(
        set(_SECRET_REF_RE.findall(text)) | set(_SECRET_BRACKET_REF_RE.findall(text))
    )


def _has_dynamic_secret_reference(text):
    return bool(_SECRET_DYNAMIC_BRACKET_REF_RE.search(text))


def _secrets_inherit(doc):
    if not isinstance(doc, dict):
        return False
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return False
    return any(
        isinstance(job, dict)
        and isinstance(job.get("secrets"), str)
        and job["secrets"].strip().lower() == "inherit"
        for job in jobs.values()
    )


def _permission_specs(doc):
    """(scope_label, spec) for top-level and per-job `permissions:` blocks."""
    specs = []
    if not isinstance(doc, dict):
        return specs
    if "permissions" in doc:
        specs.append(("top-level", doc.get("permissions")))
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for name, job in jobs.items():
            if isinstance(job, dict) and "permissions" in job:
                specs.append(("job `%s`" % _safe_inline(name), job.get("permissions")))
    return specs


def _permission_has_write(spec):
    """True if a `permissions:` spec grants any write scope (`write-all` or a
    scope mapped to `write`) - an elevation worth surfacing."""
    if isinstance(spec, str):
        return spec.strip().lower() == "write-all"
    if isinstance(spec, dict):
        for value in spec.values():
            if str(value).strip().lower() == "write":
                return True
    return False


def _format_permission(spec):
    if spec is None:
        return "none (read-only)"
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        return ", ".join("%s: %s" % (k, spec[k]) for k in spec)
    return str(spec)


def _parse_uses(uses):
    """(name, ref, kind) for a step `uses:` value. kind in
    {'local','docker','remote'}; None for an empty value."""
    u = str(uses or "").strip()
    if not u:
        return None
    if u.startswith("./") or u.startswith("../"):
        return (u, "", "local")
    if u.startswith("docker://"):
        return (u, "", "docker")
    if "@" in u:
        name, ref = u.rsplit("@", 1)
    else:
        name, ref = u, ""
    return (name, ref, "remote")


def _classify_action(name, ref, kind, owner):
    """(category, pinned) for one action reference.

    category in {'local','docker','first','third'}; pinned is True only for a
    remote action pinned to a full 40-char commit SHA (mutable tag/branch refs
    and unpinned docker images are the supply-chain risk to surface)."""
    if kind == "local":
        return ("local", False)
    if kind == "docker":
        return ("docker", "@sha256:" in name)
    action_owner = (name.split("/", 1)[0] if "/" in name else name).lower()
    first_party = action_owner in ("actions", "github", str(owner).lower())
    pinned = bool(_FULL_SHA_RE.match(ref))
    return ("first" if first_party else "third", pinned)


def _ci_summary_file_kind(path):
    lower = path.lower()
    if lower.startswith(".github/workflows/") and lower.endswith((".yml", ".yaml")):
        return "workflow"
    if lower.endswith(("/action.yml", "/action.yaml")) or lower in (
        "action.yml",
        "action.yaml",
    ):
        return "action"
    return None


def _action_runtime(doc, file_kind):
    if file_kind != "action":
        return None
    runs = doc.get("runs")
    if not isinstance(runs, dict):
        return {"kind": "unknown", "using": ""}
    using = str(runs.get("using") or "").strip()
    normalized = using.lower()
    if normalized == "composite":
        return {"kind": "composite", "using": using}
    if normalized == "docker":
        return {
            "kind": "docker",
            "using": using,
            "image": str(runs.get("image") or ""),
        }
    if normalized.startswith("node"):
        return {
            "kind": "node",
            "using": using,
            "main": str(runs.get("main") or ""),
        }
    return {"kind": "unknown", "using": using}


def _analyze_ci_file(slug, path, head_sha, status, owner):
    """Deterministic, read-only findings for ONE changed workflow/action file at
    the PR head. Returns a facts dict; on read/parse failure returns a dict with
    `unreadable`/`unparsed` set so the caller can fail closed for that file."""
    file_kind = _ci_summary_file_kind(path)
    if file_kind is None:
        return {"path": path, "status": status, "unanalyzable": True}
    text = _fetch_file_text(slug, path, head_sha)
    if text is None:
        return {"path": path, "status": status, "unreadable": True}
    if yaml is None:
        return {"path": path, "status": status, "unparsed": True}
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return {"path": path, "status": status, "unparsed": True}
    if not isinstance(doc, dict):
        return {"path": path, "status": status, "unparsed": True}

    action_runtime = _action_runtime(doc, file_kind)
    triggers = sorted(_on_triggers(doc))
    perms = _permission_specs(doc)
    secrets = _secret_names(text)
    dynamic_secrets = _has_dynamic_secret_reference(text)
    actions = []
    checkouts = []
    run_steps = 0
    for step in _workflow_steps(doc):
        if step.get("run") is not None:
            run_steps += 1
        parsed = _parse_uses(step.get("uses"))
        if not parsed:
            continue
        name, ref, ukind = parsed
        category, pinned = _classify_action(name, ref, ukind, owner)
        actions.append(
            {
                "name": name,
                "ref": ref,
                "category": category,
                "pinned": pinned,
                "called_workflow": False,
            }
        )
        if _is_actions_checkout(name):
            with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
            checkouts.append(
                {
                    "ref": str(with_.get("ref") or ""),
                    "repository": str(with_.get("repository") or ""),
                }
            )
    for uses in _reusable_workflow_uses(doc):
        parsed = _parse_uses(uses)
        if not parsed:
            continue
        name, ref, ukind = parsed
        category, pinned = _classify_action(name, ref, ukind, owner)
        actions.append(
            {
                "name": name,
                "ref": ref,
                "category": category,
                "pinned": pinned,
                "called_workflow": True,
            }
        )
    return {
        "path": path,
        "status": status,
        "target_repository": slug,
        "triggers": triggers,
        "pr_target": "pull_request_target" in triggers,
        "checks_head": _checks_out_pr_head(doc),
        "permissions": perms,
        "perms_write": any(_permission_has_write(s) for _, s in perms),
        "secrets": secrets,
        "dynamic_secrets": dynamic_secrets,
        "secrets_inherit": _secrets_inherit(doc),
        "checkouts": checkouts,
        "actions": actions,
        "run_steps": run_steps,
        "action_runtime": action_runtime,
        "partially_analyzed": (
            dynamic_secrets
            or (action_runtime is not None and action_runtime["kind"] != "composite")
        )
        or any(
            _checkout_repository_indeterminate(checkout["repository"], slug)
            or (
                not checkout["ref"]
                and _checkout_repository_is_same(checkout["repository"], slug)
                and _checkout_default_event_context(triggers) is None
            )
            for checkout in checkouts
            if not checkout["ref"]
        ),
    }


def _checkout_repository_is_same(repository, target_repository):
    repository = str(repository or "").strip()
    target_repository = str(target_repository or "").strip()
    return (
        not repository
        or bool(_GITHUB_REPOSITORY_EXPR_RE.match(repository))
        or bool(
            target_repository and repository.casefold() == target_repository.casefold()
        )
    )


def _checkout_repository_indeterminate(repository, target_repository):
    repository = str(repository or "").strip()
    return (
        bool(repository)
        and not _checkout_repository_is_same(repository, target_repository)
        and ("${{" in repository or "}}" in repository)
    )


def _checkout_default_event_context(triggers):
    trigger_set = {str(trigger) for trigger in triggers}
    has_pr_target = "pull_request_target" in trigger_set
    has_pr = "pull_request" in trigger_set
    if has_pr_target and not has_pr:
        return "pull_request_target"
    if has_pr and not has_pr_target:
        return "pull_request"
    return None


def _checkout_ref_label(ref, repository="", target_repository="", triggers=()):
    """A human label for a checkout `ref` input. Flags a PR-head ref (the
    pwn-request source) without echoing anything but the expression itself."""
    if not ref:
        if _checkout_repository_is_same(repository, target_repository):
            event_context = _checkout_default_event_context(triggers)
            if event_context == "pull_request_target":
                return (
                    "event default (`GITHUB_SHA`) - `pull_request_target` base branch "
                    "(trusted base code)"
                )
            if event_context == "pull_request":
                return "event default (`GITHUB_SHA`) - contributor PR code"
            return "event-dependent default (`GITHUB_SHA`) - review manually"
        if _checkout_repository_indeterminate(repository, target_repository):
            return (
                "indeterminate repository `%s`; default ref cannot be determined - "
                "review manually" % _safe_inline(repository)
            )
        if repository:
            return "default branch (mutable) of `%s`" % _safe_inline(repository)
        return "event default (`GITHUB_SHA`) - contributor PR code"
    if _PR_HEAD_REF_RE.search(ref):
        return "PR head - `%s`" % _safe_inline(ref)
    return "`%s`" % _safe_inline(ref)


def _summary_flags(analysis):
    """High-severity flags for one file's findings (advisory, most-severe
    first). These are the patterns that let contributor-controlled code run
    with repository privileges or mutate the supply chain."""
    flags = []
    path = _safe_inline(analysis["path"])
    if analysis["pr_target"] and analysis["checks_head"]:
        flags.append(
            "`%s`: runs on `pull_request_target` AND checks out the PR head - "
            "the pwn-request pattern (fork code runs with repo secrets)" % path
        )
    elif analysis["pr_target"]:
        flags.append(
            "`%s`: runs on `pull_request_target`, which executes with repo "
            "secrets even for fork PRs" % path
        )
    if analysis["perms_write"]:
        flags.append("`%s`: grants a write token permission" % path)
    if analysis["secrets_inherit"]:
        flags.append("`%s`: passes `secrets: inherit` to a called workflow" % path)
    if analysis["pr_target"] and analysis["secrets"]:
        flags.append(
            "`%s`: references repository secrets under `pull_request_target` (%s)"
            % (path, ", ".join("`%s`" % _safe_inline(s) for s in analysis["secrets"]))
        )
    for act in analysis["actions"]:
        if act["category"] == "third" and not act["pinned"]:
            reference = (
                "third-party reusable workflow"
                if act.get("called_workflow")
                else "third-party action"
            )
            flags.append(
                "`%s`: %s `%s` is not pinned to a commit SHA"
                % (path, reference, _safe_inline(_uses_display(act)))
            )
        elif act["category"] == "docker" and not act["pinned"]:
            flags.append(
                "`%s`: docker action `%s` is not pinned to a digest"
                % (path, _safe_inline(act["name"]))
            )
    return flags


def _uses_display(act):
    return act["name"] + ("@" + act["ref"] if act["ref"] else "")


def _file_fact_lines(analysis):
    """The per-file advisory fact bullets (deterministic; values code-wrapped)."""
    path = _safe_inline(analysis["path"])
    status = _safe_inline(analysis["status"] or "changed")
    lines = ["- `%s` (%s)" % (path, status)]
    if analysis.get("unreadable"):
        lines.append("  - Could not read this file at the PR head - review manually")
        return lines
    if analysis.get("unparsed"):
        lines.append("  - Could not parse this file as YAML - review manually")
        return lines
    if analysis.get("unanalyzable"):
        lines.append("  - This is not a workflow or action manifest - review manually")
        return lines

    omitted = []
    triggers = analysis["triggers"]
    if triggers:
        shown, extra = _summary_values(triggers)
        trig = ", ".join("`%s`" % _safe_inline(t) for t in shown)
        if extra:
            trig += " (+%d omitted)" % extra
            omitted.append("triggers")
        lines.append("  - Triggers: %s" % trig)
    else:
        lines.append("  - Triggers: none / not a workflow (e.g. composite action)")

    if analysis["permissions"]:
        shown, extra = _summary_values(analysis["permissions"])
        perms = "; ".join(
            "%s -> `%s`" % (label, _safe_inline(_format_permission(spec)))
            for label, spec in shown
        )
        if extra:
            perms += " (+%d omitted)" % extra
            omitted.append("permission blocks")
        lines.append("  - Permissions: %s" % perms)
    else:
        lines.append("  - Permissions: not set (inherits the repo default)")

    if (
        analysis["secrets"]
        or analysis["secrets_inherit"]
        or analysis.get("dynamic_secrets")
    ):
        shown, extra = _summary_values(analysis["secrets"])
        names = ", ".join("`%s`" % _safe_inline(s) for s in shown)
        detail = names
        if extra:
            detail += " (+%d omitted)" % extra
            omitted.append("secret names")
        if analysis.get("dynamic_secrets"):
            detail += (", " if detail else "") + "dynamic/unknown secret reference"
        if analysis["secrets_inherit"]:
            detail += (", " if detail else "") + "`secrets: inherit`"
        lines.append(
            "  - Secrets/token: %s%s"
            % (detail, " - review manually" if analysis.get("dynamic_secrets") else "")
        )
    else:
        lines.append("  - Secrets/token: none referenced")

    if analysis["checkouts"]:
        counts = []  # preserve order, collapse identical labels with a count
        shown, extra = _summary_values(analysis["checkouts"])
        for co in shown:
            label = _checkout_ref_label(
                co["ref"],
                co["repository"],
                analysis.get("target_repository", ""),
                analysis.get("triggers", ()),
            )
            if co["repository"] and co["ref"]:
                label += " from `%s`" % _safe_inline(co["repository"])
            for entry in counts:
                if entry[0] == label:
                    entry[1] += 1
                    break
            else:
                counts.append([label, 1])
        parts = [lab + (" (x%d)" % n if n > 1 else "") for lab, n in counts]
        checkout = "; ".join(parts)
        if extra:
            checkout += " (+%d omitted)" % extra
            omitted.append("checkout steps")
        lines.append("  - Checkout: %s" % checkout)
    else:
        lines.append("  - Checkout: no explicit `actions/checkout` step")

    runtime = analysis.get("action_runtime")
    if runtime and runtime["kind"] == "docker":
        using = _safe_inline(runtime["using"])
        image = _safe_inline(runtime["image"] or "not declared")
        lines.append("  - Docker action runtime: `%s`, image `%s`" % (using, image))
        lines.append(
            "  - Action runtime is not fully analyzed automatically - review manually"
        )
    elif runtime and runtime["kind"] == "node":
        using = _safe_inline(runtime["using"])
        main = _safe_inline(runtime["main"] or "not declared")
        lines.append(
            "  - JavaScript action runtime: `%s`, entrypoint `%s`" % (using, main)
        )
        lines.append(
            "  - Action runtime is not fully analyzed automatically - review manually"
        )
    elif runtime and runtime["kind"] == "unknown":
        using = _safe_inline(runtime["using"] or "not declared")
        lines.append(
            "  - Action runtime `%s` is missing or unrecognized - review manually"
            % using
        )

    third = [a for a in analysis["actions"] if a["category"] in ("third", "docker")]
    local = [a for a in analysis["actions"] if a["category"] == "local"]
    if third:
        shown, extra = _summary_values(third)
        parts = []
        for a in shown:
            pin = "SHA-pinned" if a["pinned"] else "NOT SHA-pinned"
            parts.append("`%s` (%s)" % (_safe_inline(_uses_display(a)), pin))
        if extra:
            parts.append("+%d omitted" % extra)
            omitted.append("third-party actions")
        lines.append("  - Third-party actions/workflows: %s" % ", ".join(parts))
    elif analysis.get("partially_analyzed"):
        lines.append(
            "  - Third-party actions/workflows: not fully analyzed - review manually"
        )
    else:
        lines.append("  - Third-party actions/workflows: none (first-party only)")
    if local:
        shown, extra = _summary_values(local)
        parts = ", ".join("`%s`" % _safe_inline(a["name"]) for a in shown)
        if extra:
            parts += ", +%d omitted" % extra
            omitted.append("local actions")
        lines.append("  - Local actions (contributor-controlled code): %s" % parts)

    if analysis["run_steps"]:
        lines.append(
            "  - Run steps: %d (execute checked-out code)" % analysis["run_steps"]
        )
    if omitted:
        lines.append(
            "  - Additional %s omitted to keep this card concise - review the diff manually"
            % ", ".join(omitted)
        )
    return lines


def _summary_values(values):
    """The displayed prefix of a fact list and how many values were omitted."""
    shown = values[:CI_SUMMARY_MAX_VALUES]
    return (shown, len(values) - len(shown))


def _bounded_ci_security_summary(lines):
    """Keep the advisory body comfortably below GitHub's issue-body limit."""
    text = "\n".join(lines)
    if len(text) <= CI_SUMMARY_MAX_CHARS:
        return text
    note = (
        "\n\n_Note: This automated summary was truncated to keep the card concise "
        "- review the full diff manually._"
    )
    prefix = text[: CI_SUMMARY_MAX_CHARS - len(note)].rsplit("\n", 1)[0]
    return prefix + note


def _format_ci_security_summary(analyses, complete, omitted_files=0):
    """Assemble the advisory findings body from per-file analyses."""
    flags = []
    for a in analyses:
        if not (a.get("unreadable") or a.get("unparsed") or a.get("unanalyzable")):
            flags.extend(_summary_flags(a))
    incomplete = not complete or any(
        a.get("unreadable")
        or a.get("unparsed")
        or a.get("unanalyzable")
        or a.get("partially_analyzed")
        for a in analyses
    )
    shown_flags = flags[:CI_SUMMARY_MAX_FLAGS]
    omitted_flags = len(flags) - len(shown_flags)

    lines = []
    if incomplete:
        lines.append(
            "**Automated analysis incomplete - review the full diff manually.**"
        )
    elif shown_flags:
        lines.append("**Flags (most-severe first):**")
        lines.extend("- %s" % f for f in shown_flags)
    elif omitted_files:
        lines.append("**Flags:** none in the summarized files - review the full diff.")
    else:
        lines.append(
            "**Flags:** none detected by the automated scan - still review the diff."
        )
    lines.append("")
    lines.append("**Changed workflow/action files:**")
    for a in analyses:
        lines.extend(_file_fact_lines(a))
    if incomplete:
        lines.append(
            "- _Note: automated analysis was incomplete; review the full diff manually._"
        )
    elif omitted_files:
        lines.append(
            "- _Note: %d changed workflow/action file%s omitted to keep this card concise "
            "- review the full diff manually._"
            % (omitted_files, "s" if omitted_files != 1 else "")
        )
    if omitted_flags:
        lines.append(
            "- _Note: %d additional flag%s omitted to keep this card concise - review "
            "the full diff manually._"
            % (omitted_flags, "s" if omitted_flags != 1 else "")
        )
    return _bounded_ci_security_summary(lines)


def ci_security_summary(slug, pr, head_sha, changed_files=None):
    """Advisory, read-only security summary of the workflow/action files a fork
    PR changes, for the CI-approval HOLD card (see the section comment above).

    Returns a markdown findings string, `""` when the PR changes no
    workflow/action execution file (nothing to summarize), or the
    `CI_SUMMARY_UNANALYZABLE` note when the changes cannot be analyzed. NEVER
    raises, NEVER approves, NEVER writes to the target repo."""
    try:
        owner = slug.split("/", 1)[0]
        changes, ok, complete = _list_pr_file_changes(slug, pr, changed_files)
        if not ok or not complete:
            return CI_SUMMARY_UNANALYZABLE
        risky = [c for c in changes if _risky_ci_files([c["filename"]])]
        if not risky:
            return ""
        analyses = [
            _analyze_ci_file(slug, c["filename"], head_sha, c["status"], owner)
            for c in risky[:CI_SUMMARY_MAX_FILES]
        ]
        if any(
            a.get("unreadable") or a.get("unparsed") or a.get("unanalyzable")
            for a in analyses
        ):
            return CI_SUMMARY_UNANALYZABLE
        return _format_ci_security_summary(
            analyses, complete, omitted_files=len(risky) - len(analyses)
        )
    except Exception as e:  # never let a summary bug break the scan
        print(
            "::warning::wheelhouse ci security summary error for %s#%s: %s"
            % (slug, pr, str(e)[:160]),
            file=sys.stderr,
        )
        return CI_SUMMARY_UNANALYZABLE


def _security_summary_from_card_body(body):
    heading = "### Security review (advisory)"
    start = (body or "").find(heading)
    if start < 0:
        return None
    after_heading = body[start + len(heading) :].lstrip("\n")
    _, separator, after_notice = after_heading.partition("\n\n")
    if not separator:
        return None
    summary, _, _ = after_notice.partition("\n### ")
    summary = summary.rstrip()
    return summary or None


def _ci_security_summary_diff_revision(pr):
    base_ref = str(pr.get("base_ref") or "").strip()
    base_sha = str(pr.get("base_sha") or "").strip()
    if not base_ref or not base_sha:
        return ""
    return json.dumps([base_ref, base_sha], separators=(",", ":"))


def ci_security_summary_cache(cards):
    """Build a best-effort cache of verified current CI summary sections from
    Wheelhouse cards. The cache key includes the card's target labels, head SHA,
    base-diff revision, and summary version so stale or user-lookalike cards are
    ignored. Cache misses merely re-run the read-only analysis; they never
    influence CI approval or scan routing."""
    cache = {}
    for card in cards or []:
        state = parse_state_block(card.get("body", ""))
        if (
            not isinstance(state, dict)
            or state.get("kind") != "ci-approval"
            or state.get(CI_SECURITY_SUMMARY_VERSION_FIELD)
            != CI_SECURITY_SUMMARY_VERSION
        ):
            continue
        repo = state.get("repo")
        number = state.get("number")
        head_sha = state.get("head_sha")
        diff_revision = state.get(CI_SECURITY_SUMMARY_DIFF_FIELD)
        if (
            not repo
            or not head_sha
            or not isinstance(diff_revision, str)
            or not diff_revision
        ):
            continue
        try:
            key = (str(repo), int(number))
        except (TypeError, ValueError):
            continue
        labels = set(_label_names_from_issue(card))
        if not {
            "repo:%s" % key[0],
            "kind:ci-approval",
            "target:%s-%s" % key,
        }.issubset(labels):
            continue
        if state.get(CI_SECURITY_SUMMARY_HEAD_FIELD) != head_sha:
            continue
        present = bool(state.get(CI_SECURITY_SUMMARY_PRESENT_FIELD))
        summary = _security_summary_from_card_body(card.get("body", ""))
        if present and summary is None:
            continue
        if not present:
            summary = ""
        cache[key] = {
            "head_sha": str(head_sha),
            "diff_revision": diff_revision,
            "summary": summary,
        }
    return cache


def _cached_ci_security_summary(cache, repo, pr, head_sha, diff_revision):
    entry = (cache or {}).get((repo, int(pr)))
    if (
        entry
        and diff_revision
        and entry.get("head_sha") == (head_sha or "")
        and entry.get("diff_revision") == diff_revision
    ):
        return (True, entry.get("summary") or "")
    return (False, "")


def _attach_ci_security_summary(item, slug, pr, cache=None):
    """Attach the advisory read-only security summary to a ci-approval hold card
    item. Display-only: it never approves, never writes, and never affects
    routing or the hold. A failure falls back to the manual-review note."""
    head_sha = pr.get("head_sha", "") or ""
    diff_revision = _ci_security_summary_diff_revision(pr)
    cached, summary = _cached_ci_security_summary(
        cache, item.get("repo", ""), pr["number"], head_sha, diff_revision
    )
    if not cached:
        try:
            summary = ci_security_summary(
                slug, pr["number"], head_sha, pr.get("changed_files")
            )
        except Exception as e:  # defense in depth - ci_security_summary already guards
            print(
                "::warning::wheelhouse ci security summary error %s#%s: %s"
                % (slug, pr.get("number"), str(e)[:160]),
                file=sys.stderr,
            )
            summary = CI_SUMMARY_UNANALYZABLE
    item[CI_SECURITY_SUMMARY_HEAD_FIELD] = head_sha
    item[CI_SECURITY_SUMMARY_DIFF_FIELD] = diff_revision
    item[CI_SECURITY_SUMMARY_VERSION_FIELD] = CI_SECURITY_SUMMARY_VERSION
    item[CI_SECURITY_SUMMARY_PRESENT_FIELD] = bool(summary)
    if summary:
        item["security_summary"] = summary


def _safe_inline(value, limit=160):
    """Sanitize a contributor-derived value for safe display inside a single
    inline-code span: collapse whitespace, neutralize backticks (so it cannot
    break out of the span into markdown/HTML), and truncate. Purely cosmetic -
    the summary never echoes verbatim file lines or secret values."""
    s = re.sub(r"\s+", " ", str(value)).strip()
    s = s.replace("`", "'")
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def _workflow_run_matches_pr(slug, run_id, pr, head_sha, head_ref):
    """Verify a candidate action_required workflow run belongs to the PR.

    GitHub usually fills workflow_run.pull_requests, which must contain exactly
    the target PR. For fork-originated action_required runs GitHub may return an
    empty list, so the fallback binding is the already-filtered run detail's
    exact head SHA plus head branch.
    """
    r = _gh_api_capture("/repos/%s/actions/runs/%s" % (slug, run_id))
    if r.returncode != 0:
        return (None, "run detail fetch failed: %s" % r.stderr.strip()[:160])
    try:
        run = json.loads(r.stdout)
    except ValueError:
        return (None, "run detail returned invalid JSON")
    run_head = str(run.get("head_sha") or "")
    if run_head != str(head_sha):
        return (False, "head %s" % (run_head[:12] or "<missing>"))
    prs = run.get("pull_requests")
    if not isinstance(prs, list):
        return (None, "run detail returned unexpected pull_requests")
    numbers = []
    for item in prs:
        if not isinstance(item, dict) or item.get("number") is None:
            return (None, "run detail returned unexpected pull_requests")
        numbers.append(str(item.get("number")))
    if not numbers:
        run_branch = str(run.get("head_branch") or "")
        if run_branch != str(head_ref):
            return (False, "branch %s" % (run_branch or "<missing>"))
        # GitHub leaves workflow_run.pull_requests empty for fork-originated
        # action_required runs. The list query is already filtered by branch,
        # commit, and status; matching a 40-char head SHA plus head branch is
        # the strong run-to-PR binding available for the fork case this gate
        # exists to serve.
        return (True, "")
    if len(numbers) != 1:
        return (None, "run detail has %d pull request associations" % len(numbers))
    if numbers[0] != str(pr):
        return (False, "not PR #%s" % pr)
    return (True, "")


def _list_action_required_runs(slug, head_ref, head_sha):
    """List a complete exact-head page of pending workflow runs, or an error.

    Callers must treat any error as unknown state. This helper is discovery
    only: `approve_ci` separately fetches the live PR and verifies every listed
    run through `_workflow_run_matches_pr` before acting.
    """
    head_ref = str(head_ref or "")
    head_sha = str(head_sha or "")
    if not head_ref or not head_sha:
        return (None, "missing head ref/sha")
    result = subprocess.run(
        [
            "gh",
            "run",
            "list",
            "--branch",
            head_ref,
            "--commit",
            head_sha,
            "--status",
            "action_required",
            "--limit",
            str(ACTION_REQUIRED_RUN_LIMIT),
            "-R",
            slug,
            "--json",
            "databaseId,workflowDatabaseId,workflowName,headSha,headBranch,url",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return (None, "workflow run list failed: %s" % result.stderr.strip()[:160])
    if not result.stdout.strip():
        return (None, "workflow run list returned no output")
    try:
        runs = json.loads(result.stdout)
    except ValueError:
        return (None, "workflow run list returned invalid JSON")
    if not isinstance(runs, list):
        return (None, "workflow run list returned unexpected data")
    if len(runs) >= ACTION_REQUIRED_RUN_LIMIT:
        return (
            None,
            "workflow run list returned %d runs (limit %d); refusing a possibly "
            "truncated list" % (len(runs), ACTION_REQUIRED_RUN_LIMIT),
        )
    for run in runs:
        if not isinstance(run, dict) or not run.get("databaseId"):
            return (None, "workflow run list returned an entry without databaseId")
    return (runs, "")


def approve_ci(owner, repo, pr, posture=None, strict=False):
    """Approve fork-PR workflow runs awaiting maintainer approval.

    `posture` (from `repo_pr_target_posture`) is passed by the scan-time auto path
    to avoid re-reading the repo's workflows; the manual path leaves it None and
    it is computed here. The security verdict is `ci_safety` - the SAME definition
    the auto path uses. With `strict=True`, approval-time safety is re-read and
    any non-safe verdict blocks approval.
    Each action_required run must also verify against the PR head: populated
    pull_requests associations stay strict, while fork-originated empty
    associations are accepted only on matching head SHA plus head branch.
    Every verified current-head run is approved, including multiple runs with
    the same workflow identity when GitHub exposes each as actionable.

    Returns (status, message). status in:
      approved - one or more runs approved
      noop     - nothing awaiting approval
      hold     - SECURITY HOLD (PR changes CI-execution files / files unreadable) - NOT approved
      error    - could not act
    """
    slug = "%s/%s" % (owner, repo)
    pj = subprocess.run(
        ["gh", "api", "/repos/%s/pulls/%s" % (slug, pr)], capture_output=True, text=True
    )
    if pj.returncode != 0:
        return ("error", "pr fetch failed: %s" % pj.stderr.strip()[:160])
    try:
        pr_data = json.loads(pj.stdout)
    except ValueError:
        return ("error", "pr fetch returned invalid JSON")
    head = pr_data.get("head") or {}
    base = pr_data.get("base") or {}
    head_ref = str(head.get("ref") or "")
    head_sha = str(head.get("sha") or "")
    if not head_ref or not head_sha:
        return ("error", "pr fetch returned missing head ref/sha")
    changed_files = pr_data.get("changed_files")
    base_ref = str(base.get("ref") or "")
    default_branch = str(((base.get("repo") or {}).get("default_branch")) or "")

    base_posture = _non_default_base_posture(base_ref, default_branch)
    if base_posture is not None:
        posture = base_posture
    elif strict or posture is None:
        posture = repo_pr_target_posture(slug)
    verdict = ci_safety(slug, pr, posture, changed_files)

    # Risky CI-execution files (or an unreadable file list) -> HARD HOLD,
    # unchanged. A pull_request_target posture does NOT hard-block the manual
    # path; it only adds a warning (see _approve_warning_suffix).
    if verdict["risky_files"]:
        return (
            "hold",
            "SECURITY HOLD: #%s changes CI-execution files - NOT auto-approving. Approving fork "
            "CI would run the PR's OWN workflow/action code with repo perms. Needs manual review: %s"
            % (pr, _display_list(verdict["risky_files"])),
        )

    warn = _approve_warning_suffix(verdict)
    if strict and not verdict["safe"]:
        return (
            "error",
            "#%s (%s@%s): strict auto-approval blocked by approval-time "
            "safety verdict: %s%s"
            % (pr, head_ref, head_sha[:8], verdict.get("reason") or "not safe", warn),
        )

    runs, list_error = _list_action_required_runs(slug, head_ref, head_sha)
    if list_error:
        return (
            "error",
            "#%s (%s@%s): %s%s" % (pr, head_ref, head_sha[:8], list_error, warn),
        )
    if not runs:
        return (
            "noop",
            "#%s (%s@%s): no workflow runs awaiting approval%s"
            % (pr, head_ref, head_sha[:8], warn),
        )

    matching = []
    skipped = []
    for run in runs:
        name = run.get("workflowName", "?")
        match, reason = _workflow_run_matches_pr(
            slug, run["databaseId"], pr, head_sha, head_ref
        )
        if match is None:
            return (
                "error",
                "#%s (%s@%s): workflow run %s could not be verified: %s%s"
                % (pr, head_ref, head_sha[:8], name, reason, warn),
            )
        if match:
            matching.append(run)
        else:
            skipped.append("%s:%s" % (name, reason))

    if not matching:
        msg = "#%s (%s@%s): no matching workflow runs awaiting approval" % (
            pr,
            head_ref,
            head_sha[:8],
        )
        if skipped:
            msg += " (skipped %d unrelated run(s): %s)" % (
                len(skipped),
                ", ".join(skipped)[:240],
            )
        return ("noop", msg + warn)

    done = []
    failed = []
    for run in matching:
        rid = run["databaseId"]
        ar = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                "/repos/%s/actions/runs/%s/approve" % (slug, rid),
            ],
            capture_output=True,
            text=True,
        )
        name = run.get("workflowName", "?")
        if ar.returncode == 0:
            done.append("%s:OK" % name)
        else:
            done.append("%s:FAIL" % name)
            failed.append(
                "%s:%s" % (name, ar.stderr.strip()[:160] or "approval failed")
            )
    if failed:
        return (
            "error",
            "#%s (%s@%s): approved %d/%d matching run(s), failed [%s] [%s]%s"
            % (
                pr,
                head_ref,
                head_sha[:8],
                len(matching) - len(failed),
                len(matching),
                ", ".join(failed),
                ", ".join(done),
                warn,
            ),
        )
    return (
        "approved",
        "#%s (%s@%s): approved %d matching run(s) [%s]%s"
        % (pr, head_ref, head_sha[:8], len(matching), ", ".join(done), warn),
    )


# --------------------------------------------------------------------------- #
# fleet-scan health ledger
# --------------------------------------------------------------------------- #
# A repo that intermittently fails the fleet scan (ok:false) is invisible to the
# rest of the pipeline for that run and self-heals on the next successful scan.
# But a repo that fails EVERY scan (e.g. an oversized query GitHub keeps 5xxing)
# stays dark indefinitely and hides behind an otherwise-green scheduled run.
# This ledger persists a per-repo consecutive-failure count across runs (state
# lives in GitHub, not on disk: a dedicated closed issue in THIS cards repo,
# carrying a hidden `wheelhouse-scan-health` marker) so the backstop can raise a
# loud, run-failing signal once a repo has been dark for several scans in a row.
# It is bookkeeping for THIS repo, so it is written with the default GITHUB_TOKEN,
# never FLEET_TOKEN.
SCAN_HEALTH_MARKER = "wheelhouse-scan-health"
SCAN_HEALTH_LABEL = "wheelhouse:scan-health"
SCAN_HEALTH_TITLE = "Wheelhouse fleet-scan health (automated)"
# Consecutive ok:false scans before the backstop shouts. Hourly scans -> 3 in a
# row is ~3h of darkness. Overridable via env for a fork that scans on a
# different cadence.
SCAN_HEALTH_ALERT_THRESHOLD = 3
_SCAN_HEALTH_RE = re.compile(
    r"<!--\s*%s:\s*(\{.*?\})\s*-->" % re.escape(SCAN_HEALTH_MARKER), re.S
)


def _scan_health_threshold():
    raw = os.environ.get("WHEELHOUSE_SCAN_HEALTH_THRESHOLD", "").strip()
    if raw:
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            pass
    return SCAN_HEALTH_ALERT_THRESHOLD


def parse_scan_health(body):
    """Extract the persisted per-repo consecutive-failure map from the health
    issue body. Returns {} for a missing/blank/unparseable ledger."""
    if not body:
        return {}
    m = _SCAN_HEALTH_RE.search(body)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except (ValueError, TypeError):
        return {}
    repos = data.get("repos") if isinstance(data, dict) else None
    return repos if isinstance(repos, dict) else {}


def _prev_failure_count(prev_entry):
    if isinstance(prev_entry, dict):
        try:
            return int(prev_entry.get("consecutive_failures", 0))
        except (TypeError, ValueError):
            return 0
    if isinstance(prev_entry, bool):  # guard: bool is an int subclass
        return 0
    if isinstance(prev_entry, int):
        return prev_entry
    return 0


def update_scan_health(prev, repos, threshold=SCAN_HEALTH_ALERT_THRESHOLD):
    """Pure health-ledger step. Given the previous per-repo failure counts and
    the current scan's `repos` result map, return (new_counts, alerts).

    - ok:true  -> the repo's consecutive-failure count resets to 0.
    - ok:false -> the count increments; if it reaches `threshold` the repo is
      added to `alerts`.
    Repos present in `prev` but absent from this scan are carried forward
    unchanged (a partial/single-repo scan must not wipe the fleet's history) and
    never alert (they were not observed this pass). `alerts` is sorted by name for
    deterministic output."""
    prev = prev if isinstance(prev, dict) else {}
    new_counts = {}
    for name, entry in prev.items():
        # Normalize carried-forward entries so the stored shape stays uniform.
        new_counts[name] = {"consecutive_failures": _prev_failure_count(entry)}
        if isinstance(entry, dict) and entry.get("last_warning"):
            new_counts[name]["last_warning"] = str(entry["last_warning"])[:200]
    alerts = []
    for name in sorted(repos or {}):
        result = repos.get(name)
        if not isinstance(result, dict):
            result = {}
        if result.get("ok"):
            new_counts[name] = {"consecutive_failures": 0}
            continue
        count = _prev_failure_count(prev.get(name)) + 1
        entry = {"consecutive_failures": count}
        warning = str(result.get("warning") or "").strip()
        if warning:
            entry["last_warning"] = warning[:200]
        new_counts[name] = entry
        if count >= threshold:
            alerts.append({"name": name, "count": count, "warning": warning})
    return new_counts, alerts


def render_scan_health_body(counts, updated_at=""):
    """Render the health-ledger issue body: a short human summary plus the hidden
    machine-readable marker."""
    counts = counts if isinstance(counts, dict) else {}
    dark = sorted(
        (n for n, e in counts.items() if _prev_failure_count(e) > 0),
        key=lambda n: (-_prev_failure_count(counts[n]), n),
    )
    lines = [
        "Automated ledger for the Wheelhouse fleet-scan backstop - do not edit by "
        "hand.",
        "",
        "It records how many consecutive scheduled scans each fleet repo has "
        "failed (`ok:false`) so a persistently-unreadable repo cannot hide behind "
        "an otherwise-green scan.",
        "",
    ]
    if dark:
        lines.append("Currently failing:")
        for name in dark:
            entry = counts[name] if isinstance(counts[name], dict) else {}
            note = ": %s" % entry["last_warning"] if entry.get("last_warning") else ""
            lines.append(
                "- `%s` - %d consecutive scan failure(s)%s"
                % (name, _prev_failure_count(entry), note)
            )
    else:
        lines.append("All scanned fleet repos are currently readable.")
    lines.append("")
    ledger = {"updated_at": updated_at or "", "repos": counts}
    lines.append(
        "<!-- %s: %s -->"
        % (SCAN_HEALTH_MARKER, json.dumps(ledger, separators=(",", ":")))
    )
    return "\n".join(lines)


def _this_repo_slug():
    slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not slug or "/" not in slug:
        sys.exit("GITHUB_REPOSITORY not set (owner/name) for scan-health")
    return slug


def _find_scan_health_issue(slug):
    """Locate the marker-owned health-ledger issue in any state."""
    path = "repos/%s/issues?state=all&labels=%s&per_page=100" % (
        slug,
        quote(SCAN_HEALTH_LABEL),
    )
    issues = _flatten_paginated_comments(gh_rest(path, paginate=True, slurp=True))
    for it in issues:
        if not isinstance(it, dict) or "pull_request" in it:
            continue
        if _SCAN_HEALTH_RE.search(it.get("body") or ""):
            return it
    return None


def _create_scan_health_issue(slug, body):
    # gh_rest can't emit the `labels[]` array the create endpoint wants, so this
    # one call goes direct. Created labeled, then closed so it stays out of the
    # owner's open-issue queue while remaining findable by label.
    _ensure_repo_label(slug, SCAN_HEALTH_LABEL)
    r = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            "repos/%s/issues" % slug,
            "-f",
            "title=" + SCAN_HEALTH_TITLE,
            "-f",
            "body=" + body,
            "-f",
            "labels[]=" + SCAN_HEALTH_LABEL,
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            "create scan-health issue failed: %s" % (r.stderr.strip() or "gh error")
        )
    issue = json.loads(r.stdout)
    number = issue.get("number")
    if number:
        gh_rest(
            "repos/%s/issues/%s" % (slug, number),
            method="PATCH",
            fields={"state": "closed"},
        )
    return issue


def cmd_scan_health(scan_path):
    """Update the persisted per-repo consecutive-failure ledger from a scan.json
    and raise a loud, run-failing `::error::` for any repo dark past the
    threshold. Fails OPEN on any ledger I/O error so health bookkeeping can never
    turn a scan red on its own hiccup."""
    threshold = _scan_health_threshold()
    try:
        with open(scan_path) as f:
            payload = json.load(f)
    except (OSError, ValueError) as e:
        # No usable scan output (e.g. the scan step itself failed, which already
        # fails the job) - nothing to record.
        print(
            "::warning::scan-health: could not read %s: %s" % (scan_path, str(e)[:160]),
            file=sys.stderr,
        )
        return
    repos = payload.get("repos") if isinstance(payload, dict) else None
    repos = repos if isinstance(repos, dict) else {}
    updated_at = payload.get("generated_at", "") if isinstance(payload, dict) else ""

    alerts = []
    try:
        slug = _this_repo_slug()
        issue = _find_scan_health_issue(slug)
        prev = parse_scan_health(issue.get("body") if issue else None)
        new_counts, alerts = update_scan_health(prev, repos, threshold)
        body = render_scan_health_body(new_counts, updated_at)
        if issue and issue.get("number"):
            gh_rest(
                "repos/%s/issues/%s" % (slug, issue["number"]),
                method="PATCH",
                fields={"body": body, "state": "closed"},
            )
        else:
            _create_scan_health_issue(slug, body)
    except SystemExit:
        raise
    except Exception as e:
        # Ledger persistence failed - do not fail the scan over bookkeeping.
        print(
            "::warning::scan-health ledger update failed: %s" % str(e)[:200],
            file=sys.stderr,
        )
        return

    for a in alerts:
        note = ": %s" % a["warning"] if a.get("warning") else ""
        print(
            "::error::wheelhouse fleet-scan: %s has failed %d consecutive scans%s"
            % (a["name"], a["count"], note),
            file=sys.stderr,
        )
    if alerts:
        # Fail the (final) backstop step so a persistently-dark repo cannot hide
        # behind a green scheduled run. Reconcile has already run in an earlier
        # step, so failing here never skips self-healing for the healthy repos.
        sys.exit("fleet-scan health: %d repo(s) dark past threshold" % len(alerts))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def _load_ci_security_summary_cache(path):
    if not path:
        return {}
    try:
        with open(path) as f:
            cards = json.load(f)
    except (OSError, ValueError) as e:
        print(
            "::warning::wheelhouse could not load CI security summary cache %s: %s"
            % (path, str(e)[:160]),
            file=sys.stderr,
        )
        return {}
    return ci_security_summary_cache(cards if isinstance(cards, list) else [])


def cmd_scan(only_repo=None, cards_path=None):
    owner = get_owner()
    cfg = load_config()
    repos = cfg["repos"]
    if only_repo:
        if only_repo not in repos:
            sys.exit(
                "unknown repo '%s' (configured: %s)" % (only_repo, ", ".join(repos))
            )
        names = [only_repo]
    else:
        names = list(repos)

    summary_cache = _load_ci_security_summary_cache(cards_path)
    out_repos = {}
    items = []
    for name in names:
        result, repo_items = build_repo(
            owner,
            repos[name],
            cfg["card_issues"],
            cfg["auto_approve_ci"],
            cfg["auto_triage"],
            cfg["auto_triage_issues"],
            cfg["pending_contributor_cleanup"],
            cfg["pending_contributor_cleanup_days"],
            cfg["pending_contributor_reminder_days"],
            cfg["pending_contributor_cleanup_targets"],
            summary_cache,
            cfg["auto_merge"],
            cfg["triage_attempt_cap_per_revision"],
        )
        out_repos[name] = result
        items.extend(repo_items)
        if result.get("warning"):
            # `build_repo` already prefixes the repo slug onto the ok:false "scan
            # failed" warning; other warnings still name the repo here so a dark
            # or degraded repo is always identifiable from the scan log.
            warning = result["warning"]
            if not result.get("ok"):
                print("::warning::%s" % warning, file=sys.stderr)
            else:
                print("::warning::%s: %s" % (name, warning), file=sys.stderr)

    payload = {
        "owner": owner,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "card_issues": cfg["card_issues"],
        "auto_approve_ci": cfg["auto_approve_ci"],
        "auto_merge": cfg["auto_merge"],
        "auto_triage": cfg["auto_triage"],
        "auto_triage_issues": cfg["auto_triage_issues"],
        "triage_attempt_cap_per_revision": cfg["triage_attempt_cap_per_revision"],
        "triage_daily_ceiling": cfg["triage_daily_ceiling"],
        "pending_contributor_cleanup": cfg["pending_contributor_cleanup"],
        "repos": out_repos,
        "items": items,
    }
    print(json.dumps(payload, indent=2))


def cmd_approve_ci(repo, pr):
    owner = get_owner()
    cfg = load_config()
    if repo not in cfg["repos"]:
        sys.exit("unknown repo '%s'" % repo)
    status, message = approve_ci(owner, repo, pr)
    print(message)
    if status == "hold":
        sys.exit(4)  # distinct exit: blocked for security review
    if status == "error":
        sys.exit(1)


def cmd_checks(repo):
    owner = get_owner()
    cfg = load_config()
    if repo not in cfg["repos"]:
        sys.exit("unknown repo '%s'" % repo)
    rc = cfg["repos"][repo]
    data = gh_graphql(owner, rc["name"])
    comp = rc.get("compliance_check")
    pats = rc.get("test_check_patterns", []) or []
    names = set()
    prs, complete = _page_open_prs(owner, rc["name"], data["pullRequests"])
    if not complete:
        sys.exit("could not read every open PR; refusing incomplete check-name list")
    for pr in prs:
        _, _, _, n = check_status(pr, rc)
        names.update(n)
    print("check names on %s (compliance_check=%r):" % (repo, comp))
    for n in sorted(names):
        tag = (
            "  <- COMPLIANCE"
            if (comp and n == comp)
            else ("  <- test" if any(p in n for p in pats) else "")
        )
        print("  %s%s" % (n, tag))
    w = config_warning(repo, comp, sorted(names))
    if w:
        print("!! " + w)


def maintainers():
    """The set of logins allowed to drive decisions: the repo owner (from
    $OWNER / $GITHUB_REPOSITORY_OWNER) plus the optional configured `maintainer`.

    This is the SINGLE source of truth for "who is the maintainer" - the gate
    (`authorized`), the natural-language conversation-history filter, and the
    scan author filter all use it, so trusted-author rules never drift apart."""
    owner = (
        os.environ.get("OWNER", "") or os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    ).strip()
    maintainer = ""
    try:
        maintainer = load_config()["maintainer"]
    except SystemExit:
        pass
    return {x for x in (owner, maintainer) if x}


def cmd_authorized():
    """Print true/false: may $SENDER drive decisions on this machine?"""
    sender = os.environ.get("SENDER", "").strip()
    print("true" if sender and sender in maintainers() else "false")


def cmd_repos():
    cfg = load_config()
    for name, rc in cfg["repos"].items():
        print(
            "%-20s gate=%s tests=%s"
            % (name, rc.get("compliance_check"), rc.get("test_check_patterns"))
        )


def cmd_nl_decisions_enabled():
    print("true" if load_config()["nl_decisions"] else "false")


def cmd_auto_triage_enabled(repo):
    cfg = load_config()
    if repo not in cfg["repos"]:
        sys.exit("unknown repo '%s'" % repo)
    print(
        "true"
        if _auto_triage_enabled(cfg["repos"][repo], cfg["auto_triage"])
        else "false"
    )


def cmd_auto_triage_issues_enabled(repo):
    cfg = load_config()
    if repo not in cfg["repos"]:
        sys.exit("unknown repo '%s'" % repo)
    print(
        "true"
        if _auto_triage_issues_enabled(cfg["repos"][repo], cfg["auto_triage_issues"])
        else "false"
    )


def cmd_thank_on_merge_enabled(repo):
    cfg = load_config()
    if repo not in cfg["repos"]:
        sys.exit("unknown repo '%s'" % repo)
    print(
        "true"
        if _thank_on_merge_enabled(cfg["repos"][repo], cfg["thank_on_merge"])
        else "false"
    )


def cmd_auto_merge_enabled(repo):
    cfg = load_config()
    if repo not in cfg["repos"]:
        sys.exit("unknown repo '%s'" % repo)
    print(
        "true"
        if _auto_merge_enabled(cfg["repos"][repo], cfg["auto_merge"])
        else "false"
    )


def cmd_state(field):
    """Print one field of the state block in $ISSUE_BODY (for the deep-review workflow)."""
    st = parse_state_block(os.environ.get("ISSUE_BODY", ""))
    print((st or {}).get(field, ""))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "scan":
        args = sys.argv[2:]
        cards_path = None
        if "--cards" in args:
            index = args.index("--cards")
            if index + 1 >= len(args):
                sys.exit(__doc__)
            cards_path = args[index + 1]
            del args[index : index + 2]
        if len(args) > 1:
            sys.exit(__doc__)
        cmd_scan(args[0] if args else None, cards_path)
    elif cmd == "scan-health" and len(sys.argv) == 3:
        cmd_scan_health(sys.argv[2])
    elif cmd == "approve-ci" and len(sys.argv) == 4:
        cmd_approve_ci(sys.argv[2], sys.argv[3])
    elif cmd == "checks" and len(sys.argv) == 3:
        cmd_checks(sys.argv[2])
    elif cmd == "authorized":
        cmd_authorized()
    elif cmd == "nl-decisions-enabled":
        cmd_nl_decisions_enabled()
    elif cmd == "auto-triage-enabled" and len(sys.argv) == 3:
        cmd_auto_triage_enabled(sys.argv[2])
    elif cmd == "auto-triage-issues-enabled" and len(sys.argv) == 3:
        cmd_auto_triage_issues_enabled(sys.argv[2])
    elif cmd == "thank-on-merge-enabled" and len(sys.argv) == 3:
        cmd_thank_on_merge_enabled(sys.argv[2])
    elif cmd == "auto-merge-enabled" and len(sys.argv) == 3:
        cmd_auto_merge_enabled(sys.argv[2])
    elif cmd == "state" and len(sys.argv) == 3:
        cmd_state(sys.argv[2])
    elif cmd == "repos":
        cmd_repos()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
