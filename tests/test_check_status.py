#!/usr/bin/env python3
"""
Direct unit tests for wheelhouse_core.check_status()'s compliance
aggregation and statusCheckRollup.state backstop, no network.

Regression coverage for card #392 + card #1537 concurrency duplicates:
two GraphQL check-run contexts sharing the compliance check's exact name
(one CANCELLED, one SUCCESS - the duplicate-approved-run / cancel-in-progress
scenario) must aggregate by equivalent check identity, not raw worst-wins and
not last-write-wins. A CANCELLED sibling is ignorable only when a completed
SUCCESS for the same required check exists on the current head; cancelled-only
evidence never becomes pass; FAILURE/TIMED_OUT/ACTION_REQUIRED/STARTUP_FAILURE
still fail; pending still waits. The statusCheckRollup.state backstop must not
re-poison a context set proven as only SUCCESS plus ignorable cancellations,
while genuine unaccounted failures remain fail-safe.

Also covers card #543's config gap: axi's `test_check_patterns` accepts BOTH
`build-and-test` (the JS SDK gate) and `drift` (the catalog-consistency gate),
which run on disjoint paths. A docs/catalog PR (compliance green + drift green,
no build-and-test) must compute tests=green and classify `merge-ready`, while
drift RED or PENDING must NOT be merge-ready (test worst-wins still holds); an
SDK PR must keep today's behavior exactly - build-and-test red/pending blocks,
green passes - so adding `drift` never weakens the SDK posture. The mixed case
(both present, either one non-green) stays worst-wins too.

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

AXI_CFG = core.load_config()["repos"]["axi"]


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
    # Card #1537 / lavish-axi#179 shape: concurrency cancel-in-progress left
    # CANCELLED siblings beside a legitimate SUCCESS on the same head.
    contexts = [
        check_run("PR must be raised via no-mistakes", conclusion="CANCELLED"),
        check_run("build-and-test (ubuntu-latest)"),
        check_run("PR must be raised via no-mistakes", conclusion="SUCCESS"),
    ]
    comp, tests, ci, names = core.check_status(pr_with(rollup("SUCCESS", contexts)), CFG)
    check(
        "duplicate compliance ctx CANCELLED-then-SUCCESS -> pass (ignorable cancel)",
        comp == "pass",
    )
    check("tests are unaffected by the compliance duplicate", tests == "green")


def test_duplicate_compliance_contexts_success_then_cancelled():
    # Same incident, contexts in the opposite array order - must not depend on
    # GraphQL array order (card #392 last-write-wins lesson still holds).
    contexts = [
        check_run("PR must be raised via no-mistakes", conclusion="SUCCESS"),
        check_run("build-and-test (ubuntu-latest)"),
        check_run("PR must be raised via no-mistakes", conclusion="CANCELLED"),
    ]
    comp, tests, ci, names = core.check_status(pr_with(rollup("SUCCESS", contexts)), CFG)
    check(
        "duplicate compliance ctx SUCCESS-then-CANCELLED -> pass (order-independent)",
        comp == "pass",
    )


def test_duplicate_compliance_two_cancelled_plus_success():
    # Exact lavish-axi#179 shape: one SUCCESS + two CANCELLED same-name runs.
    contexts = [
        check_run("PR must be raised via no-mistakes", conclusion="CANCELLED"),
        check_run("PR must be raised via no-mistakes", conclusion="SUCCESS"),
        check_run("PR must be raised via no-mistakes", conclusion="CANCELLED"),
        check_run("build-and-test (ubuntu-latest)"),
    ]
    comp, tests, ci, names = core.check_status(pr_with(rollup("FAILURE", contexts)), CFG)
    check(
        "two CANCELLED + SUCCESS same-name compliance -> pass",
        comp == "pass",
    )
    check(
        "rollup FAILURE from ignorable cancels does not re-poison proven pass",
        comp == "pass",
    )
    check("two CANCELLED + SUCCESS leaves tests green", tests == "green")


def test_cancelled_only_compliance_is_not_pass():
    contexts = [
        check_run("PR must be raised via no-mistakes", conclusion="CANCELLED"),
        check_run("PR must be raised via no-mistakes", conclusion="CANCELLED"),
        check_run("build-and-test (ubuntu-latest)"),
    ]
    comp, tests, ci, names = core.check_status(pr_with(rollup("FAILURE", contexts)), CFG)
    check("cancelled-only compliance never becomes pass", comp == "fail")


def test_success_plus_failure_still_fails():
    contexts = [
        check_run("PR must be raised via no-mistakes", conclusion="SUCCESS"),
        check_run("PR must be raised via no-mistakes", conclusion="FAILURE"),
        check_run("build-and-test (ubuntu-latest)"),
    ]
    for order in (contexts, list(reversed(contexts))):
        comp, tests, ci, names = core.check_status(pr_with(rollup("FAILURE", order)), CFG)
        check(
            "SUCCESS + FAILURE same-name compliance stays fail (order-independent)",
            comp == "fail",
        )


def test_success_plus_timed_out_still_fails():
    contexts = [
        check_run("PR must be raised via no-mistakes", conclusion="SUCCESS"),
        check_run("PR must be raised via no-mistakes", conclusion="TIMED_OUT"),
    ]
    comp, tests, ci, names = core.check_status(pr_with(rollup("FAILURE", contexts)), CFG)
    check("SUCCESS + TIMED_OUT same-name compliance stays fail", comp == "fail")


def test_success_plus_startup_failure_still_fails():
    contexts = [
        check_run("PR must be raised via no-mistakes", conclusion="SUCCESS"),
        check_run("PR must be raised via no-mistakes", conclusion="STARTUP_FAILURE"),
    ]
    comp, tests, ci, names = core.check_status(pr_with(rollup("FAILURE", contexts)), CFG)
    check("SUCCESS + STARTUP_FAILURE same-name compliance stays fail", comp == "fail")


def test_success_plus_action_required_still_fails_or_waits():
    contexts = [
        check_run("PR must be raised via no-mistakes", conclusion="SUCCESS"),
        check_run(
            "PR must be raised via no-mistakes",
            conclusion="ACTION_REQUIRED",
            status="COMPLETED",
        ),
    ]
    comp, tests, ci, names = core.check_status(pr_with(rollup("FAILURE", contexts)), CFG)
    check(
        "SUCCESS + ACTION_REQUIRED same-name compliance stays fail",
        comp == "fail",
    )


def test_success_plus_pending_sibling_stays_pending():
    contexts = [
        check_run("PR must be raised via no-mistakes", conclusion="SUCCESS"),
        check_run(
            "PR must be raised via no-mistakes",
            conclusion=None,
            status="IN_PROGRESS",
        ),
        check_run("build-and-test (ubuntu-latest)"),
    ]
    comp, tests, ci, names = core.check_status(
        pr_with(rollup("PENDING", contexts)), CFG
    )
    check("SUCCESS + pending same-name compliance stays pending", comp == "pending")


def test_test_pattern_cancelled_with_success_is_green():
    contexts = [
        check_run("PR must be raised via no-mistakes"),
        check_run("build-and-test (ubuntu-latest)", conclusion="CANCELLED"),
        check_run("build-and-test (ubuntu-latest)", conclusion="SUCCESS"),
    ]
    comp, tests, ci, names = core.check_status(pr_with(rollup("FAILURE", contexts)), CFG)
    check("test pattern CANCELLED + SUCCESS same name -> green", tests == "green")
    check("test pattern cancel sibling leaves compliance pass", comp == "pass")


def test_test_pattern_cancelled_only_is_fail():
    contexts = [
        check_run("PR must be raised via no-mistakes"),
        check_run("build-and-test (ubuntu-latest)", conclusion="CANCELLED"),
    ]
    comp, tests, ci, names = core.check_status(pr_with(rollup("FAILURE", contexts)), CFG)
    check("test pattern cancelled-only stays fail", tests == "fail")


def test_different_check_names_do_not_mask_each_other():
    # A cancelled run of a DIFFERENT check must not be treated as the compliance
    # success's sibling, and an untracked failure still trips the rollup backstop.
    contexts = [
        check_run("PR must be raised via no-mistakes", conclusion="SUCCESS"),
        check_run("Some other required check", conclusion="FAILURE"),
        check_run("build-and-test (ubuntu-latest)"),
    ]
    comp, tests, ci, names = core.check_status(pr_with(rollup("FAILURE", contexts)), CFG)
    check(
        "untracked failure keeps rollup FAILURE backstop (fail-safe)",
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


def test_rollup_failure_from_ignorable_cancels_does_not_repoison():
    contexts = [
        check_run("PR must be raised via no-mistakes", conclusion="SUCCESS"),
        check_run("PR must be raised via no-mistakes", conclusion="CANCELLED"),
        check_run("build-and-test (ubuntu-latest)", conclusion="SUCCESS"),
        check_run("build-and-test (ubuntu-latest)", conclusion="CANCELLED"),
        check_run("Generated files must not be hand-edited", conclusion="SUCCESS"),
    ]
    comp, tests, ci, names = core.check_status(pr_with(rollup("FAILURE", contexts)), CFG)
    check(
        "rollup FAILURE accounted for by ignorable cancels stays pass",
        comp == "pass",
    )
    check(
        "rollup FAILURE accounted for by ignorable cancels keeps tests green",
        tests == "green",
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


# --- card #543: axi accepts both `build-and-test` and `drift` test signals -----


def axi_bucket(contexts, rollup_state="SUCCESS", mergeable="MERGEABLE"):
    """The full path a real axi PR takes: check_status -> classify.

    Returns (comp, tests, bucket). `rollup_state` is held at SUCCESS so the
    routing is driven purely by the per-context test signal (the card #392
    rollup backstop is tested separately above); axi PRs are cross-repo forks,
    so cross_repo=True and a MERGEABLE PR stays in its merge/review bucket.
    """
    comp, tests, ci, _ = core.check_status(
        pr_with(rollup(rollup_state, contexts)), AXI_CFG
    )
    bucket = core.classify(False, comp, tests, ci, cross_repo=True, mergeable=mergeable)
    return comp, tests, bucket


def test_axi_catalog_pr_drift_green_is_merge_ready():
    # A docs/catalog PR: compliance + drift pass, build-and-test never ran.
    # Before the fix `drift` matched no pattern -> tests=none -> review-needed.
    contexts = [
        check_run("PR must be raised via no-mistakes"),
        check_run("Generated files must not be hand-edited"),
        check_run("drift"),
    ]
    comp, tests, bucket = axi_bucket(contexts)
    check("axi catalog: comp == pass", comp == "pass")
    check("axi catalog: drift-green signal makes tests == green", tests == "green")
    check("axi catalog: green drift classifies merge-ready", bucket == "merge-ready")


def test_axi_catalog_pr_drift_red_is_not_merge_ready():
    contexts = [
        check_run("PR must be raised via no-mistakes"),
        check_run("drift", conclusion="FAILURE"),
    ]
    comp, tests, bucket = axi_bucket(contexts)
    check("axi catalog drift RED: tests == fail (worst-wins)", tests == "fail")
    check("axi catalog drift RED: not merge-ready", bucket != "merge-ready")
    check("axi catalog drift RED: routes to fix-tests", bucket == "fix-tests")


def test_axi_catalog_pr_drift_pending_is_not_merge_ready():
    contexts = [
        check_run("PR must be raised via no-mistakes"),
        check_run("drift", conclusion=None, status="IN_PROGRESS"),
    ]
    comp, tests, bucket = axi_bucket(contexts)
    check("axi catalog drift PENDING: tests == pending", tests == "pending")
    check("axi catalog drift PENDING: not merge-ready", bucket != "merge-ready")
    check("axi catalog drift PENDING: routes to ci-running", bucket == "ci-running")


def test_axi_sdk_pr_build_and_test_green_unchanged():
    # SDK PR: the build-and-test matrix runs, drift never does. Adding `drift`
    # to the patterns must not change this from today's behavior.
    contexts = [
        check_run("PR must be raised via no-mistakes"),
        check_run("build-and-test (ubuntu-latest)"),
        check_run("build-and-test (macos-latest)"),
        check_run("build-and-test (windows-latest)"),
    ]
    comp, tests, bucket = axi_bucket(contexts)
    check("axi SDK green: tests == green (unchanged)", tests == "green")
    check("axi SDK green: classifies merge-ready (unchanged)", bucket == "merge-ready")


def test_axi_sdk_pr_build_and_test_red_still_blocks():
    contexts = [
        check_run("PR must be raised via no-mistakes"),
        check_run("build-and-test (ubuntu-latest)"),
        check_run("build-and-test (macos-latest)", conclusion="FAILURE"),
    ]
    comp, tests, bucket = axi_bucket(contexts)
    check("axi SDK build-and-test RED: tests == fail (unchanged)", tests == "fail")
    check("axi SDK build-and-test RED: not merge-ready", bucket != "merge-ready")
    check("axi SDK build-and-test RED: routes to fix-tests", bucket == "fix-tests")


def test_axi_sdk_pr_build_and_test_pending_still_blocks():
    contexts = [
        check_run("PR must be raised via no-mistakes"),
        check_run("build-and-test (ubuntu-latest)", conclusion=None, status="IN_PROGRESS"),
    ]
    comp, tests, bucket = axi_bucket(contexts)
    check("axi SDK build-and-test PENDING: tests == pending (unchanged)", tests == "pending")
    check("axi SDK build-and-test PENDING: not merge-ready", bucket != "merge-ready")


def test_axi_mixed_both_green_is_merge_ready():
    # Defensive: if some future PR ever triggered BOTH gates, all-green is green.
    contexts = [
        check_run("PR must be raised via no-mistakes"),
        check_run("build-and-test (ubuntu-latest)"),
        check_run("drift"),
    ]
    comp, tests, bucket = axi_bucket(contexts)
    check("axi mixed both-green: tests == green", tests == "green")
    check("axi mixed both-green: merge-ready", bucket == "merge-ready")


def test_axi_mixed_build_red_drift_green_not_green():
    contexts = [
        check_run("PR must be raised via no-mistakes"),
        check_run("build-and-test (ubuntu-latest)", conclusion="FAILURE"),
        check_run("drift"),
    ]
    comp, tests, bucket = axi_bucket(contexts)
    check("axi mixed build-RED drift-green: tests == fail (worst-wins)", tests == "fail")
    check("axi mixed build-RED drift-green: not merge-ready", bucket != "merge-ready")


def test_axi_mixed_build_green_drift_red_not_green():
    contexts = [
        check_run("PR must be raised via no-mistakes"),
        check_run("build-and-test (ubuntu-latest)"),
        check_run("drift", conclusion="FAILURE"),
    ]
    comp, tests, bucket = axi_bucket(contexts)
    check("axi mixed build-green drift-RED: tests == fail (worst-wins)", tests == "fail")
    check("axi mixed build-green drift-RED: not merge-ready", bucket != "merge-ready")


def main():
    test_duplicate_compliance_contexts_cancelled_then_success()
    test_duplicate_compliance_contexts_success_then_cancelled()
    test_duplicate_compliance_two_cancelled_plus_success()
    test_cancelled_only_compliance_is_not_pass()
    test_success_plus_failure_still_fails()
    test_success_plus_timed_out_still_fails()
    test_success_plus_startup_failure_still_fails()
    test_success_plus_action_required_still_fails_or_waits()
    test_success_plus_pending_sibling_stays_pending()
    test_test_pattern_cancelled_with_success_is_green()
    test_test_pattern_cancelled_only_is_fail()
    test_different_check_names_do_not_mask_each_other()
    test_rollup_failure_backstop_downgrades_all_success_read()
    test_rollup_failure_from_ignorable_cancels_does_not_repoison()
    test_rollup_failure_backstop_fails_closed_without_a_compliance_gate()
    test_genuinely_green_pr_still_passes()
    test_axi_catalog_pr_drift_green_is_merge_ready()
    test_axi_catalog_pr_drift_red_is_not_merge_ready()
    test_axi_catalog_pr_drift_pending_is_not_merge_ready()
    test_axi_sdk_pr_build_and_test_green_unchanged()
    test_axi_sdk_pr_build_and_test_red_still_blocks()
    test_axi_sdk_pr_build_and_test_pending_still_blocks()
    test_axi_mixed_both_green_is_merge_ready()
    test_axi_mixed_build_red_drift_green_not_green()
    test_axi_mixed_build_green_drift_red_not_green()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all check_status tests passed")


if __name__ == "__main__":
    main()
