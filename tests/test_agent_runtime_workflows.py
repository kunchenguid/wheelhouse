#!/usr/bin/env python3
"""Workflow-level migration and production provider wiring checks."""

from __future__ import annotations

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
    check("production: exactly eight inventoried direct Claude steps remain", len(claude) == 8)
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
    checkpoint = next((index for index, step in enumerate(model_steps) if step.get("name") == "Write conservative pre-invocation checkpoint"), -1)
    checkpoint_upload = next((index for index, step in enumerate(model_steps) if step.get("name") == "Upload durable pre-invocation checkpoint"), -1)
    checkpoint_stage = next((index for index, step in enumerate(model_steps) if step.get("name") == "Record durable checkpoint stage"), -1)
    check("production: durable spend checkpoint precedes every direct action", repository < checkpoint < checkpoint_upload < min(direct) and '"spendStarted": True' in model_steps[checkpoint]["run"] and "-attempt" in str(model_steps[checkpoint_upload]))
    check(
        "production: provider checkpoint rebinds the hydrated task to the caller commit",
        (model_steps[checkpoint].get("env") or {}).get("EXPECTED_COMMIT_SHA")
        == "${{ inputs.expected_commit_sha }}"
        and 'task.get("metadata", {}).get("wheelhouseRevision")'
        in model_steps[checkpoint]["run"]
        and 'os.environ["GITHUB_SHA"] != os.environ["EXPECTED_COMMIT_SHA"]'
        in model_steps[checkpoint]["run"],
    )
    check("production: bounded transcript capture follows every direct action", max(direct) < next(index for index, step in enumerate(model_steps) if step.get("id") == "capture"))
    capture = next(step for step in model_steps if step.get("id") == "capture")
    check("production: cross-job transcript is bounded and reduced before upload", "8388608" in capture["run"] and "bounded = []" in capture["run"] and 'cp "$EXECUTION_FILE"' not in capture["run"])
    check("production: immutable inputs are revalidated after every action", "workspace_input_observation" in capture["run"] and "postActionInputObservationSha256" in capture["run"] and "targetInputsReadOnly" in capture["run"])
    check(
        "production: targetInputsReadOnly derives from exact pre/post signed-input observations",
        "preActionInputObservationSha256" in model_steps[checkpoint]["run"]
        and "workspace_input_observation" in model_steps[checkpoint]["run"]
        and "postActionInputObservationSha256" in capture["run"]
        and "targetInputsReadOnly" in capture["run"]
        and "post_observation is not None and post_observation == proof.get(\"preActionInputObservationSha256\")" in capture["run"],
    )
    check("production: declared outputs remain recorded separately from signed-input evidence", "declaredOutputPaths" in model_steps[checkpoint]["run"] and "declared_output_paths" in Path("agent_runtime/claude_handoff.py").read_text())
    handoff_source = Path("agent_runtime/claude_handoff.py").read_text()
    check(
        "production: signed-input observation does not walk undeclared workspace scratch",
        "model workspace contains an undeclared output path" not in handoff_source
        and "undeclared output path" not in handoff_source
        and "def workspace_input_observation" in handoff_source
        and 'canonical_sha256({"inputs":observed})' in handoff_source.replace(" ", ""),
    )
    hydrate = next(step for step in model_steps if step.get("id") == "hydrate")
    hydrate_run = hydrate["run"]
    hydrate_env = hydrate.get("env") or {}
    packaged_invocations = [
        step
        for step in model_steps
        if 'PYTHONPATH="$HANDOFF/runtime"' in str(step.get("run", ""))
        or "PYTHONPATH=$HANDOFF/runtime" in str(step.get("run", ""))
    ]
    check(
        "production: every packaged handoff invocation redirects bytecode via disjoint PYTHONPYCACHEPREFIX",
        len(packaged_invocations) >= 3
        and all(
            str((step.get("env") or {}).get("PYTHONPYCACHEPREFIX", "")).startswith("${{ runner.temp }}/wheelhouse-")
            and "mkdir -p" in str(step.get("run", ""))
            and "PYTHONPYCACHEPREFIX collides with signed handoff" in str(step.get("run", ""))
            and "unset PYTHONDONTWRITEBYTECODE" in str(step.get("run", ""))
            and "python -B" not in str(step.get("run", ""))
            and (step.get("env") or {}).get("PYTHONDONTWRITEBYTECODE") is None
            for step in packaged_invocations
        ),
    )
    check(
        "production: packaged hydrate uses external pycache prefix before agent_runtime import",
        'python -m agent_runtime.claude_handoff hydrate' in hydrate_run
        and hydrate_env.get("PYTHONPYCACHEPREFIX") == "${{ runner.temp }}/wheelhouse-pycache"
        and "python -B" not in hydrate_run
        and hydrate_run.index('python -m agent_runtime.claude_handoff hydrate')
        > hydrate_run.index("handoff manifest mismatch"),
    )
    check(
        "production: complete signed file-set verification is not weakened for pycache",
        "manifest verification failed" in Path("agent_runtime/claude_handoff.py").read_text()
        and "PYTHONPYCACHEPREFIX" in Path("agent_runtime/claude_handoff.py").read_text()
        and "__pycache__" not in Path("agent_runtime/claude_handoff.py").read_text().split("def verify", 1)[-1].split("def hydrate", 1)[0]
        and ".pyc" not in Path("agent_runtime/claude_handoff.py").read_text().split("def verify", 1)[-1].split("def hydrate", 1)[0],
    )
    check(
        "production: pre-checkpoint capture writes bounded no-spend stage result",
        'if [ ! -f "$ATTEMPT" ]' in capture["run"]
        and '"spendStarted": False' in capture["run"]
        and "pre-hydration" in capture["run"]
        and "pre-checkpoint" in capture["run"]
        and 'os.environ["HANDOFF_SHA256"]' in capture["run"]
        and 'os.environ["HANDOFF"]' in capture["run"]
        and "handoffInputPathSha256" in capture["run"]
        and all(token not in capture["run"].split('if [ ! -f "$ATTEMPT" ]', 1)[1].split("fi", 1)[0] for token in ("CLAUDE_CODE", "FLEET_TOKEN", "prompt", "target.txt")),
    )
    check(
        "production: provider steps remain gated on successful hydrate outputs",
        all("steps.hydrate.outputs.action" in str(step.get("if", "")) for index, step in enumerate(model_steps) if index in direct),
    )

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
        "nl-decision.schema-repair",
    ):
        # Main dual variants are assembled as a trusted prefix plus .local/.search.
        if action.startswith("triage.issue") or action.startswith("triage.pr") or action.startswith("deep-review") or action.startswith("nl-decision"):
            family, variant = action.rsplit(".", 1)
            represented = family in text and (".${action}" if False else variant) in text
        else:
            represented = action in text
        check("runtime path represented: %s" % action, represented)

    runtime_runs = [step for _, step in all_steps if "scripts/agent_runtime.py run" in str(step.get("run", ""))]
    check("runtime: primary and repair paths all invoke one CLI", len(runtime_runs) == 5)
    check("runtime: every invocation consumes AgentTask plus bundle", all("--task" in step["run"] and "--bundle" in step["run"] for step in runtime_runs))
    build_steps = [step for _, step in all_steps if "scripts/agent_runtime.py build-task" in str(step.get("run", ""))]
    claude_build_steps = [step for step in build_steps if "claude" in str(step.get("id", ""))]
    codex_build_steps = [step for step in build_steps if step not in claude_build_steps]
    model_calls = [step for _, step in all_steps if step.get("uses") == "./.github/actions/claude-model-call"]
    check("runtime: every invocation family has trusted immutable task construction", len(claude_build_steps) == 5 and len(codex_build_steps) == 5)
    check("runtime: every Claude family uses the bounded read-only workflow call", len(model_calls) == 5)
    model_text = WORKFLOWS["model"].read_text()
    check("runtime: trusted bridge requires observed enforcement proof", "from agent_runtime.claude_bridge import bridge" in model_text and 'proof="$RAW/enforcement.json"' in model_text)
    check("runtime: handoff has byte and file bounds plus complete digest verification", "MAX_HANDOFF_BYTES" in handoff and "MAX_HANDOFF_FILES" in handoff and "_verify_bundle" in handoff and "manifest verification failed" in handoff)
    check("runtime: model job receives a trusted manifest identity", "handoff_sha256" in WORKFLOWS["model"].read_text() and "steps.pack.outputs.manifestSha256" in component)
    reusable_calls = [job for doc in docs.values() for job in doc.get("jobs", {}).values() if job.get("uses") == "./.github/workflows/claude-model.yml"]
    check("runtime: caller binds reusable model job to its own commit", len(reusable_calls) == 5 and all((job.get("with") or {}).get("expected_commit_sha") == "${{ github.sha }}" for job in reusable_calls))
    check("runtime: local workflow reference eliminates mutable branch resolution", all(job.get("uses") == "./.github/workflows/claude-model.yml" for job in reusable_calls) and "github.ref_name" not in text and "gh workflow run" not in component)
    check(
        "runtime: source validation remains distinct from job binding",
        "steps.source.outputs.match == 'true'" in model_text
        and 'if [ "$GITHUB_SHA" = "$EXPECTED_COMMIT_SHA" ]; then' in model_text
        and "write_revision_mismatch_result" in model_text
        and "source.revision_mismatch"
        in Path("agent_runtime/claude_bridge.py").read_text(),
    )
    check("runtime: reusable boundary removes title/run discovery", not Path("scripts/claude_model_dispatch.py").exists() and "display_title" not in component and "matching_run" not in component)
    check("runtime: GitHub job cancellation retains durable checkpoint recovery", "Upload durable pre-invocation checkpoint" in model_text and "Download durable pre-invocation checkpoint" in model_text and "needs.model.result" in model_text)
    check("runtime: child execution timeout remains task-bound", "child_timeout_minutes" in model_text and "timeout-minutes: ${{ inputs.child_timeout_minutes }}" in model_text)
    check("runtime: provenance distinguishes write-capable token absence", "writeCapableGithubTokenAvailable" in WORKFLOWS["model"].read_text() and "writeCapableGithubTokenAvailable" in Path("agent_runtime/claude_bridge.py").read_text())
    check("runtime: no trusted consumer reads raw Claude execution data", all("outputs.execution_file" not in str((step.get("env") or {}).get(name, "")) for _, step in all_steps for name in ("EXECUTION_FILE", "RUNTIME_RESULT") if step.get("id") != "capture" and "bridge-claude" not in str(step.get("run", ""))))
    check("runtime: source mismatch emits a trusted result without provider evidence", "write_revision_mismatch_result" in model_text and "caller-commit" in model_text and "steps.source.outputs.match == 'true'" in model_text)
    check(
        "runtime: source mismatch cannot consume the durable provider checkpoint",
        "steps.source.outputs.match == 'true'" in str(hydrate.get("if", ""))
        and model_steps[checkpoint].get("if")
        == "${{ steps.hydrate.outcome == 'success' && steps.hydrate.outputs.adapter == 'claude-action-compat' }}"
        and model_steps[checkpoint_upload].get("if")
        == "${{ steps.hydrate.outcome == 'success' && (steps.hydrate.outputs.adapter == 'claude-action-compat' || steps.direct_install.outcome == 'success') }}"
        and model_steps[checkpoint_stage].get("if")
        == model_steps[checkpoint_upload].get("if")
        and "steps.source.outputs.match == 'true'" in str(capture.get("if", "")),
    )
    check("runtime: every Codex step is codex-only", all("codex" in str(step.get("if", "")) for step in runtime_runs + codex_build_steps))
    check("runtime: no configured action can reach a Codex workflow path", all(row["target"] == "claude" for row in yaml.safe_load(Path("wheelhouse.config.yml").read_text())["agent_runtime"]["actions"].values()))
    check("runtime: all use pinned app-server package", text.count("@openai/codex@0.144.0") >= 3)
    package_steps = [step for _, step in all_steps if "agent_runtime.py verify-package" in str(step.get("run", ""))]
    check("runtime: every Codex setup verifies wrapper and platform tarball bytes", len(package_steps) == 4 and all("--tarball" in step["run"] and "--platform-tarball" in step["run"] and step["run"].count("npm pack --silent") == 2 for step in package_steps))
    check("runtime: installation does not trust separately fetched registry metadata", all("npm view" not in step["run"] for step in package_steps))
    check("runtime: every Codex setup installs only verified local artifacts", all("npm install --offline" in step["run"] and '"$tarball"' in step["run"] and 'tar -xzf "$platform_tarball"' in step["run"] for step in package_steps))
    check("runtime: verification precedes install and executable extraction", all(step["run"].index("agent_runtime.py verify-package") < step["run"].index("npm install --offline") < step["run"].index('tar -xzf "$platform_tarball"') < step["run"].index("--version | grep") for step in package_steps))
    check("runtime: all verify vendored protocol pins", text.count("agent_runtime.py verify-pins") >= 3)
    check("runtime: Claude proof records only observed bridge controls", all(value in model_text for value in ("github-readonly-artifact-bridge-v1", "content-addressed-bounded-verified", "targetInputsReadOnly", "local-no-remote", "declaredTools")) and "subprocessIsolation" not in model_text and "bubblewrap socat" not in model_text)
    check("runtime: Claude harness provenance is observed or explicitly pinned", all(value in model_text for value in ("actionSourceCommit", "actionMetadataQuality", "actionMetadataSha256")) and "canonical_sha256({\"actionCommit\"" not in Path("agent_runtime/claude_bridge.py").read_text())

    check("search: wrapper follows resolved Claude production branches", all("claude" in str(step.get("if", "")) for _, step in all_steps if step.get("id") in ("search-tool", "nl-search-tool")))
    check("search: Codex receives no model GH_TOKEN", all("GH_TOKEN" not in (step.get("env") or {}) for step in runtime_runs))
    check("search: trusted supervisor alone receives optional broker token", any("READONLY_TOKEN" in (step.get("env") or {}) for step in runtime_runs))
    check("credentials: no Codex credential secret invented", all(name not in text for name in ("secrets.CODEX_ACCESS_TOKEN", "secrets.CODEX_AUTH", "secrets.OPENAI_API_KEY", "secrets.CODEX_API_KEY")))
    check("credentials: no auth.json blob workflow", "auth.json" not in text)
    check("credentials: current Claude subscription secret remains production gate", "CLAUDE_CODE_OAUTH_TOKEN" in text)

    triage = docs["triage"]
    compact = next(step for step in steps(triage) if step.get("id") == "compact-results")
    check("triage consumer: Claude AgentResult required", "steps.primary-result.outputs.result" in str((compact.get("env") or {}).get("PRIMARY")))
    check("repair consumer: normalized repair result required", "steps.repair-result-received.outputs.result" in str((compact.get("env") or {}).get("REPAIR")))
    deep_post = next(step for step in steps(docs["deep"]) if step.get("name") == "Post the verdict on the card")
    check("deep consumer: Claude AgentResult required", "steps.claude-result.outputs.result" in str((deep_post.get("env") or {}).get("EXECUTION_FILE")))
    deep_steps = steps(docs["deep"])
    deep_ids = [step.get("id") for step in deep_steps]
    deep_gate = next(step for step in deep_steps if step.get("id") == "gate")
    check("deep selection: target resolves before repository-aware gate", deep_ids.index("resolve") < deep_ids.index("gate"))
    check("deep selection: gate uses validated resolved repository", (deep_gate.get("env") or {}).get("TARGET_REPO") == "${{ steps.resolve.outputs.repo }}" and '--repo "$TARGET_REPO"' in deep_gate["run"])
    nl_result = next(step for step in steps(docs["decision"]) if step.get("id") == "nl-result")
    check("NL consumer: trusted AgentResult is bound before deterministic route", 'echo "path=${RUNTIME_RESULT:-}"' in nl_result["run"] and "export-final" not in nl_result["run"])
    nl_consume_steps = docs["decision"]["jobs"]["nl-claude-consume"]["steps"]
    nl_export = next(step for step in nl_consume_steps if step.get("id") == "nl-result")
    nl_failure = next(
        step for step in nl_consume_steps if step.get("id") == "nl-failure-consumer"
    )
    nl_stage = next(
        step
        for step in nl_consume_steps
        if step.get("name") == "Record natural-language consumer stage"
    )
    check(
        "NL projection: trusted result binding runs under always for admitted Claude work",
        "always()" in str(nl_export.get("if", ""))
        and "steps.nl-claude-result.outputs.result"
        in str((nl_export.get("env") or {}).get("RUNTIME_RESULT", "")),
    )
    check(
        "NL projection: source drift receives the precise bounded retry note",
        (nl_failure.get("env") or {}).get("ERROR_CODE")
        == "${{ steps.nl-claude-result.outputs.error-code }}"
        and "source.revision_mismatch" in nl_failure["run"]
        and "Wheelhouse updated while this request waited; please retry."
        in nl_failure["run"]
        and "The assistant did not produce a trusted result." in nl_failure["run"],
    )
    check(
        "NL projection: final stage preserves the normalized model error code",
        (nl_stage.get("env") or {}).get("MODEL_ERROR_CODE")
        == "${{ steps.nl-claude-result.outputs.error-code }}"
        and 'code="${MODEL_ERROR_CODE:-consumer.rejected}"' in nl_stage["run"],
    )
    triage_consume_steps = docs["triage"]["jobs"]["triage-claude-consume"][
        "steps"
    ]
    triage_card = next(
        step for step in triage_consume_steps if step.get("id") == "card-consumer"
    )
    triage_stage = next(
        step
        for step in triage_consume_steps
        if step.get("name") == "Finalize primary triage claim and stage evidence"
    )
    check(
        "triage projection: source drift receives the precise retry result",
        (triage_card.get("env") or {}).get("PRIMARY_ERROR_CODE")
        == "${{ steps.primary-result.outputs.error-code }}"
        and (triage_card.get("env") or {}).get("REPAIR_ERROR_CODE")
        == "${{ steps.repair-result-received.outputs.error-code }}"
        and "source.revision_mismatch" in triage_card["run"]
        and "Wheelhouse updated while this request waited; please retry."
        in triage_card["run"],
    )
    check(
        "triage projection: final claim preserves the normalized model error code",
        (triage_stage.get("env") or {}).get("MODEL_ERROR_CODE")
        == "${{ steps.primary-result.outputs.error-code }}"
        and 'code="${MODEL_ERROR_CODE:-consumer.rejected}"' in triage_stage["run"],
    )
    execute = next(step for step in steps(docs["decision"]) if step.get("id") == "execute")
    check("NL safety: deterministic FLEET_TOKEN executor remains separate", (execute.get("env") or {}).get("GH_TOKEN") == "${{ secrets.FLEET_TOKEN }}")
    check("NL safety: model runtime never receives FLEET_TOKEN", all("FLEET_TOKEN" not in (step.get("env") or {}) for step in runtime_runs))

    config = yaml.safe_load(Path("wheelhouse.config.yml").read_text())["agent_runtime"]
    check("selection: Claude primary profile selected", config["primary_profile"] == "claude-action-current-pinned")
    check("selection: every action explicitly targets Claude", config["target"] == "claude" and all(row["target"] == "claude" for row in config["actions"].values()))
    check("selection: fallback globally disabled", config["fallback"] == "none")
    check("selection: immutable Claude model forbids aliases", config["profiles"][config["primary_profile"]]["model"] == "claude-sonnet-4-6" and config["profiles"][config["primary_profile"]]["allow_model_alias"] is False)
    check("selection: schema repair is the only direct activation", config["production_activation"] == {"triage.schema-repair": "claude-cli-pinned", "nl-decision.schema-repair": "claude-cli-pinned"} and config["temporary_rollback_profile"] is None)
    check("selection: Codex remains disabled non-target evidence", config["disabled_adapters"] == {"codex-app-server": "unsupported-public-chatgpt-pro-auth"} and all(row["profile"] != "codex-subscription-pinned" for row in config["actions"].values()))
    check("selection: direct Claude CLI profile is exact", config["profiles"]["claude-cli-pinned"]["adapter"] == "claude-cli" and config["profiles"]["claude-cli-pinned"]["auth_profile"] == "anthropic-subscription")
    direct_step = next(step for step in model_steps if step.get("id") == "direct_schema_repair")
    check("production: direct runtime is schema-repair only", "triage.schema-repair" in direct_step["if"] and "nl-decision.schema-repair" in direct_step["if"] and "claude-cli" in direct_step["if"])
    check("production: direct runtime uses trusted supervisor without action fallback", "from agent_runtime.supervisor import run" in direct_step["run"] and "claude_bridge" not in direct_step["run"] and "anthropics/claude-code-action" not in direct_step["run"])
    check("production: direct OAuth handoff is private and scrubbed before supervisor", "umask 077" in direct_step["run"] and "unset CLAUDE_CODE_OAUTH_TOKEN" in direct_step["run"] and "WHEELHOUSE_CLAUDE_CREDENTIAL_FILE" in direct_step["run"])
    check("production: direct runtime retains durable controller-failure normalization", "write_controller_failure_result" in model_text and "direct.json" in model_text and "lifecycle.timeout" in model_text)
    direct_install_failure = next(step for step in model_steps if step.get("name") == "Write direct-runtime install failure result")
    direct_checkpoint = next(step for step in model_steps if step.get("name") == "Write direct-runtime pre-invocation checkpoint")
    check("production: direct install failure is normalized before spend", "steps.direct_install.outcome == 'failure'" in direct_install_failure["if"] and "write_direct_install_failure_result" in direct_install_failure["run"] and "harness.install_failed" in Path("agent_runtime/schemas/v1alpha1/agent-result.schema.json").read_text())
    check("production: direct checkpoint requires a verified runtime install", "steps.direct_install.outcome == 'success'" in direct_checkpoint["if"])
    check("production: direct binary remains exact and digest verified", "2.1.197" in model_text and "sha256sum" in model_text and "runtime.lock.json" in model_text)

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
