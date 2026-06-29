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
python -m py_compile scripts/*.py tests/*.py
python tests/test_decision.py
python tests/test_card_refresh.py
python tests/test_reconcile.py
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
That covers the fleet config, the `FLEET_TOKEN` secret, and the optional LLM side-jobs.
