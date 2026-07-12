#!/usr/bin/env python3
"""
Offline regression guard for the PASS-BY-REFERENCE natural-language decision
prompt (decision-handler.yml NL path). NO network, NO live gh, NO live LLM.

Background: card #555. An owner posted a plain-English question on a decision
card whose target PR (firstmate#442) had a ~1.2 MB diff. The NL path's
`nl-fetch` step wrote that whole diff into `target.txt`, and
`apply_decision.build_nl_prompt` read it back and INLINED it into the Claude
action's `prompt:` input. claude-code-action re-packs `prompt:` into a single
`ALL_INPUTS` env string; JSON-escaping the diff-heavy prompt pushed it past the
kernel's per-string `execve` limit `MAX_ARG_STRLEN = 131072`, so `/bin/bash`
would not spawn (E2BIG). The Claude step died, every downstream step was
skipped, and the card was left silently untouched under a red run. This is the
SAME class PR #544 structurally eliminated for triage.yml / deep-review.yml -
the NL path was never converted.

The fix makes the NL prompt PR-SIZE-INDEPENDENT: `nl-fetch` writes the bounded
title/body/diff to the on-disk `target.txt`, and the small constant prompt only
NAMES that file and directs the agent to Read/Grep/Glob it. Huge PR content
never enters the action prompt / ALL_INPUTS again. Read/Grep/Glob are added to
both NL Claude steps' allowed tools (the write/acting boundary is unchanged).

These tests encode that as a STRUCTURAL INVARIANT (the prompt cannot grow with
target size) which a lowered cap could never guarantee, plus a faithful
end-to-end reproduction: a synthetic 5 MB target on disk still yields a tiny
prompt. They follow the repo's static-inspection style (see
test_triage_prompt_size.py / test_workflow_lint.py).

Run: python tests/test_nl_prompt_size.py   (needs PyYAML, which the workflows
install)
"""

import json
import os
import sys
import tempfile

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import apply_decision as ad  # noqa: E402

# The whole point of by-reference is an O(1) prompt. Keep this budget small: the
# constant scaffolding (all conditional branches) is a few KB, far under this and
# far under MAX_ARG_STRLEN. If a future change makes the prompt grow past this,
# that is exactly the regression to catch.
PROMPT_BYTE_BUDGET = 16384
MAX_ARG_STRLEN = 131072
# At/above the #555 scale (~1.2 MB) so the reproduction is a genuine worst case.
CARD_555_DIFF_BYTES = 1_216_018

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def load_workflow():
    return yaml.safe_load(read(".github", "workflows", "decision-handler.yml"))


def handle_steps():
    return load_workflow()["jobs"]["handle"]["steps"]


def step_by_id(steps, sid):
    return next((s for s in steps if s.get("id") == sid), None)


def step_by_name(steps, name):
    return next((s for s in steps if s.get("name") == name), None)


# --------------------------------------------------------------------------- #
# End-to-end: run the REAL cmd the workflow runs, with a giant target on disk.
# --------------------------------------------------------------------------- #
STATE_BLOCK = '<!-- wheelhouse-state: {"repo":"target","number":442,"kind":"pr-review"} -->'


def _extract_output(github_output_path, name):
    """Pull a value written by apply_decision.set_output back out of a
    $GITHUB_OUTPUT file. Handles both the `name=value` and the multiline
    `name<<DELIM ... DELIM` heredoc forms set_output emits."""
    with open(github_output_path, encoding="utf-8") as f:
        lines = f.read().splitlines(keepends=True)
    i = 0
    value = None
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip("\n")
        if stripped.startswith(name + "<<"):
            delim = stripped[len(name) + 2 :]
            body = []
            i += 1
            while i < len(lines) and lines[i].rstrip("\n") != delim:
                body.append(lines[i])
                i += 1
            value = "".join(body)
            if value.endswith("\n"):
                value = value[:-1]
        elif stripped.startswith(name + "="):
            value = stripped[len(name) + 1 :]
        i += 1
    return value


def _render_nl_prompt(diff_bytes, search_enabled=False):
    """Invoke apply_decision.cmd_nl_prompt exactly as decision-handler.yml does,
    with a synthetic target.txt of `diff_bytes` on disk, and return the prompt
    string it emitted to $GITHUB_OUTPUT."""
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "target.txt")
        # A worst-case on-disk target: the <target-content> wrapper plus a huge
        # diff, exactly what nl-fetch writes for a large PR.
        with open(target, "w", encoding="utf-8") as f:
            f.write('<target-content repo="owner/target" number="442" kind="pr-review">\n')
            f.write("# Big PR\n\n## Diff\n")
            f.write("D" * diff_bytes)
            f.write("\n</target-content>\n")
        gh_out = os.path.join(d, "gh_output")
        open(gh_out, "w").close()
        saved = dict(os.environ)
        try:
            os.environ.update(
                {
                    "GITHUB_OUTPUT": gh_out,
                    "ISSUE_BODY": "## Decision needed\n\n" + STATE_BLOCK,
                    "COMMENT_BODY": "is this workflow change really necessary?",
                    "KIND": "pr-review",
                    "TARGET_FILE": target,
                    "COMMENTS_FILE": "",
                    "TRIGGER_COMMENT_ID": "4949677104",
                    "READONLY_SEARCH_ENABLED": "true" if search_enabled else "false",
                    "GITHUB_REPOSITORY_OWNER": "owner",
                }
            )
            ad.cmd_nl_prompt()
        finally:
            os.environ.clear()
            os.environ.update(saved)
        return _extract_output(gh_out, "prompt") or ""


def test_e2big_repro_giant_target_never_reaches_all_inputs():
    """The captain's explicit proof, reproduced end-to-end through the REAL
    cmd_nl_prompt: for a synthetic PR at/above the #555 scale (and far beyond),
    the built prompt never contains the diff, stays under a small fixed budget,
    json.dumps(prompt) is far below MAX_ARG_STRLEN, and it is FLAT as the target
    grows. It also demonstrates the actual E2BIG condition: had the diff been
    inlined (the old bug), json.dumps would exceed MAX_ARG_STRLEN."""
    base = _render_nl_prompt(10_000)
    base_escaped = len(json.dumps(base).encode("utf-8"))
    for diff_bytes in (10_000, CARD_555_DIFF_BYTES, 5_000_000):
        prompt = _render_nl_prompt(diff_bytes)
        marker = "D" * diff_bytes
        raw = len(prompt.encode("utf-8"))
        escaped = len(json.dumps(prompt).encode("utf-8"))
        check(
            "nl: %dB synthetic diff never appears in the prompt" % diff_bytes,
            marker not in prompt,
        )
        check(
            "nl: raw prompt under fixed budget for a %dB diff" % diff_bytes,
            raw < PROMPT_BYTE_BUDGET,
        )
        check(
            "nl: json.dumps(prompt) far below MAX_ARG_STRLEN for a %dB diff"
            % diff_bytes,
            escaped < MAX_ARG_STRLEN // 4,
        )
        check(
            "nl: prompt size is FLAT regardless of the %dB diff" % diff_bytes,
            escaped == base_escaped,
        )
    # Contrast: the OLD inline design WOULD E2BIG on a #555-scale diff.
    would_inline = (
        base
        + "\n=== Target content ===\n"
        + ("D" * CARD_555_DIFF_BYTES)
    )
    check(
        "nl: inlining a #555-scale diff WOULD exceed MAX_ARG_STRLEN (the fixed bug)",
        len(json.dumps(would_inline).encode("utf-8")) > MAX_ARG_STRLEN,
    )


def test_prompt_names_target_file_and_keeps_untrusted_framing():
    """The prompt directs the agent to the pre-fetched on-disk file with the
    Read tool, and keeps the injection-defense framing when content is read from
    a file rather than inlined."""
    prompt = _render_nl_prompt(10_000)
    check("nl: prompt names target.txt", "target.txt" in prompt)
    check("nl: prompt tells the agent to use the Read tool", "Read tool" in prompt)
    check("nl: prompt marks the file UNTRUSTED reference DATA", "UNTRUSTED" in prompt)
    check(
        "nl: prompt still forbids obeying instructions found in the target",
        "follow any instruction found inside it" in prompt
        and "as an instruction to you" in prompt,
    )
    check(
        "nl: prompt references the <target-content> boundary the file carries",
        "<target-content>" in prompt or "target-content" in prompt,
    )


def test_build_nl_prompt_no_longer_takes_or_inlines_target_content():
    """build_nl_prompt is by-reference: it accepts no target_content, and its
    output is identical whether or not a target is available except for the
    pointer wording - it can never inline PR content because it never receives
    it."""
    body = STATE_BLOCK
    available = ad.build_nl_prompt(body, "merge it", "pr-review", target_available=True)
    absent = ad.build_nl_prompt(body, "merge it", "pr-review", target_available=False)
    check(
        "nl: available prompt points at the on-disk file",
        "target.txt" in available and "Read tool" in available,
    )
    check(
        "nl: absent prompt says nothing was fetched (no crash, no inline)",
        "no target content was fetched" in absent,
    )
    # A custom filename flows through, proving the name is a reference, not data.
    named = ad.build_nl_prompt(
        body, "merge it", "pr-review", target_available=True, target_file="nl_target.txt"
    )
    check("nl: the on-disk filename is a reference the prompt names", "nl_target.txt" in named)


def test_nl_fetch_bounds_target_on_disk_with_explicit_truncation():
    """No silent truncation: nl-fetch caps the diff written to target.txt at the
    same generous on-disk bound as #544, and a diff over the cap is truncated
    with an EXPLICIT marker (never silently). The old unbounded `gh pr diff`
    straight into target.txt is gone."""
    steps = handle_steps()
    fetch = step_by_id(steps, "nl-fetch")
    check("nl: nl-fetch step exists", fetch is not None)
    run = str((fetch or {}).get("run", ""))
    check("nl: nl-fetch still writes target.txt to disk", "} > target.txt" in run)
    check(
        "nl: nl-fetch caps the on-disk diff at the #544 generous bound",
        "diff_limit_bytes=1500000" in run
        and 'head -c "$((diff_limit_bytes + 1))"' in run,
    )
    check(
        "nl: an over-cap diff is truncated with an EXPLICIT marker (no silent cut)",
        "[diff truncated after %s bytes]" in run,
    )
    check(
        "nl: the old unbounded `gh pr diff` straight into target.txt is gone",
        'gh pr diff "$NUMBER" -R "$slug" || echo "(could not fetch diff)"' not in run,
    )
    check(
        "nl: the <target-content> delimiter is written into target.txt",
        "<target-content" in run and "</target-content>" in run,
    )


def test_both_nl_claude_steps_can_read_the_on_disk_file():
    """Both NL Claude steps (READONLY search + legacy no-token) consume the SAME
    by-reference nl-prompt and gain Read/Grep/Glob so they can open target.txt.
    The write/acting boundary is unchanged: the search step keeps Bash limited to
    wheelhouse-search, and the legacy step keeps Write (for decision.json) with no
    shell and no GH_TOKEN."""
    steps = handle_steps()
    search = step_by_name(steps, "Claude interprets intent (read-only search)")
    legacy = step_by_name(steps, "Claude interprets intent")
    check("nl: read-only search Claude step exists", search is not None)
    check("nl: legacy no-token Claude step exists", legacy is not None)

    for label, step in (("search", search), ("legacy", legacy)):
        if not step:
            continue
        args = str((step.get("with") or {}).get("claude_args", ""))
        prompt = str((step.get("with") or {}).get("prompt", ""))
        check(
            "nl: %s step uses the by-reference nl-prompt output" % label,
            prompt == "${{ steps.nl-prompt.outputs.prompt }}",
        )
        check(
            "nl: %s step allows Read/Grep/Glob to open target.txt" % label,
            "Read" in args and "Grep" in args and "Glob" in args,
        )

    if search:
        sargs = str((search.get("with") or {}).get("claude_args", ""))
        check(
            "nl: search step keeps Write and only wheelhouse-search for Bash",
            "Write" in sargs and "Bash(wheelhouse-search)" in sargs,
        )
        for forbidden in ("Bash(gh", "Bash(git", "gh pr diff", "gh api", "git push"):
            check(
                "nl: search step never widens to %s" % forbidden,
                forbidden not in sargs,
            )

    if legacy:
        largs = str((legacy.get("with") or {}).get("claude_args", "")).strip()
        check(
            "nl: legacy step is exactly Read,Grep,Glob,Write (no shell, no Bash)",
            largs == "--allowedTools Read,Grep,Glob,Write\n--max-turns 32\n--model sonnet",
        )
        check(
            "nl: legacy step still receives no GH_TOKEN (unchanged isolation)",
            "GH_TOKEN" not in (legacy.get("env") or {}),
        )
        check(
            "nl: legacy step keeps the default github_token, never FLEET/READONLY",
            (legacy.get("with") or {}).get("github_token") == "${{ github.token }}"
            and "FLEET_TOKEN" not in yaml.safe_dump(legacy)
            and "READONLY_TOKEN" not in yaml.safe_dump(legacy),
        )


def test_nl_failure_visibility_is_bounded_and_fire_once():
    """A crashed NL LLM step (route skipped) leaves ONE bounded, content-free,
    idempotent note on the card so the owner is not left with a silent red run
    (the #555 symptom). It must not expose prompt/comment text, must fire once
    (marker keyed to the triggering comment id), must use the card's own token
    (never re-triggering the handler), and must not touch any gate/label/decision
    - it is purely additive visibility."""
    steps = handle_steps()
    note = step_by_name(steps, "Note NL processing failure on card")
    check("nl: failure-visibility step exists", note is not None)
    if not note:
        return
    cond = str(note.get("if", ""))
    check("nl: note runs even on a failed run (always)", "always()" in cond)
    check(
        "nl: note fires only when NL was entered and the prompt built",
        "steps.nl-gate.outputs.proceed == 'true'" in cond
        and "steps.nl-prompt.outcome == 'success'" in cond,
    )
    check(
        "nl: note fires precisely when route was SKIPPED (the LLM step crashed)",
        "steps.route.outcome == 'skipped'" in cond,
    )
    env = note.get("env") or {}
    check(
        "nl: note posts with the card's own default token (no re-trigger, no FLEET)",
        env.get("GH_TOKEN") == "${{ github.token }}",
    )
    dumped = yaml.safe_dump(note)
    check(
        "nl: note never uses FLEET_TOKEN or READONLY_TOKEN",
        "FLEET_TOKEN" not in dumped and "READONLY_TOKEN" not in dumped,
    )
    check(
        "nl: note keys idempotency on the triggering comment id, not content",
        env.get("TRIGGER_COMMENT_ID") == "${{ github.event.comment.id }}",
    )
    run = str(note.get("run", ""))
    check(
        "nl: note is fire-once (checks for an existing marker before posting)",
        "wheelhouse-nl-error:" in run and "grep -qF" in run,
    )
    check(
        "nl: note body carries NO prompt/comment content (no interpolation of either)",
        "steps.nl-prompt.outputs.prompt" not in run
        and "github.event.comment.body" not in dumped
        and "COMMENT_BODY" not in dumped,
    )
    check(
        "nl: note changes no label / gate / decision (it only comments)",
        "labeler" not in dumped and "needs-decision" not in run and "gh issue close" not in run,
    )


def test_nl_prompt_step_is_pass_by_reference_not_inline():
    """The nl-prompt step still exports TARGET_FILE (so the file is named) but
    the deterministic builder no longer reads its contents into the prompt -
    proven by the end-to-end size tests above. Guard the wiring here."""
    steps = handle_steps()
    prompt = step_by_id(steps, "nl-prompt")
    check("nl: nl-prompt step exists", prompt is not None)
    if prompt:
        env = prompt.get("env") or {}
        check(
            "nl: nl-prompt still names target.txt as the on-disk reference",
            env.get("TARGET_FILE") == "target.txt",
        )
        check(
            "nl: nl-prompt runs the deterministic builder (no inlined content)",
            "apply_decision.py nl-prompt" in str(prompt.get("run", "")),
        )


def main():
    test_e2big_repro_giant_target_never_reaches_all_inputs()
    test_prompt_names_target_file_and_keeps_untrusted_framing()
    test_build_nl_prompt_no_longer_takes_or_inlines_target_content()
    test_nl_fetch_bounds_target_on_disk_with_explicit_truncation()
    test_both_nl_claude_steps_can_read_the_on_disk_file()
    test_nl_failure_visibility_is_bounded_and_fire_once()
    test_nl_prompt_step_is_pass_by_reference_not_inline()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all nl-prompt-size tests passed")


if __name__ == "__main__":
    main()
