#!/usr/bin/env python3
"""Offline regression coverage for Option B immutable changed-path flow."""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import auto_merge
import card_projection
import target_observation
import test_ci_autoapprove as fixtures
import wheelhouse_core as core

FAILURES = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def green_pr(number):
    status = {
        "state": "SUCCESS",
        "contexts": {
            "nodes": [
                fixtures.check_run("Gate", "SUCCESS"),
                fixtures.check_run("test", "SUCCESS"),
            ]
        },
    }
    status["contexts"].update({
        "totalCount": 2,
        "pageInfo": {"hasNextPage": False},
    })
    pr = fixtures.pr_node(number, status, cross_repo=False)
    pr["headRefOid"] = "%040x" % number
    pr["baseRefOid"] = "a" * 40
    pr["mergeable"] = "MERGEABLE"
    return pr


def projection_for(item):
    return card_projection.plan_card_projection(item)


def test_high_volume_and_complete_control():
    calls = []

    def exact_paths(slug, base, head, expected):
        calls.append((slug, base, head, expected))
        return (["src/pr-%d.py" % int(head, 16)], True, True)

    _, items, _ = fixtures.run_build_repo(
        [green_pr(number) for number in range(1, 227)],
        auto_approve_ci=False,
        observation_compare=exact_paths,
    )
    observations = [item["target_observation"] for item in items]
    check("repro: 226-candidate repository emits 226 review items", len(items) == 226)
    check("fix: every high-volume observation carries complete paths", all(
        obs["completeness"]["changed_paths"] and obs["completeness"]["complete"]
        for obs in observations
    ))
    check("bound: exactly one immutable read per observed PR", len(calls) == 226)
    check("bound: reads use exact repository/base/head/count", calls[225] == (
        "owner/demo", "a" * 40, "%040x" % 226, 1
    ))
    control = observations[112]
    check("control: proven complete repository shape remains current", (
        control["target"]["number"] == 113
        and control["facts"]["comp"] == "pass"
        and control["facts"]["tests"] == "green"
    ))
    expected_digest = "sha256:" + hashlib.sha256(
        json.dumps(["src/pr-113.py"], separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    check("binding: count and digest cover exact normalized rows", (
        control["changed_paths"]["count"] == 1
        and control["changed_paths"]["digest"] == expected_digest
        and control["changed_paths"]["paths"] == ["src/pr-113.py"]
    ))


def test_incomplete_paths_stay_unknown_and_denied():
    for label, result in (
        ("absent", ([], False, False)),
        ("truncated", (["src/partial.py"], True, False)),
    ):
        _, items, _ = fixtures.run_build_repo(
            [green_pr(33)], auto_approve_ci=False,
            observation_compare=lambda *_args, value=result: value,
        )
        obs = items[0]["target_observation"]
        projected = projection_for(items[0])
        check("%s: observation never infers path completeness" % label,
              not obs["completeness"]["changed_paths"] and not obs["completeness"]["complete"])
        check("%s: visible current facts remain fail-safe unknown" % label, (
            "`ci-state-unknown`" in projected["body"]
            and "Compliance: `unknown`" in projected["body"]
            and "Tests: `unknown`" in projected["body"]
        ))


def test_exact_head_mismatch_and_shared_evaluator_reducer():
    pr = green_pr(33)
    facts = core._pr_observation_contract(
        "owner", "treehouse", pr, comp="pass", tests="green", ci=True,
        bucket="merge-ready", pending_ci_approval=False,
        action_required_complete=True, observed_at="2026-01-01T00:00:00Z",
        source="exact-reread", expected_head_sha="sha999",
        configured_checks=[
            {"name": "Gate", "role": "compliance", "outcome": "pass"},
            {"name": "test", "role": "test", "outcome": "pass"},
        ], changed_paths=target_observation.changed_path_facts(
            ["src/complete.py"], complete=True
        ),
    )
    check("head mismatch: complete paths cannot make observation current",
          facts["completeness"]["changed_paths"] and not facts["completeness"]["complete"])
    check("shared fact flow: evaluator delegates to authoritative reducer helper",
          auto_merge.immutable_compare_files.__doc__.startswith("Share the observation reducer"))


def test_bounded_immutable_read_success_and_failure():
    saved = core.gh_rest
    seen = []
    try:
        core.gh_rest = lambda path: seen.append(path) or {
            "files": [{"filename": "new.py", "previous_filename": "old.py"}]
        }
        paths, ok, complete = core.immutable_compare_files(
            "owner/repo", "b" * 40, "c" * 40, 1
        )
        check("exact read: immutable URL and rename rows are retained", (
            seen == ["/repos/owner/repo/compare/%s...%s" % ("b" * 40, "c" * 40)]
            and paths == ["new.py", "old.py"] and ok and complete
        ))
        _, ok, complete = core.immutable_compare_files(
            "owner/repo", "b" * 40, "c" * 40, 2
        )
        check("exact read: bounded partial response is incomplete", ok and not complete)
        core.gh_rest = lambda _path: (_ for _ in ()).throw(RuntimeError("offline"))
        check("exact read: transport failure remains unavailable",
              core.immutable_compare_files("owner/repo", "b" * 40, "c" * 40, 1) == ([], False, False))
    finally:
        core.gh_rest = saved


if __name__ == "__main__":
    test_high_volume_and_complete_control()
    test_incomplete_paths_stay_unknown_and_denied()
    test_exact_head_mismatch_and_shared_evaluator_reducer()
    test_bounded_immutable_read_success_and_failure()
    if FAILURES:
        print("\n%d failure(s)" % len(FAILURES))
        raise SystemExit(1)
    print("\nall Option B path-completeness tests passed")
