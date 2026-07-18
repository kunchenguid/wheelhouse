#!/usr/bin/env python3
"""
Unit-exercise scan author filtering and PR updatedAt propagation with NO network.

Run: python tests/test_author_filter.py
"""

import io
import os
import sys
from contextlib import redirect_stderr

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)
import wheelhouse_core as core  # noqa: E402

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def check_run(name, conclusion="SUCCESS", status="COMPLETED"):
    return {
        "__typename": "CheckRun",
        "name": name,
        "conclusion": conclusion,
        "status": status,
    }


def green_rollup():
    return {
        "state": "SUCCESS",
        "contexts": {"nodes": [check_run("Gate"), check_run("test")]},
    }


MISSING = object()


def pr_node(
    number,
    author=None,
    status_rollup=MISSING,
    cross_repo=False,
    closes=None,
    closing_total=None,
    closing_page_info=None,
):
    if status_rollup is MISSING:
        status_rollup = green_rollup()
    node = {
        "number": number,
        "title": "PR %d" % number,
        "isDraft": False,
        "isCrossRepository": cross_repo,
        "updatedAt": "2026-01-01T00:00:00Z",
        "changedFiles": 1,
        "author": author,
        "headRefName": "feature-%d" % number,
        "headRefOid": "sha%d" % number,
        "baseRefName": "main",
        "headRepository": {"name": "demo-fork", "owner": {"login": "forker"}},
        "baseRepository": {"name": "demo", "owner": {"login": "owner"}},
        "labels": {"nodes": []},
        "closingIssuesReferences": {
            "totalCount": len(closes or []),
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"number": i} for i in (closes or [])],
        },
        "commits": {"nodes": [{"commit": {"statusCheckRollup": status_rollup}}]},
    }
    if closing_total is not None:
        node["closingIssuesReferences"]["totalCount"] = closing_total
    if closing_page_info is not None:
        node["closingIssuesReferences"]["pageInfo"] = closing_page_info
    if cross_repo is False:
        node["headRepository"] = {"name": "demo", "owner": {"login": "owner"}}
    elif cross_repo == "missing":
        node.pop("isCrossRepository")
        node.pop("headRepository")
    return node


def needs_ci_pr(number, author):
    return pr_node(number, author=author, status_rollup=None, cross_repo=True)


def issue_node(number, author=None):
    return {
        "number": number,
        "title": "Issue %d" % number,
        "updatedAt": "2026-01-01T00:00:00Z",
        "author": author,
        "labels": {"nodes": []},
    }


def graphql_data(pr_nodes=None, issue_nodes=None, pr_total=None, pr_page_info=None):
    pr_nodes = list(pr_nodes or [])
    issue_nodes = list(issue_nodes or [])
    pull_requests = {
        "totalCount": len(pr_nodes) if pr_total is None else pr_total,
        "nodes": pr_nodes,
    }
    if pr_page_info is not None:
        pull_requests["pageInfo"] = pr_page_info
    return {
        "defaultBranchRef": {"name": "main"},
        "pullRequests": pull_requests,
        "issues": {"totalCount": len(issue_nodes), "nodes": issue_nodes},
    }


def run_build_repo(
    pr_nodes=None,
    issue_nodes=None,
    *,
    card_issues=False,
    auto_approve_ci=True,
    repo_auto_approve_ci=None,
    approve_result=("error", "api fail"),
    ci_safety_result=None,
):
    calls = {"approve": []}
    repo_cfg = {
        "name": "demo",
        "compliance_check": "Gate",
        "test_check_patterns": ["test"],
    }
    if repo_auto_approve_ci is not None:
        repo_cfg["auto_approve_ci"] = repo_auto_approve_ci

    def fake_graphql(owner, name):
        return graphql_data(pr_nodes, issue_nodes)

    def fake_load_config():
        return {
            "repos": {"demo": repo_cfg},
            "maintainer": "co-maintainer",
            "nl_decisions": False,
            "card_issues": card_issues,
            "auto_approve_ci": auto_approve_ci,
        }

    def fake_approve(owner, name, pr, posture=None, strict=False):
        calls["approve"].append(pr)
        return approve_result

    save = (
        core.gh_graphql,
        core.load_config,
        core.repo_pr_target_posture,
        core.ci_safety,
        core.approve_ci,
        core.ci_security_summary,
        core._list_action_required_runs,
        os.environ.get("OWNER"),
        os.environ.get("GITHUB_REPOSITORY_OWNER"),
    )
    core.gh_graphql = fake_graphql
    core.load_config = fake_load_config
    # Keep offline: the advisory summarizer would otherwise read the target repo.
    core.ci_security_summary = lambda slug, pr, head_sha, changed_files=None: "SEC"
    core.repo_pr_target_posture = lambda slug: {
        "pr_target": False,
        "exploit": False,
        "error": False,
    }
    safe_verdict = {
        "safe": True,
        "error": False,
        "risky_files": [],
        "pr_target": False,
        "exploit": False,
        "reason": "clean",
    }
    core.ci_safety = lambda slug, pr, posture, changed_files=None: (
        safe_verdict if ci_safety_result is None else ci_safety_result
    )
    core.approve_ci = fake_approve
    core._list_action_required_runs = lambda slug, head_ref, head_sha: ([], "")
    os.environ["OWNER"] = "owner"
    os.environ["GITHUB_REPOSITORY_OWNER"] = "owner"
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            result, items = core.build_repo(
                "owner", repo_cfg, card_issues, auto_approve_ci=auto_approve_ci
            )
    finally:
        (
            core.gh_graphql,
            core.load_config,
            core.repo_pr_target_posture,
            core.ci_safety,
            core.approve_ci,
            core.ci_security_summary,
            core._list_action_required_runs,
            old_owner,
            old_repo_owner,
        ) = save
        if old_owner is None:
            os.environ.pop("OWNER", None)
        else:
            os.environ["OWNER"] = old_owner
        if old_repo_owner is None:
            os.environ.pop("GITHUB_REPOSITORY_OWNER", None)
        else:
            os.environ["GITHUB_REPOSITORY_OWNER"] = old_repo_owner
    calls["stderr"] = err.getvalue()
    return result, items, calls


OWNER = {"login": "owner", "__typename": "User"}
MAINTAINER = {"login": "co-maintainer", "__typename": "User"}
BOT_TYPE = {"login": "release-please", "__typename": "Bot"}
BOT_SUFFIX = {"login": "dependabot[bot]", "__typename": "User"}
HUMAN = {"login": "contributor", "__typename": "User"}


def test_pr_author_filter_skips_owner_maintainer_and_bots():
    prs = [
        pr_node(1, author=OWNER),
        pr_node(2, author=MAINTAINER),
        pr_node(3, author=BOT_TYPE),
        pr_node(4, author=BOT_SUFFIX),
        pr_node(5, author=HUMAN),
        pr_node(6, author=None),
    ]
    result, items, calls = run_build_repo(prs)
    numbers = [it["number"] for it in items]
    check("author-filter: owner PR skipped", 1 not in numbers)
    check("author-filter: configured maintainer PR skipped", 2 not in numbers)
    check("author-filter: Bot typename PR skipped", 3 not in numbers)
    check("author-filter: [bot] suffix PR skipped", 4 not in numbers)
    check("author-filter: human contributor PR still carded", 5 in numbers)
    check("author-filter: unknown PR author fails open", 6 in numbers)
    check(
        "author-filter: skipped PRs stay open for reconcile self-heal",
        result["open_pr_numbers"] == [1, 2, 3, 4, 5, 6],
    )
    check("author-filter: non-CI PRs do not invoke approve_ci", calls["approve"] == [])


def test_pr_items_carry_updated_at_for_activity_sort():
    pr = pr_node(5, author=HUMAN)
    pr["updatedAt"] = "2026-02-03T04:05:06Z"
    result, items, calls = run_build_repo([pr])
    check("activity-sort: PR item is emitted", len(items) == 1)
    check(
        "activity-sort: PR item carries target updatedAt",
        items and items[0].get("updated_at") == "2026-02-03T04:05:06Z",
    )


def test_ci_approval_author_filter_preserves_safe_auto_approve():
    prs = [
        needs_ci_pr(10, OWNER),
        needs_ci_pr(11, BOT_TYPE),
        needs_ci_pr(12, HUMAN),
    ]
    result, items, calls = run_build_repo(prs, approve_result=("approved", "ok"))
    numbers = [it["number"] for it in items]
    check("author-filter: owner ci-approval PR skipped", 10 not in numbers)
    check("author-filter: bot ci-approval PR skipped", 11 not in numbers)
    check("author-filter: approved human ci-approval PR skipped", 12 not in numbers)
    check(
        "author-filter: approve_ci considered every safe ci-approval PR",
        calls["approve"] == ["10", "11", "12"],
    )
    check(
        "author-filter: safe approvals still emit notices",
        calls["stderr"].count("::notice::demo#") == 3,
    )


def test_ci_approval_author_filter_suppresses_cards_after_approve_failure():
    prs = [
        needs_ci_pr(20, OWNER),
        needs_ci_pr(21, BOT_TYPE),
        needs_ci_pr(22, HUMAN),
    ]
    result, items, calls = run_build_repo(prs)
    numbers = [it["number"] for it in items]
    check("author-filter: failed owner ci-approval card suppressed", 20 not in numbers)
    check("author-filter: failed bot ci-approval card suppressed", 21 not in numbers)
    check("author-filter: failed human ci-approval PR still carded", numbers == [22])
    check(
        "author-filter: approve_ci still attempted excluded safe PRs",
        calls["approve"] == ["20", "21", "22"],
    )
    check(
        "author-filter: excluded approve failures still log warnings",
        "suppressed-card demo#20" in calls["stderr"]
        and "suppressed-card demo#21" in calls["stderr"],
    )
    check(
        "author-filter: human approve failure keeps carded log",
        "auto-approve carded demo#22" in calls["stderr"],
    )


def check_ci_approval_author_filter_bypasses_opt_out(label, **kwargs):
    prs = [needs_ci_pr(50, OWNER), needs_ci_pr(51, HUMAN)]
    result, items, calls = run_build_repo(
        prs, approve_result=("approved", "ok"), **kwargs
    )
    numbers = [it["number"] for it in items]
    check(
        "author-filter: %s excluded safe CI bypasses opt-out" % label,
        calls["approve"] == ["50"],
    )
    check(
        "author-filter: %s excluded safe CI card suppressed" % label,
        50 not in numbers,
    )
    check(
        "author-filter: %s human safe CI still cards" % label,
        numbers == [51],
    )
    check(
        "author-filter: %s excluded safe CI logs notice" % label,
        "::notice::demo#50 auto-approved" in calls["stderr"],
    )
    check(
        "author-filter: %s human opt-out logs carded warning" % label,
        "auto-approve carded demo#51: verdict safe (clean); not auto-approved (auto-approve disabled)"
        in calls["stderr"],
    )


def test_ci_approval_author_filter_bypasses_global_opt_out_for_excluded_author():
    check_ci_approval_author_filter_bypasses_opt_out(
        "global opt-out", auto_approve_ci=False
    )


def test_ci_approval_author_filter_bypasses_repo_opt_out_for_excluded_author():
    check_ci_approval_author_filter_bypasses_opt_out(
        "repo opt-out", repo_auto_approve_ci=False
    )


def test_ci_approval_author_filter_suppresses_unsafe_cards_without_approve():
    risky = {
        "safe": False,
        "error": False,
        "risky_files": [".github/workflows/ci.yml"],
        "pr_target": False,
        "exploit": False,
        "reason": "risky",
    }
    prs = [needs_ci_pr(30, OWNER), needs_ci_pr(31, HUMAN)]
    result, items, calls = run_build_repo(prs, ci_safety_result=risky)
    numbers = [it["number"] for it in items]
    check("author-filter: risky owner ci-approval card suppressed", 30 not in numbers)
    check("author-filter: risky human ci-approval PR still carded", numbers == [31])
    check("author-filter: risky PRs do not invoke approve_ci", calls["approve"] == [])
    check(
        "author-filter: suppressed risky PR still logs warning",
        "suppressed-card demo#30" in calls["stderr"],
    )


def test_ci_approval_author_filter_suppresses_unknown_fork_cards():
    prs = [
        pr_node(40, author=OWNER, status_rollup=None, cross_repo="missing"),
        pr_node(41, author=HUMAN, status_rollup=None, cross_repo="missing"),
    ]
    result, items, calls = run_build_repo(prs)
    numbers = [it["number"] for it in items]
    check("author-filter: unknown-fork owner card suppressed", 40 not in numbers)
    check("author-filter: unknown-fork human PR still carded", numbers == [41])
    check(
        "author-filter: unknown fork status still skips approve_ci",
        calls["approve"] == [],
    )
    check(
        "author-filter: suppressed unknown fork still logs warning",
        "suppressed-card demo#40" in calls["stderr"],
    )


def test_issue_author_filter_matches_pr_filter():
    issues = [
        issue_node(101, author=OWNER),
        issue_node(102, author=MAINTAINER),
        issue_node(103, author=BOT_TYPE),
        issue_node(104, author=BOT_SUFFIX),
        issue_node(105, author=HUMAN),
        issue_node(106, author=None),
    ]
    result, items, calls = run_build_repo(issue_nodes=issues, card_issues=True)
    numbers = [it["number"] for it in items]
    check("author-filter: owner issue skipped", 101 not in numbers)
    check("author-filter: configured maintainer issue skipped", 102 not in numbers)
    check("author-filter: Bot typename issue skipped", 103 not in numbers)
    check("author-filter: [bot] suffix issue skipped", 104 not in numbers)
    check("author-filter: human contributor issue still carded", 105 in numbers)
    check("author-filter: unknown issue author fails open", 106 in numbers)
    check(
        "author-filter: skipped issues stay open for reconcile self-heal",
        result["open_issue_numbers"] == [101, 102, 103, 104, 105, 106],
    )


def test_issue_scan_pages_all_open_issues():
    first = [issue_node(n, author=HUMAN) for n in range(1, 101)]
    second = [issue_node(n, author=HUMAN) for n in range(101, 106)]
    first_data = graphql_data([], first)
    first_data["issues"]["totalCount"] = 105
    first_data["issues"]["pageInfo"] = {
        "hasNextPage": True,
        "endCursor": "cursor-100",
    }
    calls = {"pages": []}
    repo_cfg = {
        "name": "demo",
        "compliance_check": "Gate",
        "test_check_patterns": ["test"],
    }

    def fake_graphql(owner, name):
        return first_data

    def fake_issue_page(owner, name, after):
        calls["pages"].append(after)
        return {
            "totalCount": 105,
            "nodes": second,
            "pageInfo": {"hasNextPage": False, "endCursor": "cursor-105"},
        }

    def fake_load_config():
        return {
            "repos": {"demo": repo_cfg},
            "maintainer": "co-maintainer",
            "nl_decisions": False,
            "card_issues": True,
            "auto_approve_ci": True,
        }

    save = (
        core.gh_graphql,
        core.gh_graphql_issue_page,
        core.load_config,
        os.environ.get("OWNER"),
        os.environ.get("GITHUB_REPOSITORY_OWNER"),
    )
    core.gh_graphql = fake_graphql
    core.gh_graphql_issue_page = fake_issue_page
    core.load_config = fake_load_config
    os.environ["OWNER"] = "owner"
    os.environ["GITHUB_REPOSITORY_OWNER"] = "owner"
    try:
        result, items = core.build_repo("owner", repo_cfg, True)
    finally:
        (
            core.gh_graphql,
            core.gh_graphql_issue_page,
            core.load_config,
            old_owner,
            old_repo_owner,
        ) = save
        if old_owner is None:
            os.environ.pop("OWNER", None)
        else:
            os.environ["OWNER"] = old_owner
        if old_repo_owner is None:
            os.environ.pop("GITHUB_REPOSITORY_OWNER", None)
        else:
            os.environ["GITHUB_REPOSITORY_OWNER"] = old_repo_owner
    numbers = [it["number"] for it in items if it["kind"] == "issue-triage"]
    check("issue-scan: next page requested", calls["pages"] == ["cursor-100"])
    check(
        "issue-scan: cards include issues beyond first page",
        numbers == list(range(1, 106)),
    )
    check(
        "issue-scan: open issue numbers are complete",
        result["open_issue_numbers"] == list(range(1, 106)),
    )
    check(
        "issue-scan: completed pagination is not truncated",
        result["truncated"] is False,
    )


def test_issue_cards_skip_when_pr_scan_incomplete():
    issue = issue_node(999, author=HUMAN)
    first_prs = [pr_node(n, author=HUMAN) for n in range(1, 101)]
    repo_cfg = {
        "name": "demo",
        "compliance_check": "Gate",
        "test_check_patterns": ["test"],
    }

    def fake_graphql(owner, name):
        return graphql_data(first_prs, [issue], pr_total=101)

    def fake_load_config():
        return {
            "repos": {"demo": repo_cfg},
            "maintainer": "co-maintainer",
            "nl_decisions": False,
            "card_issues": True,
            "auto_approve_ci": True,
        }

    save = (
        core.gh_graphql,
        core.load_config,
        os.environ.get("OWNER"),
        os.environ.get("GITHUB_REPOSITORY_OWNER"),
    )
    core.gh_graphql = fake_graphql
    core.load_config = fake_load_config
    os.environ["OWNER"] = "owner"
    os.environ["GITHUB_REPOSITORY_OWNER"] = "owner"
    try:
        result, items = core.build_repo("owner", repo_cfg, True)
    finally:
        (
            core.gh_graphql,
            core.load_config,
            old_owner,
            old_repo_owner,
        ) = save
        if old_owner is None:
            os.environ.pop("OWNER", None)
        else:
            os.environ["OWNER"] = old_owner
        if old_repo_owner is None:
            os.environ.pop("GITHUB_REPOSITORY_OWNER", None)
        else:
            os.environ["GITHUB_REPOSITORY_OWNER"] = old_repo_owner
    issue_cards = [it for it in items if it["kind"] == "issue-triage"]
    check("issue-scan: incomplete PR scan suppresses issue cards", issue_cards == [])
    check("issue-scan: incomplete PR scan is truncated", result["truncated"] is True)
    check(
        "issue-scan: incomplete PR scan reports warning",
        "PR scan incomplete" in result["warning"],
    )


def test_issue_scan_pages_open_prs_for_addressed_filter():
    first_prs = [pr_node(n, author=HUMAN) for n in range(1, 101)]
    linked_pr = pr_node(101, author=HUMAN, closes=[999])
    issue = issue_node(999, author=HUMAN)
    first_data = graphql_data(
        first_prs,
        [issue],
        pr_total=101,
        pr_page_info={"hasNextPage": True, "endCursor": "pr-cursor-100"},
    )
    calls = {"pr_pages": []}
    repo_cfg = {
        "name": "demo",
        "compliance_check": "Gate",
        "test_check_patterns": ["test"],
    }

    def fake_graphql(owner, name):
        return first_data

    def fake_pr_page(owner, name, after):
        calls["pr_pages"].append(after)
        return {
            "totalCount": 101,
            "nodes": [linked_pr],
            "pageInfo": {"hasNextPage": False, "endCursor": "pr-cursor-101"},
        }

    def fake_load_config():
        return {
            "repos": {"demo": repo_cfg},
            "maintainer": "co-maintainer",
            "nl_decisions": False,
            "card_issues": True,
            "auto_approve_ci": True,
        }

    save = (
        core.gh_graphql,
        core.gh_graphql_pr_page,
        core.load_config,
        os.environ.get("OWNER"),
        os.environ.get("GITHUB_REPOSITORY_OWNER"),
    )
    core.gh_graphql = fake_graphql
    core.gh_graphql_pr_page = fake_pr_page
    core.load_config = fake_load_config
    os.environ["OWNER"] = "owner"
    os.environ["GITHUB_REPOSITORY_OWNER"] = "owner"
    try:
        result, items = core.build_repo("owner", repo_cfg, True)
    finally:
        (
            core.gh_graphql,
            core.gh_graphql_pr_page,
            core.load_config,
            old_owner,
            old_repo_owner,
        ) = save
        if old_owner is None:
            os.environ.pop("OWNER", None)
        else:
            os.environ["OWNER"] = old_owner
        if old_repo_owner is None:
            os.environ.pop("GITHUB_REPOSITORY_OWNER", None)
        else:
            os.environ["GITHUB_REPOSITORY_OWNER"] = old_repo_owner
    issue_cards = [it for it in items if it["kind"] == "issue-triage"]
    check(
        "issue-scan: open PR next page requested",
        calls["pr_pages"] == ["pr-cursor-100"],
    )
    check("issue-scan: linked issue from PR page is not carded", issue_cards == [])
    check(
        "issue-scan: PR pagination completes open PR numbers",
        result["open_pr_numbers"] == list(range(1, 102)),
    )
    check(
        "issue-scan: completed PR pagination is not truncated",
        result["truncated"] is False,
    )


def test_issue_scan_pages_closing_references_for_addressed_filter():
    pr = pr_node(
        1,
        author=HUMAN,
        closes=range(1, 101),
        closing_total=101,
        closing_page_info={"hasNextPage": True, "endCursor": "closing-cursor-100"},
    )
    issue = issue_node(101, author=HUMAN)
    calls = {"closing_pages": []}
    repo_cfg = {
        "name": "demo",
        "compliance_check": "Gate",
        "test_check_patterns": ["test"],
    }

    def fake_graphql(owner, name):
        return graphql_data([pr], [issue])

    def fake_closing_page(owner, name, number, after):
        calls["closing_pages"].append((number, after))
        return {
            "totalCount": 101,
            "nodes": [{"number": 101}],
            "pageInfo": {"hasNextPage": False, "endCursor": "closing-cursor-101"},
        }

    def fake_load_config():
        return {
            "repos": {"demo": repo_cfg},
            "maintainer": "co-maintainer",
            "nl_decisions": False,
            "card_issues": True,
            "auto_approve_ci": True,
        }

    save = (
        core.gh_graphql,
        core.gh_graphql_closing_refs_page,
        core.load_config,
        os.environ.get("OWNER"),
        os.environ.get("GITHUB_REPOSITORY_OWNER"),
    )
    core.gh_graphql = fake_graphql
    core.gh_graphql_closing_refs_page = fake_closing_page
    core.load_config = fake_load_config
    os.environ["OWNER"] = "owner"
    os.environ["GITHUB_REPOSITORY_OWNER"] = "owner"
    try:
        result, items = core.build_repo("owner", repo_cfg, True)
    finally:
        (
            core.gh_graphql,
            core.gh_graphql_closing_refs_page,
            core.load_config,
            old_owner,
            old_repo_owner,
        ) = save
        if old_owner is None:
            os.environ.pop("OWNER", None)
        else:
            os.environ["OWNER"] = old_owner
        if old_repo_owner is None:
            os.environ.pop("GITHUB_REPOSITORY_OWNER", None)
        else:
            os.environ["GITHUB_REPOSITORY_OWNER"] = old_repo_owner
    issue_cards = [it for it in items if it["kind"] == "issue-triage"]
    check(
        "issue-scan: closing references next page requested",
        calls["closing_pages"] == [(1, "closing-cursor-100")],
    )
    check("issue-scan: linked issue from closing page is not carded", issue_cards == [])
    check(
        "issue-scan: completed closing reference pagination is not truncated",
        result["truncated"] is False,
    )


def test_cleanup_closure_recomputes_addressed_issue_map():
    pr = pr_node(1, author=HUMAN, closes=[101])
    issue = issue_node(101, author=HUMAN)
    calls = {"sweep": []}
    repo_cfg = {
        "name": "demo",
        "compliance_check": "Gate",
        "test_check_patterns": ["test"],
    }

    def fake_graphql(owner, name):
        return graphql_data([pr], [issue])

    def fake_load_config():
        return {
            "repos": {"demo": repo_cfg},
            "maintainer": "co-maintainer",
            "nl_decisions": False,
            "card_issues": True,
            "auto_approve_ci": True,
        }

    def fake_sweep(owner, repo_cfg, prs, maintainer_logins, **kwargs):
        calls["sweep"].append([p["number"] for p in prs])
        return {1}

    save = (
        core.gh_graphql,
        core.load_config,
        core.sweep_pending_contributor_actions,
        os.environ.get("OWNER"),
        os.environ.get("GITHUB_REPOSITORY_OWNER"),
    )
    core.gh_graphql = fake_graphql
    core.load_config = fake_load_config
    core.sweep_pending_contributor_actions = fake_sweep
    os.environ["OWNER"] = "owner"
    os.environ["GITHUB_REPOSITORY_OWNER"] = "owner"
    try:
        result, items = core.build_repo(
            "owner", repo_cfg, True, pending_contributor_cleanup=True
        )
    finally:
        (
            core.gh_graphql,
            core.load_config,
            core.sweep_pending_contributor_actions,
            old_owner,
            old_repo_owner,
        ) = save
        if old_owner is None:
            os.environ.pop("OWNER", None)
        else:
            os.environ["OWNER"] = old_owner
        if old_repo_owner is None:
            os.environ.pop("GITHUB_REPOSITORY_OWNER", None)
        else:
            os.environ["GITHUB_REPOSITORY_OWNER"] = old_repo_owner
    issue_cards = [it for it in items if it["kind"] == "issue-triage"]
    check(
        "cleanup: stale PR removed from open PR scan", result["open_pr_numbers"] == []
    )
    check(
        "cleanup: linked issue is carded after cleanup closes only PR",
        [it["number"] for it in issue_cards] == [101],
    )


def main():
    test_pr_author_filter_skips_owner_maintainer_and_bots()
    test_pr_items_carry_updated_at_for_activity_sort()
    test_ci_approval_author_filter_preserves_safe_auto_approve()
    test_ci_approval_author_filter_suppresses_cards_after_approve_failure()
    test_ci_approval_author_filter_bypasses_global_opt_out_for_excluded_author()
    test_ci_approval_author_filter_bypasses_repo_opt_out_for_excluded_author()
    test_ci_approval_author_filter_suppresses_unsafe_cards_without_approve()
    test_ci_approval_author_filter_suppresses_unknown_fork_cards()
    test_issue_author_filter_matches_pr_filter()
    test_issue_scan_pages_all_open_issues()
    test_issue_cards_skip_when_pr_scan_incomplete()
    test_issue_scan_pages_open_prs_for_addressed_filter()
    test_issue_scan_pages_closing_references_for_addressed_filter()
    test_cleanup_closure_recomputes_addressed_issue_map()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all author-filter tests passed")


if __name__ == "__main__":
    main()
