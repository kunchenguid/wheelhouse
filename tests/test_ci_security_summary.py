#!/usr/bin/env python3
"""
Unit-exercise the advisory, read-only CI-approval security summary with NO
network.

Run: python tests/test_ci_security_summary.py   (needs PyYAML; no network)

When a fork PR touches workflow/action execution files, the pwn-request HOLD in
`approve_ci`/`ci_safety` cards it for manual review (unchanged). Wheelhouse then
attaches a deterministic, read-only security summary of ONLY those changed files
so the owner makes the SAME manual call faster and better-informed. The summary
is presentation/context only.

These tests prove the captain's contract:
  * the HOLD stays effective and the summary CANNOT trigger any action - the
    summarizer only issues READ (`gh api` GET) calls and never invokes
    `approve_ci` or any write/method flag;
  * risky patterns are SURFACED - `pull_request_target`, PR-head checkout (the
    pwn-request combo), write token permissions, `secrets: inherit`, referenced
    secret NAMES, and third-party actions not pinned to a commit SHA;
  * secret VALUES are NEVER exposed - the summary reports only structured facts
    (names/refs), never verbatim file lines, so a token-shaped literal in a
    workflow does not leak;
  * it fails CLOSED - unreadable/incomplete file lists and unreadable/unparseable
    files degrade to a "review manually" note and NEVER raise, so the card holds;
  * a PR that changes no workflow/action file yields an empty summary (no
    section), and benign first-party / SHA-pinned workflows raise no flags.
"""

import os
import sys

import yaml  # noqa: F401  (ensures PyYAML present; core needs it to parse)

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)
import render_card as rc  # noqa: E402
import wheelhouse_core as core  # noqa: E402

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def summary_for(files, texts, changed_files=None):
    """Drive ci_security_summary with the two network reads stubbed.

    `files` = [{filename, status}, ...] (the PR files list; assumed complete
    unless `changed_files` says otherwise). `texts` maps path -> head content
    (or None to simulate an unreadable file)."""
    save_list = core._list_pr_file_changes
    save_fetch = core._fetch_file_text
    complete = changed_files is None or len(files) >= changed_files
    core._list_pr_file_changes = lambda slug, pr, cf=None: (list(files), True, complete)
    core._fetch_file_text = lambda slug, path, ref=None: texts.get(path)
    try:
        return core.ci_security_summary(
            "kunchenguid/firstmate", "1", "headsha", changed_files
        )
    finally:
        core._list_pr_file_changes = save_list
        core._fetch_file_text = save_fetch


WF = ".github/workflows/danger.yml"

EXPLOIT_WF = """
name: danger
on:
  pull_request_target:
    types: [opened]
permissions:
  contents: write
  id-token: write
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - uses: evilorg/some-action@main
      - uses: pinned/act@1234567890123456789012345678901234567890
      - run: ./contributor-script.sh
        env:
          TOKEN: ${{ secrets.NPM_TOKEN }}
"""

SAFE_WF = """
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
permissions:
  contents: read
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: shellcheck bin/*.sh
"""


# --------------------------------------------------------------------------- #
# Risky patterns are surfaced.
# --------------------------------------------------------------------------- #
def test_exploit_workflow_surfaces_all_risky_patterns():
    s = summary_for([{"filename": WF, "status": "added"}], {WF: EXPLOIT_WF})
    check("exploit: flags pull_request_target + PR-head checkout (pwn-request)",
          "pull_request_target" in s and "pwn-request" in s and "PR head" in s)
    check("exploit: flags a write token permission", "write token permission" in s)
    check("exploit: surfaces the referenced secret NAME",
          "`NPM_TOKEN`" in s)
    check("exploit: flags the unpinned third-party action",
          "evilorg/some-action@main" in s and "not pinned to a commit SHA" in s)
    check("exploit: shows the changed file and its status",
          ("`%s`" % WF) in s and "(added)" in s)


def test_pinned_third_party_action_not_flagged():
    s = summary_for([{"filename": WF, "status": "added"}], {WF: EXPLOIT_WF})
    # The SHA-pinned action appears as a fact but is NOT in the flag list.
    check("pinning: SHA-pinned action is marked SHA-pinned",
          "pinned/act@1234567890123456789012345678901234567890" in s
          and "SHA-pinned" in s)
    check("pinning: SHA-pinned action not called out as a flag",
          "pinned/act@1234567890123456789012345678901234567890 is not pinned" not in s)


def test_secrets_inherit_flagged():
    wf = "name: x\non:\n  pull_request:\njobs:\n  a:\n    uses: ./.github/workflows/b.yml\n    secrets: inherit\n"
    s = summary_for([{"filename": WF, "status": "modified"}], {WF: wf})
    check("secrets-inherit: flagged", "secrets: inherit" in s)


def test_composite_action_file_is_analyzed():
    action = (
        "name: comp\nruns:\n  using: composite\n  steps:\n"
        "    - uses: thirdparty/tool@v1\n"
        "      shell: bash\n"
        "    - run: ./do-thing.sh\n"
        "      shell: bash\n"
    )
    path = "my-action/action.yml"
    s = summary_for([{"filename": path, "status": "added"}], {path: action})
    check("composite: action.yml is treated as a risky changed file",
          ("`%s`" % path) in s)
    check("composite: unpinned third-party action inside it is flagged",
          "thirdparty/tool@v1" in s and "not pinned to a commit SHA" in s)
    check("composite: run steps are surfaced",
          "Run steps: 1" in s)


def test_non_manifest_action_file_requires_manual_diff_review():
    path = ".github/actions/example/index.js"
    s = summary_for([{"filename": path, "status": "modified"}], {path: "{}"})
    check("action code: non-manifest file fails closed",
          s == core.CI_SUMMARY_UNANALYZABLE)


def test_non_mapping_manifest_requires_manual_diff_review():
    path = ".github/actions/example/action.yml"
    s = summary_for([{"filename": path, "status": "modified"}], {path: "plain text"})
    check("action manifest: scalar YAML fails closed",
          s == core.CI_SUMMARY_UNANALYZABLE)


def test_reusable_workflow_and_inherited_secrets_are_surfaced():
    wf = (
        "name: call\non:\n  pull_request:\njobs:\n  deploy:\n"
        "    uses: evil/repo/.github/workflows/deploy.yml@main\n"
        "    secrets: inherit\n"
    )
    s = summary_for([{"filename": WF, "status": "modified"}], {WF: wf})
    check("reusable workflow: unpinned called workflow is flagged",
          "third-party reusable workflow" in s
          and "evil/repo/.github/workflows/deploy.yml@main" in s)
    check("reusable workflow: parsed inherited secrets are flagged",
          "secrets: inherit" in s)


def test_bracket_secret_reference_is_surfaced():
    wf = (
        "name: target\non:\n  pull_request_target:\njobs:\n  deploy:\n"
        "    steps:\n      - run: echo ok\n"
        "        env:\n          TOKEN: ${{ secrets['DEPLOY_TOKEN'] }}\n"
    )
    s = summary_for([{"filename": WF, "status": "modified"}], {WF: wf})
    check("secret reference: bracket syntax surfaces the secret name",
          "`DEPLOY_TOKEN`" in s)


# --------------------------------------------------------------------------- #
# Secret VALUES are never exposed.
# --------------------------------------------------------------------------- #
def test_secret_values_are_never_echoed():
    # A workflow that (badly) hardcodes a token-shaped literal in a run step.
    # The summary must report structured facts only, never that literal.
    leaky = (
        "name: leak\non:\n  pull_request:\npermissions:\n  contents: read\n"
        "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: echo 'ghp_THISLOOKSLIKEASECRETVALUE0123456789abcd'\n"
        "      - run: curl -H \"authorization: bearer sk-live-DEADBEEFdeadbeef\" x\n"
    )
    s = summary_for([{"filename": WF, "status": "modified"}], {WF: leaky})
    check("no-leak: token-shaped literal is not echoed",
          "ghp_THISLOOKSLIKEASECRETVALUE0123456789abcd" not in s)
    check("no-leak: bearer-token-shaped literal is not echoed",
          "sk-live-DEADBEEFdeadbeef" not in s)
    check("no-leak: verbatim run-step text is not dumped",
          "echo '" not in s and "curl -H" not in s)


def test_value_sanitizer_neutralizes_markdown_breakout():
    # A contributor-controlled action name containing backticks/markdown must be
    # neutralized so it cannot break out of the inline-code span on the card.
    v = core._safe_inline("evil`](http://x)`\nname")
    check("sanitize: backticks neutralized", "`" not in v)
    check("sanitize: newlines collapsed", "\n" not in v)


def test_permission_job_name_is_sanitized_before_markdown_formatting():
    job = "bad`\n### injected"
    specs = core._permission_specs(
        {"jobs": {job: {"permissions": {"contents": "write"}}}}
    )
    check("sanitize: permission job label has no newlines", "\n" not in specs[0][0])
    summary = core._format_ci_security_summary(
        [{
            "path": WF,
            "status": "modified",
            "triggers": [],
            "pr_target": False,
            "checks_head": False,
            "permissions": specs,
            "perms_write": True,
            "secrets": [],
            "secrets_inherit": False,
            "checkouts": [],
            "actions": [],
            "run_steps": 0,
        }],
        True,
    )
    check("sanitize: permission job cannot inject a Markdown heading",
          "\n### injected" not in summary)


# --------------------------------------------------------------------------- #
# Benign inputs.
# --------------------------------------------------------------------------- #
def test_safe_first_party_workflow_has_no_flags():
    s = summary_for([{"filename": ".github/workflows/ci.yml", "status": "modified"}],
                    {".github/workflows/ci.yml": SAFE_WF})
    check("safe: no flags detected", "none detected by the automated scan" in s)
    check("safe: no pull_request_target flag", "pwn-request" not in s)
    check("safe: minimal permissions shown", "contents: read" in s)


def test_no_workflow_change_returns_empty():
    s = summary_for(
        [{"filename": "src/app.py", "status": "modified"},
         {"filename": "README.md", "status": "modified"}],
        {},
    )
    check("empty: no risky file -> empty summary (no section)", s == "")


# --------------------------------------------------------------------------- #
# Fail closed.
# --------------------------------------------------------------------------- #
def test_file_list_read_failure_fails_closed():
    save = core._list_pr_file_changes
    core._list_pr_file_changes = lambda slug, pr, cf=None: ([], False, False)
    try:
        s = core.ci_security_summary("o/r", "1", "sha", 1)
    finally:
        core._list_pr_file_changes = save
    check("fail-closed: unreadable file list -> unanalyzable note",
          s == core.CI_SUMMARY_UNANALYZABLE)


def test_incomplete_file_list_without_risky_fails_closed():
    # No risky file seen, but the list was incomplete -> cannot claim "nothing".
    s = summary_for(
        [{"filename": "src/app.py", "status": "modified"}], {}, changed_files=5
    )
    check("fail-closed: incomplete list + no risky seen -> unanalyzable note",
          s == core.CI_SUMMARY_UNANALYZABLE)


def test_incomplete_file_list_with_risky_file_fails_closed():
    s = summary_for(
        [{"filename": WF, "status": "modified"}], {WF: SAFE_WF}, changed_files=2
    )
    check("fail-closed: incomplete list + risky file -> unanalyzable note",
          s == core.CI_SUMMARY_UNANALYZABLE)


def test_unreadable_risky_file_notes_manual_review():
    s = summary_for([{"filename": WF, "status": "modified"}], {WF: None})
    check("fail-closed: unreadable risky file -> unanalyzable note",
          s == core.CI_SUMMARY_UNANALYZABLE)


def test_unparseable_risky_file_notes_manual_review():
    s = summary_for([{"filename": WF, "status": "modified"}], {WF: "a: [unterminated"})
    check("fail-closed: unparseable risky file -> unanalyzable note",
          s == core.CI_SUMMARY_UNANALYZABLE)


def test_summary_output_is_bounded_with_manual_diff_notice():
    actions = "\n".join(
        "      - uses: org/action%d@main" % n for n in range(200)
    )
    files = [
        {"filename": ".github/workflows/%03d.yml" % n, "status": "modified"}
        for n in range(core.CI_SUMMARY_MAX_FILES + 4)
    ]
    texts = {
        f["filename"]: "name: x\non:\n  pull_request:\njobs:\n  a:\n    steps:\n" + actions
        for f in files
    }
    s = summary_for(files, texts)
    check("bound: rendered summary has a strict character cap",
          len(s) <= core.CI_SUMMARY_MAX_CHARS)
    check("bound: omitted facts require manual diff review",
          "review the full diff manually" in s)


def test_never_raises_even_when_reads_throw():
    save = core._list_pr_file_changes
    core._list_pr_file_changes = lambda slug, pr, cf=None: (_ for _ in ()).throw(
        RuntimeError("boom")
    )
    try:
        s = core.ci_security_summary("o/r", "1", "sha", 1)
    finally:
        core._list_pr_file_changes = save
    check("robust: an internal error -> unanalyzable note, never raises",
          s == core.CI_SUMMARY_UNANALYZABLE)


# --------------------------------------------------------------------------- #
# The summary is READ-ONLY and cannot act.
# --------------------------------------------------------------------------- #
def test_summary_is_read_only_and_never_approves():
    """Drive the REAL ci_security_summary with only `subprocess.run` stubbed, and
    assert every GitHub call is a read (`gh api <path>`, no write method) and
    that `approve_ci` is never invoked from the summary path."""
    import base64

    recorded = []

    def fake_run(argv, capture_output=True, text=True):
        recorded.append(list(argv))

        class R:
            returncode = 0

        r = R()
        if "--paginate" in argv:  # the PR files list
            r.stdout = (
                '{"filename":"%s","status":"added"}\n' % WF
            )
        else:  # a contents read
            payload = '{"encoding":"base64","content":"%s"}' % (
                base64.b64encode(EXPLOIT_WF.encode()).decode()
            )
            r.stdout = payload
        r.stderr = ""
        return r

    approve_calls = []

    save_run = core.subprocess.run
    save_approve = core.approve_ci
    core.subprocess.run = fake_run
    core.approve_ci = lambda *a, **k: approve_calls.append((a, k)) or ("noop", "")
    try:
        s = core.ci_security_summary("kunchenguid/firstmate", "1", "headsha", 1)
    finally:
        core.subprocess.run = save_run
        core.approve_ci = save_approve

    check("read-only: produced a real summary through the read path",
          "pwn-request" in s)
    check("read-only: at least one gh api read was issued", len(recorded) >= 1)
    all_reads = all(
        argv[:2] == ["gh", "api"]
        and not any(
            tok in argv
            for tok in ("-X", "--method", "-f", "-F", "--field", "--input")
        )
        and not any(str(t).upper() in ("POST", "PUT", "PATCH", "DELETE") for t in argv)
        for argv in recorded
    )
    check("read-only: every gh call is a read (no write method/field)", all_reads)
    check("read-only: approve_ci is never called by the summarizer",
          approve_calls == [])


# --------------------------------------------------------------------------- #
# Render side: the advisory section is scoped, framed, and non-material.
# --------------------------------------------------------------------------- #
def _ci_item(**over):
    item = {
        "repo": "firstmate",
        "number": 345,
        "kind": "ci-approval",
        "head_sha": "abc123",
        "author": "contributor",
        "bucket": "needs-ci-approval",
        "comp": "n/a",
        "tests": "n/a",
        "url": "https://x",
        "title": "demo",
        "recommendation": "Review",
        "priority": "med",
    }
    item.update(over)
    return item


def test_render_ci_approval_card_shows_advisory_section():
    body = rc.render(_ci_item(security_summary="**Flags:** none\n- `ci.yml` (modified)"))[
        "body"
    ]
    check("render: advisory security section heading present",
          "### Security review (advisory)" in body)
    check("render: framed as advisory/untrusted context",
          "advisory, untrusted context" in body)
    check("render: states it does NOT approve CI",
          "does **not** approve CI" in body)
    check("render: the findings body is included", "- `ci.yml` (modified)" in body)
    check("render: decision checkboxes (the hold UI) are intact",
          "opt:approve-ci" in body and "opt:hold" in body)
    state = rc.parse_state_block(body)
    check("render: security_summary is NOT in the state block (non-material)",
          "security_summary" not in state)
    check("render: card carries the current render_version",
          state.get("render_version") == rc.CARD_RENDER_VERSION)


def test_security_summary_cache_reuses_current_card_body():
    summary = "**Flags:** none detected by the automated scan - still review the diff."
    item = _ci_item(
        security_summary=summary,
        ci_security_summary_head_sha="abc123",
        ci_security_summary_version=core.CI_SECURITY_SUMMARY_VERSION,
        ci_security_summary_present=True,
    )
    body = rc.render(item)["body"]
    cache = core.ci_security_summary_cache([{"body": body}])
    check("cache: current summary is available by target",
          cache == {("firstmate", 345): {"head_sha": "abc123", "summary": summary}})
    state = rc.parse_state_block(body)
    check("cache: summary cache markers are non-material",
          rc.material_changed(item, state) is False)
    check("cache: matching marker does not trigger a refresh",
          rc.security_summary_stale(item, state) is False)
    item["ci_security_summary_version"] += 1
    check("cache: a newer summary version triggers one refresh",
          rc.security_summary_stale(item, state) is True)


def test_security_summary_cache_records_analyzed_empty_result():
    item = _ci_item(
        ci_security_summary_head_sha="abc123",
        ci_security_summary_version=core.CI_SECURITY_SUMMARY_VERSION,
        ci_security_summary_present=False,
    )
    cache = core.ci_security_summary_cache([{"body": rc.render(item)["body"]}])
    check("cache: empty summary result is cached",
          cache == {("firstmate", 345): {"head_sha": "abc123", "summary": ""}})


def test_render_scopes_section_to_ci_approval_only():
    body = rc.render(_ci_item(kind="pr-review", security_summary="X"))["body"]
    check("render: pr-review card does not show the security section",
          "### Security review" not in body)


def test_render_ci_approval_without_summary_has_no_section():
    body = rc.render(_ci_item())["body"]
    check("render: ci-approval card without a summary shows no section",
          "### Security review" not in body)


def test_security_summary_does_not_trigger_a_refresh():
    # Two otherwise-identical items differing ONLY in security_summary must NOT
    # look materially changed, so the advisory text never churns a card.
    state = rc.parse_state_block(rc.render(_ci_item(security_summary="one"))["body"])
    check("non-material: differing summary is not a material change",
          rc.material_changed(_ci_item(security_summary="two"), state) is False)


def main():
    test_exploit_workflow_surfaces_all_risky_patterns()
    test_pinned_third_party_action_not_flagged()
    test_secrets_inherit_flagged()
    test_composite_action_file_is_analyzed()
    test_non_manifest_action_file_requires_manual_diff_review()
    test_non_mapping_manifest_requires_manual_diff_review()
    test_reusable_workflow_and_inherited_secrets_are_surfaced()
    test_bracket_secret_reference_is_surfaced()
    test_secret_values_are_never_echoed()
    test_value_sanitizer_neutralizes_markdown_breakout()
    test_permission_job_name_is_sanitized_before_markdown_formatting()
    test_safe_first_party_workflow_has_no_flags()
    test_no_workflow_change_returns_empty()
    test_file_list_read_failure_fails_closed()
    test_incomplete_file_list_without_risky_fails_closed()
    test_incomplete_file_list_with_risky_file_fails_closed()
    test_unreadable_risky_file_notes_manual_review()
    test_unparseable_risky_file_notes_manual_review()
    test_summary_output_is_bounded_with_manual_diff_notice()
    test_never_raises_even_when_reads_throw()
    test_summary_is_read_only_and_never_approves()
    test_render_ci_approval_card_shows_advisory_section()
    test_security_summary_cache_reuses_current_card_body()
    test_security_summary_cache_records_analyzed_empty_result()
    test_render_scopes_section_to_ci_approval_only()
    test_render_ci_approval_without_summary_has_no_section()
    test_security_summary_does_not_trigger_a_refresh()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all ci-security-summary tests passed")


if __name__ == "__main__":
    main()
