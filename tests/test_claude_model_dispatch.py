#!/usr/bin/env python3
"""Caller-commit-bound Claude reusable workflow checks."""

from __future__ import annotations

from pathlib import Path

import yaml

FAILURES: list[str] = []
ROOT = Path(__file__).resolve().parents[1]


def check(name: str, condition: bool) -> None:
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def load(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def triggers(document: dict) -> dict:
    return document.get("on") or document.get(True) or {}


def main() -> None:
    model = load(".github/workflows/claude-model.yml")
    model_text = (ROOT / ".github/workflows/claude-model.yml").read_text(encoding="utf-8")
    prepare_text = (ROOT / ".github/actions/claude-model-call/action.yml").read_text(encoding="utf-8")
    result_text = (ROOT / ".github/actions/claude-model-result/action.yml").read_text(encoding="utf-8")
    callers = {
        name: load(path)
        for name, path in {
            "triage": ".github/workflows/triage.yml",
            "deep": ".github/workflows/deep-review.yml",
            "decision": ".github/workflows/decision-handler.yml",
        }.items()
    }
    call_jobs = [
        (name, job)
        for name, document in callers.items()
        for job in document["jobs"].values()
        if job.get("uses") == "./.github/workflows/claude-model.yml"
    ]

    on = triggers(model)
    check("dispatch: model workflow is reusable only", set(on) == {"workflow_call"})
    call = on.get("workflow_call") or {}
    check("dispatch: workflow call exposes bounded scalar outputs", set((call.get("outputs") or {})) == {"result_artifact", "status", "error_code", "observed_commit_sha"})
    check("dispatch: model workflow keeps exact read-only permissions", model.get("permissions") == {"actions": "read", "contents": "read"})
    check("dispatch: all four Claude families use the local reusable workflow", len(call_jobs) == 4 and {name for name, _ in call_jobs} == {"triage", "deep", "decision"})
    check("dispatch: every local call binds expected source to github.sha", all((job.get("with") or {}).get("expected_commit_sha") == "${{ github.sha }}" for _, job in call_jobs))
    check("dispatch: every local call passes only model and read-only broker secrets", all(set(job.get("secrets") or {}) == {"CLAUDE_CODE_OAUTH_TOKEN", "READONLY_TOKEN"} for _, job in call_jobs))
    check("dispatch: no caller inherits FLEET_TOKEN into the model boundary", all("FLEET_TOKEN" not in str(job.get("secrets") or {}) and job.get("secrets") != "inherit" for _, job in call_jobs))
    check("dispatch: mutable branch dispatch is absent", "github.ref_name" not in model_text + prepare_text and "gh workflow run" not in model_text + prepare_text and "workflow_dispatch" not in on)
    check("dispatch: obsolete branch-polling controller is removed", not (ROOT / "scripts/claude_model_dispatch.py").exists())
    check("dispatch: source gate still compares observed and expected SHA", 'if [ "$GITHUB_SHA" = "$EXPECTED_COMMIT_SHA" ]' in model_text and "steps.source.outputs.match == 'true'" in model_text)
    check("dispatch: provider admission remains downstream of verified hydration", all("steps.hydrate.outputs.action" in str(step.get("if", "")) for step in model["jobs"]["model"]["steps"] if str(step.get("uses", "")).startswith("anthropics/claude-code-action@")))
    check("dispatch: complete handoff verification remains in model and finalizer jobs", model_text.count("handoff manifest mismatch") >= 2 and model_text.count("handoff symlink rejected") >= 2)
    check("dispatch: job timeout and durable checkpoint retain cancellation recovery", "timeout-minutes: ${{ inputs.child_timeout_minutes }}" in model_text and "Upload durable pre-invocation checkpoint" in model_text and "Download durable pre-invocation checkpoint" in model_text)
    check("dispatch: normalized result binding is verified at both artifact boundaries", "verify_result_binding" in model_text and "verify_result_event_binding" in model_text and "agent_runtime.py verify-result" in result_text)
    receiver_calls = [
        step
        for document in callers.values()
        for job in document["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses") == "./.github/actions/claude-model-result"
    ]
    check(
        "dispatch: each normalized result receiver has a disjoint static workspace",
        len(receiver_calls) == 5
        and len(
            {
                (step.get("with") or {}).get("invocation-id")
                for step in receiver_calls
            }
        )
        == 5,
    )
    check("dispatch: preparer uploads hidden signed paths", "include-hidden-files: true" in prepare_text and "agent_runtime.claude_handoff pack" in prepare_text)

    if FAILURES:
        raise SystemExit("%d caller-bound Claude workflow checks failed" % len(FAILURES))
    print("\nall caller-bound Claude workflow checks passed")


if __name__ == "__main__":
    main()
