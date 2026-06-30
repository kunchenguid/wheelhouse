#!/usr/bin/env python3
"""
Unit-exercise scan author filtering with NO network.

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


def pr_node(number, author=None, status_rollup=MISSING, cross_repo=False):
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
        "closingIssuesReferences": {"nodes": []},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": status_rollup}}]},
    }
    if cross_repo is False:
        node["headRepository"] = {"name": "demo", "owner": {"login": "owner"}}
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


def graphql_data(pr_nodes=None, issue_nodes=None):
    pr_nodes = list(pr_nodes or [])
    issue_nodes = list(issue_nodes or [])
    return {
        "defaultBranchRef": {"name": "main"},
        "pullRequests": {"totalCount": len(pr_nodes), "nodes": pr_nodes},
        "issues": {"totalCount": len(issue_nodes), "nodes": issue_nodes},
    }


def run_build_repo(
    pr_nodes=None,
    issue_nodes=None,
    *,
    card_issues=False,
    approve_result=("error", "api fail"),
):
    calls = {"approve": []}
    repo_cfg = {
        "name": "demo",
        "compliance_check": "Gate",
        "test_check_patterns": ["test"],
    }

    def fake_graphql(owner, name):
        return graphql_data(pr_nodes, issue_nodes)

    def fake_load_config():
        return {
            "repos": {"demo": repo_cfg},
            "maintainer": "co-maintainer",
            "nl_decisions": False,
            "card_issues": card_issues,
            "auto_approve_ci": True,
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
        os.environ.get("OWNER"),
        os.environ.get("GITHUB_REPOSITORY_OWNER"),
    )
    core.gh_graphql = fake_graphql
    core.load_config = fake_load_config
    core.repo_pr_target_posture = lambda slug: {
        "pr_target": False,
        "exploit": False,
        "error": False,
    }
    core.ci_safety = lambda slug, pr, posture, changed_files=None: {
        "safe": True,
        "error": False,
        "risky_files": [],
        "pr_target": False,
        "exploit": False,
        "reason": "clean",
    }
    core.approve_ci = fake_approve
    os.environ["OWNER"] = "owner"
    os.environ["GITHUB_REPOSITORY_OWNER"] = "owner"
    try:
        with redirect_stderr(io.StringIO()):
            result, items = core.build_repo(
                "owner", repo_cfg, card_issues, auto_approve_ci=True
            )
    finally:
        (
            core.gh_graphql,
            core.load_config,
            core.repo_pr_target_posture,
            core.ci_safety,
            core.approve_ci,
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


def test_ci_approval_author_filter_runs_before_auto_approve():
    prs = [
        needs_ci_pr(10, OWNER),
        needs_ci_pr(11, BOT_TYPE),
        needs_ci_pr(12, HUMAN),
    ]
    result, items, calls = run_build_repo(prs)
    numbers = [it["number"] for it in items]
    check("author-filter: owner ci-approval PR skipped", 10 not in numbers)
    check("author-filter: bot ci-approval PR skipped", 11 not in numbers)
    check("author-filter: human ci-approval PR still carded", numbers == [12])
    check("author-filter: approve_ci only considered the human PR", calls["approve"] == ["12"])


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


def main():
    test_pr_author_filter_skips_owner_maintainer_and_bots()
    test_ci_approval_author_filter_runs_before_auto_approve()
    test_issue_author_filter_matches_pr_filter()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all author-filter tests passed")


if __name__ == "__main__":
    main()
