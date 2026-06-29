#!/usr/bin/env python3
"""
Unit-exercise the scan-time fork-CI auto-approval with NO network.

Run: python tests/test_ci_autoapprove.py   (needs PyYAML; no network)

Wheelhouse auto-approves a fork PR's awaiting CI run when - and only when - the
SAME security verdict the manual gate uses says it is provably safe, so the
routine "approve CI" clicks disappear and only the risky ones still raise a card.
These tests cover:

  * the shared verdict `ci_safety` - risky-file HOLD, pull_request_target
    posture, the exploit-pattern flag, and every fail-closed branch (PR files
    unreadable, workflows unreadable);
  * the per-repo `pull_request_target` posture detection
    (`repo_pr_target_posture` + the pure `_on_triggers` / `_checks_out_pr_head`
    helpers), including the YAML 1.1 `on:`-parses-as-True gotcha and fail-closed
    read/parse errors;
  * the auto-approve-vs-card routing in `build_repo`: a safe PR is approved and
    raises NO card, a risky/posture/error PR still raises a card (with a
    warning), an approve failure or exception falls back to a card, and an
    ok:false repo is never auto-approved;
  * idempotency by construction (a PR no longer `needs-ci-approval` is never
    re-approved), default-on, explicit opt-out, and the per-repo override.
"""
import io
import os
import sys
import tempfile
from contextlib import redirect_stderr

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import wheelhouse_core as core  # noqa: E402

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


CLEAN_POSTURE = {"pr_target": False, "exploit": False, "error": False}


# --------------------------------------------------------------------------- #
# ci_safety: the ONE shared verdict (risky files + posture, fail closed)
# --------------------------------------------------------------------------- #
def safety(files, ok, posture):
    """Run ci_safety with a stubbed PR file list and a given repo posture."""
    save = core._list_pr_files
    core._list_pr_files = lambda slug, pr: (files, ok)
    try:
        return core.ci_safety("o/r", "1", posture)
    finally:
        core._list_pr_files = save


def test_ci_safety_clean_is_safe():
    v = safety([], True, CLEAN_POSTURE)
    check("ci_safety: clean PR -> safe", v["safe"] is True)
    check("ci_safety: clean PR -> no error", v["error"] is False)
    check("ci_safety: clean PR -> no risky files", v["risky_files"] == [])


def test_ci_safety_risky_files_hold():
    v = safety([".github/workflows/ci.yml", "src/x.py"], True, CLEAN_POSTURE)
    check("ci_safety: CI-execution file change -> not safe", v["safe"] is False)
    check("ci_safety: only the risky file is reported",
          v["risky_files"] == [".github/workflows/ci.yml"])
    # The other pwn-request vectors are all caught.
    for f in (".github/actions/x/action.yml", "action.yml", "action.yaml",
              "nested/action.yaml"):
        v = safety([f], True, CLEAN_POSTURE)
        check("ci_safety: risky path %r -> not safe" % f, v["safe"] is False and v["risky_files"])


def test_ci_safety_file_list_error_fails_closed():
    v = safety([], False, CLEAN_POSTURE)  # gh could not list the PR's files
    check("ci_safety: unreadable PR files -> not safe", v["safe"] is False)
    check("ci_safety: unreadable PR files -> error flag", v["error"] is True)
    check("ci_safety: unreadable PR files -> a (sentinel) risky file", bool(v["risky_files"]))


def test_ci_safety_pr_target_posture_blocks_auto():
    v = safety([], True, {"pr_target": True, "exploit": False, "error": False})
    check("ci_safety: pull_request_target posture -> not safe", v["safe"] is False)
    check("ci_safety: pull_request_target posture surfaced", v["pr_target"] is True)
    check("ci_safety: pull_request_target alone is not a read error", v["error"] is False)


def test_ci_safety_exploit_flag_passthrough():
    v = safety([], True, {"pr_target": True, "exploit": True, "error": False})
    check("ci_safety: exploit flag surfaced", v["exploit"] is True)
    check("ci_safety: exploit shows loudly in reason", "pwn-request" in v["reason"])


def test_ci_safety_posture_read_error_fails_closed():
    v = safety([], True, {"pr_target": True, "exploit": False, "error": True})
    check("ci_safety: posture read error -> not safe", v["safe"] is False)
    check("ci_safety: posture read error -> error flag", v["error"] is True)


# --------------------------------------------------------------------------- #
# pure trigger / exploit-pattern helpers
# --------------------------------------------------------------------------- #
def test_on_triggers_handles_every_form_and_yaml_gotcha():
    # The bare `on:` key parses as the YAML 1.1 boolean True - must still work.
    d = yaml.safe_load("on: pull_request_target\njobs: {}\n")
    check("on-triggers: string form (on:->True key) detected",
          "pull_request_target" in core._on_triggers(d))
    d = yaml.safe_load("on: [pull_request, pull_request_target]\n")
    check("on-triggers: list form detected", "pull_request_target" in core._on_triggers(d))
    d = yaml.safe_load("on:\n  pull_request_target:\n    types: [opened]\n")
    check("on-triggers: mapping form detected", "pull_request_target" in core._on_triggers(d))
    d = yaml.safe_load("on: pull_request\n")
    check("on-triggers: plain pull_request NOT flagged",
          "pull_request_target" not in core._on_triggers(d))
    check("on-triggers: non-dict doc -> empty set", core._on_triggers(None) == set())


EXPLOIT_WF = """
on: pull_request_target
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - run: make test
"""

HEAD_REF_WF = """
on: pull_request_target
jobs:
  b:
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.head_ref }}
"""

SAFE_WF = """
on: pull_request_target
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
      - run: echo hi
"""


def test_checks_out_pr_head():
    check("checkout-head: head.sha checkout flagged",
          core._checks_out_pr_head(yaml.safe_load(EXPLOIT_WF)) is True)
    check("checkout-head: github.head_ref checkout flagged",
          core._checks_out_pr_head(yaml.safe_load(HEAD_REF_WF)) is True)
    check("checkout-head: plain checkout NOT flagged",
          core._checks_out_pr_head(yaml.safe_load(SAFE_WF)) is False)
    check("checkout-head: non-dict -> False", core._checks_out_pr_head(None) is False)


# --------------------------------------------------------------------------- #
# repo_pr_target_posture: read once per repo, fail closed
# --------------------------------------------------------------------------- #
def posture(list_result, texts):
    save_l = core._list_workflow_files
    save_f = core._fetch_workflow_text
    core._list_workflow_files = lambda slug: list_result
    core._fetch_workflow_text = lambda slug, path: texts.get(path)
    try:
        return core.repo_pr_target_posture("o/r")
    finally:
        core._list_workflow_files = save_l
        core._fetch_workflow_text = save_f


def test_posture_no_workflows_dir_is_clean():
    p = posture(([], "none"), {})
    check("posture: no .github/workflows dir -> no posture, no error",
          p == {"pr_target": False, "exploit": False, "error": False})


def test_posture_listing_error_fails_closed():
    p = posture(([], "error"), {})
    check("posture: listing read error -> pr_target True (fail closed)", p["pr_target"] is True)
    check("posture: listing read error -> error flag", p["error"] is True)


def test_posture_plain_pull_request_is_clean():
    p = posture((["w/a.yml"], "ok"), {"w/a.yml": "on: pull_request\njobs: {}\n"})
    check("posture: only pull_request -> no posture", p["pr_target"] is False)
    check("posture: only pull_request -> no error", p["error"] is False)


def test_posture_detects_pull_request_target():
    p = posture((["w/a.yml"], "ok"), {"w/a.yml": "on: pull_request_target\njobs: {}\n"})
    check("posture: pull_request_target detected", p["pr_target"] is True)
    check("posture: no exploit when no PR-head checkout", p["exploit"] is False)


def test_posture_detects_exploit_pattern():
    p = posture((["w/a.yml"], "ok"), {"w/a.yml": EXPLOIT_WF})
    check("posture: exploit pattern flagged", p["pr_target"] is True and p["exploit"] is True)


def test_posture_unreadable_or_unparseable_file_fails_closed():
    p = posture((["w/a.yml"], "ok"), {"w/a.yml": None})  # content unreadable
    check("posture: unreadable workflow file -> fail closed",
          p["pr_target"] is True and p["error"] is True)
    p = posture((["w/a.yml"], "ok"), {"w/a.yml": "on: [\n"})  # invalid YAML
    check("posture: unparseable workflow -> fail closed",
          p["pr_target"] is True and p["error"] is True)


# --------------------------------------------------------------------------- #
# build_repo routing: auto-approve vs card
# --------------------------------------------------------------------------- #
def check_run(name, conclusion=None, status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name, "conclusion": conclusion, "status": status}


def rollup(contexts):
    return {"state": "PENDING", "contexts": {"nodes": contexts}}


def pr_node(number, status_rollup, draft=False):
    return {
        "number": number, "title": "PR %d" % number, "isDraft": draft,
        "updatedAt": "2026-01-01T00:00:00Z",
        "author": {"login": "contributor"},
        "headRefName": "feature-%d" % number, "headRefOid": "sha%d" % number,
        "labels": {"nodes": []},
        "closingIssuesReferences": {"nodes": []},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": status_rollup}}]},
    }


def graphql_data(pr_nodes):
    return {
        "pullRequests": {"totalCount": len(pr_nodes), "nodes": pr_nodes},
        "issues": {"totalCount": 0, "nodes": []},
    }


SAFE_VERDICT = {"safe": True, "error": False, "risky_files": [],
                "pr_target": False, "exploit": False, "reason": "clean"}


def run_build_repo(pr_nodes, *, auto_approve_ci=True, repo_over=None, posture_value=None,
                   verdict=None, approve_result=("approved", "approved 1 run"),
                   approve_raises=False, graphql_raises=False):
    """Drive build_repo with the network-touching dependencies stubbed."""
    calls = {"approve": [], "posture": 0, "safety": []}
    repo_cfg = {"name": "demo", "compliance_check": "Gate", "test_check_patterns": ["test"]}
    if repo_over:
        repo_cfg.update(repo_over)

    def fake_graphql(owner, name):
        if graphql_raises:
            raise RuntimeError("boom")
        return graphql_data(pr_nodes)

    def fake_posture(slug):
        calls["posture"] += 1
        return CLEAN_POSTURE if posture_value is None else posture_value

    def fake_ci_safety(slug, pr, repo_posture):
        calls["safety"].append((slug, pr))
        return SAFE_VERDICT if verdict is None else verdict

    def fake_approve(owner, name, pr, posture=None):
        calls["approve"].append((owner, name, pr, posture))
        if approve_raises:
            raise RuntimeError("approve boom")
        return approve_result

    save = (core.gh_graphql, core.repo_pr_target_posture, core.ci_safety, core.approve_ci)
    core.gh_graphql, core.repo_pr_target_posture = fake_graphql, fake_posture
    core.ci_safety, core.approve_ci = fake_ci_safety, fake_approve
    try:
        with redirect_stderr(io.StringIO()):
            result, items = core.build_repo("owner", repo_cfg, False, auto_approve_ci=auto_approve_ci)
    finally:
        core.gh_graphql, core.repo_pr_target_posture, core.ci_safety, core.approve_ci = save
    return result, items, calls


def needs_ci_pr(number=1):
    return pr_node(number, None)  # no status rollup -> ci absent -> needs-ci-approval


def test_safe_pr_is_auto_approved_no_card():
    result, items, calls = run_build_repo([needs_ci_pr()])
    check("route: safe PR raises NO card", items == [])
    check("route: safe PR is approved exactly once", len(calls["approve"]) == 1)
    check("route: approve received the per-repo posture",
          calls["approve"] and calls["approve"][0][3] == CLEAN_POSTURE)
    check("route: repo result still ok", result["ok"] is True)


def test_risky_pr_raises_card_not_approved():
    verdict = {"safe": False, "error": False, "risky_files": [".github/workflows/ci.yml"],
               "pr_target": False, "exploit": False, "reason": "risky"}
    result, items, calls = run_build_repo([needs_ci_pr()], verdict=verdict)
    check("route: risky PR raises a ci-approval card", len(items) == 1 and items[0]["kind"] == "ci-approval")
    check("route: risky PR card carries a warning", bool(items[0].get("warning")))
    check("route: risky PR is NOT auto-approved", calls["approve"] == [])


def test_pr_target_posture_raises_card_with_warning():
    verdict = {"safe": False, "error": False, "risky_files": [],
               "pr_target": True, "exploit": False, "reason": "pull_request_target"}
    result, items, calls = run_build_repo([needs_ci_pr()], verdict=verdict)
    check("route: pull_request_target PR raises a card", len(items) == 1)
    check("route: pull_request_target card warns about it",
          "pull_request_target" in (items[0].get("warning") or ""))
    check("route: pull_request_target PR is NOT auto-approved", calls["approve"] == [])


def test_exploit_pattern_card_warns_loudly():
    verdict = {"safe": False, "error": False, "risky_files": [],
               "pr_target": True, "exploit": True, "reason": "pwn-request"}
    result, items, calls = run_build_repo([needs_ci_pr()], verdict=verdict)
    check("route: exploit-pattern PR raises a card", len(items) == 1)
    check("route: exploit-pattern card warns loudly (DANGER)",
          "DANGER" in (items[0].get("warning") or ""))
    check("route: exploit-pattern PR is NOT auto-approved", calls["approve"] == [])


def test_ci_safety_error_raises_card():
    verdict = {"safe": False, "error": True,
               "risky_files": ["<could-not-list-files - failing closed>"],
               "pr_target": False, "exploit": False, "reason": "fail-closed"}
    result, items, calls = run_build_repo([needs_ci_pr()], verdict=verdict)
    check("route: verdict error -> a card", len(items) == 1)
    check("route: verdict error -> NOT auto-approved", calls["approve"] == [])


def test_approve_failure_falls_back_to_card():
    result, items, calls = run_build_repo([needs_ci_pr()], approve_result=("error", "api fail"))
    check("route: approve error falls back to a card (nothing lost)", len(items) == 1)
    check("route: approve was attempted before falling back", len(calls["approve"]) == 1)


def test_approve_hold_falls_back_to_card():
    result, items, calls = run_build_repo([needs_ci_pr()], approve_result=("hold", "held"))
    check("route: approve hold falls back to a card", len(items) == 1)


def test_approve_exception_falls_back_to_card():
    result, items, calls = run_build_repo([needs_ci_pr()], approve_raises=True)
    check("route: an approve that raises falls back to a card", len(items) == 1)
    check("route: approve was attempted", len(calls["approve"]) == 1)


def test_opt_out_global_disables_auto_approve():
    result, items, calls = run_build_repo([needs_ci_pr()], auto_approve_ci=False)
    check("opt-out: safe PR STILL raises a card", len(items) == 1)
    check("opt-out: approve never called", calls["approve"] == [])


def test_opt_out_card_still_carries_pr_target_warning():
    verdict = {"safe": False, "error": False, "risky_files": [],
               "pr_target": True, "exploit": False, "reason": "pull_request_target"}
    result, items, calls = run_build_repo([needs_ci_pr()], auto_approve_ci=False, verdict=verdict)
    check("opt-out: card still warns about pull_request_target",
          "pull_request_target" in (items[0].get("warning") or ""))
    check("opt-out: still no auto-approve", calls["approve"] == [])


def test_per_repo_override_disables_auto_approve():
    result, items, calls = run_build_repo([needs_ci_pr()], repo_over={"auto_approve_ci": False})
    check("override: per-repo false beats global on -> a card", len(items) == 1)
    check("override: per-repo false -> approve never called", calls["approve"] == [])


def test_idempotent_non_ci_approval_pr_never_reapproved():
    # Once approved, the next scan sees CI running / results, NOT needs-ci-approval.
    running = pr_node(2, rollup([check_run("Gate", None, status="IN_PROGRESS")]))
    result, items, calls = run_build_repo([running])
    check("idempotent: ci-running PR produces no card", items == [])
    check("idempotent: ci-running PR never calls approve", calls["approve"] == [])
    check("idempotent: posture not read when no ci-approval PR", calls["posture"] == 0)

    merge_ready = pr_node(3, rollup([check_run("Gate", "SUCCESS"), check_run("build-test", "SUCCESS")]))
    result, items, calls = run_build_repo([merge_ready])
    check("idempotent: merge-ready PR is a pr-review card, not approved",
          len(items) == 1 and items[0]["kind"] == "pr-review")
    check("idempotent: merge-ready PR never calls approve", calls["approve"] == [])


def test_ok_false_repo_is_never_auto_approved():
    result, items, calls = run_build_repo([needs_ci_pr()], graphql_raises=True)
    check("ok:false: failed scan returns no items", result["ok"] is False and items == [])
    check("ok:false: failed scan never auto-approves", calls["approve"] == [])
    check("ok:false: failed scan never reads posture", calls["posture"] == 0)


def test_posture_read_once_per_repo_for_multiple_ci_prs():
    result, items, calls = run_build_repo([needs_ci_pr(1), needs_ci_pr(2), needs_ci_pr(3)])
    check("route: all three safe PRs auto-approved (no cards)", items == [])
    check("route: each PR approved", len(calls["approve"]) == 3)
    check("route: posture read ONCE for the whole repo", calls["posture"] == 1)


# --------------------------------------------------------------------------- #
# config: default-on, opt-out, per-repo override helper
# --------------------------------------------------------------------------- #
def _load_config_with(text):
    save = core.config_path
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "wheelhouse.config.yml")
        with open(p, "w") as f:
            f.write(text)
        core.config_path = lambda: p
        try:
            return core.load_config()
        finally:
            core.config_path = save


def test_config_default_on_when_key_absent():
    cfg = _load_config_with("repos: []\n")
    check("config: auto_approve_ci defaults ON when the key is absent",
          cfg["auto_approve_ci"] is True)


def test_config_explicit_opt_out_honored():
    cfg = _load_config_with("repos: []\nauto_approve_ci: false\n")
    check("config: explicit auto_approve_ci:false is honored",
          cfg["auto_approve_ci"] is False)


def test_auto_approve_enabled_per_repo_override():
    check("flag: absent per-repo -> global default True",
          core._auto_approve_enabled({}, True) is True)
    check("flag: absent per-repo -> global default False",
          core._auto_approve_enabled({}, False) is False)
    check("flag: per-repo false overrides global true",
          core._auto_approve_enabled({"auto_approve_ci": False}, True) is False)
    check("flag: per-repo true overrides global false",
          core._auto_approve_enabled({"auto_approve_ci": True}, False) is True)


def main():
    test_ci_safety_clean_is_safe()
    test_ci_safety_risky_files_hold()
    test_ci_safety_file_list_error_fails_closed()
    test_ci_safety_pr_target_posture_blocks_auto()
    test_ci_safety_exploit_flag_passthrough()
    test_ci_safety_posture_read_error_fails_closed()
    test_on_triggers_handles_every_form_and_yaml_gotcha()
    test_checks_out_pr_head()
    test_posture_no_workflows_dir_is_clean()
    test_posture_listing_error_fails_closed()
    test_posture_plain_pull_request_is_clean()
    test_posture_detects_pull_request_target()
    test_posture_detects_exploit_pattern()
    test_posture_unreadable_or_unparseable_file_fails_closed()
    test_safe_pr_is_auto_approved_no_card()
    test_risky_pr_raises_card_not_approved()
    test_pr_target_posture_raises_card_with_warning()
    test_exploit_pattern_card_warns_loudly()
    test_ci_safety_error_raises_card()
    test_approve_failure_falls_back_to_card()
    test_approve_hold_falls_back_to_card()
    test_approve_exception_falls_back_to_card()
    test_opt_out_global_disables_auto_approve()
    test_opt_out_card_still_carries_pr_target_warning()
    test_per_repo_override_disables_auto_approve()
    test_idempotent_non_ci_approval_pr_never_reapproved()
    test_ok_false_repo_is_never_auto_approved()
    test_posture_read_once_per_repo_for_multiple_ci_prs()
    test_config_default_on_when_key_absent()
    test_config_explicit_opt_out_honored()
    test_auto_approve_enabled_per_repo_override()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all ci-autoapprove tests passed")


if __name__ == "__main__":
    main()
