# Contributing to Wheelhouse

Every change to this repository is raised through [no-mistakes](https://github.com/kunchenguid/no-mistakes), the local git proxy that validates code before it reaches `main`.
Wheelhouse dogfoods the same contribution gate it applies to the rest of the fleet: the `no-mistakes-required.yml` workflow runs a check named **"PR must be raised via no-mistakes"** on every PR to `main`, and that check passes only for PRs the no-mistakes pipeline opened.

## How to submit a change

1. Make your change on a branch (never commit directly to `main`).
2. Run the pipeline instead of pushing by hand:

   ```
   git push no-mistakes
   ```

   no-mistakes runs the required review/test/lint/CI steps, then opens (or updates) the PR and writes a deterministic `## Pipeline` section into the PR body.
   That section carries the signature the gate looks for:

   ```
   Updates from [git push no-mistakes](https://github.com/kunchenguid/no-mistakes)
   ```

3. Let the checks finish, then merge through the normal PR flow.

A PR opened directly on GitHub (without the no-mistakes signature in its body) fails the **"PR must be raised via no-mistakes"** check and cannot be merged.
Automated authors are exempt: PRs from `github-actions[bot]`, `dependabot[bot]`, and `release-please[bot]` skip the gate.

## Local validation

There is no build step.
Before pushing, validate locally:

```
python -m py_compile agent_runtime/*.py agent_runtime/adapters/*.py scripts/*.py tests/*.py
ruff check --select F821 agent_runtime scripts tests
python scripts/agent_runtime.py verify-pins
python tests/test_agent_runtime_contract.py
python tests/test_agent_runtime_source_review.py
python tests/test_agent_runtime_capabilities.py
python tests/test_agent_runtime_security.py
python tests/test_agent_runtime_lifecycle.py
python tests/test_agent_runtime_consumers.py
python tests/test_agent_runtime_dispatch.py
python tests/test_agent_runtime_claude_handoff.py
python tests/test_agent_runtime_claude_bridge.py
python tests/test_agent_runtime_workflows.py
python tests/test_agent_runtime_admission.py
python tests/test_agent_runtime_result_binding.py
python tests/test_agent_runtime_repo_snapshot.py
python tests/test_agent_runtime_claude_adapter.py
python tests/test_agent_runtime_schema_repair_cutover.py
python tests/test_agent_outage_recovery_gate.py
python tests/test_claude_model_dispatch.py
python tests/test_decision.py
python tests/test_qualify_refs.py
python tests/test_card_refresh.py
python tests/test_card_reuse.py
python tests/test_reconcile.py
python tests/test_merge_conflict.py
python tests/test_pending_contributor_cleanup.py
python tests/test_ci_autoapprove.py
python tests/test_target_observation.py
python tests/test_option_b_architecture.py
python tests/test_target_reconcile_transaction.py
python tests/test_ci_security_summary.py
python tests/test_check_status.py
python tests/test_compliance_event_evidence.py
python tests/test_author_filter.py
python tests/test_auto_triage.py
python tests/test_triage_budget.py
python tests/test_triage_replay.py
python tests/test_triage_prompt_size.py
python tests/test_triage_result_delivery.py
python tests/test_triage_schema_repair.py
python tests/test_auto_merge_v1.py
python tests/test_automerge_card_ui.py
python tests/test_automerge_workflow_hold.py
python tests/test_deep_review.py
python tests/test_nl_decisions_search.py
python tests/test_nl_prompt_size.py
python tests/test_nl_schema_repair.py
python tests/test_public_clone.py
python tests/test_public_clone_e2e.py
python tests/test_workflow_lint.py
python tests/test_scan_reliability.py
python tests/test_config_schema.py
python - <<'PY'
from pathlib import Path
import yaml

for pattern in (".github/workflows/*.yml", ".github/ISSUE_TEMPLATE/*.yml", "wheelhouse.config.yml"):
    for path in sorted(Path(".").glob(pattern)):
        with path.open() as fh:
            yaml.safe_load(fh)
PY
```

If `actionlint` is available, also run:

```
actionlint .github/workflows/*.yml
```

## Setting up the repository itself

If you are forking Wheelhouse to run your own queue rather than changing this codebase, follow the numbered checklist in the [README](README.md#setup---a-numbered-checklist) instead.
That covers the fleet config, the `FLEET_TOKEN` secret, and the agent-assisted features.
