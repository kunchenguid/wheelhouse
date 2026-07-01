#!/usr/bin/env python3
"""
Wheelhouse - deterministic brain (ported from the local OSS-triage machinery).

Runs inside GitHub Actions. One GraphQL query per repo fetches every open
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

This is the GHA port of `data/triage/triage.py`. What the Actions model
replaces has been dropped: the local single-flight lock (-> Actions
`concurrency`), the lavish board and nudge-ledger (-> issues/labels/comments as
state), per-repo `owner` (-> derived from github.repository_owner).

Usage:
  wheelhouse_core.py scan                 scan all configured repos -> JSON worklist; may auto-approve safe fork CI, nudge conflicted PR-review candidates, and log outcomes
  wheelhouse_core.py scan <repo>          scan a single configured repo; may auto-approve safe fork CI, nudge conflicted PR-review candidates, and log outcomes
  wheelhouse_core.py approve-ci <repo> <pr>   security-gated fork-CI approval (exit 4 = HOLD)
  wheelhouse_core.py checks <repo>        list distinct check names on a repo's PRs (onboarding)
  wheelhouse_core.py authorized           print true/false: is $SENDER allowed to drive decisions?
  wheelhouse_core.py nl-decisions-enabled print true/false: is nl_decisions on in config?
  wheelhouse_core.py state <field>        print one field of the state block in $ISSUE_BODY
  wheelhouse_core.py repos                list configured repos

Owner is derived from $GITHUB_REPOSITORY_OWNER (or --owner). Cross-repo reads
and fork-CI approvals use the ambient GH_TOKEN (set to FLEET_TOKEN by the
calling workflow step).
"""

import base64
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

try:
    import yaml
except ImportError:  # pragma: no cover - workflows `pip install pyyaml` first
    sys.exit("PyYAML is required (pip install pyyaml)")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Config search order: repo root, then .github/.
CONFIG_CANDIDATES = [
    os.path.join(ROOT, "wheelhouse.config.yml"),
    os.path.join(ROOT, "wheelhouse.config.yaml"),
    os.path.join(ROOT, ".github", "wheelhouse.config.yml"),
    os.path.join(ROOT, ".github", "wheelhouse.config.yaml"),
]

GQL = """
query($owner:String!, $name:String!) {
  repository(owner:$owner, name:$name) {
    defaultBranchRef { name }
    pullRequests(states:OPEN, first:100, orderBy:{field:UPDATED_AT, direction:DESC}) {
      totalCount
      nodes {
        number title isDraft updatedAt changedFiles isCrossRepository mergeable
        author { login __typename }
        headRefName headRefOid baseRefName
        headRepository { name owner { login } }
        baseRepository { name owner { login } }
        labels(first:20){ nodes{ name } }
        closingIssuesReferences(first:10){ nodes{ number } }
        commits(last:1){ nodes{ commit{ statusCheckRollup{
          state
          contexts(first:100){ nodes{
            __typename
            ... on CheckRun { name conclusion status }
            ... on StatusContext { context state }
          }}
        }}}}
      }
    }
    issues(states:OPEN, first:100, orderBy:{field:UPDATED_AT, direction:DESC}) {
      totalCount
      nodes { number title updatedAt author{login __typename} labels(first:20){nodes{name}} }
    }
  }
}
"""

# Buckets that need the maintainer's call vs. ones waiting on the contributor.
NEEDS_MAINTAINER = {"merge-ready", "needs-ci-approval", "review-needed"}
# (waiting-on-contributor: needs-reraise, needs-rebase, fix-tests, draft, ci-running)
REBASE_NUDGE_MARKER_PREFIX = "wheelhouse-rebase-nudge"

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


# --------------------------------------------------------------------------- #
# config + owner
# --------------------------------------------------------------------------- #
def config_path():
    for p in CONFIG_CANDIDATES:
        if os.path.exists(p):
            return p
    sys.exit("no wheelhouse.config.yml found (looked in repo root and .github/)")


def load_config():
    with open(config_path()) as f:
        cfg = yaml.safe_load(f) or {}
    repos = cfg.get("repos") or []
    by_name = {}
    for r in repos:
        if isinstance(r, dict) and r.get("name"):
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
    }


def get_owner():
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    if not owner:
        sys.exit("owner not set (GITHUB_REPOSITORY_OWNER missing)")
    return owner


# --------------------------------------------------------------------------- #
# gh wrappers (ambient GH_TOKEN, set per-step by the workflow)
# --------------------------------------------------------------------------- #
def gh_graphql(owner, name):
    r = subprocess.run(
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
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "gh graphql failed")
    data = json.loads(r.stdout)
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"]))
    return data["data"]["repository"]


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
def check_status(pr, cfg):
    """Return (compliance, tests, ci_present, names).

    compliance in pass/fail/pending/missing/n/a/none; tests in green/fail/pending/none.
    """
    commits = pr["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    if not rollup or not rollup["contexts"]["nodes"]:
        return ("none", "none", False, [])
    comp_name = cfg.get("compliance_check")
    patterns = cfg.get("test_check_patterns", []) or []
    compliance = "missing" if comp_name else "n/a"
    tests = []
    names = []
    for c in rollup["contexts"]["nodes"]:
        if c["__typename"] == "CheckRun":
            name = c.get("name") or ""
            names.append(name)
            concl = (c.get("conclusion") or "").upper()
            status = (c.get("status") or "").upper()
            done = status == "COMPLETED" or status == ""
            if comp_name and name == comp_name:
                compliance = (
                    "pass"
                    if concl == "SUCCESS"
                    else "fail"
                    if concl
                    in (
                        "FAILURE",
                        "TIMED_OUT",
                        "CANCELLED",
                        "ACTION_REQUIRED",
                        "STARTUP_FAILURE",
                    )
                    else "pending"
                )
            elif any(p in name for p in patterns):
                tests.append(
                    "pass"
                    if (done and concl == "SUCCESS")
                    else "fail"
                    if (done and concl in ("FAILURE", "TIMED_OUT", "CANCELLED"))
                    else "pending"
                )
        else:  # StatusContext
            ctx = c.get("context") or ""
            names.append(ctx)
            st = (c.get("state") or "").upper()
            if comp_name and ctx == comp_name:
                compliance = (
                    "pass"
                    if st == "SUCCESS"
                    else "fail"
                    if st in ("FAILURE", "ERROR")
                    else "pending"
                )
            elif any(p in ctx for p in patterns):
                tests.append(
                    "pass"
                    if st == "SUCCESS"
                    else "pending"
                    if st == "PENDING"
                    else "fail"
                )
    if not tests:
        tstate = "none"
    elif "fail" in tests:
        tstate = "fail"
    elif "pending" in tests:
        tstate = "pending"
    else:
        tstate = "green"
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


def _author_login(author):
    if not isinstance(author, dict):
        return ""
    return str(author.get("login") or "").strip()


def _author_typename(author):
    if not isinstance(author, dict):
        return ""
    return str(author.get("__typename") or "").strip()


def _author_is_bot(author):
    typename = _author_typename(author)
    login = _author_login(author)
    return typename == "Bot" or login.casefold().endswith("[bot]")


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


def _has_rebase_nudge(comments, head_sha):
    marker = _rebase_nudge_marker(head_sha)
    for comment in _flatten_paginated_comments(comments):
        if not isinstance(comment, dict):
            continue
        if marker in str(comment.get("body") or ""):
            return True
    return False


def _rebase_nudge_body(repo, number, head_sha):
    marker = _rebase_nudge_marker(head_sha)
    short = str(head_sha or "").strip()[:8] or "current head"
    return (
        "Thanks for the PR. Wheelhouse found that this branch currently has a "
        "merge conflict with the base branch, so it is stepping out of the "
        "maintainer queue for now.\n\n"
        "Please rebase on or merge the base branch into your branch, resolve "
        "the conflict, and push the result. Once GitHub reports the PR as "
        "mergeable again, Wheelhouse will resurface it for maintainer review.\n\n"
        "<sub>Conflict noted for %s#%s at `%s`.</sub>\n"
        "%s"
        % (repo, number, short, marker)
    )


def _post_rebase_nudge_if_needed(slug, repo, number, head_sha):
    comments = gh_rest(
        "/repos/%s/issues/%s/comments?per_page=100" % (slug, number),
        paginate=True,
        slurp=True,
    )
    if _has_rebase_nudge(comments, head_sha):
        return False
    gh_rest(
        "/repos/%s/issues/%s/comments" % (slug, number),
        method="POST",
        fields={"body": _rebase_nudge_body(repo, number, head_sha)},
    )
    return True


def _maybe_nudge_rebase(slug, repo, pr):
    try:
        posted = _post_rebase_nudge_if_needed(
            slug, repo, pr["number"], pr.get("head_sha")
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
            "default branch, so it fails closed for manual review."
            % (base, default)
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

    Returns (handled, card_note, log_note) where:
      * handled=True  -> the run was auto-approved OR there is no pending run to
        approve; emit NO card. `card_note` is unused (None) and `log_note` is
        the audit line for the scan-step `::notice::`.
      * handled=False -> return a card fallback; `card_note` is the safety warning
        to surface on the card body (may be "", left EXACTLY as before), and
        `log_note` is the per-PR outcome line for the scan-step `::warning::`.
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
            return (True, None, "auto-approved (%s): %s" % (reason, message))
        if status == "noop":
            return (
                True,
                None,
                "verdict safe (%s); approve_ci noop: %s" % (reason, message),
            )
        # hold / error -> fall through to caller fallback (fail-closed), keeping the why.
        card_note = "auto-approve did not complete (%s: %s)" % (status, message)
        safety_note = _ci_safety_note(verdict)
        if safety_note:
            card_note += "; " + safety_note
        log_note = "verdict safe (%s); approve_ci %s: %s" % (reason, status, message)
        return (False, card_note, log_note)
    # Auto-approve disabled, or an unsafe verdict -> caller fallback; no approve attempted.
    log_note = "verdict %s (%s); not auto-approved%s" % (
        "safe" if verdict["safe"] else "unsafe",
        reason,
        "" if auto_enabled else " (auto-approve disabled)",
    )
    return (False, _ci_safety_note(verdict), log_note)


def build_repo(owner, repo_cfg, card_issues, auto_approve_ci=True):
    """Scan one repo. Returns (repo_result, items).

    Decision cards are for other people's work, so scan-built PR-review and
    issue-triage items skip known owner/maintainer/bot authors. Missing author
    metadata fails open.

    `auto_approve_ci` is the fleet-wide default (config `auto_approve_ci`, itself
    defaulting True); a repo may override it per-repo. Same-repo PRs with no CI
    signal route to normal review, not CI approval. Unknown fork status keeps a
    manual CI-approval card with no auto-approve attempt for contributor-authored
    PRs and logs a suppressed card for owner/maintainer/bot-authored PRs. When
    enabled, or when the author is excluded from the decision queue, a fork PR
    whose `ci_safety` verdict is provably safe is approved here (in the
    FLEET_TOKEN scan context), or verified as having no pending run, and emits NO
    card; risky/uncertain contributor PRs still become cards while excluded-author
    PRs only log suppressed-card warnings. Each handled ci-approval PR also emits
    exactly one stderr notice/warning outcome line.
    Conflicted PR-review candidates become `needs-rebase`: no decision card is
    emitted, and contributor-authored PRs get at most one rebase nudge per head
    SHA via a hidden comment marker. This runs only on the ok:true success path
    below, so an ok:false repo (early return) is never auto-approved or nudged."""
    name = repo_cfg["name"]
    slug = "%s/%s" % (owner, name)
    try:
        data = gh_graphql(owner, name)
    except (
        Exception
    ) as e:  # resilient: a missing/unreadable repo does not abort the scan
        return (
            {
                "name": name,
                "ok": False,
                "warning": "scan failed: %s" % str(e)[:200],
                "open_pr_numbers": [],
                "open_issue_numbers": [],
            },
            [],
        )

    prs = data["pullRequests"]["nodes"]
    issues = data["issues"]["nodes"]
    default_branch = ((data.get("defaultBranchRef") or {}).get("name") or "").strip()
    maintainer_logins = {login.casefold() for login in maintainers()}
    all_names = set()
    enriched = []
    closing = {}  # issue -> [pr numbers]
    for pr in prs:
        author = pr.get("author") or {}
        comp, tests, ci, names = check_status(pr, repo_cfg)
        all_names.update(names)
        cross_repo = _pr_is_cross_repo(pr)
        bucket = classify(
            pr["isDraft"], comp, tests, ci, cross_repo, pr.get("mergeable")
        )
        author_excluded = _author_excluded_from_queue(author, maintainer_logins)
        closes = [i["number"] for i in pr["closingIssuesReferences"]["nodes"]]
        for i in closes:
            closing.setdefault(i, []).append(pr["number"])
        enriched.append(
            {
                "number": pr["number"],
                "title": pr["title"],
                "author": _author_login(author) or "?",
                "author_excluded": author_excluded,
                "comp": comp,
                "tests": tests,
                "ci": ci,
                "bucket": bucket,
                "closes": closes,
                "head_sha": pr["headRefOid"],
                "changed_files": pr.get("changedFiles"),
                "base_ref": pr.get("baseRefName"),
                "cross_repo": cross_repo,
            }
        )
        if bucket == "needs-rebase" and not author_excluded:
            _maybe_nudge_rebase(slug, name, enriched[-1])

    open_issue_numbers = [it["number"] for it in issues]
    addressed = {n for n in closing if n in set(open_issue_numbers)}

    auto_enabled = _auto_approve_enabled(repo_cfg, auto_approve_ci)
    default_posture = None

    items = []
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
            "title": pr["title"],
            "author": pr["author"],
            "bucket": pr["bucket"],
            "comp": pr["comp"],
            "tests": pr["tests"],
            "url": "https://github.com/%s/pull/%d" % (slug, pr["number"]),
            "summary": summary,
            "recommendation": _recommendation(pr["bucket"]),
            "priority": priority,
        }

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
            handled, card_note, log_note = _auto_approve_or_card(
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

        items.append(item)

    if card_issues:
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
                    "title": it["title"],
                    "author": _author_login(author) or "?",
                    "bucket": "issue-triage",
                    "comp": "n/a",
                    "tests": "n/a",
                    "url": "https://github.com/%s/issues/%d" % (slug, it["number"]),
                    "summary": "open issue, no linked PR",
                    "recommendation": _recommendation("issue-triage"),
                    "priority": PRIORITY["issue-triage"],
                }
            )

    warning = config_warning(name, repo_cfg.get("compliance_check"), sorted(all_names))
    result = {
        "name": name,
        "ok": True,
        "open_pr_numbers": [p["number"] for p in enriched],
        "open_issue_numbers": open_issue_numbers,
        "truncated": data["pullRequests"]["totalCount"] > len(prs)
        or data["issues"]["totalCount"] > len(issues),
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
            if "actions/checkout" not in str(step.get("uses") or ""):
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

    run_list_limit = 30
    lst = subprocess.run(
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
            str(run_list_limit),
            "-R",
            slug,
            "--json",
            "databaseId,workflowName,headSha,headBranch,url",
        ],
        capture_output=True,
        text=True,
    )
    if lst.returncode != 0:
        return (
            "error",
            "#%s (%s@%s): workflow run list failed: %s%s"
            % (pr, head_ref, head_sha[:8], lst.stderr.strip()[:160], warn),
        )
    if not lst.stdout.strip():
        return (
            "error",
            "#%s (%s@%s): workflow run list returned no output%s"
            % (pr, head_ref, head_sha[:8], warn),
        )
    try:
        runs = json.loads(lst.stdout)
    except ValueError:
        return (
            "error",
            "#%s (%s@%s): workflow run list returned invalid JSON%s"
            % (pr, head_ref, head_sha[:8], warn),
        )
    if not isinstance(runs, list):
        return (
            "error",
            "#%s (%s@%s): workflow run list returned unexpected data%s"
            % (pr, head_ref, head_sha[:8], warn),
        )
    if len(runs) >= run_list_limit:
        return (
            "error",
            "#%s (%s@%s): workflow run list returned %d runs (limit %d); "
            "refusing to approve a possibly truncated list%s"
            % (pr, head_ref, head_sha[:8], len(runs), run_list_limit, warn),
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
        if not isinstance(run, dict) or not run.get("databaseId"):
            return (
                "error",
                "#%s (%s@%s): workflow run list returned an entry without databaseId%s"
                % (pr, head_ref, head_sha[:8], warn),
            )
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
# commands
# --------------------------------------------------------------------------- #
def cmd_scan(only_repo=None):
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

    out_repos = {}
    items = []
    for name in names:
        result, repo_items = build_repo(
            owner, repos[name], cfg["card_issues"], cfg["auto_approve_ci"]
        )
        out_repos[name] = result
        items.extend(repo_items)
        if result.get("warning"):
            print("::warning::%s" % result["warning"], file=sys.stderr)

    payload = {
        "owner": owner,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "card_issues": cfg["card_issues"],
        "auto_approve_ci": cfg["auto_approve_ci"],
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
    for pr in data["pullRequests"]["nodes"]:
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


def cmd_state(field):
    """Print one field of the state block in $ISSUE_BODY (for the deep-review workflow)."""
    st = parse_state_block(os.environ.get("ISSUE_BODY", ""))
    print((st or {}).get(field, ""))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "scan":
        cmd_scan(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "approve-ci" and len(sys.argv) == 4:
        cmd_approve_ci(sys.argv[2], sys.argv[3])
    elif cmd == "checks" and len(sys.argv) == 3:
        cmd_checks(sys.argv[2])
    elif cmd == "authorized":
        cmd_authorized()
    elif cmd == "nl-decisions-enabled":
        cmd_nl_decisions_enabled()
    elif cmd == "state" and len(sys.argv) == 3:
        cmd_state(sys.argv[2])
    elif cmd == "repos":
        cmd_repos()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
