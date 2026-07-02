#!/usr/bin/env python3
"""
Offline regression guard: no `.github/workflows/*.yml` step may combine
`gh api --slurp` with `--jq` on the same command. NO network, NO live gh.

`gh api` rejects `--slurp` together with `--jq`/`--template` (mutually
exclusive in the installed gh CLI: "the --slurp option is not supported with
--jq or --template"). This slipped past the rest of the offline suite because
none of it runs the live `gh api` call - only a live scan-backstop run failed
at the "List open cards" step. This test catches the class of mistake by
scanning every workflow's `run:` steps as text for a `gh api ...` invocation
that carries both `--slurp` and `--jq`. The fix is to keep `--paginate
--slurp` (for full pagination) but pipe the result into a standalone `jq`
instead of passing `--jq` to `gh api` itself.

Run: python tests/test_workflow_lint.py   (needs PyYAML, which the workflows
install)
"""

import glob
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def run_steps(workflow_path):
    with open(workflow_path) as f:
        doc = yaml.safe_load(f)
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            run = step.get("run")
            if isinstance(run, str):
                yield run


def gh_api_combines_slurp_and_jq(run_text):
    # Coarse, dependency-light text scan (not a shell parser): a `run:` step
    # that invokes `gh api` and carries both `--slurp` and `--jq` anywhere in
    # it is exactly the broken pattern, whether the flags land on the same
    # physical line or are split across a `\`-continued shell command.
    return (
        "gh api" in run_text and "--slurp" in run_text and "--jq" in run_text
    )


def test_no_workflow_combines_slurp_and_jq():
    workflow_files = sorted(
        glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))
    )
    check("found workflow files to scan", len(workflow_files) > 0)
    for path in workflow_files:
        rel = os.path.relpath(path, ROOT)
        offending = [
            run for run in run_steps(path) if gh_api_combines_slurp_and_jq(run)
        ]
        check(
            "%s: no `gh api` step combines --slurp with --jq" % rel,
            not offending,
        )


def main():
    test_no_workflow_combines_slurp_and_jq()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all workflow-lint tests passed")


if __name__ == "__main__":
    main()
