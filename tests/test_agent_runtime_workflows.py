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
    check("production: every direct step requires resolved Claude selection", all("mode == 'claude'" in str(step.get("if", "")) or "runtime_mode == 'claude'" in str(step.get("if", "")) for _, step in claude))
    check("production: direct Claude is never a failure fallback", all("failure()" not in str(step.get("if", "")) and "agent-runtime.outcome" not in str(step.get("if", "")) for _, step in claude))

    text = "\n".join(path.read_text() for path in WORKFLOWS.values())
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
    check("runtime: every invocation has trusted immutable task construction", len(build_steps) == 4)
    check("runtime: every Codex step is codex-only", all("codex" in str(step.get("if", "")) for step in runtime_runs + build_steps))
    check("runtime: no configured action can reach a Codex workflow path", all(row["target"] == "claude" for row in yaml.safe_load(Path("wheelhouse.config.yml").read_text())["agent_runtime"]["actions"].values()))
    check("runtime: all use pinned app-server package", text.count("@openai/codex@0.144.0") >= 3)
    package_steps = [step for _, step in all_steps if "agent_runtime.py verify-package" in str(step.get("run", ""))]
    check("runtime: every Codex setup verifies wrapper and platform tarball bytes", len(package_steps) == 4 and all("--tarball" in step["run"] and "--platform-tarball" in step["run"] and step["run"].count("npm pack --silent") == 2 for step in package_steps))
    check("runtime: installation does not trust separately fetched registry metadata", all("npm view" not in step["run"] for step in package_steps))
    check("runtime: every Codex setup installs only verified local artifacts", all("npm install --offline" in step["run"] and '"$tarball"' in step["run"] and 'tar -xzf "$platform_tarball"' in step["run"] for step in package_steps))
    check("runtime: verification precedes install and executable extraction", all(step["run"].index("agent_runtime.py verify-package") < step["run"].index("npm install --offline") < step["run"].index('tar -xzf "$platform_tarball"') < step["run"].index("--version | grep") for step in package_steps))
    check("runtime: all verify vendored protocol pins", text.count("agent_runtime.py verify-pins") >= 3)
    check("runtime: external bubblewrap sandbox installed", text.count("bubblewrap") >= 3)

    check("search: wrapper follows resolved Claude production branches", all("claude" in str(step.get("if", "")) for _, step in all_steps if step.get("id") in ("search-tool", "nl-search-tool")))
    check("search: Codex receives no model GH_TOKEN", all("GH_TOKEN" not in (step.get("env") or {}) for step in runtime_runs))
    check("search: trusted supervisor alone receives optional broker token", any("READONLY_TOKEN" in (step.get("env") or {}) for step in runtime_runs))
    check("credentials: no Codex credential secret invented", all(name not in text for name in ("secrets.CODEX_ACCESS_TOKEN", "secrets.CODEX_AUTH", "secrets.OPENAI_API_KEY", "secrets.CODEX_API_KEY")))
    check("credentials: no auth.json blob workflow", "auth.json" not in text)
    check("credentials: current Claude subscription secret remains production gate", "CLAUDE_CODE_OAUTH_TOKEN" in text)

    triage = docs["triage"]
    triage_steps = {step.get("id"): step for step in steps(triage) if step.get("id")}
    check("triage consumer: normalized result preferred", "steps.agent-runtime.outputs.result" in str((triage_steps["triage-result"].get("env") or {}).get("EXECUTION_FILE")))
    check("repair consumer: normalized repair result preferred", "RUNTIME_RESULT" in (triage_steps["repair-result"].get("env") or {}))
    deep_post = next(step for step in steps(docs["deep"]) if step.get("name") == "Post the verdict on the card")
    check("deep consumer: normalized result preferred", "steps.agent-runtime.outputs.result" in str((deep_post.get("env") or {}).get("EXECUTION_FILE")))
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
    check("selection: no activation or temporary rollback settings remain", "production_activation" not in config and "temporary_rollback_profile" not in config)
    check("selection: Codex remains disabled non-target evidence", config["disabled_adapters"] == {"codex-app-server": "unsupported-public-chatgpt-pro-auth"} and all(row["profile"] != "codex-subscription-pinned" for row in config["actions"].values()))

    policy_text = "\n".join(Path(path).read_text(encoding="utf-8") for path in ("README.md", "AGENTS.md", "docs/AGENT_RUNTIME.md"))
    check("docs: Claude is production primary without temporary rollback language", "Claude is the production primary" in policy_text and "selected future primary" not in policy_text and "temporary Claude" not in policy_text)
    check("docs: Codex is disabled non-target evidence", "disabled non-target adapter evidence" in policy_text and "Codex is not an active target or expected future primary" in policy_text)
    check("docs: GLM OpenCode remains investigation-only", "GLM through Z.AI OpenCode" in policy_text and "unapproved investigation" in policy_text)

    if FAILURES:
        raise SystemExit("%d agent runtime workflow checks failed" % len(FAILURES))
    print("\nall agent runtime workflow tests passed")


if __name__ == "__main__":
    main()
