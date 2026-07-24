#!/usr/bin/env python3
"""Offline contract and pure projection-planner coverage for Stage 1."""

import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import render_card  # noqa: E402
import target_observation as contracts  # noqa: E402
import target_reconcile  # noqa: E402

os.environ["GITHUB_REPOSITORY_OWNER"] = "owner"
_failures = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        _failures.append(name)


def observation(
    *,
    bucket="ci-running",
    comp="pending",
    tests="pending",
    check_phase="pending",
    complete=True,
    head="head-22222222222222222222222222222222222",
    expected_head="head-22222222222222222222222222222222222",
    pending_approval=False,
    error="",
    source="exact-reread",
):
    return contracts.make_observation(
        "owner",
        "demo",
        42,
        head_sha=head,
        base_sha="base-11111111111111111111111111111111111",
        expected_head_sha=expected_head,
        observed_at="2026-07-22T18:21:45Z",
        source=source,
        completeness={
            "complete": complete,
            "target": True,
            "checks": complete,
            "action_required_runs": complete,
            "head_matches_expected": not expected_head or head == expected_head,
            "check_contexts_seen": 2 if complete else 1,
            "check_contexts_total": 2,
            "mergeability": "conclusive",
        },
        facts={
            "open": True,
            "title": "Observed target",
            "author": "contributor",
            "updated_at": "2026-07-22T18:21:44Z",
            "draft": False,
            "cross_repo": True,
            "head_ref": "topic",
            "mergeable": "MERGEABLE",
            "ci": True,
            "comp": comp,
            "tests": tests,
            "bucket": bucket,
            "approval_phase": (
                "approval-required" if pending_approval else "not-required"
            ),
            "check_phase": check_phase,
            "configured_checks": (
                (
                    []
                    if comp in {"none", "n/a", "missing"}
                    else [
                        {
                            "name": "gate",
                            "role": "compliance",
                            "outcome": (
                                "pass"
                                if comp == "pass"
                                else ("fail" if comp == "fail" else "pending")
                            ),
                        }
                    ]
                )
                + (
                    []
                    if tests == "none"
                    else [
                        {
                            "name": "tests",
                            "role": "test",
                            "outcome": (
                                "pass"
                                if tests == "green"
                                else ("fail" if tests == "fail" else "pending")
                            ),
                        }
                    ]
                )
                if complete
                else []
            ),
        },
        changed_paths=contracts.changed_path_facts(
            ["src/demo.py"] if complete else [],
            complete=complete,
            count=1 if complete else None,
        ),
        error=error,
    )


def initial_observation():
    return observation(
        bucket="needs-ci-approval",
        comp="none",
        tests="none",
        check_phase="pending",
        expected_head="",
        pending_approval=True,
        source="bulk-scan",
    )


def item():
    return {
        "repo": "demo",
        "number": 42,
        "kind": "pr-review",
        "head_sha": "head-22222222222222222222222222222222222",
        "base_sha": "base-11111111111111111111111111111111111",
        "updated_at": "2026-07-22T18:20:00Z",
        "title": "Pre-action target",
        "author": "contributor",
        "bucket": "ci-state-unknown",
        "comp": "unknown",
        "tests": "unknown",
        "url": "https://github.com/owner/demo/pull/42",
        "summary": "invalidated",
        "recommendation": "wait",
        "priority": "low",
        "target_observation": initial_observation(),
    }


def approved_receipt(status="approved"):
    return contracts.make_approval_receipt(
        "owner",
        "demo",
        42,
        expected_head_sha="head-22222222222222222222222222222222222",
        initial_observation_id=initial_observation()["observation_id"],
        status=status,
        completed_at="2026-07-22T18:20:47Z",
    )


def test_observation_contract_identity_and_completeness_are_tamper_evident():
    value = observation()
    check(
        "observation: versioned identity/head/time/completeness round-trip",
        contracts.normalize_observation(value) == value
        and value["schema"] == contracts.OBSERVATION_SCHEMA
        and value["revision"]["head_sha"].startswith("head-")
        and value["observed_at"] == "2026-07-22T18:21:45Z"
        and value["completeness"]["complete"] is True,
    )
    tampered = copy.deepcopy(value)
    tampered["facts"]["tests"] = "green"
    check(
        "observation: fact tampering invalidates observation identity",
        contracts.normalize_observation(tampered) is None,
    )


def test_approval_receipts_record_effect_and_invalidation():
    approved = approved_receipt("approved")
    noop = approved_receipt("noop")
    uncertain = approved_receipt("error")
    check(
        "receipt: approved effect invalidates every projected CI dimension",
        approved["effect"] == "changed"
        and approved["requires_reobservation"] is True
        and approved["invalidates"] == list(contracts.APPROVAL_INVALIDATES),
    )
    check(
        "receipt: verified noop is unchanged and does not force a reread",
        noop["effect"] == "unchanged"
        and noop["invalidates"] == []
        and noop["requires_reobservation"] is False,
    )
    check(
        "receipt: failed/partial action is uncertain and invalidates safely",
        uncertain["effect"] == "unknown"
        and uncertain["requires_reobservation"] is True,
    )


def test_pending_projection_is_as_of_and_material():
    projected = target_reconcile.plan_ci_wait_projection(
        "owner", item(), observation(), approved_receipt()
    )
    ref = projected["projection_ref"]
    rendered = render_card.render(projected)
    state = render_card.parse_state_block(rendered["body"])
    check(
        "projection: successful approval followed by pending checks is ci-running",
        projected["bucket"] == "ci-running"
        and projected["comp"] == "pending"
        and projected["tests"] == "pending"
        and ref["freshness"] == "pending",
    )
    check(
        "projection: pending as-of time and observation identity persist",
        state["projection_ref"]["observation_id"]
        == observation()["observation_id"]
        and state["projection_ref"]["observed_at"]
        == "2026-07-22T18:21:45Z"
        and "checks were pending as of" in rendered["body"],
    )
    prior = dict(state)
    prior["bucket"] = "needs-ci-approval"
    check(
        "projection: bucket/freshness/reference head are material",
        render_card.material_changed(projected, prior),
    )


def test_complete_terminal_projection_uses_normal_classification():
    terminal = observation(
        bucket="merge-ready",
        comp="pass",
        tests="green",
        check_phase="terminal",
    )
    projected = target_reconcile.plan_ci_wait_projection(
        "owner", item(), terminal, approved_receipt()
    )
    check(
        "projection: complete terminal reread uses normal pass/green bucket",
        projected["bucket"] == "merge-ready"
        and projected["comp"] == "pass"
        and projected["tests"] == "green"
        and projected["projection_ref"]["freshness"] == "current",
    )


def test_incomplete_or_mismatched_observation_is_explicit_unknown():
    incomplete = observation(
        complete=False,
        error="context list incomplete",
    )
    projected = target_reconcile.plan_ci_wait_projection(
        "owner", item(), incomplete, approved_receipt()
    )
    rendered = render_card.render(projected)
    check(
        "projection: incomplete exact reread claims neither green nor approval",
        projected["bucket"] == "ci-state-unknown"
        and projected["comp"] == "unknown"
        and projected["tests"] == "unknown"
        and projected["projection_ref"]["freshness"] == "unknown",
    )
    check(
        "projection: user sees explicit unknown/last-known wording",
        "could not be completely verified" in rendered["body"].lower()
        and "not current assertions" in rendered["body"].lower(),
    )


def test_successful_receipt_never_reprojects_needs_approval_for_same_head():
    contradictory = observation(
        bucket="needs-ci-approval",
        comp="none",
        tests="none",
        check_phase="pending",
        pending_approval=True,
    )
    projected = target_reconcile.plan_ci_wait_projection(
        "owner", item(), contradictory, approved_receipt()
    )
    check(
        "projection: same-head approval receipt forbids needs-ci-approval",
        projected["bucket"] == "ci-state-unknown"
        and projected["projection_ref"]["freshness"] == "unknown"
        and projected["bucket"] != "needs-ci-approval",
    )


def main():
    test_observation_contract_identity_and_completeness_are_tamper_evident()
    test_approval_receipts_record_effect_and_invalidation()
    test_pending_projection_is_as_of_and_material()
    test_complete_terminal_projection_uses_normal_classification()
    test_incomplete_or_mismatched_observation_is_explicit_unknown()
    test_successful_receipt_never_reprojects_needs_approval_for_same_head()
    if _failures:
        print("\n%d failure(s): %s" % (len(_failures), ", ".join(_failures)))
        raise SystemExit(1)
    print("\nall target-observation tests passed")


if __name__ == "__main__":
    main()
