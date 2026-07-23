#!/usr/bin/env python3
"""Current-body compliance evidence regressions, offline and read-only.

Exercises the opt-in ``wheelhouse.actions-current-body/v1`` contract from the
Actions workflow-run API through the production check reducer. The PR #549
fixture is historical data only; this test performs no GitHub mutation.

Run: python tests/test_compliance_event_evidence.py
"""

import copy
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import auto_merge as am  # noqa: E402
import wheelhouse_core as core  # noqa: E402

_failures = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        _failures.append(name)


OWNER = "kunchenguid"
REPO = "no-mistakes"
PR = 549
HEAD = "a11e044816795963f9a8c1cc980afb2b436e5dc0"
CHECK = "PR must be raised via no-mistakes"
WORKFLOW = {
    "id": 7711,
    "path": ".github/workflows/no-mistakes-required.yml",
    "name": "Require no-mistakes",
}
CFG = {
    "name": REPO,
    "compliance_check": CHECK,
    "compliance_evidence": {
        "schema": core.COMPLIANCE_EVIDENCE_SCHEMA,
        "workflow_path": WORKFLOW["path"],
        "workflow_name": WORKFLOW["name"],
    },
    "test_check_patterns": ["test (", "e2e"],
}


def workflow_run(
    run_id,
    run_number,
    action,
    conclusion="success",
    status="completed",
    **overrides,
):
    title = (
        "PR #%d body compliance - %s - event %d (run %d)"
        % (PR, action, run_number, run_id)
    )
    value = {
        "id": run_id,
        "run_number": run_number,
        "run_attempt": 1,
        "workflow_id": WORKFLOW["id"],
        "name": title,
        "path": WORKFLOW["path"],
        "event": "pull_request",
        "head_sha": HEAD,
        "head_branch": "fm/nm-gh-checks-diagnostics",
        "display_title": title,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "pull_requests": [{"number": PR}],
    }
    value.update(overrides)
    return value


def check_run(check_id, run_id, conclusion="SUCCESS", status="COMPLETED", name=CHECK):
    return {
        "__typename": "CheckRun",
        "databaseId": check_id,
        "detailsUrl": (
            "https://github.com/%s/%s/actions/runs/%d/job/%d"
            % (OWNER, REPO, run_id, check_id)
        ),
        "name": name,
        "conclusion": conclusion,
        "status": status,
    }


def contexts(nodes, complete=True):
    return {
        "totalCount": len(nodes) if complete else len(nodes) + 1,
        "pageInfo": {"hasNextPage": not complete},
        "nodes": nodes,
    }


def pr_with(nodes, rollup_state="SUCCESS", complete=True):
    return {
        "number": PR,
        "headRefOid": HEAD,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {
                            "state": rollup_state,
                            "contexts": contexts(nodes, complete=complete),
                        }
                    }
                }
            ]
        },
    }


def reduce(runs, nodes, rollup_state="SUCCESS", complete=True):
    pr = pr_with(nodes, rollup_state=rollup_state, complete=complete)
    evidence = core._build_compliance_evidence(
        OWNER,
        REPO,
        PR,
        HEAD,
        core.compliance_evidence_config(CFG)[0],
        WORKFLOW,
        runs,
        len(runs),
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"],
    )
    pr[core._COMPLIANCE_EVIDENCE_KEY] = evidence
    return core.check_status(pr, CFG), evidence


def test_signed_opened_success_passes():
    run = workflow_run(1001, 10, "opened")
    (comp, _, _, _), evidence = reduce([run], [check_run(5001, 1001)])
    check("signed opened success is current compliance pass", comp == "pass")
    check(
        "opened success binds immutable run and check identities",
        evidence.get("complete") is True
        and evidence["latest"]["run_id"] == 1001
        and evidence["latest"]["run_number"] == 10
        and evidence["latest"]["check_run_id"] == 5001,
    )


def test_later_unsigned_edit_cannot_reuse_older_success():
    opened = workflow_run(1001, 10, "opened")
    edited_pending = workflow_run(
        1002, 11, "edited", status="in_progress", conclusion=None
    )
    pending_pr = pr_with([check_run(5001, 1001)])
    pending_evidence = core._build_compliance_evidence(
        OWNER,
        REPO,
        PR,
        HEAD,
        core.compliance_evidence_config(CFG)[0],
        WORKFLOW,
        [opened, edited_pending],
        2,
        pending_pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"],
    )
    pending_pr[core._COMPLIANCE_EVIDENCE_KEY] = pending_evidence
    comp = core.check_status(pending_pr, CFG)[0]
    check("later edited event pending blocks older signed success", comp == "pending")
    check(
        "target observation reports authoritative latest event as pending",
        core._check_observation_shape(pending_pr)[3] == "pending",
    )

    edited_failed = workflow_run(1002, 11, "edited", conclusion="failure")
    (comp, _, _, _), _ = reduce(
        [opened, edited_failed],
        [check_run(5001, 1001), check_run(5002, 1002, conclusion="FAILURE")],
        rollup_state="FAILURE",
    )
    check("later unsigned edited failure beats older signed success", comp == "fail")


def test_later_corrected_signed_edit_supersedes_older_failure():
    runs = [
        workflow_run(1001, 10, "opened"),
        workflow_run(1002, 11, "edited", conclusion="failure"),
        workflow_run(1003, 12, "edited"),
    ]
    nodes = [
        check_run(5001, 1001),
        check_run(5002, 1002, conclusion="FAILURE"),
        check_run(5003, 1003),
    ]
    (comp, _, _, _), _ = reduce(runs, nodes, rollup_state="FAILURE")
    check("later corrected signed edit returns compliance to pass", comp == "pass")


def test_monotonic_event_order_beats_completion_order():
    runs = [
        workflow_run(1001, 10, "opened", conclusion="failure"),
        workflow_run(1002, 11, "edited", status="in_progress", conclusion=None),
    ]
    nodes = [check_run(5001, 1001, conclusion="FAILURE")]
    (comp, _, _, _), evidence = reduce(runs, nodes, rollup_state="FAILURE")
    check("older terminal completion cannot override newer pending event", comp == "pending")
    check("latest selection uses run_number, not completion or array order", evidence["latest"]["run_id"] == 1002)

    reversed_runs = list(reversed(runs))
    (reversed_comp, _, _, _), reversed_evidence = reduce(
        reversed_runs, nodes, rollup_state="FAILURE"
    )
    check(
        "Actions array order cannot change current event",
        reversed_comp == "pending" and reversed_evidence["latest"]["run_id"] == 1002,
    )


def test_latest_conservative_states_never_fall_back():
    opened = workflow_run(1001, 10, "opened")
    opened_context = check_run(5001, 1001)
    for conclusion in ("cancelled", "action_required"):
        latest = workflow_run(1002, 11, "edited", conclusion=conclusion)
        (comp, _, _, _), evidence = reduce(
            [opened, latest], [opened_context], rollup_state="FAILURE"
        )
        check(
            "latest %s remains conservative" % conclusion,
            comp == "fail" and evidence.get("complete") is True,
        )

    (_, _, _, _), incomplete = reduce(
        [opened], [opened_context], complete=False
    )
    pr = pr_with([opened_context], complete=False)
    pr[core._COMPLIANCE_EVIDENCE_KEY] = incomplete
    comp = core.check_status(pr, CFG)[0]
    check("incomplete context read cannot reuse older success", comp == "pending")
    check(
        "incomplete event enrichment marks target check evidence incomplete",
        core._check_observation_shape(pr)[0] is False,
    )

    missing = workflow_run(1002, 11, "edited")
    missing.pop("run_attempt")
    (comp, _, _, _), evidence = reduce([opened, missing], [opened_context])
    check(
        "missing Actions run metadata is unavailable, not old success",
        comp == "pending" and evidence.get("complete") is False,
    )


def test_identity_and_order_mismatches_are_rejected():
    baseline = workflow_run(1001, 10, "opened")
    mutations = {
        "head": {"head_sha": "b" * 40},
        "PR": {
            "display_title": "PR #550 body compliance - opened - event 10 (run 1001)"
        },
        "workflow": {"workflow_id": WORKFLOW["id"] + 1},
        "event": {"event": "workflow_dispatch"},
        "event identity": {
            "display_title": "PR #549 body compliance - closed - event 10 (run 1001)"
        },
        "run order": {
            "display_title": "PR #549 body compliance - opened - event 9 (run 1001)"
        },
        "run ID": {
            "display_title": "PR #549 body compliance - opened - event 10 (run 9999)"
        },
    }
    for label, changes in mutations.items():
        malformed = copy.deepcopy(baseline)
        malformed.update(changes)
        (comp, _, _, _), evidence = reduce(
            [malformed], [check_run(5001, 1001)]
        )
        check(
            "%s mismatch is rejected" % label,
            comp == "pending" and evidence.get("complete") is False,
        )

    (comp, _, _, _), evidence = reduce(
        [baseline], [check_run(5001, 9999)]
    )
    check(
        "check-to-run identity mismatch is rejected",
        comp == "pending" and evidence.get("complete") is False,
    )

    duplicate = workflow_run(1002, 10, "edited")
    (comp, _, _, _), evidence = reduce(
        [baseline, duplicate], [check_run(5001, 1001)]
    )
    check(
        "duplicated run order is rejected ambiguously",
        comp == "pending" and evidence.get("complete") is False,
    )

    second = workflow_run(1002, 11, "edited")
    (comp, _, _, _), evidence = reduce(
        [baseline, second],
        [check_run(5001, 1001), check_run(5001, 1002)],
    )
    check(
        "duplicated check identity is rejected ambiguously",
        comp == "pending" and evidence.get("complete") is False,
    )


def test_untracked_rollup_failure_still_fails_closed():
    run = workflow_run(1001, 10, "opened")
    nodes = [
        check_run(5001, 1001),
        check_run(6001, 2001, conclusion="FAILURE", name="Other required gate"),
    ]
    (comp, _, _, _), _ = reduce([run], nodes, rollup_state="FAILURE")
    check("event-aware pass does not excuse an untracked failure", comp == "fail")


def test_pr549_production_shaped_open_edit_edit_history():
    """PR #549 identities from the incident, projected under the landed contract."""
    runs = [
        workflow_run(29962844999, 586, "opened"),
        workflow_run(29962943078, 587, "edited", conclusion="failure"),
        workflow_run(29965243268, 588, "edited"),
    ]
    nodes = [
        check_run(89077262159, 29962844999),
        check_run(89077259052, 29962943078, conclusion="FAILURE"),
        check_run(89077256064, 29965243268),
        check_run(99000000001, 39965243268, name="test (ubuntu-latest)"),
        check_run(99000000002, 39965243269, name="e2e"),
    ]
    (comp, tests, _, _), evidence = reduce(runs, nodes, rollup_state="FAILURE")
    bucket = core.classify(
        False, comp, tests, True, cross_repo=True, mergeable="MERGEABLE"
    )
    check("PR #549-shaped signed/unsigned/signed history projects pass", comp == "pass")
    check("PR #549-shaped production tests remain green", tests == "green")
    check("PR #549-shaped current truth routes merge-ready", bucket == "merge-ready")
    check(
        "PR #549-shaped latest identity is the second edited event",
        evidence["latest"]["run_id"] == 29965243268
        and evidence["latest"]["run_number"] == 588,
    )


def test_actions_reads_are_complete_bounded_and_cached():
    runs = []
    for index in range(1, 102):
        status = "completed" if index == 101 else "queued"
        runs.append(
            workflow_run(
                100000 + index,
                index,
                "opened" if index == 1 else "edited",
                status=status,
                conclusion="success" if status == "completed" else None,
            )
        )
    latest = runs[-1]
    pr = pr_with([check_run(7001, latest["id"])])
    calls = []
    saved = core.gh_rest

    def fake_rest(path, method=None, fields=None, jq=None, paginate=False, slurp=False):
        calls.append(path)
        if "/actions/workflows/" in path and "/runs?" not in path:
            return {
                "id": WORKFLOW["id"],
                "path": WORKFLOW["path"],
                "name": WORKFLOW["name"],
                "state": "active",
            }
        if "&page=1" in path:
            return {"total_count": len(runs), "workflow_runs": runs[:100]}
        if "&page=2" in path:
            return {"total_count": len(runs), "workflow_runs": runs[100:]}
        raise AssertionError("unexpected API path %s" % path)

    core.gh_rest = fake_rest
    try:
        cache = {}
        evidence = core.enrich_compliance_evidence(
            OWNER, REPO, pr, CFG, cache=cache
        )
        first_call_count = len(calls)
        core.enrich_compliance_evidence(OWNER, REPO, pr, CFG, cache=cache)
        check(
            "Actions enrichment paginates to a complete exact-head set",
            evidence.get("complete") is True
            and evidence["runs_seen"] == 101
            and evidence["runs_total"] == 101,
        )
        check("workflow metadata and exact-head pages are cached per scan", len(calls) == first_call_count == 3)
    finally:
        core.gh_rest = saved


def test_invalid_config_and_unavailable_api_degrade_without_scan_exception():
    bad_cfg = copy.deepcopy(CFG)
    bad_cfg["compliance_evidence"]["workflow_path"] = "../workflow.yml"
    pr = pr_with([check_run(5001, 1001)])
    evidence = core.enrich_compliance_evidence(OWNER, REPO, pr, bad_cfg)
    check(
        "invalid evidence config fails closed without enabling legacy reduction",
        evidence.get("complete") is False and core.check_status(pr, bad_cfg)[0] == "pending",
    )

    saved = core.gh_rest

    def unavailable(*args, **kwargs):
        raise RuntimeError("temporary Actions API failure")

    core.gh_rest = unavailable
    try:
        pr = pr_with([check_run(5001, 1001)])
        evidence = core.enrich_compliance_evidence(OWNER, REPO, pr, CFG)
        check(
            "unavailable Actions enrichment is conservative and non-throwing",
            evidence.get("complete") is False and core.check_status(pr, CFG)[0] == "pending",
        )
    finally:
        core.gh_rest = saved


def test_g7_uses_fresh_event_evidence():
    latest_run = workflow_run(1002, 11, "edited", conclusion="failure")
    gql_pr = pr_with([check_run(5002, 1002, conclusion="FAILURE")], rollup_state="FAILURE")
    saved_graphql = core._gh_graphql_data
    saved_rest = core.gh_rest

    def fake_graphql(_args):
        return {"data": {"repository": {"pullRequest": gql_pr}}}

    def fake_rest(path, method=None, fields=None, jq=None, paginate=False, slurp=False):
        if "/runs?" in path:
            return {"total_count": 1, "workflow_runs": [latest_run]}
        return {
            "id": WORKFLOW["id"],
            "path": WORKFLOW["path"],
            "name": WORKFLOW["name"],
            "state": "active",
        }

    core._gh_graphql_data = fake_graphql
    core.gh_rest = fake_rest
    try:
        ok, reason = am.live_check_status(OWNER, REPO, PR, HEAD, CFG)
        check(
            "G7 fresh check reread consumes latest event evidence",
            not ok and "comp=fail" in reason,
        )
    finally:
        core._gh_graphql_data = saved_graphql
        core.gh_rest = saved_rest


def main():
    test_signed_opened_success_passes()
    test_later_unsigned_edit_cannot_reuse_older_success()
    test_later_corrected_signed_edit_supersedes_older_failure()
    test_monotonic_event_order_beats_completion_order()
    test_latest_conservative_states_never_fall_back()
    test_identity_and_order_mismatches_are_rejected()
    test_untracked_rollup_failure_still_fails_closed()
    test_pr549_production_shaped_open_edit_edit_history()
    test_actions_reads_are_complete_bounded_and_cached()
    test_invalid_config_and_unavailable_api_degrade_without_scan_exception()
    test_g7_uses_fresh_event_evidence()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all current-body compliance evidence tests passed")


if __name__ == "__main__":
    main()
