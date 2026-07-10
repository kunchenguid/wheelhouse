#!/usr/bin/env python3
"""
Direct unit tests for wheelhouse_core.check_status()'s compliance
aggregation and statusCheckRollup.state backstop, no network.

Regression coverage for card #392: two GraphQL check-run contexts sharing
the compliance check's exact name (one CANCELLED, one SUCCESS - the
duplicate-approved-run scenario) must aggregate worst-wins, exactly like
`tests` already does, instead of letting whichever context the loop visits
last silently overwrite the result. Also covers the statusCheckRollup.state
backstop and a genuinely-green PR regression check.

Run: python tests/test_check_status.py
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)
import wheelhouse_core as core  # noqa: E402

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


CFG = {
    "compliance_check": "PR must be raised via no-mistakes",
    "test_check_patterns": ["build-and-test"],
}


def check_run(name, conclusion="SUCCESS", status="COMPLETED"):
    return {
        "__typename": "CheckRun",
        "name": name,
        "conclusion": conclusion,
        "status": status,
    }


def rollup(state, contexts):
    return {"state": state, "contexts": {"nodes": contexts}}


def pr_with(rollup_data):
    return {"commits": {"nodes": [{"commit": {"statusCheckRollup": rollup_data}}]}}


def test_duplicate_compliance_contexts_cancelled_then_success():
    contexts = [
        check_run("PR must be raised via no-mistakes", conclusion="CANCELLED"),
        check_run("build-and-test (ubuntu-latest)"),
        check_run("PR must be raised via no-mistakes", conclusion="SUCCESS"),
    ]
    comp, tests, ci, names = core.check_status(pr_with(rollup("SUCCESS", contexts)), CFG)
    check(
        "duplicate compliance ctx CANCELLED-then-SUCCESS -> fail (worst-wins)",
        comp == "fail",
    )
    check("tests are unaffected by the compliance duplicate", tests == "green")


def test_duplicate_compliance_contexts_success_then_cancelled():
    # Same incident, contexts in the opposite array order - the exact bug was
    # that the scalar overwrite made the result depend on iteration order.
    contexts = [
        check_run("PR must be raised via no-mistakes", conclusion="SUCCESS"),
        check_run("build-and-test (ubuntu-latest)"),
        check_run("PR must be raised via no-mistakes", conclusion="CANCELLED"),
    ]
    comp, tests, ci, names = core.check_status(pr_with(rollup("SUCCESS", contexts)), CFG)
    check(
        "duplicate compliance ctx SUCCESS-then-CANCELLED -> fail (order-independent)",
        comp == "fail",
    )


def test_rollup_failure_backstop_downgrades_all_success_read():
    # Every per-context read the config knows about is SUCCESS, but GitHub's
    # own authoritative rollup state disagrees (e.g. a required check this
    # config doesn't track) - the backstop must still refuse to say "pass".
    contexts = [
        check_run("PR must be raised via no-mistakes", conclusion="SUCCESS"),
        check_run("build-and-test (ubuntu-latest)"),
    ]
    comp, tests, ci, names = core.check_status(pr_with(rollup("FAILURE", contexts)), CFG)
    check(
        "rollup FAILURE backstop refuses an otherwise-pass compliance read",
        comp != "pass",
    )


def test_rollup_failure_backstop_fails_closed_without_a_compliance_gate():
    # `compliance_check: null` means there is intentionally no required gate,
    # not that an otherwise failed rollup becomes safe to merge. This is the
    # configuration used by repositories that have CI but no no-mistakes gate.
    no_gate_cfg = {
        "compliance_check": None,
        "test_check_patterns": ["test"],
    }
    contexts = [check_run("test (ubuntu-latest)")]
    comp, tests, ci, names = core.check_status(
        pr_with(rollup("FAILURE", contexts)), no_gate_cfg
    )
    check(
        "rollup FAILURE with no compliance gate -> fail (not n/a)",
        comp == "fail",
    )
    check("no-gate rollup failure preserves a green test signal", tests == "green")


def test_genuinely_green_pr_still_passes():
    contexts = [
        check_run("PR must be raised via no-mistakes", conclusion="SUCCESS"),
        check_run("build-and-test (ubuntu-latest)"),
        check_run("build-and-test (macos-latest)"),
    ]
    comp, tests, ci, names = core.check_status(pr_with(rollup("SUCCESS", contexts)), CFG)
    check("genuinely green PR: comp == pass (no regression)", comp == "pass")
    check("genuinely green PR: tests == green (no regression)", tests == "green")


def main():
    test_duplicate_compliance_contexts_cancelled_then_success()
    test_duplicate_compliance_contexts_success_then_cancelled()
    test_rollup_failure_backstop_downgrades_all_success_read()
    test_rollup_failure_backstop_fails_closed_without_a_compliance_gate()
    test_genuinely_green_pr_still_passes()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all check_status tests passed")


if __name__ == "__main__":
    main()
