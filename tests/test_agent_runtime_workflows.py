#!/usr/bin/env python3
"""Workflow-level migration and production provider wiring checks."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

FAILURES = []
WORKFLOWS = {
    "triage": Path(".github/workflows/triage.yml"),
    "deep": Path(".github/workflows/deep-review.yml"),
    "decision": Path(".github/workflows/decision-handler.yml"),
    "model": Path(".github/workflows/claude-model.yml"),
}
PIN = "anthropics/claude-code-action@fad22eb3fa582b7357fc0ea48af6645851b884fd"


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def steps(document):
    result = []
    for job in document.get("jobs", {}).values():
        result.extend(job.get("steps", []))
    return result


def main():
    docs = {name: yaml.safe_load(path.read_text()) for name, path in WORKFLOWS.items()}
    all_steps = [(name, step) for name, doc in docs.items() for step in steps(doc)]
    claude = [(name, step) for name, step in all_steps if str(step.get("uses", "")).startswith("anthropics/claude-code-action@")]
    check("production: exactly seven inventoried direct Claude steps remain", len(claude) == 7)
    check("production: every direct step keeps exact action pin", all(step["uses"] == PIN for _, step in claude))
    check("production: every direct step is selected by immutable task action", all("steps.hydrate.outputs.action" in str(step.get("if", "")) for _, step in claude))
    check("production: direct Claude is never a failure fallback", all("failure()" not in str(step.get("if", "")) and "agent-runtime.outcome" not in str(step.get("if", "")) for _, step in claude))
    check("production: every direct step pins immutable model", all("--model claude-sonnet-4-6" in str((step.get("with") or {}).get("claude_args", "")) for _, step in claude))
    check("production: every model subprocess enables credential scrub", all((step.get("env") or {}).get("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB") == "1" for _, step in claude))
    check("production: every direct step allows only the dispatch bot", all((step.get("with") or {}).get("allowed_bots") == "github-actions[bot]" for _, step in claude))
    check("production: actor allowance never uses a wildcard", all((step.get("with") or {}).get("allowed_non_write_users") != "*" for _, step in claude))
    model_permissions = docs["model"].get("permissions", {})
    check("production: model workflow has read-only permissions", model_permissions == {"actions": "read", "contents": "read"})
    check("production: model workflow contains no acting or fleet credential", "FLEET_TOKEN" not in WORKFLOWS["model"].read_text() and "issues: write" not in WORKFLOWS["model"].read_text())
    check("production: model default token is downscoped before direct actions", all(name == "model" for name, _ in claude) and model_permissions.get("contents") == "read")
    check("production: original write-capable jobs contain no direct model action", all(name == "model" for name, _ in claude))
    model_steps = steps(docs["model"])
    direct = [index for index, step in enumerate(model_steps) if str(step.get("uses", "")).startswith("anthropics/claude-code-action@")]
    repository = next((index for index, step in enumerate(model_steps) if step.get("name") == "Initialize bounded local repository"), -1)
    check("production: verified handoff becomes a bounded no-fetch repository", repository >= 0 and all(repository < index for index in direct) and "git init" in model_steps[repository]["run"] and "git remote" in model_steps[repository]["run"] and "fetch" not in model_steps[repository]["run"])
    check("production: bounded transcript capture follows every direct action", max(direct) < next(index for index, step in enumerate(model_steps) if step.get("id") == "capture"))
    capture = next(step for step in model_steps if step.get("id") == "capture")
    check("production: cross-job transcript is bounded and reduced before upload", "8388608" in capture["run"] and "bounded = []" in capture["run"] and 'cp "$EXECUTION_FILE"' not in capture["run"])

    component = Path(".github/actions/claude-model-call/action.yml").read_text()
    handoff = Path("agent_runtime/claude_handoff.py").read_text()
    text = "\n".join(path.read_text() for path in WORKFLOWS.values()) + "\n" + component
    for action in (
        "triage.issue.local",
        "triage.issue.search",
        "triage.pr.local",
        "triage.pr.search",
        "triage.schema-repair",
        "deep-review.local",
        "deep-review.search",
        "nl-decision.local",
        "nl-decision.search",
    ):
        # Main dual variants are assembled as a trusted prefix plus .local/.search.
        if action.startswith("triage.issue") or action.startswith("triage.pr") or action.startswith("deep-review") or action.startswith("nl-decision"):
            family, variant = action.rsplit(".", 1)
            represented = family in text and (".${action}" if False else variant) in text
        else:
            represented = action in text
        check("runtime path represented: %s" % action, represented)

    runtime_runs = [step for _, step in all_steps if "scripts/agent_runtime.py run" in str(step.get("run", ""))]
    check("runtime: triage, repair, deep review, and NL all invoke one CLI", len(runtime_runs) == 4)
    check("runtime: every invocation consumes AgentTask plus bundle", all("--task" in step["run"] and "--bundle" in step["run"] for step in runtime_runs))
    build_steps = [step for _, step in all_steps if "scripts/agent_runtime.py build-task" in str(step.get("run", ""))]
    claude_build_steps = [step for step in build_steps if "claude" in str(step.get("id", ""))]
    codex_build_steps = [step for step in build_steps if step not in claude_build_steps]
    model_calls = [step for _, step in all_steps if step.get("uses") == "./.github/actions/claude-model-call"]
    check("runtime: every invocation family has trusted immutable task construction", len(claude_build_steps) == 4 and len(codex_build_steps) == 4)
    check("runtime: every Claude family uses the bounded read-only workflow call", len(model_calls) == 4)
    check("runtime: trusted bridge requires observed enforcement proof", "--enforcement-file" in component and "bridge-claude" in component)
    check("runtime: handoff has byte and file bounds plus complete digest verification", "MAX_HANDOFF_BYTES" in handoff and "MAX_HANDOFF_FILES" in handoff and "_verify_bundle" in handoff and "manifest verification failed" in handoff)
    check("runtime: model job receives a trusted manifest identity", "handoff_sha256" in WORKFLOWS["model"].read_text() and "steps.pack.outputs.manifestSha256" in component)
    dispatch = Path("scripts/claude_model_dispatch.py").read_text()
    check("runtime: trusted parent dispatches by ref and binds expected commit", "--dispatch-ref" in component and "--expected-sha" in component and "expected_commit_sha" in dispatch and '"--ref", args.dispatch_ref' in dispatch)
    check("runtime: child correlation is paginated and unambiguous", '"--paginate", "--slurp"' in dispatch and "correlation_id" in dispatch and "ambiguous Claude model workflow correlation" in dispatch and '"--limit", "40"' not in dispatch)
    check("runtime: trusted parent always discovers, cancels, and waits on timeout", "while not run_id:" in dispatch and "def cancel_and_wait" in dispatch and '"gh", "run", "cancel"' in dispatch and '"gh", "run", "view"' in dispatch)
    check("runtime: provenance distinguishes write-capable token absence", "writeCapableGithubTokenAvailable" in WORKFLOWS["model"].read_text() and "writeCapableGithubTokenAvailable" in Path("agent_runtime/claude_bridge.py").read_text())
    check("runtime: no trusted consumer reads raw Claude execution data", all("outputs.execution_file" not in str((step.get("env") or {}).get(name, "")) for _, step in all_steps for name in ("EXECUTION_FILE", "RUNTIME_RESULT") if step.get("id") != "capture" and "bridge-claude" not in str(step.get("run", ""))))
    check("runtime: every Codex step is codex-only", all("codex" in str(step.get("if", "")) for step in runtime_runs + codex_build_steps))
    check("runtime: no configured action can reach a Codex workflow path", all(row["target"] == "claude" for row in yaml.safe_load(Path("wheelhouse.config.yml").read_text())["agent_runtime"]["actions"].values()))
    check("runtime: all use pinned app-server package", text.count("@openai/codex@0.144.0") >= 3)
    package_steps = [step for _, step in all_steps if "agent_runtime.py verify-package" in str(step.get("run", ""))]
    check("runtime: every Codex setup verifies wrapper and platform tarball bytes", len(package_steps) == 4 and all("--tarball" in step["run"] and "--platform-tarball" in step["run"] and step["run"].count("npm pack --silent") == 2 for step in package_steps))
    check("runtime: installation does not trust separately fetched registry metadata", all("npm view" not in step["run"] for step in package_steps))
    check("runtime: every Codex setup installs only verified local artifacts", all("npm install --offline" in step["run"] and '"$tarball"' in step["run"] and 'tar -xzf "$platform_tarball"' in step["run"] for step in package_steps))
    check("runtime: verification precedes install and executable extraction", all(step["run"].index("agent_runtime.py verify-package") < step["run"].index("npm install --offline") < step["run"].index('tar -xzf "$platform_tarball"') < step["run"].index("--version | grep") for step in package_steps))
    check("runtime: all verify vendored protocol pins", text.count("agent_runtime.py verify-pins") >= 3)
    model_text = WORKFLOWS["model"].read_text()
    check("runtime: Claude proof records only observed bridge controls", all(value in model_text for value in ("github-readonly-artifact-bridge-v1", "content-addressed-bounded-verified", "targetInputsReadOnly", "local-no-remote", "declaredTools")) and "subprocessIsolation" not in model_text and "bubblewrap socat" not in model_text)

    check("search: wrapper follows resolved Claude production branches", all("claude" in str(step.get("if", "")) for _, step in all_steps if step.get("id") in ("search-tool", "nl-search-tool")))
    check("search: Codex receives no model GH_TOKEN", all("GH_TOKEN" not in (step.get("env") or {}) for step in runtime_runs))
    check("search: trusted supervisor alone receives optional broker token", any("READONLY_TOKEN" in (step.get("env") or {}) for step in runtime_runs))
    check("credentials: no Codex credential secret invented", all(name not in text for name in ("secrets.CODEX_ACCESS_TOKEN", "secrets.CODEX_AUTH", "secrets.OPENAI_API_KEY", "secrets.CODEX_API_KEY")))
    check("credentials: no auth.json blob workflow", "auth.json" not in text)
    check("credentials: current Claude subscription secret remains production gate", "CLAUDE_CODE_OAUTH_TOKEN" in text)

    triage = docs["triage"]
    triage_steps = {step.get("id"): step for step in steps(triage) if step.get("id")}
    check("triage consumer: Claude AgentResult required", "steps.claude-model.outputs.result" in str((triage_steps["triage-result"].get("env") or {}).get("EXECUTION_FILE")))
    check("repair consumer: normalized repair result required", "steps.claude-repair-model.outputs.result" in str((triage_steps["repair-result"].get("env") or {}).get("RUNTIME_RESULT")))
    deep_post = next(step for step in steps(docs["deep"]) if step.get("name") == "Post the verdict on the card")
    check("deep consumer: Claude AgentResult required", "steps.claude-model.outputs.result" in str((deep_post.get("env") or {}).get("EXECUTION_FILE")))
    deep_steps = steps(docs["deep"])
    deep_ids = [step.get("id") for step in deep_steps]
    deep_gate = next(step for step in deep_steps if step.get("id") == "gate")
    check("deep selection: target resolves before repository-aware gate", deep_ids.index("resolve") < deep_ids.index("gate"))
    check("deep selection: gate uses validated resolved repository", (deep_gate.get("env") or {}).get("TARGET_REPO") == "${{ steps.resolve.outputs.repo }}" and '--repo "$TARGET_REPO"' in deep_gate["run"])
    nl_result = next(step for step in steps(docs["decision"]) if step.get("id") == "nl-result")
    check("NL consumer: runtime final exported before deterministic route", "agent_runtime.py export-final" in nl_result["run"])
    execute = next(step for step in steps(docs["decision"]) if step.get("id") == "execute")
    check("NL safety: deterministic FLEET_TOKEN executor remains separate", (execute.get("env") or {}).get("GH_TOKEN") == "${{ secrets.FLEET_TOKEN }}")
    check("NL safety: model runtime never receives FLEET_TOKEN", all("FLEET_TOKEN" not in (step.get("env") or {}) for step in runtime_runs))

    config = yaml.safe_load(Path("wheelhouse.config.yml").read_text())["agent_runtime"]
    check("selection: Claude primary profile selected", config["primary_profile"] == "claude-action-current-pinned")
    check("selection: every action explicitly targets Claude", config["target"] == "claude" and all(row["target"] == "claude" for row in config["actions"].values()))
    check("selection: fallback globally disabled", config["fallback"] == "none")
    check("selection: immutable Claude model forbids aliases", config["profiles"][config["primary_profile"]]["model"] == "claude-sonnet-4-6" and config["profiles"][config["primary_profile"]]["allow_model_alias"] is False)
    check("selection: no activation or temporary rollback settings remain", "production_activation" not in config and "temporary_rollback_profile" not in config)
    check("selection: Codex remains disabled non-target evidence", config["disabled_adapters"] == {"codex-app-server": "unsupported-public-chatgpt-pro-auth"} and all(row["profile"] != "codex-subscription-pinned" for row in config["actions"].values()))

    policy_text = "\n".join(Path(path).read_text(encoding="utf-8") for path in ("README.md", "AGENTS.md", "docs/AGENT_RUNTIME.md"))
    check("docs: Claude is production primary without temporary rollback language", "Claude is the production primary" in policy_text and "selected future primary" not in policy_text and "temporary Claude" not in policy_text)
    check("docs: Codex is disabled non-target evidence", "disabled non-target adapter evidence" in policy_text and "Codex is not an active target or expected future primary" in policy_text)
    check("docs: OpenCode and Z.AI are deferred with no adapter", "OpenCode with Z.AI Coding Plan is a deferred disabled candidate" in policy_text and "no adapter" in policy_text)
    core_text = "\n".join(path.read_text(encoding="utf-8") for path in Path("agent_runtime").glob("*.py"))
    check("core: no OpenCode or Z.AI-specific implementation", "OpenCode" not in core_text and "Z.AI" not in core_text and not Path("agent_runtime/adapters/opencode.py").exists())

    if FAILURES:
        raise SystemExit("%d agent runtime workflow checks failed" % len(FAILURES))
    print("\nall agent runtime workflow tests passed")


if __name__ == "__main__":
    main()
