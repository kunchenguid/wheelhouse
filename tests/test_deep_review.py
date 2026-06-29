#!/usr/bin/env python3
"""
Offline checks for the always-on, code-grounded deep-review feature and the
non-consuming Investigate checkbox. NO network, NO live LLM.

Run: python tests/test_deep_review.py   (needs PyYAML, which the workflows install)

The live Claude path can only be exercised end-to-end in CI with the token set,
so these tests pin the *wiring* instead:

  * render: pr-review and issue-triage cards render an `investigate` checkbox
    (with its `<!-- opt:investigate -->` marker); ci-approval does NOT;
  * always-on: the `deep_review` enable flag is gone everywhere - config,
    `wheelhouse_core.load_config`, and the `deep-review-enabled` CLI command -
    leaving only the irreducible CLAUDE_CODE_OAUTH_TOKEN gate;
  * token-absent: deep-review.yml posts the one-line "needs token" note rather
    than silently no-opping;
  * code-grounded + security: deep-review.yml checks out the target with
    FLEET_TOKEN and `persist-credentials: false`, runs Claude restricted to
    read-only exploration tools (Read/Grep/Glob/Write), and the Claude step never
    receives FLEET_TOKEN; the verdict is posted with the default token;
  * investigate trigger: decision-handler.yml has `actions: write` and an
    Investigate step that clears the box and dispatches deep-review.yml via
    workflow_dispatch on the default token.
"""
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import render_card as rc  # noqa: E402
import wheelhouse_core as core  # noqa: E402

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def load_yaml(*parts):
    return yaml.safe_load(read(*parts))


def steps_of(workflow_doc, job):
    return workflow_doc["jobs"][job]["steps"]


# --------------------------------------------------------------------------- #
# render: the Investigate checkbox is offered on the right kinds
# --------------------------------------------------------------------------- #
def test_investigate_rendered_per_kind():
    check("render: pr-review offers investigate",
          "investigate" in rc.CHECKBOX_OPTIONS["pr-review"])
    check("render: issue-triage offers investigate",
          "investigate" in rc.CHECKBOX_OPTIONS["issue-triage"])
    check("render: ci-approval does NOT offer investigate",
          "investigate" not in rc.CHECKBOX_OPTIONS["ci-approval"])
    check("render: investigate has a human label", bool(rc.OPTION_LABELS.get("investigate")))

    pr = rc.render({"repo": "r", "number": 7, "kind": "pr-review", "title": "t"})
    check("render: pr card carries the opt:investigate marker",
          "<!-- opt:investigate -->" in pr["body"])
    check("render: pr card renders investigate as an unticked box",
          "- [ ] " in pr["body"] and "<!-- opt:investigate -->" in pr["body"])

    ci = rc.render({"repo": "r", "number": 8, "kind": "ci-approval", "title": "t"})
    check("render: ci-approval card has NO investigate marker",
          "<!-- opt:investigate -->" not in ci["body"])


# --------------------------------------------------------------------------- #
# always-on: the deep_review enable flag is gone everywhere
# --------------------------------------------------------------------------- #
def test_enable_flag_removed():
    cfg_text = read("wheelhouse.config.yml")
    check("config: no `deep_review:` key remains",
          "deep_review:" not in cfg_text)
    check("config: load_config no longer carries deep_review",
          "deep_review" not in core.load_config())
    core_text = read("scripts", "wheelhouse_core.py")
    check("core: deep-review-enabled command removed",
          "deep-review-enabled" not in core_text and "deep_review" not in core_text)
    dr = read(".github", "workflows", "deep-review.yml")
    check("workflow: gate no longer consults the deep_review flag",
          "deep-review-enabled" not in dr and "deep_review" not in dr)
    check("workflow: gate still requires the model credential",
          "CLAUDE_CODE_OAUTH_TOKEN" in dr)


# --------------------------------------------------------------------------- #
# token-absent: a self-explaining note, not a silent no-op
# --------------------------------------------------------------------------- #
def test_token_absent_message():
    dr = read(".github", "workflows", "deep-review.yml")
    check("workflow: posts the one-line needs-token note",
          "Deep-review needs CLAUDE_CODE_OAUTH_TOKEN configured to run." in dr)
    doc = load_yaml(".github", "workflows", "deep-review.yml")
    names = [s.get("name", "") for s in steps_of(doc, "deep-review")]
    check("workflow: an explicit 'Explain missing token' step exists",
          any("missing token" in n.lower() for n in names))


# --------------------------------------------------------------------------- #
# code-grounded + security model
# --------------------------------------------------------------------------- #
def test_code_grounded_checkout_and_tool_isolation():
    doc = load_yaml(".github", "workflows", "deep-review.yml")
    steps = steps_of(doc, "deep-review")

    checkout = next((s for s in steps
                     if "actions/checkout" in str(s.get("uses", ""))
                     and isinstance(s.get("with"), dict)
                     and "repository" in s["with"]), None)
    check("workflow: a target-repo checkout step exists", checkout is not None)
    if checkout:
        w = checkout["with"]
        check("security: target checkout uses FLEET_TOKEN",
              "FLEET_TOKEN" in str(w.get("token", "")))
        check("security: target checkout does NOT persist credentials to disk",
              w.get("persist-credentials") is False)

    claude = next((s for s in steps if "claude-code-action" in str(s.get("uses", ""))), None)
    check("workflow: a Claude step exists", claude is not None)
    if claude:
        dumped = yaml.safe_dump(claude)
        check("security: the Claude step NEVER receives FLEET_TOKEN",
              "FLEET_TOKEN" not in dumped)
        args = str((claude.get("with") or {}).get("claude_args", ""))
        check("security: Claude is restricted to read-only exploration + Write",
              "--allowedTools" in args
              and all(t in args for t in ("Read", "Grep", "Glob", "Write")))
        check("security: Claude is NOT granted Bash / shell execution",
              "Bash" not in args)

    # The verdict is posted by the workflow (default token), not by Claude.
    dr = read(".github", "workflows", "deep-review.yml")
    check("workflow: verdict posted from verdict.md with the default token",
          "verdict.md" in dr and "github.token" in dr)


# --------------------------------------------------------------------------- #
# investigate trigger wiring in the decision handler
# --------------------------------------------------------------------------- #
def test_handler_investigate_wiring():
    dh_text = read(".github", "workflows", "decision-handler.yml")
    doc = load_yaml(".github", "workflows", "decision-handler.yml")
    perms = doc["jobs"]["handle"].get("permissions") or doc.get("permissions") or {}
    # permissions can be at job or workflow level; this workflow sets it at top.
    top_perms = doc.get("permissions") or {}
    check("handler: has actions: write to dispatch the investigation",
          top_perms.get("actions") == "write" or perms.get("actions") == "write")

    steps = steps_of(doc, "handle")
    inv = next((s for s in steps if "investigate" in str(s.get("name", "")).lower()), None)
    check("handler: an Investigate step exists", inv is not None)
    if inv:
        run = str(inv.get("run", ""))
        check("handler: investigate clears the checkbox (re-triggerable)",
              "clear-checkbox" in run)
        check("handler: investigate dispatches deep-review.yml via workflow_dispatch",
              "workflow run deep-review.yml" in run)
        check("handler: investigate runs on the default token (no FLEET_TOKEN)",
              "github.token" in str(inv.get("env", {}).get("GH_TOKEN", ""))
              and "FLEET_TOKEN" not in yaml.safe_dump(inv))
    check("handler: investigate step is owner-gated",
          inv is not None and "authorized == 'true'" in str(inv.get("if", "")))
    # The consuming execute path must NOT fire for an investigate-only event.
    check("handler: parse routes investigate to the `investigate` output",
          "steps.decide.outputs.investigate" in dh_text)


def main():
    test_investigate_rendered_per_kind()
    test_enable_flag_removed()
    test_token_absent_message()
    test_code_grounded_checkout_and_tool_isolation()
    test_handler_investigate_wiring()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all deep-review tests passed")


if __name__ == "__main__":
    main()
