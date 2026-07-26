#!/usr/bin/env python3
"""Complete offline Option B contract and E2E acceptance matrix.

No test in this module performs a network call or mutates a live card/target.
"""

import copy
import io
import inspect
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import assessment_admission as admission  # noqa: E402
import assessment_record  # noqa: E402
import auto_merge  # noqa: E402
import automerge_criteria as criteria  # noqa: E402
import card_projection  # noqa: E402
import decision_context  # noqa: E402
import projection_writer  # noqa: E402
import reconcile  # noqa: E402
import render_card  # noqa: E402
import scheduled_epoch  # noqa: E402
import target_observation  # noqa: E402
import test_auto_merge_v1 as automerge_fixture  # noqa: E402
import test_reconcile as reconcile_fixture  # noqa: E402
import wheelhouse_core as core  # noqa: E402

FAILURES = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def observation(
    number=901,
    head="head-901",
    *,
    checks=None,
    paths=None,
    bucket="merge-ready",
    complete=True,
    source="bulk-scan",
    observed_at="2026-07-23T12:00:00Z",
):
    checks = checks if checks is not None else [
        {"name": "PR must be raised via no-mistakes", "role": "compliance", "outcome": "pass"},
        {"name": "Ubuntu", "role": "test", "outcome": "pass"},
        {"name": "macOS", "role": "test", "outcome": "pass"},
        {"name": "Windows", "role": "test", "outcome": "pass"},
        {"name": "E2E", "role": "test", "outcome": "pass"},
        {"name": "deploy", "role": "informational", "outcome": "pending"},
    ]
    paths = paths if paths is not None else ["src/queue.py", "src/writer.py"]
    test_outcomes = [row["outcome"] for row in checks if row["role"] == "test"]
    tests = (
        "none"
        if not test_outcomes
        else "fail"
        if "fail" in test_outcomes
        else "pending"
        if "pending" in test_outcomes
        else "green"
    )
    return target_observation.make_observation(
        "owner",
        "firstmate",
        number,
        head_sha=head,
        base_sha="base-main",
        expected_head_sha=head,
        observed_at=observed_at,
        source=source,
        completeness={
            "complete": complete,
            "target": True,
            "checks": complete,
            "configured_checks": complete,
            "changed_paths": complete,
            "action_required_runs": complete,
            "head_matches_expected": True,
            "check_contexts_seen": len(checks) if complete else 0,
            "check_contexts_total": len(checks),
            "mergeability": "conclusive",
        },
        facts={
            "open": True,
            "title": "Option B fixture %s" % number,
            "author": "contributor",
            "updated_at": "2026-07-23T11:59:59Z",
            "draft": False,
            "cross_repo": False,
            "head_ref": "option-b-%s" % number,
            "mergeable": "MERGEABLE",
            "ci": True,
            "comp": "pass" if complete else "unknown",
            "tests": tests if complete else "unknown",
            "bucket": bucket if complete else "ci-state-unknown",
            "approval_phase": "not-required",
            "check_phase": "terminal" if complete else "unknown",
            "configured_checks": checks if complete else [],
        },
        changed_paths=target_observation.changed_path_facts(
            paths if complete else [], complete=complete
        ),
        error="" if complete else "fixture observation incomplete",
    )


def candidate(
    number,
    head,
    paths,
    *,
    references=None,
    closing_issues=None,
    card_issue=0,
    repo="firstmate",
):
    return {
        "owner": "owner",
        "repo": repo,
        "number": number,
        "head_sha": head,
        "title": "Related fixture %s" % number,
        "paths_complete": True,
        "paths": sorted(paths),
        "closing_complete": True,
        "closing_issues": sorted(closing_issues or []),
        "references_complete": True,
        "references": references or [],
        "card_issue": card_issue,
        "url": "https://github.com/owner/%s/pull/%s" % (repo, number),
        "card_url": (
            "https://github.com/owner/wheelhouse/issues/%s" % card_issue
            if card_issue
            else ""
        ),
    }


def context_for(obs, rows=None, **snapshot_options):
    rows = rows or [
        candidate(
            obs["target"]["number"],
            obs["revision"]["head_sha"],
            obs["changed_paths"]["paths"],
            card_issue=1901,
        )
    ]
    snapshot = decision_context.repository_snapshot(
        rows,
        "2026-07-23T12:00:00Z",
        **snapshot_options,
    )
    return decision_context.build_decision_context(obs, snapshot)


def assessment_for(obs, context, *, action="merge", basis_kind="other", names=None):
    return admission.admit_assessment(
        {
            "summary": "Review of the exact Option B fixture.",
            "product_implications": "The decision remains bounded to this revision.",
            "recommended_action": action,
            "recommended_reason": "Use the deterministic controls.",
            "recommendation_basis": {
                "kind": basis_kind,
                "observation_id": obs["observation_id"],
                "context_id": context["context_id"],
                "check_names": sorted(names or []),
            },
        },
        obs,
        context,
    )


def item_for(obs, context=None, assessment=None):
    facts = obs["facts"]
    value = {
        "repo": obs["target"]["repo"],
        "number": obs["target"]["number"],
        "kind": "pr-review",
        "head_sha": obs["revision"]["head_sha"],
        "base_sha": obs["revision"]["base_sha"],
        "title": facts["title"],
        "author": facts["author"],
        "updated_at": facts["updated_at"],
        "bucket": facts["bucket"],
        "comp": facts["comp"],
        "tests": facts["tests"],
        "priority": "med",
        "url": "https://github.com/owner/%s/pull/%s"
        % (obs["target"]["repo"], obs["target"]["number"]),
        "summary": "Current exact revision.",
        "recommendation": "Use deterministic controls.",
        "target_observation": obs,
        "decision_context": context or context_for(obs),
    }
    if assessment:
        value["assessment"] = assessment
    return value


def issue_from_projection(projection, number=77):
    return {
        "number": number,
        "title": projection["title"],
        "body": projection["body"],
        "labels": [{"name": label} for label in projection["managed_labels"]],
        "state": "OPEN",
        "updatedAt": "2026-07-23T12:00:01Z",
        "author": {"login": "app/github-actions"},
        "comments": [],
    }


def test_review_observation_contract_and_v1_compatibility():
    obs = observation()
    check(
        "contract: native ReviewObservation v2 round-trips",
        target_observation.normalize_review_observation(obs) == obs,
    )
    tampered = copy.deepcopy(obs)
    tampered["facts"]["tests"] = "fail"
    check(
        "contract: identity tampering is rejected",
        target_observation.normalize_review_observation(tampered) is None,
    )
    contradictory = copy.deepcopy(obs)
    contradictory["facts"]["tests"] = "none"
    contradictory["observation_id"] = target_observation._review_identity(
        contradictory
    )
    check(
        "contract: recomputed identity cannot hide aggregate/check-row contradiction",
        target_observation.normalize_review_observation(contradictory) is None,
    )

    legacy = {
        "schema": target_observation.OBSERVATION_SCHEMA_V1,
        "target": obs["target"],
        "revision": obs["revision"],
        "observed_at": obs["observed_at"],
        "source": obs["source"],
        "completeness": {
            key: value
            for key, value in obs["completeness"].items()
            if key not in {"configured_checks", "changed_paths"}
        },
        "facts": {
            key: value
            for key, value in obs["facts"].items()
            if key != "configured_checks"
        },
    }
    legacy["observation_id"] = target_observation._identity("sha256:", legacy)
    migrated = target_observation.normalize_review_observation(legacy)
    check(
        "contract: concrete persisted v1 is dual-read as strict unknown",
        migrated is not None
        and migrated["compatibility"] == "persisted-v1"
        and migrated["completeness"]["complete"] is False
        and migrated["completeness"]["configured_checks"] is False
        and migrated["completeness"]["changed_paths"] is False,
    )
    later = observation(observed_at="2026-07-23T13:00:00Z")
    check(
        "contract: observation identity is semantic across collection times",
        later["observation_id"] == obs["observation_id"]
        and later["observed_at"] != obs["observed_at"],
    )


def test_decision_context_contract():
    obs901 = observation(901, "head-901", paths=["src/central.py", "src/queue.py"])
    rows = [
        candidate(
            901,
            "head-901",
            ["src/central.py", "src/queue.py"],
            references=[{"owner": "owner", "repo": "tasks-axi", "number": 21}],
            card_issue=1901,
        ),
        candidate(905, "head-905", ["src/central.py", "src/writer.py"], card_issue=1905),
        candidate(21, "head-21", ["packages/tasks.py"], card_issue=1921, repo="tasks-axi"),
    ]
    context = context_for(obs901, rows)
    relations = {
        (entry["target"]["repo"], entry["target"]["number"]): {
            relation["kind"] for relation in entry["relations"]
        }
        for entry in context["candidates"]
    }
    check(
        "contract: exact shared-path and explicit-reference relations are neutral",
        context["status"] == "complete"
        and "exact-shared-path" in relations[("firstmate", 905)]
        and "explicit-reference" in relations[("tasks-axi", 21)],
    )
    refs, refs_complete = core._explicit_pr_references(
        "Depends on https://github.com/owner/tasks-axi/issues/21 and owner/firstmate#905"
    )
    check(
        "contract: trusted metadata extraction covers explicit PR and issue references",
        refs_complete
        and refs
        == [
            {"owner": "owner", "repo": "firstmate", "number": 905},
            {"owner": "owner", "repo": "tasks-axi", "number": 21},
        ],
    )
    check(
        "contract: deterministic strength sort and context identity round-trip",
        decision_context.normalize_decision_context(context) == context
        and [entry["target"]["number"] for entry in context["candidates"]] == [21, 905],
    )
    legacy_context = copy.deepcopy(context)
    legacy_context["schema"] = decision_context.CONTEXT_SCHEMA_V1
    legacy_context.pop("related_candidate_count")
    for entry in legacy_context["candidates"]:
        entry.pop("title")
        entry["card_url"] = ""
    # v1 was only ever written in the legacy owner/repo/number order; a
    # persisted v1 payload in any other order is not a valid v1 artifact.
    legacy_context["candidates"].sort(
        key=lambda row: (
            row["target"]["owner"],
            row["target"]["repo"],
            row["target"]["number"],
        )
    )
    legacy_context["context_id"] = decision_context._context_identity(legacy_context)
    check(
        "contract: persisted v1 context stays readable but cannot feed the v2 model handoff",
        decision_context.normalize_decision_context(legacy_context) == legacy_context
        and decision_context.compact_model_context(legacy_context) is None,
    )
    for field in ("url", "card_url"):
        legacy_long_url = copy.deepcopy(legacy_context)
        prefix = "https://github.com/"
        legacy_long_url["candidates"][0][field] = prefix + "x" * (
            decision_context.LEGACY_MAX_GITHUB_URL - len(prefix)
        )
        legacy_long_url["context_id"] = decision_context._context_identity(
            legacy_long_url
        )
        check(
            f"contract: persisted v1 {field} retains its legacy URL bound",
            decision_context.normalize_decision_context(legacy_long_url)
            == legacy_long_url,
        )
        legacy_long_url["candidates"][0][field] += "x"
        legacy_long_url["context_id"] = decision_context._context_identity(
            legacy_long_url
        )
        check(
            f"contract: persisted v1 {field} rejects URLs beyond its legacy bound",
            decision_context.normalize_decision_context(legacy_long_url) is None,
        )
    later_observation = observation(
        901,
        "head-901",
        paths=["src/central.py", "src/queue.py"],
        observed_at="2026-07-23T13:00:00Z",
    )
    later_snapshot = decision_context.repository_snapshot(
        rows, "2026-07-23T13:00:00Z"
    )
    later_context = decision_context.build_decision_context(
        later_observation, later_snapshot
    )
    admitted = assessment_for(obs901, context)
    readmitted = admission.admit_assessment(
        {
            "summary": admitted["summary"],
            "product_implications": admitted["product_implications"],
            "recommended_action": admitted["recommendation"]["action"],
            "recommended_reason": admitted["recommendation"]["reason"],
            "recommendation_basis": admitted["recommendation"]["basis"],
        },
        later_observation,
        later_context,
    )
    check(
        "contract: collection time alone preserves context and assessment admission",
        later_snapshot["snapshot_id"]
        == context["repository_snapshot"]["snapshot_id"]
        and later_context["context_id"] == context["context_id"]
        and admission.admitted(readmitted),
    )
    cross_repo_rows = [
        candidate(
            901,
            "head-901",
            ["src/central.py"],
            references=[{"owner": "owner", "repo": "tasks-axi", "number": 21}],
            closing_issues=[10],
        ),
        candidate(
            21,
            "head-21",
            ["packages/tasks.py"],
            closing_issues=[10],
            repo="tasks-axi",
        ),
    ]
    cross_repo = context_for(obs901, cross_repo_rows)
    cross_repo_relations = {
        relation["kind"]
        for relation in cross_repo["candidates"][0]["relations"]
    }
    check(
        "contract: same-closing-issue identity is repository-qualified",
        cross_repo_relations == {"explicit-reference"},
    )
    truncated = context_for(
        obs901,
        rows,
        complete=False,
        reason="repository-candidate-bound",
        candidate_count=9,
    )
    check(
        "contract: over-bound snapshot is truncated and never claims none found",
        truncated["status"] == "truncated"
        and truncated["reason"] == "repository-candidate-bound",
    )
    many_paths = ["src/shared-%s.py" % index for index in range(5)]
    relation_bound = context_for(
        observation(901, "head-901", paths=many_paths),
        [
            candidate(901, "head-901", many_paths),
            candidate(905, "head-905", many_paths),
        ],
    )
    shared = relation_bound["candidates"][0]["relations"][0]["paths"]
    check(
        "contract: bounded relation facts say truncated instead of claiming completeness",
        relation_bound["status"] == "truncated"
        and relation_bound["reason"] == "relation_bound"
        and len(shared) == decision_context.MAX_SHARED_PATHS,
    )


def test_assessment_admission_and_class_tristate():
    obs = observation()
    context = context_for(obs)
    absent_other = admission.admit_assessment(
        {
            "summary": "Schema-valid other basis.",
            "product_implications": "The canonical empty basis remains actionable.",
            "recommended_action": "merge",
            "recommended_reason": "The exact current revision is ready.",
            "recommendation_basis": {
                "kind": "other",
                "observation_id": obs["observation_id"],
                "context_id": context["context_id"],
            },
        },
        obs,
        context,
    )
    check(
        "contract: absent other check_names is admitted as empty",
        admission.admitted(absent_other)
        and absent_other["recommendation"]["basis"]["check_names"] == [],
    )
    card_1704_basis = {
        "kind": "other",
        "observation_id": "sha256:528ef0ef399f77be40d58b3d31e4bf4978b4352f2dfc4845e09ec71601560cc2",
        "context_id": "sha256:4ba1c003153d9dfc0d7540c6f3d3e77aad0a1596f3cbb65fa8447d7194194a32",
        "check_names": [
            "PR must be raised via no-mistakes",
            "Behavior tests (Herdr)",
            "Test coverage guard",
        ],
    }
    card_1704 = admission.admit_assessment(
        {
            "summary": "Malformed other basis.",
            "product_implications": "It must remain advisory only.",
            "recommended_action": "merge",
            "recommended_reason": "This must not become authority.",
            "recommendation_basis": card_1704_basis,
        },
        obs,
        context,
    )
    check(
        "contract: exact card-1704 basis remains refused by admission",
        card_1704 is None and admission.normalize_basis(card_1704_basis) is None,
    )
    for basis_kind in ("configured-tests-not-run", "configured-tests-not-green"):
        empty_configured_basis = {
            "kind": basis_kind,
            "observation_id": obs["observation_id"],
            "context_id": context["context_id"],
            "check_names": [],
        }
        check(
            "contract: %s basis with no named check remains accepted by admission"
            % basis_kind,
            admission.normalize_basis(empty_configured_basis) == empty_configured_basis,
        )
    rejected = assessment_for(
        obs,
        context,
        action="hold",
        basis_kind="configured-tests-not-run",
        names=["Ubuntu", "macOS", "Windows", "E2E"],
    )
    check(
        "contract: current green checks reject a tests-not-run basis",
        rejected["admission"] == {
            "schema": admission.ADMISSION_SCHEMA,
            "status": "rejected",
            "reason": "basis.checks_contradict",
        },
    )
    failing_rows = copy.deepcopy(obs["facts"]["configured_checks"])
    for row in failing_rows:
        if row["name"] == "Ubuntu":
            row["outcome"] = "fail"
    failing = observation(checks=failing_rows)
    failing_context = context_for(failing)
    admitted = assessment_for(
        failing,
        failing_context,
        action="request-changes",
        basis_kind="configured-tests-not-green",
        names=["Ubuntu"],
    )
    check(
        "contract: exact failing configured test admits the control recommendation",
        admission.admitted(admitted),
    )
    stale = copy.deepcopy(admitted)
    stale["target"]["head_sha"] = "old-head"
    check(
        "contract: assessment tampering is rejected",
        admission.normalize_assessment(stale) is None,
    )
    other_obs = observation(902, "head-902")
    other_context = context_for(other_obs)
    stale_binding = admission.admit_assessment(
        {
            "summary": "Stale advisory",
            "product_implications": "Must not act.",
            "recommended_action": "merge",
            "recommended_reason": "Old context.",
            "recommendation_basis": {
                "kind": "other",
                "observation_id": other_obs["observation_id"],
                "context_id": other_context["context_id"],
                "check_names": [],
            },
        },
        obs,
        context,
    )
    invalid_action = admission.admit_assessment(
        {
            "summary": "Malformed advisory",
            "product_implications": "Must not act.",
            "recommended_action": "approve-ci",
            "recommended_reason": "Wrong action family.",
            "recommendation_basis": {
                "kind": "other",
                "observation_id": obs["observation_id"],
                "context_id": context["context_id"],
                "check_names": [],
            },
        },
        obs,
        context,
    )
    check(
        "contract: stale binding and unsupported action cannot become admitted",
        stale_binding["admission"]["status"] == "stale"
        and invalid_action is None,
    )

    invalid_facts, _ = auto_merge.behavior_verdict_facts(
        {
            "behavior_class": "INELIGIBLE",
            "changes_existing_or_default_behavior": False,
            "optin_default_off": False,
        }
    )
    check(
        "contract: invalid class leaves class-C dependent fact unavailable",
        invalid_facts["g6_behavior_class"]["status"] == criteria.STATUS_UNMET
        and invalid_facts["g6_default_behavior"]["status"] == criteria.STATUS_MET
        and invalid_facts["g6_class_c_mode"]["status"] == criteria.STATUS_UNAVAILABLE,
    )
    controls = {
        cls: auto_merge.behavior_verdict_facts(
            {
                "behavior_class": cls,
                "behavior_assertions": [],
                "changes_existing_or_default_behavior": False,
                "optin_default_off": optin,
            }
        )[0]["g6_class_c_mode"]["status"]
        for cls, optin in (("A", False), ("B", False), ("C", True))
    }
    c_false = auto_merge.behavior_verdict_facts(
        {
            "behavior_class": "C",
            "behavior_assertions": [],
            "changes_existing_or_default_behavior": False,
            "optin_default_off": False,
        }
    )[0]["g6_class_c_mode"]["status"]
    check(
        "contract: valid A/B/C controls retain tri-state semantics",
        controls == {"A": criteria.STATUS_MET, "B": criteria.STATUS_MET, "C": criteria.STATUS_MET}
        and c_false == criteria.STATUS_UNMET,
    )


def test_scheduled_epoch_contract():
    body = scheduled_epoch.render(7, "12345")
    check(
        "contract: scheduled epoch round-trips exact bounded state",
        scheduled_epoch.parse(body)
        == {
            "schema": scheduled_epoch.SCHEMA,
            "epoch": 7,
            "run_id": "12345",
        },
    )
    check(
        "contract: malformed and duplicate epoch records fail closed",
        scheduled_epoch.parse(body + "\n" + body) is None
        and scheduled_epoch.parse(body.replace('"epoch":7', '"epoch":true')) is None,
    )
    old_actions = os.environ.get("GITHUB_ACTIONS")
    old_event = os.environ.get("GITHUB_EVENT_NAME")
    os.environ["GITHUB_ACTIONS"] = "true"
    os.environ["GITHUB_EVENT_NAME"] = "workflow_dispatch"
    try:
        manual = scheduled_epoch.advance()
    finally:
        if old_actions is None:
            os.environ.pop("GITHUB_ACTIONS", None)
        else:
            os.environ["GITHUB_ACTIONS"] = old_actions
        if old_event is None:
            os.environ.pop("GITHUB_EVENT_NAME", None)
        else:
            os.environ["GITHUB_EVENT_NAME"] = old_event
    check("contract: manual run cannot advance the epoch ledger", manual == 0)


def test_incomplete_v2_context_allows_advisory_spend():
    obs = observation()
    context = decision_context.unavailable_context(obs, "snapshot.unavailable")
    item = item_for(obs, context)
    labels = [{"name": "needs-decision"}]
    check(
        "contract: bound unavailable context permits bounded advisory triage",
        render_card.should_hold(item, True) is True,
    )
    projection = card_projection.plan_card_projection(
        item, prior={}, held=True, has_token=True
    )
    state = core.parse_state_block(projection["body"])
    check(
        "contract: unavailable advisory context creates a held card eligible once",
        "Related-work context is **unavailable**" in projection["body"]
        and "- [ ] Merge it" not in projection["body"]
        and render_card.should_auto_triage(item, state, labels, True),
    )
    # DecisionContext neutrality: context status, content, and identity never
    # grant or deny authority. Only the exact target observation/head binding,
    # observation completeness, well-formedness, and check-basis truth do.
    unavailable_assessment = assessment_for(obs, context)
    check(
        "contract: unavailable context neither grants nor denies authority",
        admission.admitted(unavailable_assessment),
    )
    rotated_context = context_for(
        obs,
        [
            candidate(901, "head-901", ["src/queue.py", "src/writer.py"]),
            candidate(955, "head-955", ["docs/guide.md"]),
        ],
    )
    check(
        "contract: context identity rotation alone keeps a current assessment admitted",
        rotated_context["context_id"] != context["context_id"]
        and admission.admitted(
            admission.admit_assessment(
                {
                    "summary": unavailable_assessment["summary"],
                    "product_implications": unavailable_assessment[
                        "product_implications"
                    ],
                    "recommended_action": unavailable_assessment["recommendation"][
                        "action"
                    ],
                    "recommended_reason": unavailable_assessment["recommendation"][
                        "reason"
                    ],
                    "recommendation_basis": unavailable_assessment["recommendation"][
                        "basis"
                    ],
                },
                obs,
                rotated_context,
            )
        ),
    )
    tampered_context = copy.deepcopy(context)
    tampered_context["reason"] = "forged"
    missing_observation = admission.admit_assessment(
        {
            "summary": "s",
            "product_implications": "p",
            "recommended_action": "hold",
            "recommended_reason": "r",
            "recommendation_basis": unavailable_assessment["recommendation"]["basis"],
        },
        None,
        context,
    )
    other_obs = observation(902, "head-902")
    stale_observation = admission.admit_assessment(
        {
            "summary": "s",
            "product_implications": "p",
            "recommended_action": "hold",
            "recommended_reason": "r",
            "recommendation_basis": unavailable_assessment["recommendation"]["basis"],
        },
        other_obs,
        context,
    )
    incomplete_obs = observation(complete=False)
    incomplete_observation = admission.admit_assessment(
        {
            "summary": "s",
            "product_implications": "p",
            "recommended_action": "hold",
            "recommended_reason": "r",
            "recommendation_basis": {
                "kind": "other",
                "observation_id": incomplete_obs["observation_id"],
                "context_id": context["context_id"],
                "check_names": [],
            },
        },
        incomplete_obs,
        context,
    )
    check(
        "contract: malformed/missing/rotated target evidence still denies authority",
        admission.admit_assessment(
            {
                "summary": "s",
                "product_implications": "p",
                "recommended_action": "hold",
                "recommended_reason": "r",
                "recommendation_basis": unavailable_assessment["recommendation"][
                    "basis"
                ],
            },
            obs,
            tampered_context,
        )["admission"]["reason"]
        == "binding.unavailable"
        and missing_observation["admission"]["reason"] == "binding.unavailable"
        and stale_observation["admission"]["status"] == "stale"
        and stale_observation["admission"]["reason"] == "binding.mismatch"
        and incomplete_observation["admission"]["reason"]
        == "observation.incomplete",
    )

    complete_context = context_for(obs)
    complete_item = item_for(obs, complete_context)
    legacy_state = {
        "repo": "firstmate",
        "number": 901,
        "kind": "pr-review",
        "head_sha": "head-901",
    }
    check(
        "contract: legacy first-spend card requires a targeted projection migration",
        render_card.triage_projection_migration_needed(
            complete_item,
            legacy_state,
            [{"name": "needs-decision"}],
            True,
        )
        and not render_card.should_auto_triage(
            complete_item,
            legacy_state,
            [{"name": "needs-decision"}],
            True,
        ),
    )
    migrated = card_projection.plan_card_projection(complete_item, prior={})
    migrated_state = core.parse_state_block(migrated["body"])
    check(
        "contract: exact migrated card becomes eligible for one normal cache-miss spend",
        render_card.should_auto_triage(
            complete_item,
            migrated_state,
            [{"name": "needs-decision"}],
            True,
        ),
    )


def test_triage_suppression_is_visible_and_fail_closed():
    obs = observation()
    context = context_for(obs)
    disabled = item_for(obs, context)
    disabled["auto_triage"] = False
    disabled_projection = card_projection.plan_card_projection(
        disabled, prior={}, has_token=True
    )
    missing_context = item_for(obs, context)
    missing_context.pop("decision_context")
    missing_projection = card_projection.plan_card_projection(
        missing_context, prior={}, has_token=True
    )
    issue_body = render_card.render(
        {
            "repo": "firstmate",
            "number": 77,
            "kind": "issue-triage",
            "head_sha": "",
            "updated_at": "2026-07-23T12:00:00Z",
            "title": "Issue fixture",
            "auto_triage_issues": True,
        },
        has_token=False,
    )["body"]
    check(
        "triage suppression: policy and missing binding are captain-visible",
        "repository policy disables it" in disabled_projection["body"]
        and "related-work context is missing, malformed" in missing_projection["body"]
        and not render_card.should_hold(missing_context, True),
    )
    check(
        "triage suppression: token absence is visible for issue triage too",
        "model credential is not configured" in issue_body,
    )


def _triage_payload(obs, context, *, context_id=None, observation_id=None):
    return {
        "summary": "Bounded advisory review is visible.",
        "product_implications": "Action authority remains independently admitted.",
        "recommended_action": "merge",
        "recommended_reason": "Review the exact current revision.",
        "evidence": "target.txt: 'fixture evidence'",
        "recommendation_basis": {
            "kind": "other",
            "observation_id": observation_id or obs["observation_id"],
            "context_id": context_id or context["context_id"],
            "check_names": [],
        },
    }


def _scan_context_from_observed(obs, rows):
    result = {
        "name": "firstmate",
        "ok": True,
        "open_pr_numbers": [row["number"] for row in rows],
        "open_issue_numbers": [],
        "decision_context_candidates": copy.deepcopy(rows),
        "decision_reference_candidates": [],
        "truncated": False,
        "warning": "",
    }
    item = item_for(obs, context_for(obs))
    item.pop("decision_context", None)
    saved_owner = core.get_owner
    saved_config = core.load_config
    saved_build = core.build_repo
    core.get_owner = lambda: "owner"
    core.load_config = lambda: {
        "repos": {"firstmate": {"name": "firstmate"}},
        "card_issues": False,
        "auto_approve_ci": True,
        "auto_merge": False,
        "auto_triage": True,
        "auto_triage_issues": True,
        "triage_attempt_cap_per_revision": 2,
        "triage_daily_ceiling": 1200,
        "pending_contributor_cleanup": False,
        "pending_contributor_cleanup_days": 14,
        "pending_contributor_reminder_days": 10,
        "pending_contributor_cleanup_targets": ["pr"],
    }
    core.build_repo = lambda *_args, **_kwargs: (copy.deepcopy(result), [copy.deepcopy(item)])
    stdout = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            core.cmd_scan()
    finally:
        core.get_owner = saved_owner
        core.load_config = saved_config
        core.build_repo = saved_build
    return json.loads(stdout.getvalue())["items"][0]


def test_card_1663_high_volume_context_queues_once():
    obs = observation(200, "head-200", paths=["src/shared.py"])
    enriched = [
        {
            "number": number,
            "head_sha": "head-%s" % number,
            "title": "Related fixture %s" % number,
            "changed_paths": {
                "complete": True,
                "paths_truncated": False,
                "paths": (
                    ["src/shared.py"]
                    if number in {200, 220, 221, 222}
                    else ["src/other-%s.py" % number]
                ),
            },
            "closes": [],
            "explicit_references_complete": True,
            "explicit_references": [],
        }
        for number in range(1, 238)
    ]
    rows = core._decision_context_candidate_rows(
        "owner", "firstmate", enriched, True
    )
    item = _scan_context_from_observed(obs, rows)
    # The fixture uses a human-readable base marker rather than a production
    # hex SHA; omit it from the queue freshness probe so only head caching is
    # under test here.
    item["base_sha"] = ""
    context = item["decision_context"]
    compact = decision_context.compact_model_context(context)
    held = card_projection.plan_card_projection(
        item, prior={}, held=render_card.should_hold(item, True), has_token=True
    )
    state = core.parse_state_block(held["body"])
    labels = [{"name": label} for label in held["managed_labels"]]
    eligible_once = render_card.should_auto_triage(item, state, labels, True)
    queued_body = render_card.body_with_triage_queued(held["body"], item)
    queued_state = core.parse_state_block(queued_body)
    queued_again = render_card.should_auto_triage(item, queued_state, labels, True)
    stale_result = _triage_payload(
        obs, context, observation_id="sha256:" + "0" * 64
    )
    visible = render_card.body_with_triage_result(
        queued_body, obs["revision"]["head_sha"], triage=stale_result
    )
    visible_state = core.parse_state_block(visible)
    check(
        "card-1663: 237 observed PRs yield all three deterministic relations",
        context["repository_snapshot"]["candidate_count"] == 237
        and context["status"] == "complete"
        and [row["target"]["number"] for row in context["candidates"]]
        == [220, 221, 222],
    )
    check(
        "card-1663: one compact title/URL owner feeds model-visible related work",
        compact["total_matches"] == 3
        and compact["shown_matches"] == 3
        and all(set(row) == {"title", "url"} for row in compact["items"])
        and all(row["url"].startswith("https://github.com/") for row in compact["items"]),
    )
    check(
        "card-1663: held lifecycle queues exactly once for the current head",
        state.get("held") is True
        and eligible_once
        and queued_state["triage_attempts"]["count"] == 1
        and not queued_again,
    )
    check(
        "card-1663: visible advisory result cannot bypass independent binding admission",
        "### Triage" in visible
        and visible_state["triage_assessment"]["admission"]["status"] == "stale"
        and "<!-- opt:accept-recommendation -->" not in visible,
    )
    context_rotated_result = _triage_payload(
        obs, context, context_id="sha256:" + "0" * 64
    )
    rotated_visible = render_card.body_with_triage_result(
        queued_body,
        obs["revision"]["head_sha"],
        triage=context_rotated_result,
    )
    rotated_state = core.parse_state_block(rotated_visible)
    check(
        "card-1663: context identity provenance alone never denies authority",
        admission.admitted(rotated_state["triage_assessment"])
        and "<!-- opt:accept-recommendation -->" in rotated_visible,
    )


def test_related_cap_keeps_strongest_and_stays_honest():
    obs = observation(1, "head-1", paths=["src/a.py"])
    target_row = candidate(
        1,
        "head-1",
        ["src/a.py"],
        closing_issues=[10],
        references=[
            {"owner": "owner", "repo": "firstmate", "number": number}
            for number in (8, 9, 10, 11)
        ],
    )
    rows = [target_row]
    # The lowest-numbered candidate has the weakest relation kind.
    rows.append(candidate(2, "head-2", ["src/a.py"]))
    rows.extend(
        candidate(number, "head-%s" % number, ["src/other-%s.py" % number], closing_issues=[10])
        for number in (3, 4, 5, 6, 7)
    )
    rows.extend(
        candidate(number, "head-%s" % number, ["src/ref-%s.py" % number])
        for number in (8, 9, 10, 11)
    )
    # The highest-numbered candidate has the strongest relation kind.
    rows.append(candidate(12, "head-12", ["src/other-12.py"], closing_issues=[10]))
    context = context_for(obs, rows)
    item = item_for(obs, context)
    compact = decision_context.compact_model_context(context)
    held = card_projection.plan_card_projection(
        item, prior={}, held=True, has_token=True
    )
    queued = render_card.body_with_triage_queued(held["body"], item)
    visible = render_card.body_with_triage_result(
        queued,
        obs["revision"]["head_sha"],
        triage=_triage_payload(obs, context),
    )
    state = core.parse_state_block(visible)
    facts, _behavior_class = auto_merge.fresh_verdict_facts(
        state, obs["revision"]["head_sha"]
    )
    check(
        "related cap: deliberate cap is complete context with an honest total",
        context["status"] == "complete"
        and context["reason"] == ""
        and context["related_candidate_count"] == 11
        and len(context["candidates"]) == 10
        and "Showing **10 of 11**" in visible
        and "strongest relations first" in visible
        and "deliberate display/model context cap" in visible,
    )
    check(
        "related cap: strength ordering retains the most informative candidates",
        [row["target"]["number"] for row in context["candidates"]]
        == [3, 4, 5, 6, 7, 12, 8, 9, 10, 11],
    )
    check(
        "related cap: compact model context contains no identities, paths, bodies, or diffs",
        compact["status"] == "complete"
        and compact["total_matches"] == 11
        and compact["shown_matches"] == 10
        and all(set(row) == {"title", "url"} for row in compact["items"]),
    )
    check(
        "related cap: capped context admits Accept and G6 triage credit",
        "### Triage" in visible
        and admission.admitted(state["triage_assessment"])
        and "<!-- opt:accept-recommendation -->" in visible
        and facts["g6_triage_success"]["status"] == criteria.STATUS_MET,
    )
    incomplete = context_for(
        obs,
        [
            dict(target_row, paths_complete=False),
            candidate(2, "head-2", ["src/a.py"]),
        ],
    )
    truncated_copy = "\n".join(render_card._related_work_section(incomplete))
    complete = context_for(
        obs,
        [
            candidate(1, "head-1", ["src/a.py"]),
            candidate(2, "head-2", ["src/a.py"]),
        ],
    )
    complete_copy = "\n".join(render_card._related_work_section(complete))
    check(
        "related copy: genuine incompleteness stays explicit and advisory-only",
        incomplete["status"] == "truncated"
        and incomplete["reason"] == "comparison_incomplete"
        and "comparison across open pull requests is incomplete" in truncated_copy
        and "never an overlap or action gate" in truncated_copy
        and "assessment admission is unavailable" not in truncated_copy
        and "Accept and G6 remain unavailable" not in truncated_copy,
    )
    check(
        "related copy: complete context does not claim missing evidence",
        complete["status"] == "complete"
        and "cannot claim that no related work exists" not in complete_copy
        and "Shared paths and references are not an auto-merge overlap gate." in complete_copy,
    )


def test_axi84_comparison_incomplete_keeps_target_authority():
    """Card-1676/axi#84 reproduction: one open PR with 14 changed files
    (above MAX_CHANGED_PATHS = 12) marks its path comparison incomplete and
    used to poison every assessment in the repository. The context honestly
    stays truncated, but a complete target observation is admitted and keeps
    Accept/G6 authority while the context remains advisory."""
    obs = observation(114, "1c0adc1d", paths=["README.md", "catalog.yaml", "docs/index.html"])
    rows = [
        candidate(114, "1c0adc1d", ["README.md", "catalog.yaml", "docs/index.html"]),
    ]
    for number in range(115, 127):
        if number == 120:
            # axi#84: 14 changed files, above the observation path cap, so its
            # comparison input is incomplete (paths_complete=False).
            rows.append(
                dict(
                    candidate(
                        120,
                        "head-120",
                        ["dir-%02d/file.py" % index for index in range(12)],
                    ),
                    paths_complete=False,
                )
            )
        else:
            rows.append(
                candidate(number, "head-%s" % number, ["src/other-%s.py" % number])
            )
    context = context_for(obs, rows)
    check(
        "axi#84: one over-cap PR keeps context comparison honestly incomplete",
        context["status"] == "truncated"
        and context["reason"] == "comparison_incomplete"
        and context["related_candidate_count"] == 0,
    )
    assessment = assessment_for(obs, context)
    item = item_for(obs, context, assessment)
    projection = card_projection.plan_card_projection(item, prior={})
    state = core.parse_state_block(projection["body"])
    facts, _behavior_class = auto_merge.fresh_verdict_facts(
        state, obs["revision"]["head_sha"]
    )
    check(
        "axi#84: complete target observation admits despite truncated context",
        admission.admitted(assessment)
        and render_card.assessment_current_admitted(state)
        and "<!-- opt:accept-recommendation -->" in projection["body"]
        and facts["g6_triage_success"]["status"] == criteria.STATUS_MET,
    )


def test_card1676_hub_paths_cannot_manufacture_relations():
    """Card-1676 catalog cohort: nine candidates each share ALL THREE common
    catalog files with the target, so requiring two or more shared paths
    cannot suppress them. The fanout rule (a path touched by at least half of
    the open candidate universe, floor 3) suppresses the manufactured
    relations while genuine non-hub shared paths still relate."""
    hub_paths = ["README.md", "catalog.yaml", "docs/index.html"]
    obs = observation(114, "head-114", paths=hub_paths)
    rows = [candidate(114, "head-114", hub_paths)]
    for number in range(115, 128):
        rows.append(candidate(number, "head-%s" % number, hub_paths))
    rows.append(candidate(130, "head-130", ["src/unrelated.py"]))
    context = context_for(obs, rows)
    check(
        "card-1676: every cohort member shares all three catalog files",
        all(
            len(set(hub_paths).intersection(row["paths"])) == 3
            for row in rows[1:14]
        )
        and len(rows) == 15,
    )
    check(
        "card-1676: hub fanout suppresses every manufactured relation",
        context["related_candidate_count"] == 0
        and context["candidates"] == []
        and context["status"] == "complete",
    )
    # Genuine non-hub shared path: target plus two others in the same 15-PR
    # universe (fanout 3, below half of 15) still forms a relation.
    genuine_rows = [candidate(114, "head-114", ["src/catalog.py"] + hub_paths)]
    for number in range(115, 128):
        genuine_rows.append(candidate(number, "head-%s" % number, hub_paths))
    genuine_rows.append(candidate(131, "head-131", ["src/catalog.py"]))
    genuine_rows.append(candidate(132, "head-132", ["src/catalog.py"]))
    genuine_obs = observation(114, "head-114", paths=["src/catalog.py"] + hub_paths)
    genuine = context_for(genuine_obs, genuine_rows)
    genuine_relations = {
        row["target"]["number"]: [relation["kind"] for relation in row["relations"]]
        for row in genuine["candidates"]
    }
    check(
        "card-1676: genuine non-hub shared paths still relate",
        genuine_relations.get(131) == ["exact-shared-path"]
        and genuine_relations.get(132) == ["exact-shared-path"]
        and len(genuine_relations) == 2,
    )
    # Boundary: at exactly half the universe and the absolute floor the path
    # is a hub; one candidate fewer and it is not.
    half_rows = [candidate(114, "head-114", ["src/half.py"])]
    half_rows.extend(
        candidate(200 + index, "head-%s" % (200 + index), ["src/half.py"])
        for index in range(5)
    )
    half_context = context_for(observation(114, "head-114", paths=["src/half.py"]), half_rows)
    check(
        "card-1676: half-of-universe fanout is the hub boundary",
        half_context["related_candidate_count"] == 0
        and half_context["status"] == "complete",
    )
    incomplete_rows = [
        candidate(114, "head-114", ["src/observed.py"]),
        candidate(200, "head-200", ["src/observed.py"]),
        candidate(201, "head-201", ["src/observed.py"]),
    ]
    incomplete_context = context_for(
        observation(114, "head-114", paths=["src/observed.py"]),
        incomplete_rows,
        complete=False,
        reason="repository-candidate-bound",
        candidate_count=9,
    )
    check(
        "card-1676: incomplete snapshot uses the supported open universe",
        incomplete_context["related_candidate_count"] == 2
        and incomplete_context["status"] == "truncated",
    )


def _legacy_context_rule_admit(real_admit):
    """Simulate the retired admission rule: non-complete context denied."""

    def wrapper(data, observation, context):
        result = real_admit(data, observation, context)
        normalized_context = decision_context.normalize_decision_context(context)
        if (
            result is not None
            and result["admission"]["status"] == "admitted"
            and normalized_context is not None
            and normalized_context["status"] != "complete"
        ):
            result = dict(result)
            result["admission"] = {
                "schema": admission.ADMISSION_SCHEMA,
                "status": "unavailable",
                "reason": "context.%s" % normalized_context["status"],
            }
            without_id = dict(result)
            without_id.pop("assessment_id", None)
            result["assessment_id"] = admission._identity("sha256:", without_id)
            result = admission.normalize_assessment(result)
        return result

    return wrapper


def test_context_denied_assessment_readmits_on_ordinary_refresh():
    """Zero-spend healing: a same-head assessment denied solely under the
    retired advisory-context rule is re-admitted during the ordinary
    same-revision refresh path - no replay, no model call - while genuinely
    denied or observation-rotated assessments stay denied."""
    obs = observation(114, "head-114", paths=["README.md", "catalog.yaml"])
    rows = [
        candidate(114, "head-114", ["README.md", "catalog.yaml"]),
        dict(
            candidate(120, "head-120", ["dir-%02d/file.py" % i for i in range(12)]),
            paths_complete=False,
        ),
    ]
    context = context_for(obs, rows)
    item = item_for(obs, context)
    held = card_projection.plan_card_projection(item, prior={}, held=True, has_token=True)
    queued = render_card.body_with_triage_queued(held["body"], item)
    saved_admit = render_card.assessment_admission.admit_assessment
    render_card.assessment_admission.admit_assessment = _legacy_context_rule_admit(
        saved_admit
    )
    try:
        legacy_body = render_card.body_with_triage_result(
            queued,
            obs["revision"]["head_sha"],
            triage=_triage_payload(obs, context),
        )
    finally:
        render_card.assessment_admission.admit_assessment = saved_admit
    legacy_warning = "\n".join(
        [
            "> [!WARNING]",
            "> The advisory assessment was not admitted (`context.truncated`). "
            "It cannot create **Accept recommendation** or satisfy G6.",
        ]
    )
    legacy_body = legacy_body.replace(
        render_card.TRIAGE_END + "\n\n" + legacy_warning,
        legacy_warning + "\n" + render_card.TRIAGE_END,
    )
    legacy_state = core.parse_state_block(legacy_body)
    check(
        "readmission: legacy fixture persists the retired context denial",
        legacy_state["triage_assessment"]["admission"]["status"] == "unavailable"
        and legacy_state["triage_assessment"]["admission"]["reason"]
        == "context.truncated"
        and legacy_state.get("triage_recommendation") is None
        and "<!-- opt:accept-recommendation -->" not in legacy_body
        and legacy_warning in legacy_body,
    )
    prior = issue_from_projection(
        {"title": held["title"], "body": legacy_body, "managed_labels": held["managed_labels"]}
    )
    healed = card_projection.plan_card_projection(item, prior=prior)
    healed_state = core.parse_state_block(healed["body"])
    check(
        "readmission: ordinary refresh re-admits the still-current assessment",
        healed_state["triage_assessment"]["admission"]["status"] == "admitted"
        and "assessment_admission" not in healed_state
        and healed_state.get("triage_recommendation")
        == {"action": "merge", "reason": "Review the exact current revision."}
        and "<!-- opt:accept-recommendation -->" in healed["body"]
        and "### Recommended action" not in healed["body"]
        and "The advisory assessment was not admitted" not in healed["body"]
        and render_card.assessment_current_admitted(healed_state),
    )

    # A basis contradiction the context branch had masked stays denied: the
    # re-admission recomputation surfaces the true verdict instead of healing.
    contradicting = _triage_payload(obs, context)
    contradicting["recommendation_basis"] = {
        "kind": "configured-tests-not-run",
        "observation_id": obs["observation_id"],
        "context_id": context["context_id"],
        "check_names": ["Ubuntu", "macOS", "Windows", "E2E"],
    }
    render_card.assessment_admission.admit_assessment = _legacy_context_rule_admit(
        saved_admit
    )
    try:
        contradicting_body = render_card.body_with_triage_result(
            queued,
            obs["revision"]["head_sha"],
            triage=contradicting,
        )
    finally:
        render_card.assessment_admission.admit_assessment = saved_admit
    contradicting_prior = issue_from_projection(
        {"title": held["title"], "body": contradicting_body, "managed_labels": held["managed_labels"]}
    )
    unhealed = card_projection.plan_card_projection(item, prior=contradicting_prior)
    unhealed_state = core.parse_state_block(unhealed["body"])
    check(
        "readmission: a masked check contradiction is never healed",
        unhealed_state["triage_assessment"]["admission"]["status"] != "admitted"
        and unhealed_state.get("triage_recommendation") is None
        and "<!-- opt:accept-recommendation -->" not in unhealed["body"],
    )

    # A rotated target observation (same head, new check rows) is not current:
    # the legacy assessment stays exactly as persisted.
    rotated_obs = observation(
        114,
        "head-114",
        paths=["README.md", "catalog.yaml"],
        checks=[
            {"name": "PR must be raised via no-mistakes", "role": "compliance", "outcome": "pass"},
            {"name": "Ubuntu", "role": "test", "outcome": "pass"},
        ],
        observed_at="2026-07-23T14:00:00Z",
    )
    rotated_item = item_for(rotated_obs, context_for(rotated_obs, rows))
    rotated = card_projection.plan_card_projection(rotated_item, prior=prior)
    rotated_state = core.parse_state_block(rotated["body"])
    check(
        "readmission: observation rotation keeps the legacy denial untouched",
        rotated_state["triage_assessment"]["admission"]["status"] == "unavailable"
        and rotated_state["triage_assessment"]["admission"]["reason"]
        == "context.truncated"
        and rotated_state.get("triage_recommendation") is None,
    )


def test_projection_contract_maxima_fit_one_issue_update():
    checks = [
        {
            "name": ("test-%02d-" % index) + "x" * 190,
            "role": "test",
            "outcome": "pass",
        }
        for index in range(target_observation.MAX_CHECK_ROWS)
    ]
    paths = [
        "dir-%02d/%s.py" % (index, "p" * 490)
        for index in range(target_observation.MAX_CHANGED_PATHS)
    ]
    obs = observation(checks=checks, paths=paths)
    rows = [candidate(901, "head-901", paths, card_issue=1901)]
    for index in range(decision_context.MAX_CONTEXT_CANDIDATES):
        row = candidate(
            910 + index,
            "head-%s" % (910 + index),
            paths[:3],
            card_issue=1910 + index,
        )
        row["title"] = "t" * decision_context.MAX_CANDIDATE_TITLE
        row["url"] = "https://github.com/" + "u" * 230
        row["card_url"] = "https://github.com/" + "c" * 230
        rows.append(row)
    context = context_for(obs, rows)
    assessment = admission.admit_assessment(
        {
            "summary": "s" * 4000,
            "product_implications": "p" * 4000,
            "recommended_action": "merge",
            "recommended_reason": "r" * 4000,
            "recommendation_basis": {
                "kind": "other",
                "observation_id": obs["observation_id"],
                "context_id": context["context_id"],
                "check_names": [],
            },
        },
        obs,
        context,
    )
    projection = card_projection.plan_card_projection(
        item_for(obs, context, assessment), prior={}
    )
    check(
        "projection: contract maxima stay within one verified GitHub issue body",
        projection is not None
        and len(projection["body"].encode("utf-8")) <= 60_000,
    )


def test_projection_golden_and_purity():
    obs = observation()
    context = context_for(obs)
    assessment = assessment_for(obs, context)
    item = item_for(obs, context, assessment)
    os.environ["GITHUB_REPOSITORY_OWNER"] = "wrong-environment-owner"
    first = card_projection.plan_card_projection(item, prior={})
    os.environ["GITHUB_REPOSITORY_OWNER"] = "another-wrong-owner"
    second = card_projection.plan_card_projection(item, prior={})
    check(
        "projection: identical normalized inputs are byte-identical and environment-independent",
        first == second,
    )
    state = core.parse_state_block(first["body"])
    check(
        "projection: complete output owns title/body/labels/sections/controls/state/cause",
        first["cause"] == "projection-current"
        and first["title"].startswith("[firstmate#901]")
        and "### Situation" in first["body"]
        and "### Related work" in first["body"]
        and "### Auto-merge criteria" in first["body"]
        and "### Your decision" in first["body"]
        and state[render_card.PROJECTION_OWNER_FIELD] == render_card.PROJECTION_OWNER
        and state[render_card.REVIEW_OBSERVATION_FIELD]["observation_id"] == obs["observation_id"]
        and state[render_card.DECISION_CONTEXT_FIELD]["context_id"] == context["context_id"],
    )
    golden_path = ROOT / "tests" / "fixtures" / "option_b_card_projection.json"
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    check("projection: complete golden projection is stable", first == golden)
    prior_with_human_label = issue_from_projection(first)
    prior_with_human_label["labels"].append({"name": "human:reviewed"})
    unchanged = card_projection.plan_card_projection(
        item, prior=prior_with_human_label
    )
    malformed = copy.deepcopy(first)
    malformed["changed_sections"] = ["not-a-section"]
    check(
        "projection: unmanaged labels neither churn nor break strict malformed-input denial",
        unchanged["cause"] == "noop"
        and unchanged["changed_sections"] == []
        and card_projection.normalize_card_projection(malformed) is None,
    )


def _writer_world(card):
    calls = []

    def get_card(_number):
        return copy.deepcopy(card)

    def gh(args, check=True):
        if args[:3] == ["api", "--method", "PATCH"] and "--input" in args:
            calls.append(list(args))
            path = args[args.index("--input") + 1]
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            card["title"] = payload["title"]
            card["body"] = payload["body"]
            card["labels"] = [{"name": name} for name in payload["labels"]]
            card["updatedAt"] = "2026-07-23T12:00:02Z"
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        raise AssertionError("unexpected writer gh call: %r" % args)

    return calls, get_card, gh


def test_e2e_01_denied_preclaim_then_refresh_once():
    obs = observation()
    context = context_for(obs)
    item = item_for(obs, context)
    initial = card_projection.plan_card_projection(item, prior={})
    card = issue_from_projection(initial)
    # Make one visible projection refresh due without altering target facts.
    old_body = card["body"].replace("Current exact revision.", "Old visible copy.")
    card["body"] = old_body
    expected = projection_writer.card_snapshot(card)
    writes = []

    saved = {
        "cfg": core.load_config,
        "owner": core.get_owner,
        "maintainers": core.maintainers,
        "evaluate": auto_merge.evaluate_candidate,
        "gh": render_card._gh,
        "get": render_card.get_card,
    }
    core.load_config = lambda: {"auto_merge": True, "repos": {"firstmate": {"auto_merge": True}}}
    core.get_owner = lambda: "owner"
    core.maintainers = lambda: {"owner"}
    auto_merge.evaluate_candidate = lambda *_args, **_kwargs: {
        "eligible": False,
        "hold_reason": "contributor has no prior merged PR",
        "criteria": [
            {"id": "g3_returning_contributor", "status": criteria.STATUS_UNMET}
        ],
    }
    render_card._gh = lambda *_args, **_kwargs: writes.append("unexpected")
    try:
        denied = auto_merge.preclaim_candidates(
            {"repos": {"firstmate": {"ok": True}}, "items": [item]},
            [card],
        )
        auto_merge.evaluate_candidate = lambda *_args, **_kwargs: {
            "eligible": False,
            "hold_reason": "prior-contribution read unavailable",
            "criteria": [
                {
                    "id": "g3_returning_contributor",
                    "status": criteria.STATUS_UNAVAILABLE,
                }
            ],
        }
        unavailable = auto_merge.preclaim_candidates(
            {"repos": {"firstmate": {"ok": True}}, "items": [item]},
            [card],
        )
    finally:
        core.load_config = saved["cfg"]
        core.get_owner = saved["owner"]
        core.maintainers = saved["maintainers"]
        auto_merge.evaluate_candidate = saved["evaluate"]
        render_card._gh = saved["gh"]
    check(
        "E2E-01: G3 denial happens before every card/target mutation",
        denied == [] and unavailable == [] and writes == [], 
    )

    projection = card_projection.plan_card_projection(
        item, prior=card, cause="projection-current"
    )
    calls, get_card, gh = _writer_world(card)
    render_card.get_card = get_card
    render_card._gh = gh
    try:
        committed = projection_writer.commit_projection(77, expected, projection)
        current = copy.deepcopy(card)
        noop = card_projection.plan_card_projection(item, prior=current)
        second = projection_writer.commit_projection(
            77, projection_writer.card_snapshot(current), noop
        )
    finally:
        render_card.get_card = saved["get"]
        render_card._gh = saved["gh"]
    check(
        "E2E-01: due visible refresh lands once and unchanged second scan is no-op",
        committed == "committed" and second == "noop" and len(calls) == 1,
    )


def test_e2e_02_visible_inert_absence_with_manual_interleave():
    item = reconcile_fixture.work_item()
    lifecycle = reconcile_fixture.ReconcileLifecycle(item)
    absent = reconcile_fixture.scan_payload(items=[])
    lifecycle.run(absent)
    first_body = lifecycle.issue["body"]
    first_state = core.parse_state_block(first_body)
    first_updated = lifecycle.issue["updatedAt"]
    check(
        "E2E-02: first scheduled absence is visible, open, exact, and inert",
        lifecycle.issue["state"] == "OPEN"
        and "### Target state changed" in first_body
        and "Confirmation: `1/2`" in first_body
        and "<!-- opt:" not in first_body
        and first_state["reconcile_absence"]["scheduled_epoch"] == 1
        and first_state["review_observation"]["facts"]["mergeable"] == "CONFLICTING"
        and render_card.LIFECYCLE_CONFIRM_LABEL
        in {label["name"] for label in lifecycle.issue["labels"]},
    )
    lifecycle.run(absent, event_name="workflow_dispatch")
    check(
        "E2E-02: manual run neither advances, resets, nor rewrites confirmation",
        lifecycle.issue["body"] == first_body
        and lifecycle.issue["updatedAt"] == first_updated,
    )
    lifecycle.run(absent)
    check(
        "E2E-02: second adjacent scheduled observation closes once without target action",
        lifecycle.issue["state"] == "CLOSED"
        and len(lifecycle.close_calls) == 1
        and render_card.reconcile_soft_close_provenance(
            lifecycle.close_calls[0]["body"]
        )
        is not None,
    )


def test_e2e_03_green_checks_defeat_false_basis():
    obs = observation()
    context = context_for(obs)
    rejected = assessment_for(
        obs,
        context,
        action="hold",
        basis_kind="configured-tests-not-run",
        names=["Ubuntu", "macOS", "Windows", "E2E"],
    )
    projection = card_projection.plan_card_projection(
        item_for(obs, context, rejected), prior={}
    )
    state = core.parse_state_block(projection["body"])
    check(
        "E2E-03: green reducer facts remain visible while false basis loses Accept and G6",
        "- Tests: `green`" in projection["body"]
        and "advisory assessment was not admitted (`basis.checks_contradict`)" in projection["body"]
        and "<!-- opt:accept-recommendation -->" not in projection["body"]
        and render_card.assessment_current_admitted(state) is False
        and "- [ ] Merge" in projection["body"],
    )


def test_e2e_04_invalid_class_tristate():
    facts, _ = auto_merge.behavior_verdict_facts(
        {
            "behavior_class": "INELIGIBLE",
            "changes_existing_or_default_behavior": False,
            "optin_default_off": False,
        }
    )
    check(
        "E2E-04: evaluator and projected criterion facts agree on invalid class tri-state",
        facts["g6_behavior_class"]["status"] == criteria.STATUS_UNMET
        and facts["g6_default_behavior"]["status"] == criteria.STATUS_MET
        and facts["g6_class_c_mode"]["status"] == criteria.STATUS_UNAVAILABLE,
    )


def test_e2e_05_card_1620_fixture_is_retained():
    before = list(automerge_fixture._failures)
    automerge_fixture.test_class_b_semantic_admission_boundary()
    added = automerge_fixture._failures[len(before):]
    check(
        "E2E-05: exact card-1620 class-B contract-change fixture remains denied",
        added == [],
    )


def test_e2e_06_competing_work_visible_and_advisory():
    obs901 = observation(901, "head-901", paths=["src/central.py", "src/queue.py"])
    obs905 = observation(905, "head-905", paths=["src/central.py", "src/writer.py"])
    rows = [
        candidate(
            901,
            "head-901",
            ["src/central.py", "src/queue.py"],
            references=[{"owner": "owner", "repo": "tasks-axi", "number": 21}],
            card_issue=1901,
        ),
        candidate(905, "head-905", ["src/central.py", "src/writer.py"], card_issue=1905),
        candidate(21, "head-21", ["packages/tasks.py"], card_issue=1921, repo="tasks-axi"),
    ]
    context901 = context_for(obs901, rows)
    context905 = context_for(obs905, rows)
    body901 = card_projection.plan_card_projection(
        item_for(obs901, context901), prior={}
    )["body"]
    body905 = card_projection.plan_card_projection(
        item_for(obs905, context905), prior={}
    )["body"]
    check(
        "E2E-06: 901/905 reciprocal exact-path relation and 901/21 dependency are visible",
        "owner/firstmate#905" in body901
        and "owner/firstmate#901" in body905
        and "owner/tasks-axi#21" in body901
        and "(card #1905)" in body901
        and "(card #1901)" in body905,
    )
    acting_source = inspect.getsource(auto_merge.evaluate_candidate)
    final_guard_source = inspect.getsource(auto_merge.final_auto_merge_guard)
    overlap_source = inspect.getsource(core.same_closing_issue_overlap)
    closing_map_source = inspect.getsource(core._closing_map)
    merge_source = inspect.getsource(__import__("apply_decision").do_merge)
    check(
        "E2E-06: DecisionContext remains advisory and is not an overlap acting gate",
        "decision_context" not in acting_source.lower()
        and "decision_context" not in final_guard_source.lower()
        and "decision_context" not in overlap_source.lower()
        and "decision_context" not in closing_map_source.lower()
        and "decision_context" not in merge_source.lower(),
    )


def test_e2e_07_result_recovery_and_owner_race():
    obs = observation()
    context = context_for(obs)
    item = item_for(obs, context)
    state = {
        "repo": "firstmate",
        "number": 901,
        "kind": "pr-review",
        "head_sha": "head-901",
        "triaged_sha": "head-901",
        "triage_status": "queued",
        render_card.PROJECTION_OWNER_FIELD: render_card.PROJECTION_OWNER,
    }
    row = {"number": 77, "state": state, "labels": [], "body": ""}
    record = assessment_record.make_record(
        state,
        "head-901",
        triage={
            "summary": "Recovered",
            "product_implications": "No repeat spend.",
            "recommended_next_step": "hold",
            render_card._VERIFIED_EVIDENCE_SPANS_FIELD: (
                ("target.txt", "bounded verified span"),
            ),
        },
    )
    round_trip = assessment_record.parse_body(
        assessment_record.body(record, projected=False)
    )
    check(
        "E2E-07: durable result preserves trusted source bindings through JSON",
        round_trip is not None
        and round_trip["result"]["triage"][
            render_card._VERIFIED_EVIDENCE_SPANS_FIELD
        ]
        == [["target.txt", "bounded verified span"]],
    )
    saved_find = assessment_record.find
    saved_update = render_card.update_card_triage
    saved_dispatch = render_card.dispatch_triage_workflow
    applied = []
    assessment_record.find = lambda *_args, **_kwargs: {
        "id": 9,
        "projected": False,
        "result": record,
    }
    render_card.update_card_triage = lambda *args, **kwargs: applied.append((args, kwargs)) or True
    render_card.dispatch_triage_workflow = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("recovery must not dispatch or spend")
    )
    try:
        recovered = reconcile.recover_pending_assessment_projection(
            item, row, owner="owner"
        )
    finally:
        assessment_record.find = saved_find
        render_card.update_card_triage = saved_update
        render_card.dispatch_triage_workflow = saved_dispatch
    check(
        "E2E-07: durable result recovers once without another model dispatch",
        recovered is True and len(applied) == 1 and applied[0][1]["require_queued"] is True,
    )
    finalized_state = dict(state)
    finalized_state.update(
        {
            "triage_status": "succeeded",
            render_card.ASSESSMENT_RESULT_FIELD: record["result_id"],
        }
    )
    finalized_row = dict(row, state=finalized_state)
    saved_mark = assessment_record.mark_projected
    finalized = []
    assessment_record.find = lambda *_args, **_kwargs: {
        "id": 9,
        "projected": False,
        "result": record,
    }
    assessment_record.mark_projected = (
        lambda issue, result_id: finalized.append((issue, result_id)) or True
    )
    render_card.update_card_triage = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("already-projected result must only finalize its record")
    )
    try:
        recovered_finalize = reconcile.recover_pending_assessment_projection(
            item, finalized_row, owner="owner"
        )
    finally:
        assessment_record.find = saved_find
        assessment_record.mark_projected = saved_mark
        render_card.update_card_triage = saved_update
    check(
        "E2E-07: post-projection finalization crash recovers without a card write",
        recovered_finalize is True
        and finalized == [(77, record["result_id"])],
    )

    initial = card_projection.plan_card_projection(item, prior={})
    card = issue_from_projection(initial)
    expected = projection_writer.card_snapshot(card)
    changed = copy.deepcopy(card)
    changed["comments"].append({"id": 1, "body": "owner acted"})
    saved_get = render_card.get_card
    saved_gh = render_card._gh
    writes = []
    render_card.get_card = lambda _number: copy.deepcopy(changed)
    render_card._gh = lambda *args, **kwargs: writes.append(args)
    try:
        outcome = projection_writer.commit_projection(
            77,
            expected,
            card_projection.plan_card_projection(
                item,
                prior=card,
                cause="target-activity-reflection",
            ),
        )
    finally:
        render_card.get_card = saved_get
        render_card._gh = saved_gh
    check(
        "E2E-07: owner comment after planning defers every body/label mutation",
        outcome == "deferred" and writes == [],
    )
    trigger_body = initial["body"].replace(
        "- [ ] Merge it <!-- opt:merge -->",
        "- [x] Merge it <!-- opt:merge -->",
    )
    current_body = render_card.body_with_activity_reflected(
        initial["body"],
        dict(item, updated_at="2026-07-23T13:00:00Z"),
        card_updated_at="2026-07-23T12:00:00Z",
    )
    stale_state = core.parse_state_block(current_body)
    stale_state["head_sha"] = "new-head"
    stale_body = render_card._replace_state_block(current_body, stale_state)
    check(
        "E2E-07: queued owner checkbox event survives a same-revision projection",
        render_card.owner_projection_race_recoverable(
            trigger_body, current_body
        )
        and not render_card.owner_projection_race_recoverable(
            trigger_body, stale_body
        ),
    )


def test_legacy_pr_mutations_defer_to_authoritative_writer():
    obs = observation()
    item = item_for(obs)
    projection = card_projection.plan_card_projection(item, prior={})
    state = core.parse_state_block(projection["body"])
    state.pop(render_card.PROJECTION_OWNER_FIELD)
    state.pop(render_card.REVIEW_OBSERVATION_FIELD)
    state.pop(render_card.DECISION_CONTEXT_FIELD)
    state.pop("decision_context_id")
    legacy_body = render_card._replace_state_block(projection["body"], state)
    later_item = dict(item, updated_at="2026-07-23T13:00:00Z")
    writes = []
    saved_gh = render_card._gh
    render_card._gh = lambda *args, **kwargs: writes.append((args, kwargs))
    try:
        reflected = render_card.reflect_activity(
            77,
            later_item,
            legacy_body,
            card_updated_at="2026-07-23T12:00:00Z",
        )
        try:
            render_card._edit_issue_body(77, legacy_body)
        except RuntimeError:
            direct_rejected = True
        else:
            direct_rejected = False
    finally:
        render_card._gh = saved_gh
    check(
        "migration: legacy PR mutation defers and direct writer fails closed",
        reflected is False and direct_rejected and writes == [],
    )


def test_static_workflow_token_and_single_writer_contract():
    workflow = (ROOT / ".github" / "workflows" / "scan-backstop.yml").read_text(
        encoding="utf-8"
    )
    triage = (ROOT / ".github" / "workflows" / "triage.yml").read_text(
        encoding="utf-8"
    )
    handler = (ROOT / ".github" / "workflows" / "decision-handler.yml").read_text(
        encoding="utf-8"
    )
    check(
        "static: read-only preclaim precedes default-token claim and fleet action",
        workflow.index("auto_merge.py preclaim")
        < workflow.index("auto_merge.py claim")
        < workflow.index("auto_merge.py act"),
    )
    check(
        "static: projection writer is card-only and model workflow receives no acting token",
        "FLEET_TOKEN" not in (ROOT / "scripts" / "projection_writer.py").read_text(encoding="utf-8")
        and "assessment_record.persist" in (ROOT / "scripts" / "render_card.py").read_text(encoding="utf-8")
        and "recommendation_basis" in triage
        and "render_card.review_inputs_complete(review_item)" in triage
        and "decision_context.compact_model_context(context)" in triage
        and triage.count('"decision_context": context') == 1,
    )
    check(
        "static: triage and handler serialize while owner webhook state is retained",
        "group: wheelhouse-backstop" in triage
        and "queue: max" in triage
        and "group: wheelhouse-backstop" in handler
        and "body: ${{ github.event.issue.body }}" in handler
        and "body: ${{ github.event.changes.body.from }}" in handler
        and "owner-race-recoverable" in handler
        and '$projection_recovery == "true"' in handler,
    )
    render_source = (ROOT / "scripts" / "render_card.py").read_text(
        encoding="utf-8"
    )
    check(
        "static: PR-review direct mutations have a fail-closed ownership guard",
        "pr-review projection bypassed the authoritative writer" in render_source
        and 'cause="migration-current"' in render_source,
    )
    check(
        "static: compatibility reader has one owner and an explicit removal condition",
        "Remove the v1 reader after no trusted open/reusable card contains v1"
        in (ROOT / "scripts" / "target_observation.py").read_text(encoding="utf-8"),
    )
    check(
        "static: scheduled epoch manual runs cannot advance lifecycle",
        'os.environ.get("GITHUB_EVENT_NAME") != "schedule"'
        in (ROOT / "scripts" / "scheduled_epoch.py").read_text(encoding="utf-8"),
    )
    architecture_doc = (
        ROOT / "docs" / "OPTION_B_CARD_PROJECTION.md"
    ).read_text(encoding="utf-8")
    check(
        "static: migration forbids mass rewrite and names compatibility removal condition",
        "does not mass-rewrite cards" in architecture_doc
        and "zero trusted open/reusable v1 cards" in architecture_doc,
    )
    check(
        "static: rollback disables auto-merge and preserves PR 1631 denial",
        "Disable `auto_merge` globally" in architecture_doc
        and "Preserve PR 1631's WH-AUD-05 semantic denial" in architecture_doc
        and "Fix forward is the default" in architecture_doc,
    )


def main():
    tests = [
        test_review_observation_contract_and_v1_compatibility,
        test_decision_context_contract,
        test_assessment_admission_and_class_tristate,
        test_scheduled_epoch_contract,
        test_incomplete_v2_context_allows_advisory_spend,
        test_triage_suppression_is_visible_and_fail_closed,
        test_card_1663_high_volume_context_queues_once,
        test_related_cap_keeps_strongest_and_stays_honest,
        test_axi84_comparison_incomplete_keeps_target_authority,
        test_card1676_hub_paths_cannot_manufacture_relations,
        test_context_denied_assessment_readmits_on_ordinary_refresh,
        test_projection_contract_maxima_fit_one_issue_update,
        test_projection_golden_and_purity,
        test_e2e_01_denied_preclaim_then_refresh_once,
        test_e2e_02_visible_inert_absence_with_manual_interleave,
        test_e2e_03_green_checks_defeat_false_basis,
        test_e2e_04_invalid_class_tristate,
        test_e2e_05_card_1620_fixture_is_retained,
        test_e2e_06_competing_work_visible_and_advisory,
        test_e2e_07_result_recovery_and_owner_race,
        test_legacy_pr_mutations_defer_to_authoritative_writer,
        test_static_workflow_token_and_single_writer_contract,
    ]
    for test in tests:
        test()
    if FAILURES:
        raise SystemExit("%d Option B failure(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
    print("\nall Option B architecture tests passed")


if __name__ == "__main__":
    main()
