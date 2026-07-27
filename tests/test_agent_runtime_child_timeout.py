"""Regression: claude-action-compat child execution timeout covers job overhead.

Production card #1759 (run 30248637187): the reusable model job's GitHub
Actions timeout is measured from job start, but the job spends a measured
~30-40s before the model starts (handoff hydration, checkpoint, action setup,
Claude Code install, SDK init) plus post-model capture/upload time. With
``childExecutionTimeoutMs = ceil(hard / 60000) * 60000`` the triage.pr lane's
enforced model budget was only 360s - ~36s = ~324s, BELOW its designed 330s
hard budget. The job timeout killed the claude-code-action mid-execution at
6m16s, before it could commit its execution file, and the card landed on
``consumer.committed.primary.output.missing`` with no triage for its current
head. The near-miss control (run 30254186583, model finished at ~324s) proves
the boundary: its action committed the execution file seconds before the same
timeout fired and its card received an admitted assessment.

The fix mirrors the two-minute setup/upload allowance the
``claude-model-call`` composite action has always documented for the
claude-cli lane: the claude-action-compat lane's ``childExecutionTimeoutMs``
is now the hard budget rounded up to whole minutes plus
``CLAUDE_ACTION_JOB_OVERHEAD_MS``.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_runtime.config import resolve_selection
from agent_runtime.task_builder import (
    ACTION_LIMITS,
    CLAUDE_ACTION_JOB_OVERHEAD_MS,
    build_task,
)

FAILURES = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def claude_task(root: Path, action: str) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    prompt = root / "prompt.txt"
    target = root / "target.txt"
    prompt.write_text("Return the bounded result.\n", encoding="utf-8")
    target.write_text("fixture evidence anchor text for runtime tests\n", encoding="utf-8")
    bundle = root / "bundle"
    return build_task(
        action=action,
        selection=resolve_selection(action, "repo"),
        prompt_path=str(prompt),
        bundle_dir=str(bundle),
        output_path=str(bundle / "task.json"),
        owner="owner",
        repo="repo",
        number=7,
        target_kind="pr-review",
        revision="cf7f065de35b9e2931ec883ff650008b6ddfd39e",
        wheelhouse_revision="30271b6907e568419cdc48694a11b0c2f699b433",
        event_key="a" * 64,
        target_file=str(target),
    )


# The largest pre-model job overhead observed across production triage runs
# (job start -> "Claude Code initialized"): 38s. The allowance must leave the
# model its full designed hard budget even after that overhead.
MEASURED_MAX_SETUP_MS = 38_000

# claude-action-compat serves the eight non-repair actions; the two
# schema-repair actions run the claude-cli direct supervisor lane instead.
ACTION_COMPAT_ACTIONS = sorted(
    action for action in ACTION_LIMITS if not action.endswith(".schema-repair")
)
REPAIR_ACTIONS = sorted(
    action for action in ACTION_LIMITS if action.endswith(".schema-repair")
)


def main():
    check(
        "limits: eight non-repair actions ride the claude-action-compat lane",
        len(ACTION_COMPAT_ACTIONS) == 8 and len(REPAIR_ACTIONS) == 2,
    )
    with tempfile.TemporaryDirectory() as tmp:
        for action in ACTION_COMPAT_ACTIONS:
            task = claude_task(Path(tmp) / action, action)
            limits = task["spec"]["limits"]
            soft, hard, _, _, _ = ACTION_LIMITS[action]
            timeout = limits["childExecutionTimeoutMs"]
            expected = ((hard + 59_999) // 60_000) * 60_000 + CLAUDE_ACTION_JOB_OVERHEAD_MS
            check(
                "limits: %s timeout is hard budget plus the job-overhead allowance" % action,
                timeout == expected and CLAUDE_ACTION_JOB_OVERHEAD_MS == 120_000,
            )
            check(
                "limits: %s timeout stays a whole number of minutes" % action,
                timeout % 60_000 == 0,
            )
            check(
                "limits: %s enforced model budget still covers the designed hard "
                "budget after the worst measured pre-model setup" % action,
                timeout - MEASURED_MAX_SETUP_MS >= hard,
            )
            check(
                "limits: %s stays inside the AgentTask schema bound" % action,
                60_000 <= timeout <= 3_600_000,
            )
            check(
                "limits: %s keeps soft/hard deadlines with the action-owned lane" % action,
                limits["softDeadlineMs"] is None and limits["hardDeadlineMs"] is None,
            )
        for action in REPAIR_ACTIONS:
            task = claude_task(Path(tmp) / action, action)
            limits = task["spec"]["limits"]
            soft, hard, _, _, _ = ACTION_LIMITS[action]
            check(
                "limits: %s keeps its supervisor-owned deadlines and no "
                "action-lane timeout (the composite action's +2 policy applies)" % action,
                limits["childExecutionTimeoutMs"] is None
                and limits["softDeadlineMs"] == soft
                and limits["hardDeadlineMs"] == hard,
            )

    # The card #1759 counterfactual pinned as arithmetic: under the old
    # formula the enforced model budget (360s) minus the worst measured setup
    # (38s) fell 8s short of the designed 330s hard budget, which is exactly
    # the band where run 30248637187 (killed at ~337s of model execution) and
    # run 30248300898 lost their results while near-miss run 30254186583
    # (~324s) still committed.
    old_formula = ((330_000 + 59_999) // 60_000) * 60_000
    check(
        "limits: the old formula left the model budget below the designed hard budget",
        old_formula - MEASURED_MAX_SETUP_MS < 330_000,
    )
    task = claude_task(Path(tempfile.mkdtemp()) / "p", "triage.pr.search")
    check(
        "limits: card #1759's lane now enforces 480s (8 minutes)",
        task["spec"]["limits"]["childExecutionTimeoutMs"] == 480_000,
    )

    # The single-owner boundary: the task value is the sole timeout source, so
    # the composite action must pass it through verbatim for
    # claude-action-compat and keep its separate documented +2 policy only for
    # the supervisor-owned claude-cli lane, and the model job must keep its
    # checkpoint equality assertion binding job timeout to the task value.
    composite = Path(".github/actions/claude-model-call/action.yml").read_text(encoding="utf-8")
    check(
        "dispatch: composite passes the task timeout through verbatim for the action lane",
        'if [ "$adapter" = "claude-action-compat" ]; then' in composite
        and "timeout_minutes=$((timeout_ms / 60000))" in composite,
    )
    check(
        "dispatch: composite keeps its own +2 minute policy only for the supervisor lane",
        'elif [ "$adapter" = "claude-cli" ]; then' in composite
        and "timeout_minutes=$(((timeout_ms + 59999) / 60000 + 2))" in composite,
    )
    model_workflow = Path(".github/workflows/claude-model.yml").read_text(encoding="utf-8")
    check(
        "dispatch: model job timeout remains bound to the task value",
        "timeout-minutes: ${{ inputs.child_timeout_minutes }}" in model_workflow
        and 'raise SystemExit("child job timeout does not match AgentTask")' in model_workflow,
    )

    if FAILURES:
        raise SystemExit("%d child-timeout checks failed" % len(FAILURES))
    print("\nall child-timeout checks passed")


if __name__ == "__main__":
    main()
