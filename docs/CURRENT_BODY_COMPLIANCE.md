# Current-body compliance evidence

Wheelhouse normally reduces duplicate check contexts by exact check name on the
current PR head. That remains the portable default. A repository can opt into a
stronger Actions event contract when its compliance result depends on mutable PR
metadata such as the pull request body.

The first producer of this contract is the no-mistakes workflow landed in
no-mistakes merge commit `912bc756a44c1b78fa29c5c9c2c722cf92f3aef8`.

## Producer contract

The producer must meet all of these requirements:

1. The workflow uses the `pull_request` event and retains the read-only fork
   boundary.
2. Every `opened` and `edited` event has an immutable concurrency identity and
   executes the stable compliance job to terminal success or failure.
   `synchronize` and `reopened` may retain safe head-event coalescing.
3. The stable job/check name is the configured `compliance_check`.
4. The workflow has a stable file path and workflow name.
5. Its controlled `run-name` is exactly:

   ```text
   PR #<number> body compliance - <action> - event <run_number> (run <run_id>)
   ```

   `<action>` is one of `opened`, `edited`, `synchronize`, or `reopened`.
   GitHub's workflow run API must expose the same controlled value in `name` and
   `display_title`.
6. GitHub's immutable `run_id` identifies one event. The workflow's monotonic
   `run_number` orders events. A rerun keeps both identities and increments
   `run_attempt`.
7. The workflow run `head_sha` is the exact reviewed head. A terminal `success`
   means that event satisfied the body policy; a terminal `failure` means it did
   not. Canceled, action-required, neutral, incomplete, and non-terminal states
   are not passing evidence.

The title is not trusted by itself. It is accepted only after it agrees with
GitHub-owned run fields and the configured workflow identity.

## Consumer configuration

Opt in one repository explicitly:

```yaml
repos:
  - name: my-repo
    compliance_check: "PR must be raised via no-mistakes"
    compliance_evidence:
      schema: "wheelhouse.actions-current-body/v1"
      workflow_path: ".github/workflows/no-mistakes-required.yml"
      workflow_name: "Require no-mistakes"
    test_check_patterns: ["test"]
```

The mapping has an exact schema. Unknown keys, unsupported schema versions,
unsafe workflow paths, blank names, or a missing `compliance_check` fail closed.
A malformed opt-in never silently falls back to legacy same-name reduction.
Only repositories that have deployed the producer contract should enable it.

## Evidence reads and reduction

For an opted-in PR, Wheelhouse:

1. Reads the configured workflow metadata and requires its GitHub workflow ID,
   exact path, exact name, and active state.
2. Lists the complete `pull_request` workflow-run history filtered to the exact
   PR head SHA. Pages contain at most 100 runs and the total is bounded at 300.
   A changing count, malformed page, or larger history is incomplete evidence.
3. Requires every run to match the workflow ID/path, event, exact head,
   controlled run identity, GitHub run ID, run number, and PR association when
   GitHub supplies one. GitHub often leaves `pull_requests` empty for fork runs,
   so the controlled PR identity plus the other exact bindings is the fallback.
4. Binds compliance CheckRun contexts to Actions runs through GitHub's
   `databaseId` and canonical `detailsUrl` run/job identities. Check-run IDs are
   identity links only. They never determine event order.
5. Selects the greatest validated `run_number`, independent of API array order
   or check completion order.

The latest event alone determines current compliance:

- terminal success: `pass`;
- substantive terminal failure, action-required, or canceled: conservative
  `fail`;
- non-terminal or neutral: `pending`;
- missing, malformed, duplicated, mismatched, or incomplete metadata:
  `pending` with an incomplete target observation.

A latest passing event may supersede an older same-workflow failure only when
all same-name compliance contexts bind to validated event runs. Other check
failures, incomplete context lists, and unexplained raw rollup failures continue
to fail closed. An older success can never satisfy a later unresolved event.

## Caching and freshness

A fleet scan caches the immutable workflow identity once per repository and one
complete run list per exact head. This avoids repeated Actions reads across the
same observation without making the data durable.

Exact card reconciliation and auto-merge G7 run in fresh processes and perform
fresh metadata/run reads before projecting or acting. An Actions API failure
makes only the opted-in compliance evidence unavailable; it does not abort an
ordinary fleet scan or change non-opted-in repositories.

Fork approval remains separate. Wheelhouse still enumerates and approves every
independently actionable exact-head run, including same-workflow runs. Event
evidence does not deduplicate approval candidates or weaken the existing fork
CI safety gate.
