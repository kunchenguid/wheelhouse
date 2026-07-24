#!/usr/bin/env python3
"""
Unit-exercise merge-conflict routing and contributor nudges with NO network.

Run: python tests/test_merge_conflict.py
"""

import io
import json
import os
import re
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)
import reconcile  # noqa: E402
import wheelhouse_core as core  # noqa: E402

_failures = []
UNSET = object()


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def visible_text(body):
    """Strip HTML comments so assertions check contributor-visible copy only."""
    return re.sub(r"<!--.*?-->", "", body or "", flags=re.DOTALL)


def has_visible_automation_disclosure(body):
    visible = visible_text(body)
    return "Automated reminder:" in visible and "automated" in visible.lower()


def check_run(name, conclusion="SUCCESS", status="COMPLETED"):
    return {
        "__typename": "CheckRun",
        "name": name,
        "conclusion": conclusion,
        "status": status,
    }


def rollup(contexts):
    return {
        "state": "SUCCESS",
        "contexts": {
            "totalCount": len(contexts),
            "pageInfo": {"hasNextPage": False},
            "nodes": contexts,
        },
    }


def green_rollup():
    return rollup([check_run("Gate"), check_run("test")])


HUMAN = {"login": "contributor", "__typename": "User"}
OWNER = {"login": "owner", "__typename": "User"}
BOT = {"login": "dependabot[bot]", "__typename": "Bot"}


def pr_node(
    number,
    *,
    status_rollup=None,
    mergeable="MERGEABLE",
    cross_repo=False,
    author=None,
):
    if status_rollup == "green":
        status_rollup = green_rollup()
    if author is None:
        author = HUMAN
    node = {
        "number": number,
        "title": "PR %d" % number,
        "isDraft": False,
        "isCrossRepository": cross_repo,
        "mergeable": mergeable,
        "updatedAt": "2026-01-01T00:00:00Z",
        "changedFiles": 1,
        "author": author,
        "headRefName": "feature-%d" % number,
        "headRefOid": "sha%d" % number,
        "baseRefName": "main",
        "baseRefOid": "base-main",
        "headRepository": {"name": "demo-fork", "owner": {"login": "forker"}},
        "baseRepository": {"name": "demo", "owner": {"login": "owner"}},
        "labels": {"nodes": []},
        "closingIssuesReferences": {
            "totalCount": 0,
            "pageInfo": {"hasNextPage": False},
            "nodes": [],
        },
        "commits": {"nodes": [{"commit": {"statusCheckRollup": status_rollup}}]},
    }
    if cross_repo is False:
        node["headRepository"] = {"name": "demo", "owner": {"login": "owner"}}
    return node


def issue_node(number, author=None):
    if author is None:
        author = HUMAN
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


def _issue_number_from_comments_path(path):
    match = re.search(r"/issues/(\d+)/comments", path)
    if not match:
        raise AssertionError("unexpected gh_rest path: %s" % path)
    return int(match.group(1))


def run_build_repo(
    pr_nodes=None,
    issue_nodes=None,
    *,
    card_issues=False,
    auto_approve_ci=False,
    approve_result=("noop", "no workflow runs awaiting approval"),
    settle_mergeable=None,
    settle_errors=None,
    pending_contributor_cleanup=False,
    pending_contributor_cleanup_targets=UNSET,
    comments_by_pr=None,
):
    comments_by_pr = comments_by_pr if comments_by_pr is not None else {}
    calls = {
        "posts": [],
        "fetches": [],
        "safety": [],
        "patches": [],
        "labels": [],
        "approve": [],
        "settle": [],
    }
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
            "auto_approve_ci": auto_approve_ci,
        }

    def fake_rest(path, method=None, fields=None, jq=None, paginate=False, slurp=False):
        if path.endswith("/labels"):
            calls["labels"].append({"path": path, "fields": fields})
            return {}
        if "/issues/comments/" in path and method == "PATCH":
            comment_id = int(path.rsplit("/", 1)[-1])
            body = (fields or {}).get("body", "")
            calls["patches"].append({"comment_id": comment_id, "body": body})
            for comments in comments_by_pr.values():
                for comment in comments:
                    if comment.get("id") == comment_id:
                        comment["body"] = body
            return {}
        number = _issue_number_from_comments_path(path)
        if method == "POST":
            body = (fields or {}).get("body", "")
            calls["posts"].append({"number": number, "body": body})
            comments = comments_by_pr.setdefault(number, [])
            comment = {
                "id": len(comments) + 1,
                "body": body,
                "created_at": "2026-01-01T00:00:00Z",
                "user": {"login": "owner", "__typename": "User"},
            }
            comments.append(comment)
            return dict(comment)
        calls["fetches"].append(
            {"number": number, "paginate": paginate, "slurp": slurp}
        )
        comments = list(comments_by_pr.get(number, []))
        return [comments] if slurp else comments

    def fake_ci_safety(slug, pr, posture, changed_files=None):
        calls["safety"].append((slug, pr, posture, changed_files))
        return {
            "safe": True,
            "error": False,
            "risky_files": [],
            "pr_target": False,
            "exploit": False,
            "reason": "clean",
        }

    def fake_approve(
        owner, name, pr, posture=None, strict=False, expected_head_sha=None
    ):
        calls["approve"].append((owner, name, pr, posture, strict))
        return approve_result

    def fake_settle_many(owner, name, numbers):
        numbers = list(dict.fromkeys(numbers))
        if numbers:
            calls["settle"].append((owner, name, numbers))
        values = {}
        errors = {}
        for number in numbers:
            error = None
            if isinstance(settle_errors, dict):
                error = settle_errors.get(number)
            elif callable(settle_errors):
                error = settle_errors(owner, name, number)
            if error:
                values[number] = None
                errors[number] = str(error)
                continue
            if settle_mergeable is None:
                values[number] = "UNKNOWN"
            elif callable(settle_mergeable):
                values[number] = settle_mergeable(owner, name, number)
            else:
                values[number] = settle_mergeable
        return values, errors

    save = (
        core.gh_graphql,
        core.gh_rest,
        core.load_config,
        core.repo_pr_target_posture,
        core.ci_safety,
        core.approve_ci,
        core._settle_mergeables,
        core.ci_security_summary,
        core._list_action_required_runs,
        core._list_pr_files,
        os.environ.get("OWNER"),
        os.environ.get("GITHUB_REPOSITORY_OWNER"),
    )
    core.gh_graphql = fake_graphql
    core.gh_rest = fake_rest
    core.load_config = fake_load_config
    core.repo_pr_target_posture = lambda slug: {
        "pr_target": False,
        "exploit": False,
        "error": False,
    }
    core.ci_safety = fake_ci_safety
    core.approve_ci = fake_approve
    core._settle_mergeables = fake_settle_many
    core._list_action_required_runs = lambda slug, head_ref, head_sha: ([], "")
    core._list_pr_files = lambda _slug, _number, expected: (
        ["src/file-%d.py" % index for index in range(int(expected or 0))],
        True,
        True,
    )
    # Keep offline: the advisory summarizer would otherwise read the target repo.
    core.ci_security_summary = lambda slug, pr, head_sha, changed_files=None: "SEC"
    os.environ["OWNER"] = "owner"
    os.environ["GITHUB_REPOSITORY_OWNER"] = "owner"
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            kwargs = {
                "auto_approve_ci": auto_approve_ci,
                "pending_contributor_cleanup": pending_contributor_cleanup,
            }
            if pending_contributor_cleanup_targets is not UNSET:
                kwargs["pending_contributor_cleanup_targets"] = (
                    pending_contributor_cleanup_targets
                )
            result, items = core.build_repo(
                "owner",
                repo_cfg,
                card_issues,
                **kwargs,
            )
    finally:
        (
            core.gh_graphql,
            core.gh_rest,
            core.load_config,
            core.repo_pr_target_posture,
            core.ci_safety,
            core.approve_ci,
            core._settle_mergeables,
            core.ci_security_summary,
            core._list_action_required_runs,
            core._list_pr_files,
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


def labels(*names):
    return [{"name": n} for n in names]


def body_state(repo="demo", number=42, kind="pr-review"):
    state = {
        "repo": repo,
        "number": number,
        "kind": kind,
        "head_sha": "sha%d" % number,
        "options": ["merge", "close", "hold"],
        "comp": "pass",
        "tests": "green",
        "priority": "med",
    }
    return "<!-- wheelhouse-state: %s -->" % json.dumps(state, separators=(",", ":"))


def card(number=91, target=42):
    return {
        "number": number,
        "body": body_state(number=target),
        "labels": labels(
            "needs-decision",
            "repo:demo",
            "kind:pr-review",
            "priority:med",
            "target:demo-%d" % target,
        ),
        "title": "[demo#%d] Ready PR" % target,
    }


def run_reconcile(scan, cards, current_cards=None, run_number=100):
    calls = {"upsert": [], "close": [], "state": []}
    current_by_number = {
        c["number"]: c for c in (cards if current_cards is None else current_cards)
    }

    def fake_upsert(
        item,
        existing=None,
        has_token=False,
        preserve_reconcile_absence=False,
        expected_existing=None,
    ):
        calls["upsert"].append(
            {
                "item": item,
                "existing": existing,
                "has_token": has_token,
                "preserve_reconcile_absence": preserve_reconcile_absence,
            }
        )
        return (existing or {}).get("number", 7)

    def fake_close(number, message, label="resolved", expected=None):
        calls["close"].append({"number": number, "message": message, "label": label})

    def fake_get_card(number):
        return current_by_number.get(int(number))

    def fake_update_absence(
        number,
        body,
        count,
        run_number=0,
        closed_at="",
        reason="",
        observation=None,
    ):
        planned = reconcile.render_card.plan_reconcile_absence_projection(
            current_by_number[int(number)],
            count,
            run_number=run_number,
            closed_at=closed_at,
            reason=reason,
            observation=observation,
        )
        new_body = planned.get("body", body) if planned else body
        calls["state"].append({"count": count, "body_after": new_body})
        if new_body == body:
            return False
        current_by_number[int(number)]["body"] = new_body
        return True

    old_argv = sys.argv[:]
    old_github_actions = os.environ.get("GITHUB_ACTIONS")
    old_event_name = os.environ.get("GITHUB_EVENT_NAME")
    old_run_number = os.environ.get("GITHUB_RUN_NUMBER")
    old_upsert = reconcile.render_card.upsert_card
    old_close = reconcile.render_card.close_card
    old_get_card = reconcile.render_card.get_card
    old_update_absence = reconcile.render_card.update_reconcile_absence
    reconcile.render_card.upsert_card = fake_upsert
    reconcile.render_card.close_card = fake_close
    reconcile.render_card.get_card = fake_get_card
    reconcile.render_card.update_reconcile_absence = fake_update_absence
    try:
        os.environ["GITHUB_ACTIONS"] = "true"
        os.environ["GITHUB_EVENT_NAME"] = "schedule"
        os.environ["GITHUB_RUN_NUMBER"] = str(run_number)
        with tempfile.TemporaryDirectory() as d:
            scan_path = os.path.join(d, "scan.json")
            cards_path = os.path.join(d, "cards.json")
            with open(scan_path, "w") as f:
                json.dump(scan, f)
            with open(cards_path, "w") as f:
                json.dump(cards, f)
            sys.argv = ["reconcile.py", scan_path, cards_path]
            with redirect_stdout(io.StringIO()):
                reconcile.main()
    finally:
        sys.argv = old_argv
        if old_github_actions is None:
            os.environ.pop("GITHUB_ACTIONS", None)
        else:
            os.environ["GITHUB_ACTIONS"] = old_github_actions
        if old_event_name is None:
            os.environ.pop("GITHUB_EVENT_NAME", None)
        else:
            os.environ["GITHUB_EVENT_NAME"] = old_event_name
        if old_run_number is None:
            os.environ.pop("GITHUB_RUN_NUMBER", None)
        else:
            os.environ["GITHUB_RUN_NUMBER"] = old_run_number
        reconcile.render_card.upsert_card = old_upsert
        reconcile.render_card.close_card = old_close
        reconcile.render_card.get_card = old_get_card
        reconcile.render_card.update_reconcile_absence = old_update_absence
    return calls


def test_graphql_fetches_mergeability():
    check("fetch: GraphQL query requests mergeable", " mergeable" in core.GQL)


def test_classify_conflict_routes_pr_review_to_rebase():
    check(
        "classify: conflicting merge-ready PR waits for rebase",
        core.classify(False, "pass", "green", True, False, "CONFLICTING")
        == "needs-rebase",
    )
    check(
        "classify: conflicting review-needed PR waits for rebase",
        core.classify(False, "pass", "none", True, False, "CONFLICTING")
        == "needs-rebase",
    )


def test_unknown_mergeability_fails_open():
    check(
        "classify: UNKNOWN mergeability keeps merge-ready route",
        core.classify(False, "pass", "green", True, False, "UNKNOWN") == "merge-ready",
    )
    check(
        "classify: null mergeability keeps merge-ready route",
        core.classify(False, "pass", "green", True, False, None) == "merge-ready",
    )


def test_ci_approval_not_rerouted_by_conflict():
    check(
        "classify: conflicted fork without CI still needs CI approval",
        core.classify(False, "none", "none", False, True, "CONFLICTING")
        == "needs-ci-approval",
    )
    pr = pr_node(10, status_rollup=None, mergeable="CONFLICTING", cross_repo=True)
    result, items, calls = run_build_repo([pr], auto_approve_ci=False)
    check("build: ci-approval repo scan stays ok", result["ok"] is True)
    check(
        "build: conflicted fork without CI still emits ci-approval card",
        len(items) == 1
        and items[0]["kind"] == "ci-approval"
        and items[0]["bucket"] == "needs-ci-approval",
    )
    # With auto-approve off there is no noop consume path, so no nudge either -
    # the card is the owner-visible surface. Nudge only attaches to the handled
    # approve_ci noop + CONFLICTING path (see below).
    check(
        "build: ci-approval conflict does not nudge when carded", calls["posts"] == []
    )


def test_ci_noop_conflicting_fork_nudges_once_per_head():
    """Live class firstmate#257/#328: fork PR, no CI workflows, mergeable=CONFLICTING.

    Classifies to needs-ci-approval; approve_ci returns noop; without this fix the
    PR is silently dropped (no card, no nudge). The fix posts the same fire-once
    rebase nudge as needs-rebase, still without a decision card or bucket rewrite.
    """
    comments = {}
    pr = pr_node(257, status_rollup=None, mergeable="CONFLICTING", cross_repo=True)
    result1, items1, calls1 = run_build_repo(
        [pr],
        auto_approve_ci=True,
        approve_result=("noop", "no workflow runs awaiting approval"),
        comments_by_pr=comments,
    )
    result2, items2, calls2 = run_build_repo(
        [pr],
        auto_approve_ci=True,
        approve_result=("noop", "no workflow runs awaiting approval"),
        comments_by_pr=comments,
    )
    check("ci-noop-conflict: scan stays ok", result1["ok"] is True)
    check(
        "ci-noop-conflict: PR remains open in scan state",
        result1["open_pr_numbers"] == [257],
    )
    check(
        "ci-noop-conflict: emits NO decision card (noop still consumes)",
        items1 == [] and items2 == [],
    )
    check(
        "ci-noop-conflict: approve_ci was attempted (noop path)",
        len(calls1["approve"]) == 1,
    )
    check("ci-noop-conflict: first scan posts one nudge", len(calls1["posts"]) == 1)
    check("ci-noop-conflict: second scan posts no duplicate", calls2["posts"] == [])
    body = calls1["posts"][0]["body"] if calls1["posts"] else ""
    check(
        "ci-noop-conflict: reuses contributor-facing rebase wording",
        "rebase" in body and "resolve the conflict" in body,
    )
    check(
        "ci-noop-conflict: visible automation disclosure outside HTML comments",
        has_visible_automation_disclosure(body),
    )
    check(
        "ci-noop-conflict: reuses fire-once-per-head marker",
        core._rebase_nudge_marker("sha257") in body,
    )
    check(
        "ci-noop-conflict: no product name in contributor copy",
        "Wheelhouse" not in body,
    )
    check(
        "ci-noop-conflict: settle not needed when bulk mergeable is conclusive",
        calls1["settle"] == [],
    )
    _, cleanup_items, cleanup_calls = run_build_repo(
        [pr],
        auto_approve_ci=True,
        approve_result=("noop", "no workflow runs awaiting approval"),
        pending_contributor_cleanup=True,
        comments_by_pr={},
    )
    check("ci-noop-conflict: cleanup-enabled path emits no card", cleanup_items == [])
    check(
        "ci-noop-conflict: cleanup state is not armed",
        cleanup_calls["patches"] == [] and cleanup_calls["labels"] == [],
    )


def test_ci_noop_unknown_mergeable_does_not_nudge():
    """UNKNOWN is pending, never a conflict signal - no nudge until settled CONFLICTING."""
    pr = pr_node(328, status_rollup=None, mergeable="UNKNOWN", cross_repo=True)
    result, items, calls = run_build_repo(
        [pr],
        auto_approve_ci=True,
        approve_result=("noop", "no workflow runs awaiting approval"),
        settle_mergeable="UNKNOWN",  # never settles this scan
    )
    check("ci-noop-unknown: scan stays ok", result["ok"] is True)
    check("ci-noop-unknown: emits NO card", items == [])
    check("ci-noop-unknown: settle was attempted", len(calls["settle"]) == 1)
    check("ci-noop-unknown: no nudge while still UNKNOWN", calls["posts"] == [])


def test_ci_noop_unknown_settles_conflicting_then_nudges():
    pr = pr_node(329, status_rollup=None, mergeable="UNKNOWN", cross_repo=True)
    result, items, calls = run_build_repo(
        [pr],
        auto_approve_ci=True,
        approve_result=("noop", "no workflow runs awaiting approval"),
        settle_mergeable="CONFLICTING",
    )
    check("ci-noop-settled: emits NO card", items == [])
    check("ci-noop-settled: settle was attempted", len(calls["settle"]) == 1)
    check(
        "ci-noop-settled: one nudge after settled CONFLICTING", len(calls["posts"]) == 1
    )
    settled_body = calls["posts"][0]["body"] if calls["posts"] else ""
    check(
        "ci-noop-settled: marker is head-specific",
        core._rebase_nudge_marker("sha329") in settled_body,
    )
    check(
        "ci-noop-settled: visible automation disclosure outside HTML comments",
        has_visible_automation_disclosure(settled_body),
    )


def test_ci_noop_mergeability_read_failure_is_unhealthy():
    pr = pr_node(334, status_rollup=None, mergeable="UNKNOWN", cross_repo=True)
    result, items, calls = run_build_repo(
        [pr],
        auto_approve_ci=True,
        approve_result=("noop", "no workflow runs awaiting approval"),
        settle_errors={334: "HTTP 502"},
    )
    check("ci-noop-error: repo is unhealthy", result["ok"] is False)
    check("ci-noop-error: emits no worklist item", items == [])
    check("ci-noop-error: no nudge after failed settlement", calls["posts"] == [])
    check(
        "ci-noop-error: failed PR is indeterminate",
        result["indeterminate_pr_numbers"] == [334],
    )
    check(
        "ci-noop-error: warning preserves the query failure",
        "mergeability settlement query failed" in result["warning"]
        and "#334" in result["warning"]
        and "HTTP 502" in result["warning"],
    )


def test_ci_noop_unknown_mergeables_settle_in_one_batch():
    prs = [
        pr_node(332, status_rollup=None, mergeable="UNKNOWN", cross_repo=True),
        pr_node(333, status_rollup=None, mergeable="UNKNOWN", cross_repo=True),
    ]
    result, items, calls = run_build_repo(
        prs,
        auto_approve_ci=True,
        approve_result=("noop", "no workflow runs awaiting approval"),
        settle_mergeable=lambda owner, name, number: "CONFLICTING",
    )
    check("ci-noop-batch: scan stays ok", result["ok"] is True)
    check("ci-noop-batch: emits NO cards", items == [])
    check(
        "ci-noop-batch: settles every unknown noop candidate together",
        calls["settle"] == [("owner", "demo", [332, 333])],
    )
    check("ci-noop-batch: nudges every settled conflict", len(calls["posts"]) == 2)


def test_ci_noop_mergeable_fork_does_not_nudge():
    pr = pr_node(330, status_rollup=None, mergeable="MERGEABLE", cross_repo=True)
    result, items, calls = run_build_repo(
        [pr],
        auto_approve_ci=True,
        approve_result=("noop", "no workflow runs awaiting approval"),
    )
    check("ci-noop-mergeable: emits NO card", items == [])
    check("ci-noop-mergeable: no conflict so no nudge", calls["posts"] == [])
    check(
        "ci-noop-mergeable: no settle for conclusive MERGEABLE", calls["settle"] == []
    )


def test_ci_approved_with_workflows_does_not_nudge_on_conflict():
    """PRs that actually have workflows (approved, not noop) keep prior behavior."""
    pr = pr_node(331, status_rollup=None, mergeable="CONFLICTING", cross_repo=True)
    result, items, calls = run_build_repo(
        [pr],
        auto_approve_ci=True,
        approve_result=("approved", "approved 1 run"),
    )
    check("ci-approved-conflict: emits NO card", items == [])
    check(
        "ci-approved-conflict: approve was attempted",
        len(calls["approve"]) == 1,
    )
    check(
        "ci-approved-conflict: no rebase nudge on approved path",
        calls["posts"] == [],
    )


def test_conflicted_pr_suppresses_card_and_nudges_once_per_head():
    comments = {}
    pr = pr_node(42, status_rollup="green", mergeable="CONFLICTING")
    result1, items1, calls1 = run_build_repo([pr], comments_by_pr=comments)
    result2, items2, calls2 = run_build_repo([pr], comments_by_pr=comments)
    check("nudge: conflicted PR keeps repo scan ok", result1["ok"] is True)
    check(
        "nudge: conflicted PR remains open in scan state",
        result1["open_pr_numbers"] == [42],
    )
    check("nudge: conflicted PR emits no decision card", items1 == [] and items2 == [])
    check("nudge: first scan posts one comment", len(calls1["posts"]) == 1)
    check("nudge: second scan posts no duplicate", calls2["posts"] == [])
    body = calls1["posts"][0]["body"] if calls1["posts"] else ""
    check(
        "nudge: body names the rebase action",
        "rebase" in body and "resolve the conflict" in body,
    )
    check("nudge: body mentions checks re-run after fix", "checks will re-run" in body)
    check(
        "nudge: visible automation disclosure outside HTML comments",
        has_visible_automation_disclosure(body),
    )
    check("nudge: body has no internal product name", "Wheelhouse" not in body)
    check(
        "nudge: body has no internal-state jargon",
        "maintainer queue" not in body
        and "resurface" not in body
        and "stepping out" not in body
        and "triage" not in body.lower(),
    )
    check(
        "nudge: body carries a head-specific marker",
        core._rebase_nudge_marker("sha42") in body,
    )
    check(
        "nudge: comment fetch uses pagination slurp",
        calls1["fetches"] and calls1["fetches"][0]["slurp"] is True,
    )
    check("nudge: marker persists in stored comments", len(comments.get(42, [])) == 1)
    check(
        "nudge: cleanup state is not armed while cleanup disabled",
        calls1["patches"] == [] and calls1["labels"] == [],
    )
    check("nudge: second scan still ok", result2["ok"] is True)

    enabled_comments = {}
    _, _, enabled_calls = run_build_repo(
        [pr], comments_by_pr=enabled_comments, pending_contributor_cleanup=True
    )
    patch_bodies = [p["body"] for p in enabled_calls["patches"]]
    check(
        "nudge: cleanup marker is armed when cleanup enabled",
        any(core.PENDING_CONTRIBUTOR_MARKER_PREFIX in body for body in patch_bodies),
    )
    check(
        "nudge: pending contributor label is added when cleanup enabled",
        any(
            (item["fields"] or {}).get("labels[]") == core.PENDING_CONTRIBUTOR_LABEL
            for item in enabled_calls["labels"]
        ),
    )

    _, _, target_disabled_calls = run_build_repo(
        [pr],
        comments_by_pr={},
        pending_contributor_cleanup=True,
        pending_contributor_cleanup_targets=["issue"],
    )
    check(
        "nudge: cleanup state is not armed when PR target disabled",
        target_disabled_calls["patches"] == []
        and target_disabled_calls["labels"] == [],
    )

    _, _, empty_target_calls = run_build_repo(
        [pr],
        comments_by_pr={},
        pending_contributor_cleanup=True,
        pending_contributor_cleanup_targets=[],
    )
    check(
        "nudge: cleanup state is not armed when cleanup targets are empty",
        empty_target_calls["patches"] == [] and empty_target_calls["labels"] == [],
    )

    _, _, null_target_calls = run_build_repo(
        [pr],
        comments_by_pr={},
        pending_contributor_cleanup=True,
        pending_contributor_cleanup_targets=None,
    )
    check(
        "nudge: cleanup state is not armed when cleanup targets are null",
        null_target_calls["patches"] == [] and null_target_calls["labels"] == [],
    )


def test_rebase_nudge_body_is_contributor_plain_language():
    """Direct pin on the template: copy stays contributor-facing; marker stays."""
    head = "abcdef0123456789deadbeef"
    body = core._rebase_nudge_body("demo", 42, head)
    marker = core._rebase_nudge_marker(head)
    visible = visible_text(body)
    check(
        "nudge-body: explains the merge conflict",
        "merge conflict" in body and "base branch" in body,
    )
    check(
        "nudge-body: asks contributor to rebase/merge and push",
        "rebase" in body and "push" in body,
    )
    check(
        "nudge-body: says checks re-run and PR is looked at again",
        "checks will re-run" in body and "looked at again" in body,
    )
    check(
        "nudge-body: visible automation disclosure outside HTML comments",
        has_visible_automation_disclosure(body),
    )
    check(
        "nudge-body: automation disclosure is not only in the hidden marker",
        "Automated reminder:" in visible and "automated" not in marker.lower(),
    )
    check(
        "nudge-body: keeps repo/head note",
        "Noted for demo#42" in body and "`abcdef01`" in body,
    )
    check("nudge-body: no product name", "Wheelhouse" not in body)
    check(
        "nudge-body: no internal queue jargon",
        "maintainer queue" not in body
        and "resurface" not in body
        and "stepping out" not in body
        and "card" not in body.lower()
        and "triage" not in body.lower(),
    )
    check("nudge-body: hidden idempotence marker survives rewrite", marker in body)
    check(
        "nudge-body: marker is HTML-comment form",
        body.rstrip().endswith(marker) and marker.startswith("<!-- "),
    )


def test_untrusted_rebase_marker_does_not_suppress_nudge():
    comments = {
        42: [
            {
                "id": 1,
                "body": "forged\n\n" + core._rebase_nudge_marker("sha42"),
                "created_at": "2026-01-01T00:00:00Z",
                "user": HUMAN,
            }
        ]
    }
    pr = pr_node(42, status_rollup="green", mergeable="CONFLICTING")
    result, items, calls = run_build_repo([pr], comments_by_pr=comments)
    check("nudge: untrusted marker keeps scan ok", result["ok"] is True)
    check("nudge: untrusted marker emits no card", items == [])
    check(
        "nudge: untrusted marker does not suppress real nudge", len(calls["posts"]) == 1
    )


def test_nudge_skips_owner_and_bot_authors():
    prs = [
        pr_node(50, status_rollup="green", mergeable="CONFLICTING", author=OWNER),
        pr_node(51, status_rollup="green", mergeable="CONFLICTING", author=BOT),
    ]
    result, items, calls = run_build_repo(prs)
    check("nudge-skip: owner and bot scan stays ok", result["ok"] is True)
    check("nudge-skip: owner and bot cards are suppressed", items == [])
    check("nudge-skip: owner and bot are not nudged", calls["posts"] == [])
    check("nudge-skip: owner and bot do not fetch comments", calls["fetches"] == [])


def test_issue_triage_unaffected():
    result, items, calls = run_build_repo([], [issue_node(70)], card_issues=True)
    check("issue: repo scan stays ok", result["ok"] is True)
    check(
        "issue: issue-triage card still emits",
        len(items) == 1 and items[0]["kind"] == "issue-triage",
    )
    check("issue: issue-triage does not nudge", calls["posts"] == [])


def test_reconcile_consumes_conflicted_card_that_left_worklist():
    result, items, _calls = run_build_repo(
        [pr_node(42, status_rollup="green", mergeable="CONFLICTING")]
    )
    scan = {"repos": {"demo": result}, "items": items}
    pending = card(number=91, target=42)
    first = run_reconcile(scan, [pending], run_number=100)
    check(
        "reconcile: conflicted target outside worklist has no upsert",
        first["upsert"] == [],
    )
    check(
        "reconcile: first conflicted-target absence stays open with count one",
        first["close"] == []
        and len(first["state"]) == 1
        and first["state"][0]["count"] == 1,
    )

    pending["body"] = first["state"][0]["body_after"]
    pending["labels"] = labels(
        "needs-decision",
        "repo:demo",
        "kind:pr-review",
        "priority:med",
        "target:demo-42",
        reconcile.render_card.LIFECYCLE_CONFIRM_LABEL,
    )
    second = run_reconcile(scan, [pending], run_number=101)
    check(
        "reconcile: second conflicted-target absence closes stale card",
        len(second["close"]) == 1 and second["close"][0]["number"] == 91,
    )
    check(
        "reconcile: stale card close explains no maintainer decision needed",
        "no longer needs a maintainer decision" in second["close"][0]["message"],
    )


def main():
    test_graphql_fetches_mergeability()
    test_classify_conflict_routes_pr_review_to_rebase()
    test_unknown_mergeability_fails_open()
    test_ci_approval_not_rerouted_by_conflict()
    test_ci_noop_conflicting_fork_nudges_once_per_head()
    test_ci_noop_unknown_mergeable_does_not_nudge()
    test_ci_noop_unknown_settles_conflicting_then_nudges()
    test_ci_noop_mergeability_read_failure_is_unhealthy()
    test_ci_noop_unknown_mergeables_settle_in_one_batch()
    test_ci_noop_mergeable_fork_does_not_nudge()
    test_ci_approved_with_workflows_does_not_nudge_on_conflict()
    test_rebase_nudge_body_is_contributor_plain_language()
    test_conflicted_pr_suppresses_card_and_nudges_once_per_head()
    test_untrusted_rebase_marker_does_not_suppress_nudge()
    test_nudge_skips_owner_and_bot_authors()
    test_issue_triage_unaffected()
    test_reconcile_consumes_conflicted_card_that_left_worklist()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all merge conflict tests passed")


if __name__ == "__main__":
    main()
