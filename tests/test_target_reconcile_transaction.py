#!/usr/bin/env python3
"""Production-composed fork-CI observation/reconciliation regressions.

Run: python tests/test_target_reconcile_transaction.py
"""

import os
import sys
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, TESTS)

import test_ci_autoapprove as ci_fixtures  # noqa: E402
import test_reconcile as reconcile_fixtures  # noqa: E402
import wheelhouse_core as core  # noqa: E402

_failures = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        _failures.append(name)


class TargetWorld:
    """Mutable GitHub target boundary used by production constructors."""

    def __init__(self):
        self.head_sha = "old-head-1111111111111111111111111111111"
        self.phase = "terminal"
        self.approvals = []
        self.observations = []
        self.exact_incomplete = False
        self.started = time.monotonic()

    def node(self):
        if self.phase == "terminal":
            status_rollup = ci_fixtures.rollup(
                [
                    ci_fixtures.check_run("Gate", "SUCCESS"),
                    ci_fixtures.check_run("build-test", "SUCCESS"),
                ]
            )
        elif self.phase == "running":
            status_rollup = ci_fixtures.rollup(
                [
                    ci_fixtures.check_run("Gate", None, status="IN_PROGRESS"),
                    ci_fixtures.check_run(
                        "build-test", None, status="IN_PROGRESS"
                    ),
                ]
            )
        else:
            status_rollup = None
        if status_rollup is not None:
            contexts = status_rollup["contexts"]["nodes"]
            status_rollup["contexts"].update(
                {
                    "totalCount": len(contexts),
                    "pageInfo": {"hasNextPage": False},
                }
            )
            status_rollup["state"] = (
                "SUCCESS" if self.phase == "terminal" else "PENDING"
            )
        node = ci_fixtures.pr_node(179, status_rollup)
        node.update(
            {
                "state": "OPEN",
                "title": "Production-composed CI transition",
                "headRefName": "contributor-branch",
                "headRefOid": self.head_sha,
                "baseRefOid": "base-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "updatedAt": "2026-07-22T18:20:23Z",
                "mergeable": "MERGEABLE",
                "closingIssuesReferences": {
                    "totalCount": 0,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [],
                },
            }
        )
        return node

    def graphql_repo(self, _owner, _name):
        self.observations.append(
            (round(time.monotonic() - self.started, 4), "bulk", self.phase, self.head_sha)
        )
        return ci_fixtures.graphql_data([self.node()])

    def graphql_pr(self, _owner, _name, _number):
        self.observations.append(
            (round(time.monotonic() - self.started, 4), "exact", self.phase, self.head_sha)
        )
        node = self.node()
        if self.exact_incomplete:
            rollup = node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
            rollup["contexts"]["pageInfo"]["hasNextPage"] = True
        return node

    def pending_runs(self, _slug, _head_ref, head_sha):
        if head_sha != self.head_sha:
            return (None, "head changed during pending-run enumeration")
        if self.phase == "approval-wait":
            return (
                [
                    {
                        "databaseId": 9001,
                        "workflowDatabaseId": 90,
                        "workflowName": "CI",
                        "headSha": head_sha,
                        "headBranch": "contributor-branch",
                    }
                ],
                "",
            )
        return ([], "")

    def approve(
        self,
        owner,
        name,
        number,
        posture=None,
        strict=False,
        expected_head_sha=None,
    ):
        self.approvals.append(
            {
                "target": "%s/%s#%s" % (owner, name, number),
                "head_sha": self.head_sha,
                "expected_head_sha": expected_head_sha,
                "strict": strict,
            }
        )
        if expected_head_sha and expected_head_sha != self.head_sha:
            return ("error", "head changed before approval")
        self.phase = "running"
        return ("approved", "approved current-head CI")


class ProductionTransition:
    def __init__(self):
        self.world = TargetWorld()
        self.repo_cfg = {
            "name": "demo",
            "compliance_check": "Gate",
            "test_check_patterns": ["test"],
        }
        self.saved = {}

    def __enter__(self):
        names = (
            "gh_graphql",
            "gh_graphql_pr",
            "_list_action_required_runs",
            "repo_pr_target_posture",
            "ci_safety",
            "approve_ci",
            "load_config",
        )
        self.saved = {name: getattr(core, name, None) for name in names}
        core.gh_graphql = self.world.graphql_repo
        # Stage 1's shared exact observer consumes this adapter. On the legacy
        # path reconcile never calls it, which is what makes the regression fail.
        core.gh_graphql_pr = self.world.graphql_pr
        core._list_action_required_runs = self.world.pending_runs
        core.repo_pr_target_posture = lambda _slug: ci_fixtures.CLEAN_POSTURE
        core.ci_safety = lambda *_args, **_kwargs: ci_fixtures.SAFE_VERDICT
        core.approve_ci = self.world.approve
        core.load_config = lambda: {
            "repos": {"demo": self.repo_cfg},
            "maintainer": "",
            "auto_merge": False,
        }
        self.old_env = {
            key: os.environ.get(key)
            for key in (
                "GH_TOKEN",
                "WHEELHOUSE_FLEET_TOKEN",
                "GITHUB_REPOSITORY_OWNER",
                "OWNER",
            )
        }
        os.environ["GH_TOKEN"] = "card-token"
        os.environ["WHEELHOUSE_FLEET_TOKEN"] = "fleet-token"
        os.environ["GITHUB_REPOSITORY_OWNER"] = "owner"
        os.environ["OWNER"] = "owner"
        return self

    def __exit__(self, *_exc):
        for name, value in self.saved.items():
            if value is None:
                try:
                    delattr(core, name)
                except AttributeError:
                    pass
            else:
                setattr(core, name, value)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def scan(self):
        result, items = core.build_repo("owner", self.repo_cfg, False)
        return {"repos": {"demo": result}, "items": items}, items


def _run_transition(final_head=None, final_phase="terminal", exact_incomplete=False):
    with ProductionTransition() as fixture:
        old_scan, old_items = fixture.scan()
        assert len(old_items) == 1 and old_items[0]["bucket"] == "merge-ready"
        lifecycle = reconcile_fixtures.ReconcileLifecycle(old_items[0])

        fixture.world.head_sha = "new-head-2222222222222222222222222222222"
        fixture.world.phase = "approval-wait"
        time.sleep(0.01)
        wait_scan, wait_items = fixture.scan()
        snapshot_returned = time.monotonic()
        assert wait_items == []
        assert wait_scan["repos"]["demo"]["ci_wait_pr_numbers"] == [179]
        assert len(fixture.world.approvals) == 1

        time.sleep(0.01)
        fixture.world.phase = final_phase
        fixture.world.exact_incomplete = exact_incomplete
        if final_head:
            fixture.world.head_sha = final_head
        transitioned = time.monotonic()
        time.sleep(0.01)
        lifecycle.run(wait_scan)
        projected = time.monotonic()
        state = core.parse_state_block(lifecycle.issue["body"])
        return {
            "state": state,
            "body": lifecycle.issue["body"],
            "approvals": fixture.world.approvals,
            "observations": fixture.world.observations,
            "timing": {
                "snapshot_to_transition": transitioned - snapshot_returned,
                "transition_to_projection": projected - transitioned,
            },
            "token_after_projection": os.environ.get("GH_TOKEN"),
        }


def test_same_scan_completion_uses_final_exact_classification():
    result = _run_transition()
    state = result["state"]
    check(
        "transaction: approval is bound to the scanned current head",
        result["approvals"][0]["expected_head_sha"]
        == "new-head-2222222222222222222222222222222",
    )
    check(
        "transaction: exact observation occurs after the terminal transition",
        any(
            source == "exact" and phase == "terminal"
            for _, source, phase, _ in result["observations"]
        ),
    )
    check(
        "transaction: same-scan completion projects terminal pass/green",
        state.get("head_sha") == "new-head-2222222222222222222222222222222"
        and state.get("comp") == "pass"
        and state.get("tests") == "green"
        and state.get("bucket") == "merge-ready",
    )
    check(
        "transaction: terminal projection cannot retain approval-needed copy",
        "`needs-ci-approval`" not in result["body"]
        and "checks are re-running" not in result["body"],
    )
    check(
        "transaction: exact fleet read restores the default card token before write",
        result["token_after_projection"] == "card-token",
    )
    projection = state.get("projection_ref") or {}
    check(
        "transaction: observation identity/time are persisted",
        projection.get("schema") == "wheelhouse.card-projection-ref/v1"
        and str(projection.get("observation_id") or "").startswith("sha256:")
        and str(projection.get("observed_at") or "").endswith("Z")
        and projection.get("freshness") == "current",
    )


def test_same_head_pending_reread_projects_pending_as_of_state():
    result = _run_transition(final_phase="running")
    state = result["state"]
    projection = state.get("projection_ref") or {}
    check(
        "transaction: post-approval current-head running checks stay pending",
        state.get("bucket") == "ci-running"
        and state.get("comp") == "pending"
        and state.get("tests") == "pending"
        and projection.get("freshness") == "pending",
    )
    check(
        "transaction: pending card exposes the exact as-of boundary",
        "checks were pending as of" in result["body"].lower()
        and str(projection.get("observed_at") or "").endswith("Z"),
    )


def test_incomplete_exact_reread_projects_unknown_not_old_green():
    result = _run_transition(exact_incomplete=True)
    state = result["state"]
    check(
        "transaction: incomplete exact context list fails to explicit unknown",
        state.get("bucket") == "ci-state-unknown"
        and state.get("comp") == "unknown"
        and state.get("tests") == "unknown"
        and (state.get("projection_ref") or {}).get("freshness") == "unknown",
    )
    check(
        "transaction: incomplete reread does not retain current green/approval copy",
        "`needs-ci-approval`" not in result["body"]
        and "- Tests: `green`" not in result["body"]
        and "could not be completely verified" in result["body"].lower(),
    )


def test_force_push_before_projection_fails_safe_without_old_head_claims():
    pushed_head = "force-head-333333333333333333333333333333"
    result = _run_transition(final_head=pushed_head)
    state = result["state"]
    check(
        "transaction: force-pushed target is observed exactly",
        any(
            source == "exact" and head == pushed_head
            for _, source, _, head in result["observations"]
        ),
    )
    check(
        "transaction: old receipt head is not projected as current",
        state.get("head_sha") == pushed_head
        and (state.get("projection_ref") or {}).get("freshness")
        in ("unknown", "last-known"),
    )
    check(
        "transaction: mismatched final observation claims neither green nor approval-needed",
        state.get("tests") != "green"
        and state.get("comp") != "pass"
        and state.get("bucket") != "needs-ci-approval"
        and "could not be completely verified" in result["body"].lower(),
    )


def main():
    test_same_scan_completion_uses_final_exact_classification()
    test_same_head_pending_reread_projects_pending_as_of_state()
    test_incomplete_exact_reread_projects_unknown_not_old_green()
    test_force_push_before_projection_fails_safe_without_old_head_claims()
    if _failures:
        print("\n%d failure(s): %s" % (len(_failures), ", ".join(_failures)))
        raise SystemExit(1)
    print("\nall target-reconcile transaction tests passed")


if __name__ == "__main__":
    main()
