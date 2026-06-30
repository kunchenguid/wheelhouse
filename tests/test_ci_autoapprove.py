#!/usr/bin/env python3
"""
Unit-exercise the scan-time fork-CI auto-approval with NO network.

Run: python tests/test_ci_autoapprove.py   (needs PyYAML; no network)

Wheelhouse auto-approves a fork PR's awaiting CI run when - and only when - the
SAME security verdict the manual gate uses says it is provably safe, so the
routine "approve CI" clicks disappear and only risky contributor-authored ones
still raise a card.
These tests cover:

  * the shared verdict `ci_safety` - risky-file HOLD, pull_request_target
    posture, the exploit-pattern flag, and every fail-closed branch (PR files
    unreadable/incomplete, workflows unreadable);
  * the per-repo `pull_request_target` posture detection
    (`repo_pr_target_posture` + the pure `_on_triggers` / `_checks_out_pr_head`
    helpers), including the YAML 1.1 `on:`-parses-as-True gotcha and fail-closed
    read/parse errors;
  * the contributor auto-approve-vs-card routing in `build_repo`: a safe PR is
    approved and raises NO card, a risky/posture/error PR still raises a card
    (with a warning), an approve failure or exception falls back to a card, a
    verified noop emits NO stale card, same-repo no-CI PRs route away from
    ci-approval, and an ok:false repo is never auto-approved;
  * the scan-log observability contract: each auto-path CI-approval candidate
    emits one notice when approved or one warning when it falls back from the
    contributor path to a card, with the verdict reason and any approve
    status/message;
  * run-to-PR verification: same-repo runs use GitHub's populated
    `pull_requests` association, while fork runs with an empty association are
    bound by matching `head_sha` plus `head_branch`;
  * idempotency by construction (a PR no longer `needs-ci-approval` is never
    re-approved), default-on, explicit opt-out, and the per-repo override.
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr
from types import SimpleNamespace

import yaml

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)
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
def safety(files, ok, posture, changed_files=None, complete=None):
    """Run ci_safety with a stubbed PR file list and a given repo posture."""
    save = core._list_pr_files
    if changed_files is None:
        changed_files = len(files)
    if complete is None:
        complete = ok
    core._list_pr_files = lambda slug, pr, expected_count: (files, ok, complete)
    try:
        return core.ci_safety("o/r", "1", posture, changed_files)
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
    check(
        "ci_safety: only the risky file is reported",
        v["risky_files"] == [".github/workflows/ci.yml"],
    )
    # The other pwn-request vectors are all caught.
    for f in (
        ".github/actions/x/action.yml",
        "action.yml",
        "action.yaml",
        "nested/action.yaml",
    ):
        v = safety([f], True, CLEAN_POSTURE)
        check(
            "ci_safety: risky path %r -> not safe" % f,
            v["safe"] is False and v["risky_files"],
        )


def test_ci_safety_file_list_error_fails_closed():
    v = safety([], False, CLEAN_POSTURE)  # gh could not list the PR's files
    check("ci_safety: unreadable PR files -> not safe", v["safe"] is False)
    check("ci_safety: unreadable PR files -> error flag", v["error"] is True)
    check(
        "ci_safety: unreadable PR files -> a (sentinel) risky file",
        bool(v["risky_files"]),
    )


def test_ci_safety_file_list_truncation_fails_closed():
    files = ["src/file%d.py" % i for i in range(3000)]
    v = safety(files, True, CLEAN_POSTURE, changed_files=3001, complete=False)
    check("ci_safety: incomplete PR file list -> not safe", v["safe"] is False)
    check("ci_safety: incomplete PR file list -> error flag", v["error"] is True)
    check(
        "ci_safety: incomplete PR file list -> fail-closed sentinel",
        "<could-not-list-all-files - failing closed>" in v["risky_files"],
    )


def test_ci_safety_pr_target_posture_blocks_auto():
    v = safety([], True, {"pr_target": True, "exploit": False, "error": False})
    check("ci_safety: pull_request_target posture -> not safe", v["safe"] is False)
    check("ci_safety: pull_request_target posture surfaced", v["pr_target"] is True)
    check(
        "ci_safety: pull_request_target alone is not a read error", v["error"] is False
    )


def test_ci_safety_exploit_flag_passthrough():
    v = safety([], True, {"pr_target": True, "exploit": True, "error": False})
    check("ci_safety: exploit flag surfaced", v["exploit"] is True)
    check("ci_safety: exploit shows loudly in reason", "pwn-request" in v["reason"])


def test_ci_safety_posture_read_error_fails_closed():
    v = safety([], True, {"pr_target": True, "exploit": False, "error": True})
    check("ci_safety: posture read error -> not safe", v["safe"] is False)
    check("ci_safety: posture read error -> error flag", v["error"] is True)


def test_ci_safety_note_caps_risky_file_list():
    risky = [".github/workflows/%02d.yml" % i for i in range(12)]
    note = core._ci_safety_note(
        {"risky_files": risky, "pr_target": False, "exploit": False}
    )
    check("ci-safety-note: first risky file is shown", risky[0] in note)
    check("ci-safety-note: tenth risky file is shown", risky[9] in note)
    check(
        "ci-safety-note: extra risky files are summarized",
        "(+2 more; 12 total)" in note,
    )
    check("ci-safety-note: eleventh risky file is not rendered", risky[10] not in note)
    check("ci-safety-note: twelfth risky file is not rendered", risky[11] not in note)


# --------------------------------------------------------------------------- #
# pure trigger / exploit-pattern helpers
# --------------------------------------------------------------------------- #
def test_on_triggers_handles_every_form_and_yaml_gotcha():
    # The bare `on:` key parses as the YAML 1.1 boolean True - must still work.
    d = yaml.safe_load("on: pull_request_target\njobs: {}\n")
    check(
        "on-triggers: string form (on:->True key) detected",
        "pull_request_target" in core._on_triggers(d),
    )
    d = yaml.safe_load("on: [pull_request, pull_request_target]\n")
    check(
        "on-triggers: list form detected", "pull_request_target" in core._on_triggers(d)
    )
    d = yaml.safe_load("on:\n  pull_request_target:\n    types: [opened]\n")
    check(
        "on-triggers: mapping form detected",
        "pull_request_target" in core._on_triggers(d),
    )
    d = yaml.safe_load("on: pull_request\n")
    check(
        "on-triggers: plain pull_request NOT flagged",
        "pull_request_target" not in core._on_triggers(d),
    )
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
    check(
        "checkout-head: head.sha checkout flagged",
        core._checks_out_pr_head(yaml.safe_load(EXPLOIT_WF)) is True,
    )
    check(
        "checkout-head: github.head_ref checkout flagged",
        core._checks_out_pr_head(yaml.safe_load(HEAD_REF_WF)) is True,
    )
    check(
        "checkout-head: plain checkout NOT flagged",
        core._checks_out_pr_head(yaml.safe_load(SAFE_WF)) is False,
    )
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
    check(
        "posture: no .github/workflows dir -> no posture, no error",
        p == {"pr_target": False, "exploit": False, "error": False},
    )


def test_posture_listing_error_fails_closed():
    p = posture(([], "error"), {})
    check(
        "posture: listing read error -> pr_target True (fail closed)",
        p["pr_target"] is True,
    )
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
    check(
        "posture: exploit pattern flagged",
        p["pr_target"] is True and p["exploit"] is True,
    )


def test_posture_unreadable_or_unparseable_file_fails_closed():
    p = posture((["w/a.yml"], "ok"), {"w/a.yml": None})  # content unreadable
    check(
        "posture: unreadable workflow file -> fail closed",
        p["pr_target"] is True and p["error"] is True,
    )
    p = posture((["w/a.yml"], "ok"), {"w/a.yml": "on: [\n"})  # invalid YAML
    check(
        "posture: unparseable workflow -> fail closed",
        p["pr_target"] is True and p["error"] is True,
    )


def test_posture_contents_listing_limit_fails_closed():
    entries = [
        {
            "type": "file",
            "name": "wf%d.yml" % i,
            "path": ".github/workflows/wf%d.yml" % i,
        }
        for i in range(1000)
    ]
    save = core._gh_api_capture
    core._gh_api_capture = lambda path: SimpleNamespace(
        returncode=0, stdout=json.dumps(entries), stderr=""
    )
    try:
        paths, status = core._list_workflow_files("o/r")
        p = core.repo_pr_target_posture("o/r")
    finally:
        core._gh_api_capture = save
    check("posture: contents listing at API limit -> error", status == "error")
    check("posture: contents listing at API limit -> no paths trusted", paths == [])
    check(
        "posture: contents listing at API limit -> fail closed",
        p["pr_target"] is True and p["error"] is True,
    )


def test_posture_non_base64_workflow_file_fails_closed():
    listing = [{"type": "file", "name": "ci.yml", "path": ".github/workflows/ci.yml"}]

    def fake_capture(path):
        if path.endswith("/contents/.github/workflows"):
            return SimpleNamespace(returncode=0, stdout=json.dumps(listing), stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"encoding": "none", "content": ""}),
            stderr="",
        )

    save = core._gh_api_capture
    core._gh_api_capture = fake_capture
    try:
        text = core._fetch_workflow_text("o/r", ".github/workflows/ci.yml")
        p = core.repo_pr_target_posture("o/r")
    finally:
        core._gh_api_capture = save
    check("posture: non-base64 workflow content -> unreadable", text is None)
    check(
        "posture: non-base64 workflow content -> fail closed",
        p["pr_target"] is True and p["error"] is True,
    )


# --------------------------------------------------------------------------- #
# build_repo routing: auto-approve vs card
# --------------------------------------------------------------------------- #
def check_run(name, conclusion=None, status="COMPLETED"):
    return {
        "__typename": "CheckRun",
        "name": name,
        "conclusion": conclusion,
        "status": status,
    }


def rollup(contexts):
    return {"state": "PENDING", "contexts": {"nodes": contexts}}


def pr_node(number, status_rollup, draft=False, base_ref="main", cross_repo=True):
    node = {
        "number": number,
        "title": "PR %d" % number,
        "isDraft": draft,
        "isCrossRepository": cross_repo,
        "updatedAt": "2026-01-01T00:00:00Z",
        "changedFiles": 1,
        "author": {"login": "contributor"},
        "headRefName": "feature-%d" % number,
        "headRefOid": "sha%d" % number,
        "baseRefName": base_ref,
        "headRepository": {"name": "demo-fork", "owner": {"login": "forker"}},
        "baseRepository": {"name": "demo", "owner": {"login": "owner"}},
        "labels": {"nodes": []},
        "closingIssuesReferences": {"nodes": []},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": status_rollup}}]},
    }
    if cross_repo == "missing":
        node.pop("isCrossRepository")
        node.pop("headRepository")
        node.pop("baseRepository")
    return node


def graphql_data(pr_nodes, default_branch="main"):
    return {
        "defaultBranchRef": {"name": default_branch},
        "pullRequests": {"totalCount": len(pr_nodes), "nodes": pr_nodes},
        "issues": {"totalCount": 0, "nodes": []},
    }


SAFE_VERDICT = {
    "safe": True,
    "error": False,
    "risky_files": [],
    "pr_target": False,
    "exploit": False,
    "reason": "clean",
}


def run_build_repo(
    pr_nodes,
    *,
    auto_approve_ci=True,
    repo_over=None,
    posture_value=None,
    verdict=None,
    approve_result=("approved", "approved 1 run"),
    approve_raises=False,
    graphql_raises=False,
    default_branch="main",
):
    """Drive build_repo with the network-touching dependencies stubbed."""
    calls = {"approve": [], "posture": 0, "safety": []}
    repo_cfg = {
        "name": "demo",
        "compliance_check": "Gate",
        "test_check_patterns": ["test"],
    }
    if repo_over:
        repo_cfg.update(repo_over)

    def fake_graphql(owner, name):
        if graphql_raises:
            raise RuntimeError("boom")
        return graphql_data(pr_nodes, default_branch)

    def fake_posture(slug):
        calls["posture"] += 1
        return CLEAN_POSTURE if posture_value is None else posture_value

    def fake_ci_safety(slug, pr, repo_posture, changed_files=None):
        calls["safety"].append((slug, pr, repo_posture, changed_files))
        if callable(verdict):
            return verdict(slug, pr, repo_posture, changed_files)
        return SAFE_VERDICT if verdict is None else verdict

    def fake_approve(owner, name, pr, posture=None, strict=False):
        calls["approve"].append((owner, name, pr, posture, strict))
        if approve_raises:
            raise RuntimeError("approve boom")
        return approve_result

    save = (
        core.gh_graphql,
        core.repo_pr_target_posture,
        core.ci_safety,
        core.approve_ci,
    )
    core.gh_graphql, core.repo_pr_target_posture = fake_graphql, fake_posture
    core.ci_safety, core.approve_ci = fake_ci_safety, fake_approve
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            result, items = core.build_repo(
                "owner", repo_cfg, False, auto_approve_ci=auto_approve_ci
            )
    finally:
        (
            core.gh_graphql,
            core.repo_pr_target_posture,
            core.ci_safety,
            core.approve_ci,
        ) = save
    calls["stderr"] = err.getvalue()  # so logging-path tests can assert the per-PR line
    return result, items, calls


def needs_ci_pr(number=1, base_ref="main", cross_repo=True):
    return pr_node(
        number, None, base_ref=base_ref, cross_repo=cross_repo
    )  # no status rollup -> ci absent -> needs-ci-approval


def test_safe_pr_is_auto_approved_no_card():
    result, items, calls = run_build_repo([needs_ci_pr()])
    check("route: safe PR raises NO card", items == [])
    check("route: safe PR is approved exactly once", len(calls["approve"]) == 1)
    check(
        "route: approve received the per-repo posture",
        calls["approve"] and calls["approve"][0][3] == CLEAN_POSTURE,
    )
    check(
        "route: auto approve runs in strict mode",
        calls["approve"] and calls["approve"][0][4] is True,
    )
    check("route: repo result still ok", result["ok"] is True)


def test_same_repo_no_ci_routes_to_review_needed_not_ci_approval():
    pr = needs_ci_pr(cross_repo=False)
    pr["headRepository"] = {"name": "demo", "owner": {"login": "owner"}}
    pr["baseRepository"] = {"name": "demo", "owner": {"login": "owner"}}
    result, items, calls = run_build_repo([pr])
    check(
        "route: same-repo no-CI PR is a pr-review card",
        len(items) == 1 and items[0]["kind"] == "pr-review",
    )
    check(
        "route: same-repo no-CI PR uses review-needed bucket",
        items and items[0]["bucket"] == "review-needed",
    )
    check("route: same-repo no-CI PR is NOT auto-approved", calls["approve"] == [])
    check("route: same-repo no-CI PR skips posture", calls["posture"] == 0)
    check("route: same-repo no-CI PR skips ci_safety", calls["safety"] == [])


def test_unknown_fork_status_keeps_ci_card_without_auto_approval():
    result, items, calls = run_build_repo([needs_ci_pr(cross_repo="missing")])
    warning = items[0].get("warning") if items else ""
    check(
        "route: unknown fork status keeps a ci-approval card",
        len(items) == 1 and items[0]["kind"] == "ci-approval",
    )
    check(
        "route: unknown fork status warns instead of guessing",
        "could not determine" in (warning or ""),
    )
    check("route: unknown fork status is NOT auto-approved", calls["approve"] == [])
    check("route: unknown fork status skips posture", calls["posture"] == 0)
    check("route: unknown fork status skips ci_safety", calls["safety"] == [])


def test_risky_pr_raises_card_not_approved():
    verdict = {
        "safe": False,
        "error": False,
        "risky_files": [".github/workflows/ci.yml"],
        "pr_target": False,
        "exploit": False,
        "reason": "risky",
    }
    result, items, calls = run_build_repo([needs_ci_pr()], verdict=verdict)
    check(
        "route: risky PR raises a ci-approval card",
        len(items) == 1 and items[0]["kind"] == "ci-approval",
    )
    check("route: risky PR card carries a warning", bool(items[0].get("warning")))
    check("route: risky PR is NOT auto-approved", calls["approve"] == [])


def test_pr_target_posture_raises_card_with_warning():
    verdict = {
        "safe": False,
        "error": False,
        "risky_files": [],
        "pr_target": True,
        "exploit": False,
        "reason": "pull_request_target",
    }
    result, items, calls = run_build_repo([needs_ci_pr()], verdict=verdict)
    check("route: pull_request_target PR raises a card", len(items) == 1)
    check(
        "route: pull_request_target card warns about it",
        "pull_request_target" in (items[0].get("warning") or ""),
    )
    check("route: pull_request_target PR is NOT auto-approved", calls["approve"] == [])


def test_exploit_pattern_card_warns_loudly():
    verdict = {
        "safe": False,
        "error": False,
        "risky_files": [],
        "pr_target": True,
        "exploit": True,
        "reason": "pwn-request",
    }
    result, items, calls = run_build_repo([needs_ci_pr()], verdict=verdict)
    check("route: exploit-pattern PR raises a card", len(items) == 1)
    check(
        "route: exploit-pattern card warns loudly (DANGER)",
        "DANGER" in (items[0].get("warning") or ""),
    )
    check("route: exploit-pattern PR is NOT auto-approved", calls["approve"] == [])


def test_ci_safety_error_raises_card():
    verdict = {
        "safe": False,
        "error": True,
        "risky_files": ["<could-not-list-files - failing closed>"],
        "pr_target": False,
        "exploit": False,
        "reason": "fail-closed",
    }
    result, items, calls = run_build_repo([needs_ci_pr()], verdict=verdict)
    check("route: verdict error -> a card", len(items) == 1)
    check("route: verdict error -> NOT auto-approved", calls["approve"] == [])


def test_truncated_pr_file_list_routes_to_card():
    pr = needs_ci_pr()
    pr["changedFiles"] = 3001
    calls = {"approve": []}

    def fake_graphql(owner, name):
        return graphql_data([pr])

    def fake_run(cmd, capture_output=True, text=True):
        if cmd[:3] == ["gh", "api", "--paginate"]:
            files = ["src/file%d.py" % i for i in range(3000)]
            return SimpleNamespace(returncode=0, stdout="\n".join(files), stderr="")
        raise AssertionError(cmd)

    def fake_approve(owner, name, pr_number, posture=None):
        calls["approve"].append((owner, name, pr_number))
        return ("approved", "approved 1 run")

    save = (
        core.gh_graphql,
        core.repo_pr_target_posture,
        core.approve_ci,
        core.subprocess.run,
    )
    core.gh_graphql = fake_graphql
    core.repo_pr_target_posture = lambda slug: CLEAN_POSTURE
    core.approve_ci = fake_approve
    core.subprocess.run = fake_run
    try:
        with redirect_stderr(io.StringIO()):
            result, items = core.build_repo(
                "owner",
                {
                    "name": "demo",
                    "compliance_check": "Gate",
                    "test_check_patterns": ["test"],
                },
                False,
                auto_approve_ci=True,
            )
    finally:
        (
            core.gh_graphql,
            core.repo_pr_target_posture,
            core.approve_ci,
            core.subprocess.run,
        ) = save

    check("route: truncated PR file list raises a card", len(items) == 1)
    check("route: truncated PR file list is NOT auto-approved", calls["approve"] == [])
    check(
        "route: truncated PR file list warning fails closed",
        "could-not-list-all-files" in (items[0].get("warning") or ""),
    )


def test_non_default_base_pr_raises_card_without_posture_read():
    save = core._list_pr_files
    real_ci_safety = core.ci_safety
    core._list_pr_files = lambda slug, pr, expected_count: ([], True, True)
    try:
        result, items, calls = run_build_repo(
            [needs_ci_pr(base_ref="release")], verdict=real_ci_safety
        )
    finally:
        core._list_pr_files = save
    check(
        "route: non-default-base PR raises a ci-approval card",
        len(items) == 1 and items[0]["kind"] == "ci-approval",
    )
    check("route: non-default-base PR is NOT auto-approved", calls["approve"] == [])
    check(
        "route: non-default-base PR skips default-branch posture read",
        calls["posture"] == 0,
    )
    check(
        "route: non-default-base card warns about the base branch",
        "release" in (items[0].get("warning") or "")
        and "main" in (items[0].get("warning") or ""),
    )
    check(
        "route: non-default-base verdict is posture-present",
        calls["safety"] and calls["safety"][0][2].get("non_default_base") is True,
    )


def test_approve_failure_falls_back_to_card():
    result, items, calls = run_build_repo(
        [needs_ci_pr()], approve_result=("error", "api fail")
    )
    check("route: approve error falls back to a card (nothing lost)", len(items) == 1)
    check(
        "route: approve was attempted before falling back", len(calls["approve"]) == 1
    )


def test_approve_noop_consumes_stale_ci_approval_card():
    result, items, calls = run_build_repo(
        [needs_ci_pr()], approve_result=("noop", "no workflow runs awaiting approval")
    )
    check("route: approve noop emits NO stale card", items == [])
    check(
        "route: approve noop first checked the run state", len(calls["approve"]) == 1
    )


def test_approve_hold_falls_back_to_card():
    result, items, calls = run_build_repo(
        [needs_ci_pr()], approve_result=("hold", "held")
    )
    check("route: approve hold falls back to a card", len(items) == 1)


def test_approve_exception_falls_back_to_card():
    result, items, calls = run_build_repo([needs_ci_pr()], approve_raises=True)
    check("route: an approve that raises falls back to a card", len(items) == 1)
    check("route: approve was attempted", len(calls["approve"]) == 1)


# --------------------------------------------------------------------------- #
# observability: every ci-approval PR the auto path handles emits ONE log line
# (a ::notice:: when approved, a ::warning:: with the verdict reason + any
# approve_ci status/message when carded) so a silent approve failure - the
# "inert in production, approved 0 runs" bug - can never hide in the scan log.
# --------------------------------------------------------------------------- #
def test_auto_approved_pr_emits_one_notice():
    result, items, calls = run_build_repo([needs_ci_pr()])
    err = calls["stderr"]
    check("log: approved PR emits a ::notice::", "::notice::demo#1" in err)
    check("log: approved notice carries the verdict reason", "clean" in err)
    check("log: approved notice carries the approve message", "approved 1 run" in err)
    check("log: approved PR emits NO ::warning::", "::warning::" not in err)


def test_carded_approve_error_is_logged_with_status_and_message():
    # The production failure mode: verdict is safe, but the approve POST fails -
    # previously swallowed into item["warning"], now LOUD in the scan log.
    result, items, calls = run_build_repo(
        [needs_ci_pr()], approve_result=("error", "api fail: 403 forbidden")
    )
    err = calls["stderr"]
    check(
        "log: carded PR emits a ::warning::",
        "::warning::wheelhouse auto-approve carded demo#1:" in err,
    )
    check(
        "log: carded warning carries the verdict reason", "verdict safe (clean)" in err
    )
    check("log: carded warning surfaces approve_ci status", "approve_ci error" in err)
    check(
        "log: carded warning surfaces approve_ci message",
        "api fail: 403 forbidden" in err,
    )
    check("log: carded PR still raises a card", len(items) == 1)
    check(
        "log: card body warning is unchanged (no verdict-reason leak)",
        "auto-approve did not complete (error: api fail: 403 forbidden)"
        == (items[0].get("warning") or ""),
    )


def test_carded_approve_multiline_error_is_logged_on_one_line():
    message = "api fail: first line\nsecond line\r\nthird line\rfourth line"
    result, items, calls = run_build_repo(
        [needs_ci_pr()], approve_result=("error", message)
    )
    err = calls["stderr"]
    lines = err.splitlines()
    check(
        "log: multiline approve error emits one physical warning line", len(lines) == 1
    )
    check(
        "log: multiline approve error is collapsed in workflow command",
        "api fail: first line second line third line fourth line" in err,
    )
    check(
        "log: multiline approve error does not leave continuation text",
        all(line.startswith("::warning::") for line in lines),
    )
    check(
        "log: card body still carries the original multiline warning",
        ("auto-approve did not complete (error: %s)" % message)
        == (items[0].get("warning") or ""),
    )


def test_auto_approved_multiline_message_is_logged_on_one_line():
    result, items, calls = run_build_repo(
        [needs_ci_pr()], approve_result=("approved", "approved\nrun")
    )
    err = calls["stderr"]
    lines = err.splitlines()
    check(
        "log: multiline approve success emits one physical notice line", len(lines) == 1
    )
    check(
        "log: multiline approve success is collapsed in workflow command",
        "approved run" in err,
    )


def test_noop_consumed_ci_approval_is_logged_with_status():
    result, items, calls = run_build_repo(
        [needs_ci_pr()], approve_result=("noop", "no workflow runs awaiting approval")
    )
    err = calls["stderr"]
    check("log: noop consumed PR emits a ::notice::", "::notice::demo#1" in err)
    check("log: noop notice names the noop status", "approve_ci noop" in err)
    check(
        "log: noop notice surfaces the message",
        "no workflow runs awaiting approval" in err,
    )
    check(
        "log: noop notice still carries the verdict reason",
        "verdict safe (clean)" in err,
    )
    check("log: noop consumed PR emits NO ::warning::", "::warning::" not in err)
    check("log: noop consumed PR raises NO card", items == [])


def test_carded_approve_exception_is_logged():
    result, items, calls = run_build_repo([needs_ci_pr()], approve_raises=True)
    err = calls["stderr"]
    check(
        "log: an approve that raises is logged as an error outcome",
        "approve_ci error" in err,
    )
    check(
        "log: the raised-approve warning mentions it was raised",
        "auto-approve raised" in err,
    )


def test_carded_unsafe_verdict_is_logged_without_approve_status():
    # No approve was attempted, so the log line carries the verdict reason only.
    verdict = {
        "safe": False,
        "error": False,
        "risky_files": [".github/workflows/ci.yml"],
        "pr_target": False,
        "exploit": False,
        "reason": "touches CI-execution files",
    }
    result, items, calls = run_build_repo([needs_ci_pr()], verdict=verdict)
    err = calls["stderr"]
    check(
        "log: unsafe-verdict PR emits a ::warning::",
        "::warning::wheelhouse auto-approve carded demo#1:" in err,
    )
    check(
        "log: unsafe-verdict warning marks the verdict unsafe",
        "verdict unsafe (touches CI-execution files)" in err,
    )
    check(
        "log: unsafe-verdict warning has no approve_ci status (none attempted)",
        "approve_ci" not in err,
    )


def test_carded_disabled_auto_approve_is_logged():
    result, items, calls = run_build_repo([needs_ci_pr()], auto_approve_ci=False)
    err = calls["stderr"]
    check(
        "log: disabled auto-approve still emits a per-PR ::warning::",
        "::warning::wheelhouse auto-approve carded demo#1:" in err,
    )
    check("log: disabled auto-approve line says so", "auto-approve disabled" in err)
    check("log: disabled auto-approve attempted no approve", calls["approve"] == [])


def run_approve_ci(
    run_list_result,
    approval_results=None,
    run_details=None,
    pr_data=None,
    stub_safety=True,
    safety_verdict=None,
    posture=CLEAN_POSTURE,
    strict=False,
    repo_posture=None,
):
    approval_results = list(approval_results or [])
    run_details = run_details or {}
    calls = {"approved": [], "run_list": []}
    pr_payload = pr_data or {
        "head": {"ref": "feature", "sha": "sha1"},
        "base": {"ref": "main", "repo": {"default_branch": "main"}},
        "changed_files": 0,
    }
    safety_verdict = safety_verdict or SAFE_VERDICT

    def fake_run(cmd, capture_output=True, text=True):
        calls.setdefault("commands", []).append(cmd)
        if cmd[:3] == ["gh", "api", "/repos/o/r/pulls/1"]:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(pr_payload), stderr=""
            )
        if cmd[:3] == ["gh", "api", "--paginate"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "run", "list"]:
            calls["run_list"].append(cmd)
            return run_list_result
        if cmd[:3] == ["gh", "api", "--method"]:
            calls["approved"].append(cmd[-1].rsplit("/", 2)[-2])
            return approval_results.pop(0)
        if cmd[:2] == ["gh", "api"] and "/actions/runs/" in cmd[2]:
            rid = cmd[2].rsplit("/", 1)[-1]
            detail = run_details.get(
                str(rid),
                {
                    "head_sha": "sha1",
                    "head_branch": "feature",
                    "pull_requests": [{"number": 1}],
                },
            )
            if detail == "error":
                return SimpleNamespace(returncode=1, stdout="", stderr="detail failed")
            if detail == "invalid-json":
                return SimpleNamespace(returncode=0, stdout="not-json", stderr="")
            return SimpleNamespace(returncode=0, stdout=json.dumps(detail), stderr="")
        raise AssertionError(cmd)

    def fake_posture(slug):
        return CLEAN_POSTURE if repo_posture is None else repo_posture

    save = (core.subprocess.run, core.ci_safety, core.repo_pr_target_posture)
    core.subprocess.run = fake_run
    core.repo_pr_target_posture = fake_posture
    if stub_safety:
        core.ci_safety = lambda slug, pr, posture, changed_files=None: safety_verdict
    try:
        status, message = core.approve_ci("o", "r", "1", posture=posture, strict=strict)
        return status, message, calls
    finally:
        core.subprocess.run, core.ci_safety, core.repo_pr_target_posture = save


def test_approve_ci_run_list_failure_returns_error():
    status, message, calls = run_approve_ci(
        SimpleNamespace(returncode=1, stdout="", stderr="rate limited")
    )
    check("approve_ci: run-list failure -> error", status == "error")
    check("approve_ci: run-list failure mentions cause", "rate limited" in message)


def test_approve_ci_invalid_run_list_returns_error():
    status, message, calls = run_approve_ci(
        SimpleNamespace(returncode=0, stdout="not-json", stderr="")
    )
    check("approve_ci: invalid run-list JSON -> error", status == "error")
    check("approve_ci: invalid run-list JSON mentions cause", "invalid JSON" in message)


def test_approve_ci_any_failed_post_returns_error():
    runs = [
        {"databaseId": 123, "workflowName": "CI"},
        {"databaseId": 124, "workflowName": "Lint"},
    ]
    approvals = [
        SimpleNamespace(returncode=0, stdout="", stderr=""),
        SimpleNamespace(returncode=1, stdout="", stderr="forbidden"),
    ]
    status, message, calls = run_approve_ci(
        SimpleNamespace(returncode=0, stdout=json.dumps(runs), stderr=""), approvals
    )
    check("approve_ci: any failed approval POST -> error", status == "error")
    check("approve_ci: failed approval POST is named", "Lint:forbidden" in message)


def test_approve_ci_hold_message_caps_risky_file_list():
    risky = [".github/workflows/%02d.yml" % i for i in range(12)]
    verdict = {
        "safe": False,
        "error": False,
        "risky_files": risky,
        "pr_target": False,
        "exploit": False,
        "reason": "risky",
    }
    status, message, calls = run_approve_ci(
        SimpleNamespace(returncode=0, stdout="[]", stderr=""), safety_verdict=verdict
    )
    check("approve_ci: risky-file verdict still holds", status == "hold")
    check(
        "approve_ci: risky hold summarizes extra files",
        "(+2 more; 12 total)" in message,
    )
    check(
        "approve_ci: risky hold does not render the eleventh file",
        risky[10] not in message,
    )


def test_approve_ci_filters_to_current_pr_head_and_number():
    runs = [
        {"databaseId": 101, "workflowName": "Old"},
        {"databaseId": 102, "workflowName": "Other PR"},
        {"databaseId": 103, "workflowName": "Current"},
    ]
    details = {
        "101": {"head_sha": "oldsha", "pull_requests": [{"number": 1}]},
        "102": {"head_sha": "sha1", "pull_requests": [{"number": 2}]},
        "103": {"head_sha": "sha1", "pull_requests": [{"number": 1}]},
    }
    approvals = [SimpleNamespace(returncode=0, stdout="", stderr="")]
    status, message, calls = run_approve_ci(
        SimpleNamespace(returncode=0, stdout=json.dumps(runs), stderr=""),
        approvals,
        details,
    )
    check(
        "approve_ci: only matching PR/head run is approved",
        calls["approved"] == ["103"],
    )
    check("approve_ci: matching run approval succeeds", status == "approved")
    cmd = calls["run_list"][0]
    check(
        "approve_ci: run list is filtered by commit",
        "--commit" in cmd and "sha1" in cmd,
    )


def test_approve_ci_fork_run_with_empty_pr_association_is_approved():
    runs = [{"databaseId": 101, "workflowName": "CI"}]
    details = {
        "101": {"head_sha": "sha1", "head_branch": "feature", "pull_requests": []}
    }
    approvals = [SimpleNamespace(returncode=0, stdout="", stderr="")]
    status, message, calls = run_approve_ci(
        SimpleNamespace(returncode=0, stdout=json.dumps(runs), stderr=""),
        approvals,
        details,
    )
    check(
        "approve_ci: fork run with empty PR association is approved",
        status == "approved",
    )
    check(
        "approve_ci: fork run approval reaches matching run",
        calls["approved"] == ["101"],
    )


def test_approve_ci_fork_empty_pr_association_requires_matching_branch():
    runs = [{"databaseId": 101, "workflowName": "CI"}]
    details = {"101": {"head_sha": "sha1", "head_branch": "other", "pull_requests": []}}
    status, message, calls = run_approve_ci(
        SimpleNamespace(returncode=0, stdout=json.dumps(runs), stderr=""), [], details
    )
    check("approve_ci: fork run with wrong branch -> noop", status == "noop")
    check(
        "approve_ci: fork run with wrong branch is not approved",
        calls["approved"] == [],
    )
    check(
        "approve_ci: fork wrong-branch message mentions skipped run",
        "skipped" in message,
    )


def test_approve_ci_populated_pr_association_wrong_or_ambiguous_is_rejected():
    runs = [{"databaseId": 101, "workflowName": "CI"}]
    cases = [
        ("wrong", [{"number": 2}], "noop"),
        ("ambiguous", [{"number": 1}, {"number": 2}], "error"),
    ]
    for label, pull_requests, want_status in cases:
        status, message, calls = run_approve_ci(
            SimpleNamespace(returncode=0, stdout=json.dumps(runs), stderr=""),
            [SimpleNamespace(returncode=0, stdout="", stderr="")],
            {"101": {"head_sha": "sha1", "pull_requests": pull_requests}},
        )
        check(
            "approve_ci: %s populated run PR association is rejected" % label,
            status == want_status,
        )
        check(
            "approve_ci: %s run PR association is not approved" % label,
            calls["approved"] == [],
        )
        check(
            "approve_ci: %s run PR association is named" % label,
            "pull request" in message or "not PR" in message or "skipped" in message,
        )


def test_approve_ci_full_run_list_returns_error_without_approving():
    runs = [{"databaseId": i, "workflowName": "Run%d" % i} for i in range(100, 130)]
    details = {
        str(i): {"head_sha": "sha1", "pull_requests": [{"number": 1}]}
        for i in range(100, 130)
    }
    approvals = [SimpleNamespace(returncode=0, stdout="", stderr="") for _ in runs]
    status, message, calls = run_approve_ci(
        SimpleNamespace(returncode=0, stdout=json.dumps(runs), stderr=""),
        approvals,
        details,
    )
    check("approve_ci: full run-list page -> error", status == "error")
    check(
        "approve_ci: full run-list page mentions truncation risk",
        "possibly truncated" in message,
    )
    check("approve_ci: full run-list page approves nothing", calls["approved"] == [])


def test_approve_ci_no_matching_runs_returns_noop_without_approving():
    runs = [{"databaseId": 101, "workflowName": "Old"}]
    details = {"101": {"head_sha": "oldsha", "pull_requests": [{"number": 1}]}}
    status, message, calls = run_approve_ci(
        SimpleNamespace(returncode=0, stdout=json.dumps(runs), stderr=""), [], details
    )
    check("approve_ci: no matching runs -> noop", status == "noop")
    check("approve_ci: no matching runs -> no approval POST", calls["approved"] == [])
    check("approve_ci: no matching runs message mentions skipped", "skipped" in message)


def test_approve_ci_run_detail_failure_returns_error():
    runs = [{"databaseId": 101, "workflowName": "CI"}]
    status, message, calls = run_approve_ci(
        SimpleNamespace(returncode=0, stdout=json.dumps(runs), stderr=""),
        [],
        {"101": "error"},
    )
    check("approve_ci: run-detail failure -> error", status == "error")
    check("approve_ci: run-detail failure does not approve", calls["approved"] == [])


def test_approve_ci_non_default_base_warns_without_posture_read():
    runs = [{"databaseId": 101, "workflowName": "CI"}]
    approvals = [SimpleNamespace(returncode=0, stdout="", stderr="")]
    pr_data = {
        "head": {"ref": "feature", "sha": "sha1"},
        "base": {"ref": "release", "repo": {"default_branch": "main"}},
        "changed_files": 0,
    }
    status, message, calls = run_approve_ci(
        SimpleNamespace(returncode=0, stdout=json.dumps(runs), stderr=""),
        approvals,
        {"101": {"head_sha": "sha1", "pull_requests": [{"number": 1}]}},
        pr_data=pr_data,
        stub_safety=False,
    )
    check(
        "approve_ci: non-default-base manual approval can proceed", status == "approved"
    )
    check(
        "approve_ci: non-default-base manual approval warns",
        "release" in message and "main" in message,
    )
    check(
        "approve_ci: non-default-base approval reaches matching run",
        calls["approved"] == ["101"],
    )


def test_approve_ci_strict_blocks_approval_time_pr_target_posture():
    runs = [{"databaseId": 101, "workflowName": "CI"}]
    approvals = [SimpleNamespace(returncode=0, stdout="", stderr="")]
    status, message, calls = run_approve_ci(
        SimpleNamespace(returncode=0, stdout=json.dumps(runs), stderr=""),
        approvals,
        {"101": {"head_sha": "sha1", "pull_requests": [{"number": 1}]}},
        stub_safety=False,
        strict=True,
        repo_posture={"pr_target": True, "exploit": False, "error": False},
    )
    check("approve_ci: strict pr_target posture -> error", status == "error")
    check(
        "approve_ci: strict pr_target posture approves nothing", calls["approved"] == []
    )
    check(
        "approve_ci: strict pr_target posture explains safety block",
        "pull_request_target" in message,
    )


def test_approve_ci_strict_blocks_non_default_base():
    runs = [{"databaseId": 101, "workflowName": "CI"}]
    approvals = [SimpleNamespace(returncode=0, stdout="", stderr="")]
    pr_data = {
        "head": {"ref": "feature", "sha": "sha1"},
        "base": {"ref": "release", "repo": {"default_branch": "main"}},
        "changed_files": 0,
    }
    status, message, calls = run_approve_ci(
        SimpleNamespace(returncode=0, stdout=json.dumps(runs), stderr=""),
        approvals,
        {"101": {"head_sha": "sha1", "pull_requests": [{"number": 1}]}},
        pr_data=pr_data,
        stub_safety=False,
        strict=True,
    )
    check("approve_ci: strict non-default-base -> error", status == "error")
    check(
        "approve_ci: strict non-default-base approves nothing", calls["approved"] == []
    )
    check(
        "approve_ci: strict non-default-base explains safety block",
        "release" in message and "main" in message,
    )


def test_opt_out_global_disables_auto_approve():
    result, items, calls = run_build_repo([needs_ci_pr()], auto_approve_ci=False)
    check("opt-out: safe PR STILL raises a card", len(items) == 1)
    check("opt-out: approve never called", calls["approve"] == [])


def test_opt_out_card_still_carries_pr_target_warning():
    verdict = {
        "safe": False,
        "error": False,
        "risky_files": [],
        "pr_target": True,
        "exploit": False,
        "reason": "pull_request_target",
    }
    result, items, calls = run_build_repo(
        [needs_ci_pr()], auto_approve_ci=False, verdict=verdict
    )
    check(
        "opt-out: card still warns about pull_request_target",
        "pull_request_target" in (items[0].get("warning") or ""),
    )
    check("opt-out: still no auto-approve", calls["approve"] == [])


def test_per_repo_override_disables_auto_approve():
    result, items, calls = run_build_repo(
        [needs_ci_pr()], repo_over={"auto_approve_ci": False}
    )
    check("override: per-repo false beats global on -> a card", len(items) == 1)
    check("override: per-repo false -> approve never called", calls["approve"] == [])


def test_idempotent_non_ci_approval_pr_never_reapproved():
    # Once approved, the next scan sees CI running / results, NOT needs-ci-approval.
    running = pr_node(2, rollup([check_run("Gate", None, status="IN_PROGRESS")]))
    result, items, calls = run_build_repo([running])
    check("idempotent: ci-running PR produces no card", items == [])
    check("idempotent: ci-running PR never calls approve", calls["approve"] == [])
    check("idempotent: posture not read when no ci-approval PR", calls["posture"] == 0)

    merge_ready = pr_node(
        3, rollup([check_run("Gate", "SUCCESS"), check_run("build-test", "SUCCESS")])
    )
    result, items, calls = run_build_repo([merge_ready])
    check(
        "idempotent: merge-ready PR is a pr-review card, not approved",
        len(items) == 1 and items[0]["kind"] == "pr-review",
    )
    check("idempotent: merge-ready PR never calls approve", calls["approve"] == [])


def test_ok_false_repo_is_never_auto_approved():
    result, items, calls = run_build_repo([needs_ci_pr()], graphql_raises=True)
    check(
        "ok:false: failed scan returns no items", result["ok"] is False and items == []
    )
    check("ok:false: failed scan never auto-approves", calls["approve"] == [])
    check("ok:false: failed scan never reads posture", calls["posture"] == 0)


def test_posture_read_once_per_repo_for_multiple_ci_prs():
    result, items, calls = run_build_repo(
        [needs_ci_pr(1), needs_ci_pr(2), needs_ci_pr(3)]
    )
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
    check(
        "config: auto_approve_ci defaults ON when the key is absent",
        cfg["auto_approve_ci"] is True,
    )


def test_config_explicit_opt_out_honored():
    cfg = _load_config_with("repos: []\nauto_approve_ci: false\n")
    check(
        "config: explicit auto_approve_ci:false is honored",
        cfg["auto_approve_ci"] is False,
    )


def test_auto_approve_enabled_per_repo_override():
    check(
        "flag: absent per-repo -> global default True",
        core._auto_approve_enabled({}, True) is True,
    )
    check(
        "flag: absent per-repo -> global default False",
        core._auto_approve_enabled({}, False) is False,
    )
    check(
        "flag: per-repo false overrides global true",
        core._auto_approve_enabled({"auto_approve_ci": False}, True) is False,
    )
    check(
        "flag: per-repo true overrides global false",
        core._auto_approve_enabled({"auto_approve_ci": True}, False) is True,
    )


def main():
    test_ci_safety_clean_is_safe()
    test_ci_safety_risky_files_hold()
    test_ci_safety_file_list_error_fails_closed()
    test_ci_safety_file_list_truncation_fails_closed()
    test_ci_safety_pr_target_posture_blocks_auto()
    test_ci_safety_exploit_flag_passthrough()
    test_ci_safety_posture_read_error_fails_closed()
    test_ci_safety_note_caps_risky_file_list()
    test_on_triggers_handles_every_form_and_yaml_gotcha()
    test_checks_out_pr_head()
    test_posture_no_workflows_dir_is_clean()
    test_posture_listing_error_fails_closed()
    test_posture_plain_pull_request_is_clean()
    test_posture_detects_pull_request_target()
    test_posture_detects_exploit_pattern()
    test_posture_unreadable_or_unparseable_file_fails_closed()
    test_posture_contents_listing_limit_fails_closed()
    test_posture_non_base64_workflow_file_fails_closed()
    test_safe_pr_is_auto_approved_no_card()
    test_same_repo_no_ci_routes_to_review_needed_not_ci_approval()
    test_unknown_fork_status_keeps_ci_card_without_auto_approval()
    test_risky_pr_raises_card_not_approved()
    test_pr_target_posture_raises_card_with_warning()
    test_exploit_pattern_card_warns_loudly()
    test_ci_safety_error_raises_card()
    test_truncated_pr_file_list_routes_to_card()
    test_non_default_base_pr_raises_card_without_posture_read()
    test_approve_failure_falls_back_to_card()
    test_approve_noop_consumes_stale_ci_approval_card()
    test_approve_hold_falls_back_to_card()
    test_approve_exception_falls_back_to_card()
    test_auto_approved_pr_emits_one_notice()
    test_carded_approve_error_is_logged_with_status_and_message()
    test_carded_approve_multiline_error_is_logged_on_one_line()
    test_auto_approved_multiline_message_is_logged_on_one_line()
    test_noop_consumed_ci_approval_is_logged_with_status()
    test_carded_approve_exception_is_logged()
    test_carded_unsafe_verdict_is_logged_without_approve_status()
    test_carded_disabled_auto_approve_is_logged()
    test_approve_ci_run_list_failure_returns_error()
    test_approve_ci_invalid_run_list_returns_error()
    test_approve_ci_any_failed_post_returns_error()
    test_approve_ci_hold_message_caps_risky_file_list()
    test_approve_ci_filters_to_current_pr_head_and_number()
    test_approve_ci_fork_run_with_empty_pr_association_is_approved()
    test_approve_ci_fork_empty_pr_association_requires_matching_branch()
    test_approve_ci_populated_pr_association_wrong_or_ambiguous_is_rejected()
    test_approve_ci_full_run_list_returns_error_without_approving()
    test_approve_ci_no_matching_runs_returns_noop_without_approving()
    test_approve_ci_run_detail_failure_returns_error()
    test_approve_ci_non_default_base_warns_without_posture_read()
    test_approve_ci_strict_blocks_approval_time_pr_target_posture()
    test_approve_ci_strict_blocks_non_default_base()
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
