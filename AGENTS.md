# Project agent memory

Wheelhouse - a portable, forkable IssueOps machine. Issues in this repo are a
human-in-the-loop decision queue for cross-repo OSS maintenance, driven entirely
by GitHub Actions. This file holds durable, project-intrinsic notes.

The name: a ship's wheelhouse is where the captain steers. This repo is where
you steer your open-source maintenance - what needs your hand surfaces as a card
and you make the call. (The product is "Wheelhouse"; the generic verb "triage"
still appears where it's plain English, e.g. "triage the queue".)

## Non-negotiable invariants

- **Portability / fork-and-own.** Never hardcode an owner or repo name in
  workflows or scripts. Owner is always `github.repository_owner` (env
  `GITHUB_REPOSITORY_OWNER`); the fleet + policy come from the single root file
  `wheelhouse.config.yml`. A fork on any account must work after editing only that
  file and adding the secrets.
- **Security.** Owner-gate every acting path (`sender == repository_owner`, plus
  optional `maintainer` override via `wheelhouse_core.py authorized`). Cross-repo
  actions use `FLEET_TOKEN`; everything that touches THIS repo's cards uses the
  default `GITHUB_TOKEN` (this is also what prevents the decision-handler from
  re-triggering itself - GitHub does not raise workflow events for
  GITHUB_TOKEN-authored activity). The fork-CI / pwn-request HOLD (exit 4 in
  `approve_ci`) must never be removed: approving fork CI that changes
  `.github/workflows`, `.github/actions`, or `action.yml(.yaml)` is held for
  manual review and fails closed. **Scan-time auto-approve is a STRICT SUBSET of
  the manual gate**: it shares the one `ci_safety` verdict and approves only what
  is provably safe (no risky files AND no `pull_request_target` posture, all reads
  fail closed), so it can never auto-clear anything the manual path would HOLD.

## Architecture

- **State lives in GitHub, not on disk.** Open issue = pending decision; closed =
  consumed. Labels are state (`needs-decision`, `pending-triage`, `processing`,
  `resolved`, `blocked`, `wheelhouse:manual-merge-required`, `repo:*`, `kind:*`,
  `priority:*`). A hidden
  `<!-- wheelhouse-state: {...} -->` block in each card body carries
  `{repo, number, kind, head_sha, options}` plus the material fields
  `{comp, tests, priority, bucket, projection_freshness, projection_head_sha,
  projection_complete}` so a refresh can cheaply and deterministically decide
  "did this target or its current-tense projection contract materially change?"
  - see "Card refresh" in Sharp edges. `options` is also material for refresh comparison,
  but is normalized as a sorted set so checkbox reordering alone does not
  refresh the card. The state block also carries `updated_at` unconditionally
  (populated for issue-triage items, empty for pr-review) - it is NON-material,
  existing as the issue-triage auto-triage cache key and strict newer-only
  deterministic refresh stamp, mirroring how `head_sha` doubles as the
  pr-review cache key. PR cards may also carry a versioned, non-churn-triggering
  `projection_ref` with observation ID/time/source, target/head identity,
  completeness, freshness, and bucket. Its semantic head/completeness/freshness/
  bucket dimensions are the material fields above; a newer observation ID/time
  alone does not rewrite an otherwise unchanged card. The state block also carries
  `activity_reflected_at`, a NON-material target-activity sort stamp. When a
  target's GitHub `updatedAt` advances past that stamp, Wheelhouse may make one
  hidden state-only card body edit so GitHub's `sort:updated-desc` issue view
  surfaces recently active targets first. That stamp is never part of
  `MATERIAL_FIELDS`, never a triage revision, and never a decision input. Cards
  written before the stamp existed use the card issue's own GitHub `updatedAt`
  as the baseline, so legacy queues do not churn just to backfill the stamp.
  Automatic triage (pr-review
  AND issue-triage) adds non-material cache fields such as
  `triaged_sha`, `triage_status`, `triage_recommendation`, and the bounded
  schema-repair telemetry `triage_repair_status`/`triage_repair_reason` (see
  "Bounded schema repair" in Sharp edges); those are
  deliberately outside `MATERIAL_FIELDS` so a triage result never changes
  classification or forces a card refresh. The auto-inserted
  `accept-recommendation` option is stripped from material option comparisons
  because it is derived from non-material triage state, not from source-provided
  checkbox options. A held card also carries non-material `held: true` until its
  first auto-triage attempt publishes the normal decision controls. The state
  block may also carry a bounded, versioned, head-scoped NON-material
  `automerge_workflow_hold` record after the authoritative final auto-merge gate
  proves a history-only workflow touch against a complete clean net diff.
  Its matching managed `wheelhouse:manual-merge-required` label and visible
  section deny repeated claims without making the refreshable card generically
  `blocked`; malformed same-head state fails closed, while authoritative
  new-head or incompatible-kind rendering clears it.
  The state
  block also carries `render_version`, another
  non-material field alongside `triaged_sha`: it is a one-time re-render
  trigger stamped by `render()` (see "Card refresh" in Sharp edges) that exists
  purely so a display-only fix (e.g. the author `@mention` drop or automated
  status labeling) propagates to already-open cards; it is never a
  `MATERIAL_FIELDS` member and never
  influences classification. `render_card.py` writes that marker.
  Reconcile may also add one bounded, versioned, NON-material `reconcile_absence` record to a pure pending card after a conclusive scheduled run finds its still-open target outside the worklist.
  The fixed threshold is two adjacent scheduled workflow run numbers.
  The threshold record includes exact trusted machine soft-close provenance before close; malformed, duplicate, wrong-version, boolean, negative, oversized, or otherwise untrusted state reads as count zero and can never accelerate close or qualify future reuse.
  When no open card exists, `render_card.lookup_card_lifecycle` completely lists the exact target label and may reuse a closed card only when its strict state identity, managed target/repo labels, issue author, latest close actor, close timing, terminal labels, and current-schema soft-close provenance are independently trustworthy.
  A later `updatedAt` is reusable only when a bounded complete issue-timeline read proves every post-close event came from trusted Wheelhouse automation and fully explains the timestamp; unreadable, incomplete, ambiguous, or human-touched history refuses reuse.
  If several trusted candidates share the exact identity, Wheelhouse selects the highest issue number deterministically and reports the lower candidates as superseded without changing them; any identity disagreement still fails closed.
  Legacy, owner/decision-resolved, blocked, held, hard-target-closed, auto-merged/audit-protected, manually authored, malformed, target-mismatched, and ambiguous closed cards are never reopened.
  A reusable card is rendered and relabeled while still closed, re-read before reopening, then checked through a complete trusted-open lookup; a partial failure stays closed, and post-open ambiguity rolls the local card back closed.
  New-head or incompatible-kind rendering naturally drops stale triage, recommendation, verdict, criteria, held, audit, and absence state; same-revision triage is preserved only through the normal `_preserve_same_revision_triage` path.
  A conclusive worklist return clears the record while reusing the open card.
  Definitive target closure remains an immediate hard close and clears any uniquely parsed absence record first so hard-close cards cannot later satisfy reuse provenance, while `ok:false`, truncated, unresolved-mergeability, CI-wait, audit-protected, and owner/handler-raced cards remain frozen.
  Every reconcile mutation and close re-reads the live card and requires it to match the scan snapshot.
  `ingest`, `decision-handler`, and `scan-backstop` share the queued `wheelhouse-backstop` concurrency group, and create/reopen still performs a post-write uniqueness check, so the event and global paths cannot mint two actionable cards for one target.
  **Post-create admission treats the create response number plus issue-by-number reads as source of truth** (`render_card.verify_unique_open_card` / `_create_and_verify_card`): a valid create must never be closed or labeled `resolved` solely because GitHub's eventually consistent open-list/search index has not yet surfaced the new issue.
  List lag emits structured `wheelhouse card-admission list_index_lag` telemetry and still admits/queues by number; a genuinely observed alternate open card or a malformed direct object fails closed with rollback; an incomplete list probe retains the open card deferred without destructive rollback.
  Destructive admission rollbacks must fail the reconcile pass (`admission rollback(s)` counter) so a scan cannot look healthy after admission loss.
  Covered by `tests/test_card_reuse.py` delayed-index fixtures.
  `render_card.py` writes state markers, but
  `parse_state_block` also accepts the legacy `<!-- triage-state: ... -->`
  marker (cards rendered before the rename) - back-compat that must stay so a live
  queue keeps working. It also tolerates old `wheelhouse-state` cards that lack
  the material fields: a missing field reads as "unknown", so such a card is seen
  as changed exactly once and refreshes itself (backfilling the fields), then
  no-ops. The local lock/board/ledger from the original `triage.py`
  are intentionally dropped (replaced by Actions
  `concurrency` + issues/labels/comments).
  Stale pending-contributor cleanup deliberately stores its state on the TARGET
  PR, not on a Wheelhouse card: the active label is
  `wheelhouse:pending-contributor-action`, the opt-out label is
  `wheelhouse:keep-open`, and hidden JSON markers in target comments carry the
  provable ask/reminder/close records.
- **Workflows:** `ingest` (dispatch/manual -> upsert a card), `decision-handler`
  (tick/slash/**plain-English** -> act on target -> consume resolved cards,
  block non-retryable errors, or leave retryable/non-terminal cards open), `scan-backstop`
  (hourly scan -> deterministic target-side cleanup plus reconcile:
  create/reuse/refresh/activity-reflect/close - the primary keep-current path
  now that cards refresh on material change, render-version staleness, or a
  held-card publish trigger, and can make a hidden state-only activity stamp
  write when live target activity is newer than the card's reflected stamp;
  safe to run hourly because reconcile no-ops unless one of those maintenance
  triggers or an auto-triage cache miss applies, and queues automatic PR or
  issue triage when the
  current revision (a PR's `head_sha`, or an issue's `updatedAt`) lacks a fresh
  `triaged_sha` cache; its "List open cards" step lists THIS repo's open cards via
  `gh api --paginate --slurp "repos/{owner}/{repo}/issues?..." | jq '...'` -
  `gh api --slurp` and `--jq` are mutually exclusive in the installed `gh` CLI, so
  the `--paginate --slurp` result (an array of per-page arrays) is piped into a
  standalone `jq` instead of passing `--jq` to `gh api` itself;
  `tests/test_workflow_lint.py` guards against this combination reappearing in
  any workflow), `triage` (automatic,
  lightweight, advisory PR-card OR issue-card context; pr-review is gated on
  `auto_triage`, issue-triage on the INDEPENDENT `auto_triage_issues`, both also
  requiring `CLAUDE_CODE_OAUTH_TOKEN`; cache-keyed by source revision), `deep-review` (ALWAYS-ON, code-grounded;
  gated only on `CLAUDE_CODE_OAUTH_TOKEN` - no config flag),
  `no-mistakes-required` (PR-to-`main` gate: the job `name:` MUST stay exactly
  `PR must be raised via no-mistakes` - it is the check name the fleet convention
  and this repo's own `wheelhouse.config.yml compliance_check` reference - and it
  passes only when the PR body carries the no-mistakes signature
  `Updates from [git push no-mistakes](https://github.com/kunchenguid/no-mistakes)`,
  with bot authors skipped; Wheelhouse dogfoods on itself the same gate it enforces
  on the fleet, so contributions go through `git push no-mistakes` - see
  `CONTRIBUTING.md`).
- **Scripts:** `wheelhouse_core.py` (scan/classify/dedup/security gate + the
  shared CI-safety verdict `ci_safety` / `repo_pr_target_posture` and scan-time
  auto-approve in `build_repo`, the advisory read-only CI-approval security
  summary `ci_security_summary` (see "CI-approval security summary" in Sharp
  edges), stale pending-contributor cleanup
  (`sweep_pending_contributor_actions`, target-side markers/labels, and the
  rebase-nudge arming path), plus shared utils
  `parse_state_block`, `authorized`, `state`, `nl-decisions-enabled`,
  `auto-triage-enabled`, `auto-triage-issues-enabled`, `qualify_issue_refs`
  (rewrites a bare GitHub-autolink `#N` in model text to `owner/repo#N` - see
  "Cross-repo reference qualification" in Sharp edges)),
  `target_observation.py` (pure versioned exact/bulk observation, head-bound
  approval receipt, and compact projection-reference contracts),
  `target_reconcile.py` (pure CI-wait observation + receipt -> current/pending/
  unknown projection planner; no GitHub calls or Markdown),
  `render_card.py` (render + card CRUD, including the shared strict closed-card
  lookup/trust/reuse operation used by both ingest and reconcile;
  `CHECKBOX_OPTIONS`/`OPTION_LABELS` carry
  the per-kind checkboxes, including the non-consuming `investigate` box on
  pr-review/issue-triage; held `pending-triage` placeholder rendering;
  automatic triage section rendering, structured recommendation persistence,
  conditional `Accept recommendation` checkbox rendering, `triaged_sha` cache
  updates, automated-status labeling for known harness transcript lines,
  target-activity state reflection, the advisory `### Security review` section
  on CI-approval HOLD cards (`_security_review_section`), the non-authoritative
  read-only
  `### Auto-merge criteria` section and trusted history-only workflow hold on
  PR-review cards, plus trusted
  triage-result card edits that publish held cards),
  `apply_decision.py` (deterministic `parse` then
  `execute`; pre-merge workflow-touch gate in `do_merge` that blocks
  `.github/workflows/**` PRs (including renames through `previous_filename`) for
  manual UI merge; non-checkbox actions including
  `comment`, `decline`, and
  pr-review-only `request-changes` with optional cleanup arming after a successful
  GitHub review; the virtual `accept-recommendation` checkbox
  routing into existing deterministic actions; the NON-CONSUMING `investigate`
  routing + `clear-checkbox`; plus the natural-language `nl-eligible`/`nl-prompt`/`nl-route` that map an owner's
  free-text comment to a structured result), `nl_readonly_search.py` (installs
  the optional `wheelhouse-search` wrapper for READONLY_TOKEN-backed LLM
  context),
  `automerge_criteria.py` (stable criterion IDs/labels and fail-closed
  normalization shared by evaluator and renderer), `auto_merge.py`
  (authoritative G0-G6 criterion evaluation plus claim/validate/G7 act/audit and
  default-token persistence/recovery of final-gate workflow holds),
  `build_item.py` (normalize ingest payload), `reconcile.py` (backstop
  create/**refresh**/activity-reflect/close/reuse and automatic triage dispatch). `apply_decision` imports `wheelhouse_core` and
  `nl_readonly_search`; `reconcile`/`render_card` import `wheelhouse_core` (and
  `build_item` imports `render_card`) via
  `sys.path.insert(0, dirname(__file__))`.
- **Reusable actions (pinned to full SHAs).** `decision-handler` delegates two
  mechanical jobs to the `issue-ops` toolkit instead of hand-rolling them:
  `issue-ops/parser` renders the card's checkboxes as `{selected, unselected}`
  (run twice - new body + pre-edit body - so `apply_decision.py` can keep the
  "exactly one newly-ticked" diff), and `issue-ops/labeler` does every
  `processing`/`resolved`/`blocked`/`needs-decision` add/remove (with
  `create: true` so it also creates the label objects). Pin both to a commit SHA
  with a trailing `# vX.Y.Z` comment; never a floating tag.

## Sharp edges

- **`check_status()` aggregates by equivalent check identity, never scalar
  last-write-wins - cards #392 and #1537.**
  GitHub's GraphQL `statusCheckRollup.contexts` can return more than one
  check-run with the SAME name (e.g. `concurrency: cancel-in-progress`
  leaving CANCELLED siblings beside a completed SUCCESS on the same head).
  `check_status()` (`scripts/wheelhouse_core.py`) groups by exact check name
  on the current head and reduces each group: substantive
  FAILURE/TIMED_OUT/ACTION_REQUIRED/STARTUP_FAILURE -> `"fail"`; any
  non-completed -> `"pending"`; a completed SUCCESS makes same-name
  CANCELLED siblings ignorable -> `"pass"`; cancelled-only evidence is
  never pass. Across different names (e.g. matrix legs) results still
  worst-wins. A scalar last-write-wins assignment inside the loop (card
  #392) would make the result depend on GraphQL array order instead of
  policy. As a fail-toward-safe backstop, `check_status()` also clamps
  `compliance` to `"fail"` whenever GitHub's own authoritative
  `statusCheckRollup.state` is `"FAILURE"`/`"ERROR"` and the per-context read
  would otherwise say `"pass"`/`"n/a"`, except when every non-pass rollup
  context is an ignorable CANCELLED sibling of a proven same-name SUCCESS
  (card #1537 concurrency poison). Untracked/optional failures still fail
  closed. Repositories whose compliance check depends on mutable PR-body events
  may explicitly opt into `wheelhouse.actions-current-body/v1`; the producer,
  bounded Actions API reads, exact workflow/run/CheckRun bindings, latest
  `run_number` semantics, and fail-closed freshness rules are authoritative in
  `docs/CURRENT_BODY_COMPLIANCE.md` and covered by
  `tests/test_compliance_event_evidence.py`. Non-opted-in reduction is unchanged.
  See `tests/test_check_status.py`.
- **Failed decision = durable open `blocked`, never pure `needs-decision`
  (card #447), except recoverable merge conflicts.** `decision-handler.yml`
  maps `terminal_state == 'error'` onto the same label path as `blocked` (add
  `blocked`, drop `needs-decision`; do NOT close). Leaving a failed action as
  pure `needs-decision` lets reconcile's soft self-heal silently consume it as
  `resolved` when the open target later leaves the worklist. Hard-close still
  auto-cleans a `blocked` card once the target is genuinely merged/closed.
  **Exception (card #1544):** `do_merge` returns terminal `none` when GitHub
  reports a merge conflict (still posts the conflict note). Terminal `none`
  does not add `blocked` or drop `needs-decision`, so the card stays pure
  pending and the existing scan/reconcile refresh path reactivates it after a
  clean new head. Generic non-conflict failures stay durable `error`/`blocked`.
  Stale-head rechecks and NL revision binding are unchanged. Guarded by the
  YAML-inspection in `tests/test_nl_decisions_search.py`
  (`test_error_terminal_state_labels_as_blocked`, plus `none`/retryable
  keep-actionable checks), the soft/hard-close cases in `tests/test_reconcile.py`,
  and `tests/test_decision.py`
  (`test_merge_conflict_is_recoverable_not_durable_blocked`).
- **Workflow-touching PRs are manual UI merges by design (Option B; cards
  #442/#447).** `FLEET_TOKEN` intentionally has no Workflows write. Before any
  card-driven `do_merge` API call, `apply_decision._workflow_merge_block`
  inspects the PR's net file list **and** each commit in its history for
  `.github/workflows/**` paths, including a rename's `previous_filename`
  (`wheelhouse_core._workflow_merge_gated_files` -
  narrower than `_risky_ci_files`; composite `action.yml` is Contents-gated and
  not blocked here). On a hit or an unable-to-verify read, merge is skipped and
  the card lands terminal `blocked` (durable open `blocked` label, not pure
  `needs-decision`) with owner-facing manual-UI-merge guidance (includes the PR
  URL). That protects against soft-heal false-close and keeps the card out of
  auto-merge V1 claiming without changing V1 gates. Hard-close still auto-cleans
  once the target is genuinely merged/closed. Detection is re-run on every later
  `/merge` once the card is actionable again, so a rebase that drops workflow
  touches can proceed. Auto-merge V1 exclusions, the pwn-request HOLD, and token
  scopes are unchanged. Covered by `tests/test_decision.py` workflow-merge-gate
  cases.
- Decision cards are machine-created.
  The target author is shown as plain text (`by <login>`), never as a GitHub
  `@mention`.
  Cards are the owner's private queue and must not notify contributors.
  The card body's hidden state block and the
  per-checkbox `<!-- opt:KEY -->` markers are load-bearing - the handler diffs
  the `selected` lists `issue-ops/parser` returns for the new vs pre-edit body to
  find the newly-ticked option (the marker survives because the parser strips
  only the `- [x] ` prefix), and parses slash-commands against the kind's allowed
  set. Don't reformat them away.
- `.github/ISSUE_TEMPLATE/wheelhouse-decision.yml` is load-bearing, not cosmetic:
  `issue-ops/parser` only returns `{selected, unselected}` when a template marks
  the section as a `checkboxes` field, and it matches the section by EXACT heading
  text. Its `checkboxes` label MUST stay `"Your decision"` to match the
  `### Your decision` heading `render_card.py` emits. (Cards are still rendered by
  `render_card.py`, not this template; a hand-filed issue from it has no state
  block, so the handler treats it as a no-op.)
- **Card refresh (an open card must reflect CURRENT target state).** Both the
  event path (`render_card.upsert_card`) and the backstop (`reconcile.py`) keep a
  card current: when a target's MATERIAL state changes - `head_sha`, bucket,
  compliance (`comp`), tests (`tests`), projection freshness/head/completeness,
  `kind`, `priority`, or checkbox `options` - the card is re-rendered in place.
  Observation ID/time are persisted in `projection_ref` but intentionally do not
  trigger churn when every semantic dimension is unchanged. Exact deterministic card-title drift is also a
  trigger for every kind, using the renderer's same 70-character truncation,
  while summary/recommendation remain non-triggers. A valid strictly newer
  issue-triage `updated_at` is a full-refresh trigger when the advisory queued
  write does not already own that revision advance. Option comparisons use set equality; display
  order remains the order provided in the card body/state. A refresh ALSO fires
  when the card's stored `render_version` is behind the current
  `CARD_RENDER_VERSION` - a non-material, one-time, self-terminating trigger
  (`render_stale`) for propagating a display-only fix (e.g. the author
  `@mention` drop) to already-open cards that have no material trigger of their
  own. A card missing `render_version` (written before this field existed)
  reads as behind, so every pre-existing pure card refreshes exactly once and
  then carries the current version (`render()` stamps it), so it no-ops on the
  next scan - no churn loop. Bump `CARD_RENDER_VERSION` whenever a future
  display-only change should propagate the same way. A render-version-only
  refresh is a same-revision cosmetic refresh (same `head_sha` for a pr-review
  card, same `updated_at` for an issue-triage card): it reuses the same
  `_preserve_same_revision_triage` path as a same-revision refresh (an
  existing `### Triage` section and its `triaged_sha`/`triage_status` cache
  survive untouched, no re-triage for that revision), and it does NOT drop the
  "target updated" comment (that stays gated strictly on `head_sha` actually
  changing - an issue's `updated_at` alone never triggers that comment, since
  it is not a material field). `CARD_RENDER_VERSION` is currently `8`: the
  7 -> 8 bump groups auto-merge criteria by gate family and separates G6's complete-diff behavior facts from its VISION.md-dependent subtree;
  6 -> 7 bump publishes the non-authoritative read-only `### Auto-merge criteria`
  section on already-open PR-review cards; the 5 -> 6 bump publishes the
  advisory read-only `### Security review` section on
  already-open CI-approval HOLD cards (display-only; the pwn-request hold and
  manual approve are unchanged); the
  4 -> 5 bump labels known claude-code-action harness polling/status transcript
  lines in card-visible auto-triage output and older cached `### Triage`
  sections without stripping content; the 3 -> 4 bump publishes the
  conditional `Accept recommendation` checkbox and
  suppresses the top-level deterministic recommendation when structured triage
  recommendation state is present; the 2 -> 3 bump publishes the
  `/request-changes <text>` PR-review slash hint on already-open cards; the
  earlier 1 -> 2 bump retroactively re-qualifies cross-repo refs cached in an
  already-open card's `### Triage` section from before `qualify_issue_refs`
  existed.
  `_preserve_same_revision_triage` now runs the lifted section
  through `wheelhouse_core.qualify_issue_refs(section, owner, repo)` before
  re-inserting it - `owner` is `GITHUB_REPOSITORY_OWNER` (read in
  `_refresh_card`, the same env source the fresh-triage render path uses) and
  `repo` is the card's own deterministic `old_state["repo"]` (falling back to
  the item's repo), NEVER the model's own text. This is the same one-time,
  self-terminating propagation shape as the earlier author `@mention` drop:
  every pre-existing card refreshes once, gets its cached triage refs
  qualified, known automated status lines labeled, and its `render_version`
  stamped with the current version, and the next scan is a full no-op unless
  target activity later advances past the reflected stamp.
  The `TRIAGE_START`/`### Triage`/`TRIAGE_END` markers contain no
  `#N` and do not match the automated-status allowlist, so repairing the whole
  section string leaves them intact.
  Target-activity sorting is deliberately not a full refresh. If a pure pending
  card's live target `updated_at` is strictly newer than its stored
  `activity_reflected_at`, `reflect_activity` may replace only the hidden state
  block so the card issue's own GitHub `updatedAt` moves for
  `sort:updated-desc`. It never re-renders visible card UI, changes labels,
  comments, or touches the target repo. A full refresh and the auto-triage
  queued-cache write both stamp `activity_reflected_at` as part of their
  existing body edit, so they do not do a second activity-only write. For a
  legacy card with no stamp, the card issue's own `updatedAt` is the baseline,
  which avoids one-time churn across an old queue.
  The shared pure helpers live in `render_card.py`
  (`material_changed`, `render_stale`, `held_publish_needed`, `refresh_needed`,
  `activity_reflection_needed`, `is_refreshable`, `plan_label_update`);
  `reconcile.py`
  pre-checks them (using the card row it already listed) so the common
  no-change and activity-fresh case never hits the API, and `upsert_card`
  re-checks them before it edits (defense in depth for the event path). Four
  rules are load-bearing and must not be loosened:
  - **Only refresh a pure `needs-decision` card.** A re-render resets the card's
    checkboxes, so a card already `processing`/`resolved`/`blocked` is left
    completely untouched - refreshing one would clobber an in-flight decision or
    race the decision-handler. (`is_refreshable` is the guard; the lock set is
    `NON_REFRESHABLE_LABELS`.) This is the chosen safe rule, and it gates the
    `render_version` trigger exactly the same way - a mid-decision card is
    never refreshed just because it is render-stale.
    A held `pending-triage` card deliberately stays refreshable because it keeps
    `needs-decision` and carries no non-refreshable lock label.
  - **Keep activity reflection state-only.** A card that is materially unchanged,
    render-fresh, and does not need held-state publishing may still get one
    hidden `activity_reflected_at` edit when target activity advanced. That edit
    is card-only under the default token, with no target write, no label churn,
    no comment, and no full re-render. If target activity is not newer either,
    the card gets no body edit - never rewrite a card just to put back an
    identical body. The
    material check is a cheap dict compare of the state block's material
    fields, which is why those fields are carried in the state JSON; the
    render-staleness check is the same kind of cheap compare against
    `render_version`, and `held_publish_needed` is the same kind of cheap
    predicate for a held card whose auto-triage path is no longer available.
    The activity check is a cheap timestamp compare against
    `activity_reflected_at` or, for legacy cards, the card issue's own
    `updatedAt`.
  - **Replace the managed labels, don't just add.** `upsert_card` removes
    `repo:*`/`kind:*`/`priority:*`/`target:*` labels that no longer apply
    (`plan_label_update`), so a changed priority/kind doesn't leave both the old
    and new label stuck on the card. It also syncs the exact `pending-triage`
    label to the current `held` state. `needs-decision` and any human-added
    label are never removed.
  When `head_sha` changed the refresh also drops a short "target updated" card
  comment so the owner sees a re-review is warranted rather than being silently
  swapped underneath. All card writes described here, including target-activity
  reflection, stay on the ambient `GH_TOKEN` (= default `GITHUB_TOKEN`) like
  every other card write, so they never re-trigger the handler and never run
  under `FLEET_TOKEN`. reconcile only ever refreshes or reflects from scanned
  `items`, which exist solely for `ok:true` repos, so an `ok:false` repo (state
  unknown) is never refreshed or activity-stamped - the same invariant that bars
  closing its cards.
- **Automatic triage is a cached card-side side job, not routing - and now
  covers issue-triage as well as pr-review, on two INDEPENDENT toggles.**
  It applies only to pure `needs-decision` cards (including held
  `pending-triage` cards, which deliberately retain `needs-decision`), gated
  per kind: pr-review by the effective `auto_triage` setting, issue-triage by
  the effective `auto_triage_issues` setting - each with its own global default
  (both TRUE), per-repo override, and item-level opt-out, so flipping one never changes the
  other's behavior. Both also require `CLAUDE_CODE_OAUTH_TOKEN` to be present.
  For explicit ingest payloads, `auto_triage:false` / `auto_triage_issues:false`
  are item-level opt-outs only; neither can force triage on when the global or
  per-repo config disables it.
  The cache key is the card state's `triaged_sha`, compared to the item's
  current **revision** - a pr-review item's `head_sha`, or an issue-triage
  item's `updated_at` (issues have no head SHA, so their GraphQL `updatedAt`
  is the freshness key instead; it advances on any edit or new comment).
  `render_card.triage_revision(item)` / `render_card.state_revision(state,
  kind)` are the single pair of helpers that pick the right field for a kind;
  every triage cache function (`triage_fresh`, `should_auto_triage`,
  `body_with_triage_queued`, `body_with_triage_result`,
  `_preserve_same_revision_triage`) goes through them so pr-review and
  issue-triage share one code path instead of forking it.
  Missing `triaged_sha` on an existing open card counts as stale, so legacy
  cards of either kind backfill exactly once on the next eligible scan.
  Before dispatching `triage.yml`, `reconcile.py` / the ingest fast path edits
  the card state to set `triaged_sha=<current revision>` and
  `triage_status=queued` so an asynchronous error, timeout, or parse failure cannot trigger an hourly retry loop; every sanctioned later cache clear remains bounded by `triage_attempts` and the daily ledger.
  **A just-created card must be read back BY NUMBER, never via
  `find_card`'s label-filtered `gh issue list`.** That listing is not
  read-after-write consistent immediately after `gh issue create`, so reading
  it back milliseconds later can silently miss the card and skip queuing its
  first auto-triage attempt (only a later scan's pre-existing-card backfill
  path would then catch it). The same eventual-consistency gap used to drive
  destructive post-create rollback when uniqueness polled only the open-list
  index; admission now verifies the direct issue first (see state-block
  uniqueness note above) and only uses the list to detect alternate open cards.
  `_create_card`/`upsert_card` therefore always
  return an int issue number (never a URL), and `reconcile.py`'s new-card
  branch reads the fresh card via `current_card({"number": n})` -> `get_card`,
  which IS consistent. The ingest fast path mirrors this: the `upsert` CLI
  writes the created/refreshed number to `$GITHUB_OUTPUT` (`issue=N`), and
  `ingest.yml`'s "Queue auto triage" step passes it as `queue-triage --issue
  N`, so that path also reads by number; `queue-triage` keeps the `find_card`
  lookup only as a fallback when no number is supplied (back-compat for a
  manual invocation).
  `triaged_sha`, `triage_recommendation`, `updated_at`, and the visible
  `### Triage` section are non-material: they must never affect `classify`,
  `material_changed`, fork-CI approval, author filtering, or conflict routing.
  For a pr-review card, `head_sha` IS material, so a head move both refreshes
  the card and makes the fresh head eligible for a spend-guarded triage attempt
  in the same pass. For an issue-triage card, `updated_at` is NOT material (an issue's
  title/comp/tests/kind/priority/options rarely change on a new comment), so a
  new comment/edit can make the card eligible for a spend-guarded triage attempt.
  When eligible, the existing single queued write owns the revision advance;
  when ineligible, the strictly newer timestamp triggers one deterministic full
  refresh without reserving budget or dispatching an advisory.
  If config is off or the token is absent, no dispatch happens and cards render
  exactly as the deterministic card did before this feature.
  `triage.yml` itself checks out the PR head for a pr-review card (and
  verifies it did not move), or the repo's DEFAULT branch read-only for an
  issue-triage card (there is no head to verify); both paths share the same
  gate/Claude/card-update steps, security posture, and `--revision` CLI
  argument (`render_card.py triage-apply|triage-fail --revision <head_sha or
  updated_at>`).
- **Automatic-triage spend guards are enforced inside the shared queued checkpoint.**
  `render_card.mark_triage_queued` reserves from the one trusted closed UTC daily ledger before it writes the non-material, versioned `triage_attempts` count and existing queued cache together, verifies the card by number, and returns the sole permit accepted by `dispatch_triage_workflow`.
  `triage_attempt_cap_per_revision` defaults to 2 queued attempts, accepts integers 1 through 5, may be overridden per repository, and fails closed to 1 when invalid.
  The global-only `triage_daily_ceiling` defaults to 1200 reservations per UTC day, accepts integers 1 through 2000, and fails closed to 0 when invalid; one reservation authorizes one triage run whose bounded schema repair permits at most two model calls.
  The finite default keeps approved operator replay paced by reviewable waves rather than cost, while preserving a hard runaway-containment bound.
  Malformed, ambiguous, untrusted, duplicate, unreadable, or unverified ledger state denies reservations, and a crash or verification failure may leak capacity only in the safe direction.
  All current automatic queue writers are serialized by the `wheelhouse-backstop` concurrency group, while deep-review and natural-language decision runs remain outside this ceiling because they require deliberate owner actions and durable claims.
  The authoritative implementation and offline failure matrix are `scripts/render_card.py`, `scripts/wheelhouse_core.py`, and `tests/test_triage_budget.py`.
- **Automatic-triage replay is operator-only, exact-revision, and admission-safe.**
  `scripts/triage_replay.py` is reachable only from an owner-started `scan-backstop.yml` `workflow_dispatch` with a non-empty validated `replay_wave`; the input defaults empty and the scheduled path cannot reach it.
  Legacy candidate listings supply issue numbers only; an optional exact-card selector bypasses that discovery step, while every selected card and live target is still re-read by exact number before a marker write.
  Replay clears only a proven current `triage_status:error` non-success cache, marks an entirely absent cache, or re-enters the bounded duplicate-only cohort whose prior replay was denied before task construction.
  It writes the versioned non-material `triage_replay` marker and re-enters `reconcile.maybe_queue_auto_triage` so the existing reservation, queued checkpoint, sealed dispatch permit, and `triage.yml` admission remain authoritative.
  `docs/AGENT_RUNTIME.md` owns the operator selector, claim tombstone, duplicate-only evidence, and exact-revision admission-denial contracts.
  Malformed or mismatched state, markers, identities, revisions, labels, authorship, sources, attempt counts, or budget ledgers fail closed.
  `--dry-run` performs the same exact-number eligibility reads and reports planned actions without any GitHub write.
  The evidence-empty E7 and array-recovery G1 recoveries have separate incident-scoped attempts-reset cohorts in `scripts/triage_replay.py`.
  Each reset is admitted only for its dedicated sanctioned wave plus the explicitly supplied complete code-defined cohort.
  Every card is bound to its exact diagnosed revision and prior replay marker before its trusted count of 2 is reset to 1 for the queued write.
  A full cohort second-read preflight refuses any mismatch before a reset write, and a one-use v2 marker excludes completed members from ordinary replay while allowing only the same sanctioned cohort's pending members to resume.
  Each reset refuses policy drift away from cap 2 and leaves both the global per-revision cap and daily ceiling unchanged.
  `docs/AGENT_RUNTIME.md` owns the additive `wheelhouse-triage-record` migration record shape.
- **Accept recommendation is a deterministic shortcut, not model action.** A
  successful current auto-triage attempt for pr-review or issue-triage may
  prepend an `Accept recommendation` checkbox when the structured
  `triage_recommendation` state is fresh (`triaged_sha` equals the current
  revision) and its normalized action is in `ACCEPT_ALLOWED_BY_KIND`.
  It is never rendered for `ci-approval`, never maps to `approve-ci`, and legacy
  `recommended_next_step` Markdown is deliberately not parsed into an accept
  action.
  Actions that post text (`close`, `decline`, `comment`, `request-changes`)
  require a non-empty `recommended_reason`; missing, stale, failed, invalid,
  non-allowlisted, and non-structured recommendations no-op at parse time.
  Ticking the box maps to the existing deterministic executor action and
  `free_text`, preserving head-SHA rechecks and token boundaries; if the
  recommendation is `investigate`, it stays non-consuming and clears the clicked
  accept box.
  While a structured accept recommendation is available, render suppresses the
  top-level deterministic `### Recommended action` section so the card has one
  primary recommendation surface.
  Bare `#N` refs in `recommended_reason` are qualified against the card state's
  target repo before the reason can be posted, used as a decline/close note, or
  submitted as a request-changes review.
- **Held cards - a card is not owner-visible in its normal form until its
  first auto-triage attempt completes.** When `should_hold` says a brand-new
  pr-review/issue-triage card would have triage queued for it (same gate as
  auto triage itself: the per-kind flag AND `CLAUDE_CODE_OAUTH_TOKEN`), the
  card is created HELD instead of in its normal form: `needs-decision` STAYS
  (triage.yml's resolve step requires a pure, refreshable `needs-decision`
  card or it never runs), the `pending-triage` label (`HOLD_LABEL`) is added
  on top, and the body's "Your decision" section is a placeholder with no
  checkboxes - no `<!-- opt:* -->` markers, so it is naturally inert to the
  decision handler's checkbox/slash-command parsing; `apply_decision.py
  cmd_parse`/`cmd_nl_eligible` also short-circuit on the state block's
  `held` flag as defense in depth. `held` is a non-material state key (like
  `triaged_sha`) - never in `MATERIAL_FIELDS`, never affecting
  classify/material_changed/decision-parsing/target-execution/
  fork-CI-safety/author-filtering/conflict-routing.
  A held card is **published** - real checkboxes appear, `pending-triage` is
  removed - the moment its own auto-triage ATTEMPT completes, in the SAME
  `update_card_triage` call `triage-apply`/`triage-fail` already use: this is
  gated on the attempt COMPLETING, never on it SUCCEEDING, so a held card can
  never stay hidden because triage errored, timed out, or (a fail-open
  hardening beyond the original ask) even failed to DISPATCH -
  `reconcile.py`'s `maybe_queue_auto_triage` and the `queue-triage` CLI both
  now publish a held card immediately with a "could not be started" note if
  `dispatch_triage_workflow` itself throws, since the queued-cache write
  already landed and a later scan would never retry that revision otherwise.
  Publishing is keyed to the card's own CURRENT revision
  (`state_revision`/`triage_revision`): a stale attempt whose revision no
  longer matches (the card was refreshed to a newer revision while the
  attempt was in flight) is a no-op, because that refresh already queued a
  fresh attempt for the new revision which will publish the card itself -
  exactly mirroring how a stale triage result is already dropped for a
  published card.
  A refresh rechecks a currently held card with the same `should_hold(item, has_token)` gate used at creation.
  If the refreshed item still qualifies for auto triage, `upsert_card` preserves the placeholder and queues the fresh attempt as before.
  If the refreshed item no longer qualifies (for example the kind changed away from pr-review/issue-triage, config disabled auto triage, or the token is absent), `upsert_card` publishes it silently in that same refresh pass: normal checkboxes, no `pending-triage` label, no `held` state key, and no synthetic triage section or note.
  This keeps a held card's self-heal-close (its target left the worklist, or merged/closed) working through the SAME existing reconcile logic with no held-specific branching, since a held card is `is_refreshable` exactly like any other pure pending card.
  Config off or no token: a brand-new eligible card is created in its normal form immediately, exactly as before this feature - never held.
  **Fail-open safety net for a `triage.yml` run that never reaches its
  update step at all** (e.g. `resolve` itself throws on a transient `gh
  issue view` error before writing its outputs, which would otherwise leave
  the update step running with an EMPTY issue/revision and silently doing
  nothing - `triaged_sha` is already cached for that revision, so no future
  scan would ever retry it and the card would stay held forever): a final
  `always()` step runs `render_card.py triage-recover --issue --kind
  --revision`, sourced from the RAW `workflow_dispatch` inputs (never
  `steps.resolve.outputs`, which may be empty). It grounds against the
  card's actual CURRENT state and is a no-op unless the card is genuinely
  still held with `triage_status: queued` for exactly that revision -
  publishing it with a generic "did not finish" note only in that exact
  stuck case, so it can never double-write over a result the normal update
  step already recorded, whether that result was a success or a `triage-fail`.
  If the trusted source snapshot is unavailable, the workflow cannot safely run
  `render_card.py`; in that narrow case it clears the queued triage cache for
  the exact raw-input revision instead, so a future scan can retry rather than
  leaving the held card permanently hidden. This no-trusted-source security
  fallback covers both issue-triage and PR-review cards, stays exempt from full
  auto-merge re-evaluation because trusted code is unavailable, and visibly
  warns on the card that the checklist may temporarily reflect the prior queued
  state until trusted card maintenance resumes.
- **Bounded schema repair - one automatic retry for the model-schema-miss triage
  class (cards #551/#547), never for the excluded classes.** The TRIGGER is
  narrow and exact: a triage result that was DELIVERED (a non-empty
  `extract_claude_result`) but fails parse/normalize (`parse_triage_json` returns
  None). That is the #551 class. It is deliberately distinct from #556
  (delivered-then-dropped by the transcript cap - fixed separately) and from
  E2BIG / auth / rate-limit / infra, which all leave NO extractable result and so
  keep today's fail-open behavior byte-for-byte. An evidence-anchor failure
  (parse OK but the model's evidence spans do not anchor to `target.txt`) is ALSO
  excluded - a repair turn cannot conjure real target spans. The pure decision helpers
  are `render_card.plan_triage_repair` (should a schema-miss get a repair turn,
  and build its prompt) and `render_card.decide_triage_apply` (route the final
  apply to `success`/`repaired`/`repair-failed`/`anchor-fail`/`no-result`); both
  go through `triage_schema_reason`, which returns a purely STRUCTURAL reason
  (field name + defect type, NEVER a field value) safe to persist and display.
  Primary validation accepts `evidence` as either one non-empty string or a non-empty list of non-empty strings, flattening the latter with ` | ` before the shared anchor check.
  Anchoring recognizes single- and double-quoted spans, normalizes markdown backticks and asterisks on both sides, and conservatively considers source-prefixed list/newline fragments split at ellipses.
  The anti-fabrication boundary remains a verbatim target substring of at least 12 normalized characters for quoted spans and 20 for fallback fragments.
  A repair failure persists the actual structural failing stage, including a repaired anchor miss or duplicate repair claim, instead of reusing the original candidate reason.
  `triage.yml` performs EXACTLY ONE repair turn: after `triage-result`,
  `render_card.py triage-repair-prep` emits `repair_needed`/`reason` and (only on
  a schema-miss) a `repair_prompt` heredoc to `$GITHUB_OUTPUT`; the conditional
  `claude_repair` step runs that prompt in the SAME tokenless side-job posture as
  triage (no `FLEET_TOKEN`, no `READONLY_TOKEN`), bounded to `--max-turns 1` with
  FAIL-CLOSED zero-tool isolation (an empty `--allowedTools` is NOT a reliable
  guarantee in `claude-code-action` - upstream allowlist parsing can leak
  defaults incl. Bash - so a Claude Code `permissions.deny` rule set, which takes
  precedence over any allowlist, blocks every file/exec/network/subagent tool by
  name; the candidate is embedded in the prompt, so the turn needs no tools and
  must NOT re-read the diff or re-analyze). `repair-result` extracts the compact
  repaired result and the existing `Update the decision card` step passes it to
  `triage-apply --repair-execution-file`. The repaired output is re-validated
  through the SAME parse/normalize AND evidence-anchor guards; if it is still
  invalid the card lands on the existing visible triage-unavailable path, now
  carrying the structural validation reason.
  There is at most one repair attempt per admitted triage dispatch, so repair is structurally unable to loop; any separately sanctioned later dispatch must consume another per-revision attempt and daily reservation.
  Telemetry lives in NON-MATERIAL state keys
  `triage_repair_status` (`repaired` | `repair-failed`; absent = never attempted),
  `triage_repair_reason` (the structural failure), and `triage_repair_candidate`
  (a redacted, content-free candidate shape from `redacted_candidate_shape` -
  only allowlisted schema-field membership + an unknown-key COUNT, never a
  model-chosen key name or value) - like `triaged_sha`, never in
  `MATERIAL_FIELDS`, never affecting classify/material_changed/decision-parsing.
  The persisted diagnostics carry only structural facts, never raw target/comment
  content. See `tests/test_triage_schema_repair.py`.
- Natural-language decisions accept only owner/maintainer comments and are structured.
  `docs/AGENT_RUNTIME.md` owns the native structured-output and bounded schema-repair contract.
  `apply_decision.py nl-route` is the trust boundary - it validates `action` against the per-kind allowlist and only then
  sets the `decision` output that makes the SAME deterministic `execute` run
  (so every guard - allowlist, head-SHA re-check, fork-CI HOLD, token isolation,
  concurrency - applies unchanged). `answer`/`clarify` only post a card comment
  and leave the card open.
  The advisory `### Triage` section and hidden `triage_recommendation` state are
  removed from the trusted card context before the NL prompt is built, so a prior
  model recommendation cannot become an instruction to the intent-mapper.
  When `READONLY_TOKEN` is absent, the LLM receives
  `Read,Grep,Glob` and no GitHub credential. When the
  optional `READONLY_TOKEN` secret is present, the LLM step uses that read-only
  public-scoped token as both the action `github_token` input and shell
  `GH_TOKEN`, plus a narrow Bash allow-list for `wheelhouse-search`, which wraps
  scoped read-only `gh` lookups across the target repo and configured fleet
  repos.
  The pinned action's subprocess isolation removes the trusted job's default
  GitHub token before model execution, so only the explicitly selected
  read-only search credential can cross that boundary.
  Search output is UNTRUSTED DATA for answering questions only, never an
  instruction and never an authorization to act.
  The LLM never receives `FLEET_TOKEN` - it maps intent or answers, it never acts.
  After Claude runs, trusted code admits an `AgentResult` only through the
  documented result-validation boundary; `nl-route` and `execute` consume only that
  normalized result from a read-only trusted source copy with a scrubbed
  environment.
  An eligible primary-result failure can claim only one same-comment `nl-decision.schema-repair` task; it is tokenless, one-turn/no-tool, and strictly revalidated before any reply or action.
  A duplicate or still-invalid repair leaves the card open with a content-free, retryable failure note.
  `docs/AGENT_RUNTIME.md` owns the detailed runtime contract.
- Token discipline per step: scan/execute and the read-only target reads for the
  LLM (`triage` prepare + target-code checkout, `deep-review` prepare + its target-code checkout, decision-handler
  `nl-fetch`) use `FLEET_TOKEN`; all
  card writes - including every `issue-ops/labeler` step (its `github_token`
  defaults to `github.token`, passed explicitly here) - use `github.token`. The
  card's own comment thread is also this repo's data, so the NL `nl-comments`
  fetch uses `github.token`, NOT `FLEET_TOKEN`. Mixing them either breaks
  cross-repo acting or creates a re-trigger loop. The LLM step itself never gets
  `FLEET_TOKEN`; without `READONLY_TOKEN` it receives no shell credential or
  shell tools, and with `READONLY_TOKEN` it only gets that read credential as the
  action `github_token` input and shell `GH_TOKEN` for context search through
  `wheelhouse-search`. Target content reaches every LLM path only as delimited
  untrusted data in bounded files, while search output reaches the LLM only as
  delimited untrusted prompt data. Triage/deep-review code is already on disk from a
  `persist-credentials: false` checkout, so NO acting token is left on disk for
  the LLM to read.
  `READONLY_TOKEN` is never used by `execute`, never used by stale
  pending-contributor cleanup, and never gates or authorizes an action.
- **Investigate is a NON-CONSUMING checkbox (the one tick that doesn't close the
  card).** It is offered on pr-review/issue-triage cards (NOT ci-approval, a fast
  security gate). Ticking it must NEVER consume the card: `apply_decision.py
  parse` routes `investigate` to a separate `investigate` output and leaves
  `decision` empty, so the consuming execute/close steps stay dormant. The
  handler's Investigate step then (1) re-renders the card with the box cleared
  (`apply_decision.py clear-checkbox`, on `github.token` so the edit never
  re-triggers the handler) so the owner can investigate again after new commits,
  and (2) triggers the ONE investigation workflow (`deep-review.yml`). It triggers
  it via `workflow_dispatch` - NOT by applying the `needs-deep-review` label -
  because a `github.token`-applied label would not raise the `labeled` webhook
  (the very recursion barrier that stops the handler re-triggering itself), and
  using `FLEET_TOKEN` to label THIS repo's card would break token discipline and
  portability (a public Wheelhouse's `FLEET_TOKEN` need not even have write access
  here). `workflow_dispatch` via `github.token` IS the documented exception to
  recursion-prevention, so it reliably fires; that is why decision-handler needs
  `actions: write`. The dispatch carries the parsed `repo`/`number`/`kind`/
  `head_sha` from the tick event, and `deep-review.yml` uses those immutable
  inputs for bot-dispatched runs instead of re-reading the mutable card body.
  Owner-triggered `workflow_dispatch` can also be run with only `issue=...` for direct verification; that path fetches and parses the current card body with `github.token`.
  Every direct Claude action in the separately permissioned model workflow has `allowed_bots: github-actions[bot]`, because a bot-triggered trusted caller retains that exact actor in the reusable model job and the action otherwise rejects the run before it emits `execution_file`.
  Keep that allow-list exact - never `*` and never an external bot actor.
  The manual `needs-deep-review` label path is unchanged (a human applying it raises the `labeled` event normally) and remains a card-body parse path in `deep-review.yml`, alongside owner-triggered issue-only `workflow_dispatch` verification runs.
  This is a deliberate asymmetry: the manual label and issue-only workflow-dispatch paths authorize only the repository owner.
  A configured co-maintainer uses the Investigate checkbox, which runs through the maintainer-gated decision-handler (`wheelhouse_core.maintainers()` = owner + configured maintainer).
  `investigate` is in the
  per-kind `ALLOWED` set but is filtered out of the NL verb list/validation
  (`nl_allowed`): an investigation is a deliberate click, not free-text intent, so
  the NL path neither offers nor accepts it.
- **`/request-changes <text>` is a pr-review-only, slash-command-only,
  non-terminal action - unlike `investigate`, it IS NL-selectable.** The
  `/request_changes <text>` alias is accepted too. It submits
  a GitHub `REQUEST_CHANGES` PR review (`POST
  /repos/{owner}/{repo}/pulls/{number}/reviews` with `{"body": text, "event":
  "REQUEST_CHANGES"}`) via `apply_decision.do_request_changes`, executed on the
  same `execute`-step `FLEET_TOKEN` wiring `do_merge`/`do_comment` already use -
  no new secret, no new token scope, no new workflow step. It is slash-only
  (like `comment`; `decline` is also omitted from checkboxes so a slash command
  can carry a custom reason) because GitHub issue-form checkboxes can't carry
  free text, so it is NOT a `CHECKBOX_OPTIONS` entry in `render_card.py` - only
  `SLASH` table entries in `apply_decision.py` and a `SLASH_HINT` mention.
  It is routed through the normal
  `decision`/`cmd_execute` path (unlike `investigate`, which is routed apart via
  `NON_CONSUMING_ACTIONS`), but its terminal state is `"none"` - the same
  leave-the-card-open shape as `do_comment` - so it never closes the card.
  Like `merge`, it re-checks the PR head SHA from the card state before posting the review; if the head moved, no review is posted and the card stays pending so the next scan can refresh it to the current head.
  Because it is a normal text-bearing verb (not a meta-action like
  `investigate`), it is deliberately NOT added to `NL_EXCLUDED_ACTIONS`: it IS
  in `nl_allowed("pr-review")`, so the natural-language intent-mapper can choose
  it on its own judgment, with prompt guidance (`VERB_HELP["request-changes"]`
  in `apply_decision.build_nl_prompt`) telling it to prefer `request-changes`
  over `comment` for a blocking revision request, and over `close`/`decline`
  when the PR is salvageable and should be revised rather than rejected.
  `route_decision` requires non-empty `free_text` for `request-changes` (like
  `comment`), downgrading to `clarify` if the model omits it. Defensive-only
  additions (not new guards): `do_request_changes` checks the PR author against
  `owner` before calling the API and returns a clear error instead of a raw 422
  (GitHub rejects self-review) - belt-and-suspenders, since the queue author
  filter already excludes owner/maintainer/bot-authored PRs from ever getting a
  card; and repeated `/request-changes` calls simply post another GitHub review
  each time (allowed by the API) rather than any dismiss/supersede logic - by
  design, "one review per push cycle" is a documented convention, not enforced
  code. Security note: unlike a plain comment, a "changes requested" review can
  put the target PR into a merge-blocked state under branch-protection
  required-reviews - a real (if reversible) effect on the target repo, so this
  is the one action added to the NL-selectable set since `investigate` was
  excluded from it.
  When `pending_contributor_cleanup` is active for that repo and `pr` is an
  effective cleanup target, `do_request_changes` also arms the target PR for
  stale cleanup after the review POST succeeds.
  Arming requires a non-maintainer human target author, the current head SHA, a
  review id, and a provable `submitted_at` timestamp (reread by review id if the
  POST response omits it).
  It writes a hidden `wheelhouse-pending-contributor-action` marker comment and
  adds `wheelhouse:pending-contributor-action`.
  Any arming failure is cleanup-only: the review stays posted, the card remains
  open, and the result message says stale cleanup was not armed.
- **Stale pending-contributor cleanup is PR-only, deterministic, and fail-open.**
  The feature is default OFF in code (`pending_contributor_cleanup: false` when
  absent) but Kun's committed `wheelhouse.config.yml` opts in globally with
  `pending_contributor_cleanup_targets: ["pr"]`, a 10-day reminder, and a 14-day
  close threshold.
  Every global setting has a per-repo override, including the enable flag,
  thresholds, and targets.
  It is intentionally scoped to provable contributor-action asks:
  successful `/request-changes` reviews and merge-conflict rebase nudges.
  The ASK is the nudge, not the routing bucket.
  A conflicting fork PR in the `needs-ci-approval` ci-noop route is eligible only when the sweep can prove an existing rebase nudge.
  Routing alone never creates or proves an ask.
  Its nudge is deliberately treated through the legacy retrofit rather than requiring cleanup state, so an unarmed nudge is not orphaned.
  `_pr_conflicting_for_cleanup` gates this widened proof path with the scan's authoritative `mergeable == CONFLICTING` result, carried in the enriched scan dict.
  A `needs-ci-approval` PR may remind/close ONLY when it is authoritatively
  CONFLICTING this scan (ci-noop rebase path). A non-conflicting ci-approval
  PR is clear-only: if a pending-contributor label is present, the sweep may
  drop it after a head move or qualifying contributor activity proves a
  request-changes ask was answered (card #1537), but it never reminds, closes,
  or reclassifies the PR. UNKNOWN, MERGEABLE, and None never qualify for the
  close path.

  It never handles issue-triage, never runs from
  ingest, never runs in Claude/LLM paths, and never uses `READONLY_TOKEN`.
  The sweep runs inside `wheelhouse_core.py scan` under `FLEET_TOKEN`, before
  worklist emission, so a PR closed by cleanup is removed from the scanned open
  PR set and the addressed-issue map is recomputed without it.
  A close requires an open target PR, a non-maintainer human author, no
  `wheelhouse:keep-open` target label, an active structured marker plus
  `wheelhouse:pending-contributor-action` label (or a provable legacy rebase
  nudge), the same head SHA, a verified original ask, complete target comments,
  reviews, review-comments, timeline, and PR edit-history reads, no qualifying
  contributor activity after the ask, and a visible maintainer-authored reminder
  marker posted after the ask.
  At or after the close threshold with no reminder, it posts the reminder and
  waits for a later scan instead of closing immediately.
  Contributor comments, reviews, review comments, PR body edits, head pushes,
  and other target timeline actions after the ask stop cleanup and clear the
  active pending label.
  Maintainer and bot activity is known activity but does not reset the clock.
  A moved head also clears the active pending label.
  Any uncertainty skips: unreadable or too-large paginated endpoints, missing or
  ambiguous timestamps/authors, unaccounted target `updated_at`, untrusted
  marker authorship, an unprovable review/nudge, invalid targets, or disabled
  config all fail open.
  **Timeline `reviewed` events carry `submitted_at`, not `created_at`.**
  `_timeline_event_time` must read both keys - reading only `created_at` made
  every PR a maintainer/bot had reviewed fail open as "reviewed event missing
  timestamp", which is what kept the already-nudged fleet backlog out of the
  reminder-then-close clock and left a review-caused `updated_at` bump looking
  "unattributable". Wherever a review or `reviewed`-event read still lacks a
  usable timestamp, `_read_pr_cleanup_state` recovers it by re-reading the review
  by id (`_backfill_missing_review_times`, the same authoritative fallback
  `apply_decision.do_request_changes` uses when arming); a failed re-read leaves
  the field missing so the downstream check still fails open. A GENUINELY
  unexplained target `updated_at` (no readable activity at that time) still fails
  open - that safety net is intentional, not a bug to remove.
  Legacy `<!-- wheelhouse-rebase-nudge:<head_sha> -->` comments form the unarmed, pre-arming nudge backlog and can be retrofitted into the lifecycle.
  The retrofit requires a trusted author, the original comment timestamp/id, an unchanged head, and a cleanup-eligible conflict path.
  The first eligible pass reminds and adds the active label, and only a later pass with that reminder may close.
- NL conversation memory is owner-scoped, and the scoping IS the security
  boundary. `decision-handler.yml` fetches the card's thread (`nl-comments`,
  `github.token`) and `apply_decision.py assemble_history` renders it as a
  "Conversation so far" block of trusted context - but ONLY comments authored by
  a maintainer or by the workflow bot (`github-actions[bot]`, the assistant's own
  prior turns) survive. The maintainer set is exactly `wheelhouse_core.maintainers()`
  (repo owner + optional configured `maintainer`) - the SAME notion the
  `gate`/`authorized` path uses; do not invent a second rule. Every other author
  (a contributor, a third-party bot) is dropped ENTIRELY so unauthorized text can
  never enter the LLM's instruction context. The triggering comment is excluded
  from history by id (`github.event.comment.id`) because it is still passed
  separately as the single new instruction; the history is context only. None of
  this widens the acting trust model: optional `READONLY_TOKEN` search output is
  also untrusted reference data, the LLM still never gets `FLEET_TOKEN`, and
  `nl-route`'s allowlist re-validation is unchanged.
- `wheelhouse_core.py scan` is resilient: a repo that fails to read is reported as a
  warning (`ok:false`) and skipped, and `reconcile.py` must never close cards for
  an `ok:false` repo (state unknown).
  Open PRs, open issues, and PR closing issue references are paginated.
  If any of those pagination paths cannot complete, the repo result is marked
  `truncated` and `reconcile.py` must not self-heal close existing cards for that
  repo because state is incomplete.
  If the PR list or closing-reference scan is incomplete, `build_repo` withholds
  issue-triage cards for that repo because it cannot prove which issues are
  already addressed by open PRs.
  **Scan queries are kept small and survivable (card #411).** A large repo (~100
  open PRs) made the one-shot scan query 5xx persistently, so `build_repo`
  returned `ok:false` every scan and froze that repo's whole card slice. Page
  sizes are deliberately small - `PR_PAGE_SIZE`/`ISSUE_PAGE_SIZE` plus reduced
  nested `labels`/`closingIssuesReferences` counts (the existing cursor loop +
  `truncated` fallback carry the rest); `statusCheckRollup.contexts` stays large
  on purpose (truncating checks could hide a failing gate = a false green, the
  card #392 lesson). Every `gh api graphql` call goes through `_gh_graphql_data`,
  which retries transient 5xx/timeout (and GraphQL query-timeout `errors`) with
  exponential backoff + jitter (`_sleep` is indirected for tests); a non-transient
  error or exhausted retries still raises, preserving the `ok:false`/`truncated`
  fail-safe - retry never fabricates completeness.
  **The `build_repo` "scan failed" warning is slug-prefixed** so a dark repo is
  identifiable straight from the log (it used to carry no repo name).
- **Fleet-scan health ledger (loud signal for a persistently-dark repo).** A repo
  that fails EVERY scan hides behind an otherwise-green scheduled run.
  `scan-backstop.yml`'s final `always()` step runs `wheelhouse_core.py scan-health
  scan.json` (default `GITHUB_TOKEN` - this repo's own bookkeeping, never
  `FLEET_TOKEN`), which persists a per-repo consecutive-`ok:false` count in a
  dedicated CLOSED issue in THIS repo carrying a hidden `wheelhouse-scan-health`
  marker (found by the `wheelhouse:scan-health` label; state lives in GitHub, not
  on disk). `ok:true` resets the count; `ok:false` increments; at
  `SCAN_HEALTH_ALERT_THRESHOLD` consecutive failures (default 3, env-overridable
  via `WHEELHOUSE_SCAN_HEALTH_THRESHOLD`) it prints `::error::` per dark repo and
  exits non-zero to fail the run. It runs LAST so it never skips reconcile, and
  fails OPEN on any ledger I/O or missing scan.json (bookkeeping must never turn a
  scan red on its own hiccup). Pure helpers
  `parse_scan_health`/`update_scan_health`/`render_scan_health_body` are
  unit-tested; unscanned repos are carried forward and never alert.
- **Queue author filter.**
  Decision cards are for other people's work, so `build_repo` suppresses cards for PRs and issues authored by the canonical maintainer set (`wheelhouse_core.maintainers()` = repo owner plus optional configured `maintainer`) or by bots.
  Bot detection uses the GraphQL `author.__typename == "Bot"` signal plus the `*[bot]` login suffix fallback.
  Missing or unreadable author metadata fails open, so an unknown author can still raise a card rather than silently dropping a human contributor's work.
  The author filter suppresses card emission only; for fork PRs in `needs-ci-approval`, the normal safety-gated auto-approve/noop path still runs first so safe owner, maintainer, and bot CI runs do not hang awaiting approval.
  This deliberately bypasses the global or per-repo `auto_approve_ci: false` opt-out only for those author-excluded ci-approval PRs; contributor PRs still honor the opt-out and card as before.
  Unsafe, uncertain, or failed owner, maintainer, and bot CI-approval targets still do not emit cards, but they keep the scan-log warning.
  Skipped targets still remain in `open_pr_numbers` / `open_issue_numbers` but are absent from the `items` worklist, so `reconcile.py` advances the fixed two-scan soft-close lifecycle for any existing pure `needs-decision` owner, maintainer, or bot card on each qualifying scan.
- **UNKNOWN mergeability is an EXPECTED PENDING STATE, never a classifiable
  answer (the lavish-axi#111 duplicate-card fix).** GitHub computes PR
  mergeability LAZILY: a push to the base branch invalidates every open PR's
  cached mergeability to `UNKNOWN`, and nothing recomputes it until the PR is
  queried - the first query returns `UNKNOWN` and merely TRIGGERS the async
  compute, which settles within seconds-to-a-minute (confirmed live: a fleet repo
  went 32 `UNKNOWN` -> 0 within ~2 min of being queried; matches GitHub's
  documented REST "mergeable is null until computed, poll until non-null"
  contract). If the hourly scan is the only regular requester, an un-polled
  `UNKNOWN` makes a statically-conflicting PR fail open to `merge-ready` (a new
  card) then settle to `CONFLICTING` (`needs-rebase`, card soft-closed) - one
  create/close oscillation per base push (10 duplicate cards for #111). So a
  `merge-ready` or `review-needed` candidate whose `mergeable` reads exactly
  `UNKNOWN` is polled with short backoff (`_resolve_pr_bucket` -> `_settle_mergeable` ->
  `gh_graphql_pr_mergeable`; the first read triggers the compute, later reads
  catch it) until it resolves: `CONFLICTING` -> `needs-rebase` (out, nudged), a
  non-conflicting value -> its original worklist bucket (in). If it still can't
  be settled within the budget it returns the `MERGEABILITY_PENDING` sentinel and
  `build_repo` reports the PR in `indeterminate_pr_numbers` (kept in `open_pr_numbers`, emits NO
  worklist item), and `reconcile.py` FREEZES that card - the **hard invariant is
  that an UNKNOWN reading must NEVER flip worklist membership or create/close/
  consume a card** (in stays in, out stays out). The reconcile freeze is a
  per-PR extension of the existing `ok:false`/`truncated` unreadable-state skip.
  It does not close the card or rewrite its absence state, but its intervening
  scheduled workflow run breaks the run-number adjacency required by the fixed-K
  soft-close gate in `reconcile.py` (lifecycle coverage lives in
  `tests/test_reconcile.py`).
  A settlement query error that does not recover to a conclusive answer marks
  the repo `ok:false` so its warning reaches the scan-health ledger while the
  same freeze remains in effect.
  Only an explicit `UNKNOWN` is polled; a missing/None value keeps classify's
  fail-open (GraphQL never returns null for an open PR's `mergeable`).
- **Approve safe fork CI, wait for terminal checks, then classify.**
  `build_repo` reports freshly approved and `ci-running` contributor PRs in `ci_wait_pr_numbers`, and reconcile freezes their existing `PR_KINDS` cards against self-heal consumption just like `indeterminate_pr_numbers`.
  Author-excluded, unsafe or unverifiable, and verified-noop PRs are not frozen; approval eligibility remains the shared `ci_safety` verdict plus head verification.
  `ci_wait_refresh_items` are refresh-only `pr-review` invalidation candidates: reconcile may use one to update an existing same-kind pure `needs-decision` card, but must never create a card or queue triage for that transient revision.
  The bulk scan attaches `wheelhouse.target-observation/v1`; every attempted approval also emits a head-bound `wheelhouse.target-action-receipt/v1`. Approved or uncertain effects invalidate approval phase, check phase, comp, tests, and bucket. The provisional item therefore never repeats `needs-ci-approval` after a successful receipt.
  Before any existing CI-wait card write, reconcile re-reads the exact PR under `WHEELHOUSE_FLEET_TOKEN` through `wheelhouse_core.observe_exact_pr`, which uses the same complete-context `check_status` reduction, independent action-required enumeration, and normal classifier as scan. It restores the default card token before `render_card.upsert_card`; card snapshot CAS remains unchanged.
  A complete terminal reread uses normal classification; a complete non-terminal reread projects `ci-running` with an as-of time. Incomplete, failed, ambiguous, or expected-head-mismatched reads project explicit `ci-state-unknown`/unknown values against the actually observed head and never claim current green or current approval-needed state. A successful same-head approval receipt can never project `needs-ci-approval`.
  The rendered card persists `wheelhouse.card-projection-ref/v1` and visible current/pending/unknown as-of wording. Terminal checks observed on the next bulk scan still release the freeze and resume normal triage/lifecycle handling.
  Wheelhouse can first observe a target head change through the best-effort hourly `scan-backstop` or an optional source-repo `repository_dispatch`; neither is guaranteed before the next scan.
  At or after the first successful observation-driven refresh, a card cannot display the old head as current; before then, and after a failed corrective refresh, acting paths remain safe because the decision-handler rechecks the pinned head SHA.
  See `tests/test_ci_autoapprove.py` for receipts/emission, `tests/test_target_observation.py` for contracts/planning, `tests/test_target_reconcile_transaction.py` for production-composed timed transitions, and `tests/test_reconcile.py` for refresh, freeze, and release coverage.
- **Merge conflicts leave the maintainer queue.**
  `wheelhouse_core.py` fetches GraphQL `pullRequests.nodes.mergeable` and treats only `CONFLICTING` as authoritative.
  An explicit `UNKNOWN` is a pending value - see the UNKNOWN-pending bullet above for the merge-ready/review-needed settlement poll and membership freeze that prevent oscillation.
  A missing value alone still fails open, so the PR classifies normally until a later scan can prove a conflict.
  A conflicting PR that would otherwise route to `merge-ready` or `review-needed` becomes waiting-on-contributor `needs-rebase`, which is intentionally absent from `NEEDS_MAINTAINER`.
  This never rewrites `needs-ci-approval`: fork CI approval is independent of whether the eventual merge would conflict, and issue triage is unrelated.
  On the `ok:true` scan path, `build_repo` posts a contributor nudge under `FLEET_TOKEN` for non-owner/non-maintainer/non-bot `needs-rebase` PRs.
  The nudge body carries hidden marker `<!-- wheelhouse-rebase-nudge:<head_sha> -->`; before posting, Wheelhouse paginates the PR comments and skips if that marker already exists, so it posts at most once per conflicted head SHA and can nudge again only after a new push creates a new head.
  If stale pending-contributor cleanup is active for PRs, a newly posted
  `needs-rebase` nudge is
  patched with a structured `wheelhouse-pending-contributor-action` marker and
  the target gets `wheelhouse:pending-contributor-action`; if the nudge comment
  response lacks a comment id or timestamp, the cleanup arming fails open with a
  warning and the nudge remains posted.
  If comment lookup or posting fails, the scan logs a warning and still emits no card; it never posts without first checking for the current marker.
  The PR stays in `open_pr_numbers` but drops out of `items`, so `reconcile.py` advances any existing pure `needs-decision` card through the fixed soft-close lifecycle documented in the state-block contract above.
- **Scan-time fork-CI auto-approve (kill the routine "approve CI" click).** One
  shared `ci_safety(slug, pr, repo_posture)` verdict is the single security
  definition; `approve_ci` uses it too, so the auto path is a STRICT SUBSET of the
  manual gate. The verdict combines (a) **risky files** (`_risky_ci_files`: the
  PR touches `.github/workflows`/`.github/actions`/`action.yml(.yaml)` - the
  pwn-request HOLD, unchanged, fails closed) and (b) the per-repo
  **`pull_request_target` posture** (`repo_pr_target_posture`: read the DEFAULT
  branch's `.github/workflows/*.yml|*.yaml` ONCE per repo - never per PR - and
  see whether any workflow triggers on `pull_request_target`; fails closed if the
  workflows can't be read/parsed). Any PR whose base ref is not the repo default
  branch fails closed (posture-present, never auto-approved). A
  `pull_request_target` workflow that ALSO
  checks out the PR head (`_checks_out_pr_head`) is flagged LOUDLY as the exploit
  pattern (best-effort - parses jobs/steps; note the YAML 1.1 gotcha where the
  bare `on:` key parses as boolean `True`, handled in `_on_triggers`). In
  `build_repo` (the `FLEET_TOKEN` scan context), for each fork
  `needs-ci-approval` PR: if the verdict is `safe` (no risky files, no posture,
  no read error) and auto-approve is enabled or the author is excluded as
  owner/maintainer/bot, call `approve_ci`; `approved` and verified `noop` both emit NO card
  (log a `::notice::` to stderr - never stdout, which carries scan.json), while
  `hold`/`error`/throw fall back to a `ci-approval` card carrying the safety
  warning for contributor-authored PRs.
  Otherwise emit the `ci-approval` card exactly as before for contributor-authored
  PRs, carrying the safety warning.
  **Fail closed everywhere**: an unsafe verdict, a `hold`/`error` from the approve,
  or an approve that throws all fall back to a card for contributor-authored PRs;
  owner, maintainer, and bot-authored PRs are not approved and instead log
  `suppressed-card` with no decision card.
  `ci-approval` is fork-only: same-repo PRs with no CI signal route to
  `review-needed`, while unknown fork status fails safe by raising a manual
  `ci-approval` card with no auto-approve attempt for contributor-authored PRs
  and by logging `suppressed-card` for owner, maintainer, and bot-authored PRs.
  The exact-current-head discovery and per-run approval contract lives in [README.md's Security notes](README.md#security-notes); `tests/test_ci_autoapprove.py` guards the masked-context, duplicate-run, and unchanged safety-boundary regressions.
  An `approve_ci` `noop` is a verified "nothing awaiting approval" state, so the
  scan emits no worklist item and reconcile starts the fixed soft-close lifecycle documented in the state-block contract above for any stale card; if a real
  pending run appears on a later scan, the normal approve/card/suppressed-card
  path runs again.
  **Exception for a conflicted fork with no CI:** `needs-ci-approval` is never
  rewritten to `needs-rebase` (classification stays independent of mergeability),
  so a noop-consumed fork PR with null `statusCheckRollup` would otherwise
  drop silently with neither a card nor a rebase nudge. When the handled path is
  specifically `approve_ci` `noop` AND settled mergeability is `CONFLICTING`,
  `build_repo` posts the same fire-once-per-head contributor rebase nudge the
  `needs-rebase` path uses, then still emits no card. An `approved` path (PR
  actually has workflows) is unchanged; `UNKNOWN` mergeability is settled before
  the nudge and never nudges until conclusive `CONFLICTING`; missing mergeable
  fails open (no nudge). A settlement-query error marks the repo scan unhealthy,
  so reconcile preserves existing cards instead. This exception deliberately
  passes `arm_cleanup=False`, so it never arms pending-contributor cleanup.
  Fork-originated `action_required` workflow runs are expected to have an empty `workflow_run.pull_requests` list, so `approve_ci` verifies that fork case with the already-filtered run's exact `head_sha` plus `head_branch`; non-empty `pull_requests` stays strict and must contain exactly the target PR.
  **Observability (every outcome is logged, never silent).** `_auto_approve_or_card`
  returns `(handled, card_note, log_note, approve_status)` and `build_repo` emits exactly ONE
  stderr line per `needs-ci-approval` PR the auto path handles: a `::notice::`
  when approved or verified no-op, else a `::warning::wheelhouse auto-approve
  carded <repo>#<pr>: <log_note>` for contributor-authored PRs or
  `::warning::wheelhouse auto-approve suppressed-card <repo>#<pr>: <log_note>`
  for owner, maintainer, and bot-authored PRs.
  The `log_note` always carries the `ci_safety` verdict `reason` and, when an approve was attempted, the
  `approve_ci` `status` + `message` (e.g. `error: <gh stderr>`, `hold`), so a real
  approve failure that used to be swallowed into the card body is now visible in
  the scan-step log - the next `scan-backstop` run shows exactly why each
  safe-looking PR was not approved. Unknown fork status is logged as a carded or
  suppressed-card warning with its uncertainty reason before safety is attempted.
  This is logging only: it never changes the verdict, the approve/card decision,
  token usage, or fail-closed behavior, the `card_note` going into
  `item["warning"]` for emitted cards is unchanged, and the line is gh
  stderr/status text, never a secret value.
  Idempotent by construction: once approved the next scan sees CI running/results
  (not `needs-ci-approval`), so it is not re-approved; a later push that adds a
  workflow file or flips the posture routes contributor-authored PRs back to a
  card and owner, maintainer, or bot-authored PRs to `suppressed-card`.
  The auto path
  runs ONLY on the `ok:true` success path of `build_repo` (an `ok:false` repo
  returns early), so an unknown-state repo is never auto-approved - the same
  invariant that bars closing its cards. Token discipline holds: the approve is a
  cross-repo write under `FLEET_TOKEN` (where scan already runs); the "no card"
  path performs no card write at all, and cards are still written later by
  `reconcile.py` under `GITHUB_TOKEN`. **Manual-path asymmetry:** risky files ->
  HARD HOLD (exit 4), unchanged; a `pull_request_target` posture does NOT
  hard-block the manual approve (`_approve_warning_suffix` only WARNS, because the
  `pull_request_target` run fires automatically with secrets regardless of this
  approval - blocking would only withhold the harmless read-only `pull_request`
  run). **Honest caveat (document, don't overclaim):** the approval gate covers
  the fork `pull_request` run; `pull_request_target` runs are NOT gated by it, so
  the posture check is a "don't silently auto-clear + make me aware" signal plus
  the loud exploit flag, not a direct block of that vector. **Config:**
  `auto_approve_ci` defaults to **`true`** when absent (so a fresh fork gets the
  noise reduction; set `false` to restore click-to-approve-everything), and a
  per-repo `auto_approve_ci: false` on any `repos:` entry overrides the global
  (`_auto_approve_enabled`). The warning is display-only (not a material refresh
  field), since a ci-approval card's existence/refresh is already driven by the
  PR's own head_sha/comp/tests.
- **CI-approval security summary (context only - the pwn-request HOLD stays).**
  A fork PR touching CI-execution files still HOLDS for manual review, unchanged
  (`ci_safety`/`approve_ci`, exit 4). `build_repo` additionally attaches a
  deterministic, read-only security summary of ONLY the changed workflow/action
  files (`wheelhouse_core.ci_security_summary` via `_attach_ci_security_summary`)
  to the emitted contributor `ci-approval` card, rendered by
  `render_card._security_review_section` as `### Security review (advisory)`.
  It surfaces the captain's categories - trigger changes (esp.
  `pull_request_target`), `permissions:` write grants, referenced secret NAMES /
  `secrets: inherit`, checkout source/ref choices (PR-head = pwn-request),
  third-party action pinning, and run-step contributor-code execution - reusing
  the existing YAML-parse helpers (`_on_triggers`, `_checks_out_pr_head`,
  `_risky_ci_files`). It reads the PR-head version of each changed file
  (`_fetch_file_text` at the head SHA via the BASE repo's contents API, which
  works for fork PR heads). **Presentation ONLY, hard invariants:** it NEVER
  approves, NEVER writes to the target, NEVER touches the hold/owner-gate/posture
  logic/classification; it reports only structured facts (names/refs), never
  verbatim file lines, so no secret VALUE can leak, and every contributor-derived
  value is code-wrapped (`_safe_inline`) so it cannot break out of the card's
  markdown. It FAILS CLOSED: any read/parse failure yields
  `CI_SUMMARY_UNANALYZABLE` ("review the diff manually") and NEVER raises, so the
  card still holds. The rendered `security_summary` string is a non-material display field (like `warning`): it never enters `MATERIAL_FIELDS`, while the state block carries only non-material cache metadata (head SHA, base-diff revision, summary format version, and whether a section is present).
  The scan cache reuses that rendered section only when its validated card labels, head SHA, and `[base_ref,base_sha]` diff revision still match, so a base move invalidates it even if the PR head does not change.
  `scan-backstop` reads this cache under the default card token before the cross-repo scan; a card-list or cache-read failure fails open to an empty cache and re-analyzes instead of skipping the fleet scan.
  A cache metadata mismatch triggers a pure-card refresh through `security_summary_stale`; existing cards otherwise pick up the section once via the `CARD_RENDER_VERSION` 5 -> 6 bump.
  It runs on the `ok:true` success path only, and only for contributor-authored HOLD cards (owner/maintainer/bot ci-approval PRs are approved or suppressed with no card, so the summarizer is never consulted).
  See `tests/test_ci_security_summary.py`.
- **Scan-time auto-merge (V1) is a STRICT SUBSET of the manual merge gate, built
  on the same scan-time safe-action architecture as fork-CI auto-approve.** All
  the logic lives in `scripts/auto_merge.py` (deterministic gates, the act
  executor, the durable ledger, and the resolved-card recorder); the merge itself
  reuses `apply_decision.do_merge` unchanged. The SHIPPED CODE DEFAULT is OFF:
  `wheelhouse_core._auto_merge_enabled` returns false when the `auto_merge` key is
  absent; that absent-key fallback is the fork-and-own product default. A repo
  auto-merges nothing until it BOTH sets `auto_merge: true` (global or per-repo)
  AND commits a `VISION.md`
  on its DEFAULT branch (the alignment rubric doubles as the opt-in signal). THIS
  repository's committed `wheelhouse.config.yml` sets the GLOBAL
  `auto_merge: true`, so forks of this repository inherit the fleet-wide switch
  on and a committed default-branch `VISION.md` is the practical per-repo opt-in.
  The absent-key code fallback stays false - only this repository's
  committed config value flipped; a per-repo `auto_merge: false` opts one repo
  back out. A
  merge-ready `pr-review` candidate is merged only when EVERY gate passes:
  G0 repo opted-in + VISION.md present; G1 a pure `needs-decision` pr-review card
  (not held); G2 the PR touches none of the unconditional exclusions
  (`_auto_merge_exclusions` - a strict SUPERSET of `_risky_ci_files` covering
  workflow/action, governance, release, dependency/supply-chain,
  security/auth/credential, billing, migration, persistence/schema,
  install/bootstrap/build, public-default, and VISION.md itself); G3 the author is
  a non-bot non-maintainer with >=1 prior merged PR in the same repo
  (captain-fixed returning-contributor definition - no revert/quality history);
  G4 live `mergeable == True` AND `mergeable_state == 'clean'` (the REST twins of
  MERGEABLE/CLEAN; anything else fails closed); G5 blast radius <=20 changed files
  AND <=1000 total changed lines (captain-fixed caps, exact boundary passes);
  G6 a fresh structured `automerge_verdict` for the CURRENT head SHA
  (`verdict_eligible`) assigning an eligible A/B/C class, confirming vision
  alignment, ruling out an ineligible existing/default behavior change, and
  recommending merge (class C also requires an explicit strictly-opt-in + default-off flag).
  The complete scan must also prove that no other open PR closes an issue closed by the candidate: `same_closing_issue_overlap` carries the existing `_overlap_note` result into eligibility, and a missing, malformed, or non-empty fact holds before claim or act.
  G7 is an immediate live re-check of head SHA, base SHA, default-branch VISION.md SHA, mergeable, clean state, configured compliance/test contexts, and same-closing-issue overlap right before `do_merge`.
  The overlap re-read uses `wheelhouse_core.same_closing_issue_overlap`, which strictly re-lists every open PR and reuses `_closing_issue_numbers`, `_closing_map`, and `_overlap_note`; any unreadable, incomplete, malformed, duplicate, or raced result holds in the final `do_merge` guard.
  Any missing/stale/malformed/uncertain/unreadable input HOLDS for human review (fail-closed), and an ok:false / truncated / `indeterminate_pr_numbers` repo is frozen exactly like reconcile.
  `evaluate_candidate` returns one ordered structured criterion result using the stable schema in `automerge_criteria.py`; action decisions consume those same facts, while `collect_card_criteria` runs a read-only full evaluation for rendering.
  `scan-backstop` passes that head-bound result through `automerge.json` to `reconcile.py`, and `render_card.py` shows every row as `MET`, `UNMET`, or `UNAVAILABLE` with concise evidence.
  Every PR-review triage body mutation other than the no-trusted-source security fallback (queued, deferred, cleared, failed, or completed) goes through `render_card._atomic_automerge_card_body`: it applies the triage state first, calls the code-owned `evaluate_candidate` exactly once with that candidate body as `card_entry` under the fleet read token, projects the visible and frozen criteria from that result, restores the default card token, and performs one card-body write. The fallback runs only when the trusted source is unavailable, so it cannot load code or perform fleet reads; it clears the queued cache under the default card token and renders a visible `### Triage` security-fallback warning that any temporarily stale criteria reflect the prior queued state until trusted card maintenance resumes.
  Criterion state is non-material and never read as authorization; action mode always re-evaluates fail-fast with an exclusive card claim, and G7 plus the unchanged `do_merge` workflow-touch gate still run immediately before merge.
  Missing historical criterion data renders every row explicitly unavailable, while a changed fresh result triggers only a display refresh through `automerge_criteria_stale`.
  A final `apply_decision._workflow_merge_gate` result of exactly `history-only-workflow-touch` after G2's complete clean net diff creates the separate NON-MATERIAL `automerge_workflow_hold` record and `wheelhouse:manual-merge-required` label/section for that head.
  The matching hold makes claim skip before any processing label, displays G7 as `UNMET`, and is preserved by same-head card maintenance and trusted soft-close reuse; a normal authoritative new-head or incompatible-kind refresh drops it with stale triage/verdict state.
  This is denial-only and remains refreshable, never generic `blocked`: `do_merge` still performs the authoritative history read for every actual merge, while unreadable/incomplete or net-diff workflow cases retain their existing generic fail-closed paths and never establish this proven-history hold.
  Hold body/label writes use only the default card token, are verified before audit-intent cleanup or claim release, and a failed persistence leaves the exclusive claim plus `final_gate_pending` audit intent for deterministic retry rather than a pure hourly reclaim loop.
  See `tests/test_automerge_card_ui.py` for the full positive/negative matrix, axi#96 lifecycle shape, old-card compatibility, and the guarantee that forged displayed `MET` rows cannot grant eligibility.
  See `tests/test_automerge_workflow_hold.py` for the two-hour no-reclaim lifecycle, head changes, persistence recovery, token boundaries, and card reuse/close behavior.
  A target repository without GitHub's "require branches to be up to date" branch protection has an irreducible sub-second window between those final GETs and GitHub's merge PUT: GitHub's merge API accepts no base-SHA precondition.
  That residual risk is bounded by the final CLEAN state, green configured checks, blast-radius caps, and unconditional exclusions.
  Enabling "require branches to be up to date" branch protection, or using a merge queue, closes it server-side because GitHub's `mergeStateStatus` becomes `BEHIND` while auto-merge requires a CLEAN merge state.
  The behavior verdict is PRODUCED by extending the pr-review triage.
  For every complete immutable diff, `triage.yml` asks for the VISION-independent behavior class, existing/default-behavior-change, and class-C opt-in/default-off facts.
  It fetches base-branch VISION.md through the contents API with NO `?ref`, never the PR head, so a PR editing VISION.md cannot bless itself, and VISION.md is also a G2 exclusion.
  Only when that trusted policy exists does triage additionally ask for alignment and the final merge recommendation; `render_card.normalize_automerge_verdict` parses the independent core plus that optional all-or-nothing extension.
  The result is PERSISTED as the
  NON-MATERIAL `automerge_verdict` state key alongside `triage_recommendation`
  (never in `MATERIAL_FIELDS`, cleared on any failed/stale attempt, carried
  through same-revision refresh). Token discipline mirrors the rest of the fleet:
  the merge is a cross-repo write on FLEET_TOKEN (the "Auto-merge eligible PRs"
  step in `scan-backstop.yml`, which reads the persisted verdict from the local
  `cards.json` - no token needed to read a file), while the durable audit ledger
  (a dedicated CLOSED issue in THIS repo with the `wheelhouse-auto-merge-log`
  marker, mirroring the scan-health ledger) and the resolved decision record
  (comment + close via `render_card.close_card`) are written by the separate
  default-token "Record auto-merges" step.
  The act step uses its separate default card token only to persist a pre-merge audit intent before calling `do_merge`.
  The order is act -> record -> reconcile, and either a pre-merge intent or a staged pending audit prevents reconcile from consuming the claim until the audit has completed or a later FLEET_TOKEN pass can determine that no merge occurred.
  Kill switches (layered): the global/per-repo `auto_merge` flag, deleting a
  repo's VISION.md, removing `CLAUDE_CODE_OAUTH_TOKEN` (no verdict -> everything
  holds), and a per-PR `wheelhouse:no-auto-merge` target label
  (`NO_AUTO_MERGE_LABEL`). Wheelhouse NEVER auto-reverts. By captain override V1
  DELIBERATELY has NO open-PR file-overlap gate and NO per-contributor/per-scan
  rate cap - their absence is intentional and distinct from the required
  same-closing-issue ambiguity hold, and is asserted by
  `tests/test_auto_merge_v1.py` (which also covers every gate, A/B/C handling,
  malformed/stale verdicts, the 20-file/1000-line boundaries, base-branch-only
  VISION reads, the self-authorization exclusion, live re-checks, the audit
  ledger/resolved record, and the kill switches, all offline).
  Claim-time card identity must account for GitHub's one API-specific automation
  actor duality: REST issue rows use `github-actions[bot]`, while
  `render_card.get_card` returns `app/github-actions` from GraphQL.
  `auto_merge._canonical_card_author` may map only that exact GraphQL spelling to
  the REST spelling at the `_trusted_card_identity` boundary.
  It must not strip prefixes, fold case, or accept any other alias.
  Keep the regression fixtures in `tests/test_auto_merge_v1.py`
  `get_card`-shaped, with `author` as a `{"login": ...}` dict.
- The `repository_dispatch` event type is `wheelhouse-item`, but `ingest.yml`
  also listens for the legacy `triage-item` (`types: [wheelhouse-item,
  triage-item]`). It is a cross-repo wire contract: source repos onboarded before
  the rename still send `triage-item`, so the alias must stay until every source
  dispatcher is updated. Same idea as the state-marker back-compat - rename the
  name, keep accepting the old one.
- **Cross-repo reference qualification.** A decision card lives in THIS
  (cards) repo, but its target is a DIFFERENT repo. GitHub autolinks a bare
  `#N` to an issue/PR in whichever repo the TEXT is posted in, so any
  model-generated free text landing on a card must never contain a bare `#N`
  meant for the target - it would silently mislink to the cards repo instead.
  Every surface where model text is rendered/posted onto a card runs it
  through the one shared, deterministic `wheelhouse_core.qualify_issue_refs(text,
  owner, repo)` before display or action, which rewrites a bare GitHub-autolink `#N` to
  `owner/repo#N` (already-qualified `owner/repo#N`, full URLs, markdown-link
  URLs, and non-reference `#` uses like `GH-123`/`#123abc`/`foo#N` are left
  untouched; null-safe and idempotent). `owner` is always
  `GITHUB_REPOSITORY_OWNER` and `repo` is always the TARGET repo name from the
  card's deterministic state (`state["repo"]`) - NEVER derived from the
  model's own output, so the model cannot redirect qualification by naming a
  different repo in its text.
  For auto-triage and deep-review card text, trusted code also runs the same
  card-visible output through `render_card.label_automated_status_lines`, which
  preserves a narrow allowlist of claude-code-action harness polling/status lines
  but prefixes them with `AUTOMATED_STATUS_LABEL` as presentation metadata.
  It is deliberately line-oriented and conservative: no text is stripped, and
  action routing, owner gates, token handling, and target posting behavior are
  unchanged.
  The three live model-output surfaces: (1) auto-triage -
  `render_card.py`'s `triage_section`/`body_with_triage_result` thread
  `owner`+`state["repo"]` through before rendering the `### Triage` block, and
  label known harness status lines after qualification;
  `recommendation_for_state` plus `apply_decision._accept_recommendation` qualify
  stored `recommended_reason` text before it can drive a target comment, a
  decline/close note, or a request-changes review (the `triage-apply`/
  `triage-fail` CLI read `GITHUB_REPOSITORY_OWNER` and `triage.yml`'s
  "Update the decision card" step passes it through its `env -i` sandbox);
  (2) deep-review - the "Post the verdict on the card" step in
  `deep-review.yml` imports `render_card` and `wheelhouse_core` in its trusted
  Python heredoc, labels known harness status lines, and qualifies the extracted
  verdict with the `resolve` step's deterministic `repo` output before posting
  via `gh issue comment`; (3) NL answer/clarify -
  `apply_decision.route_decision` (the same trust-boundary function that
  validates the LLM's structured result) qualifies `out["answer"]` using the
  card's `state["repo"]` and a caller-supplied `owner` before returning, so
  `steps.route.outputs.answer` is already qualified by the time
  decision-handler.yml's "Post NL reply" step posts it - `cmd_nl_route` reads
  `GITHUB_REPOSITORY_OWNER` from env and the `route` step in
  decision-handler.yml passes it through its own `env -i` sandbox.
  The same helper also runs during `_preserve_same_revision_triage` on
  same-revision refreshes, and `label_automated_status_lines` runs there too, so
  cached pre-qualification or pre-labeling `### Triage` sections in already-open
  cards are repaired before being reinserted; this is a card-body sweep only and
  does not rewrite historical card comments.
  All three
  prompts (`triage.yml`, `deep-review.yml`, and the NL prompt in
  `apply_decision.build_nl_prompt`) also carry a defense-in-depth instruction
  telling the model to write refs as `owner/repo#N`, never bare - but the
  deterministic rewrite is the load-bearing guarantee, not the prompt. The
  merge thank-you comment posted on the TARGET repo's own PR (see
  "Contributor-facing copy") is deliberately OUT OF SCOPE - a bare `#N` there
  is correct because that comment is posted in the target repo itself.

## LLM side-jobs

All agent-assisted paths now share Agent Runtime Contract `wheelhouse.agent-runtime/v1alpha1` as the provider-portability boundary.
The contract, action schemas, pinned Codex app-server protocol, capability negotiation, canonical tools, brokers, sandbox supervisor, adapters, consumers, and tests live under `agent_runtime/`, with the trusted CLI at `scripts/agent_runtime.py` and operator runbook at `docs/AGENT_RUNTIME.md`.
Claude is the named production primary.
The two schema-repair actions resolve to the direct `claude-cli-pinned` profile, while the other eight actions remain on `claude-action-current-pinned`.
`agent_runtime/config.py` guards that exact split, and `temporary_rollback_profile` is the reviewed one-setting rollback for an explicit durable replay.
Codex CLI `0.144.0` app-server remains implemented and tested only as disabled non-target adapter evidence because the current ChatGPT Pro plus public-repository topology has no supported secure noninteractive subscription path.
No Codex secret is requested, no action targets Codex, and current selection cannot reach a Codex workflow installation path.
Provider environment overrides are rejected; secret presence cannot select a provider, model, effort, billing path, or stronger tool policy.
OpenCode with Z.AI Coding Plan is a deferred disabled candidate only; no adapter, provider call, credential request, target, fallback, or provider-specific runtime-core policy is authorized in this phase.
Fallback remains disabled.

Every spend-capable event first creates a durable default-token card claim keyed by `agent_runtime.admission.normalized_event_identity`: triage and repair bind action plus exact target revision, NL also binds the exact comment ID, and deep review also binds its normalized trigger identity.
Existing workflow concurrency serializes same-event claim creation, duplicate delivery exits before task construction, and consumers edit the claim rather than creating duplicate model-result comments.
AgentTask `idempotencyKey` is the normalized event-key hash.
Content-free `wheelhouse-agent-stage` records begin at admission and bind action, Wheelhouse source SHA, event-key hash, and execution ID when available; they never carry prompt, comment, target, search, or credential content.

The eight pinned Claude Action steps remain present and deployable behind the unified selection boundary; six serve the non-repair actions and two are the schema-repair rollback path.
One direct supervisor step serves both schema-repair actions because they share the same one-turn, no-tool profile.
They run only in the separately permissioned `claude-model.yml` reusable workflow after a trusted parent job uploads a bounded content-addressed `AgentTask` handoff.
Each local reusable-workflow call resolves from the caller's exact commit and also passes that commit as `expected_commit_sha`; the model job must observe the same `GITHUB_SHA` before hydration, checkpointing, or provider execution.
Every invocation step is conditional on the admitted adapter and action, and no provider failure can trigger a different adapter.
Those production steps share the same Claude **subscription** token from `claude setup-token`, never an Anthropic API key.
Every action step remains pinned to `anthropics/claude-code-action` `v1.0.178` at commit `af0559ee4f514d1ef21826982bed13f7edc3c35e` (Claude Code `2.1.215`, Agent SDK `0.3.215`) and passes the immutable `--model claude-sonnet-4-6` identifier.
The direct schema-repair lane pins `ubuntu-24.04`, installs and verifies the exact Bubblewrap package from `runtime.lock.json`, proves a minimal namespace before provider admission, verifies Claude CLI `2.1.215`, passes the OAuth token only through a private file into the Claude child environment, and uses the existing Bubblewrap supervisor with native structured output and zero tools.
Trusted preflight builds an immutable `AgentTask`, and the post-action bridge requires the execution transcript's observed `system/init.model` to match before it emits an atomic `AgentResult`.
Repository inputs are packaged by `agent_runtime/task_builder.py` from the exact bound Git commit (object DB + clean index/worktree), not from live filesystem shape: ordinary `100644`/`100755` blobs are included; committed relative mode `120000` links are materialized as regular bounded content (file links copy the target blob; directory links expand committed descendants under the alias path, with alias bytes/files counted); mode `160000` gitlinks are rejected; absolute/traversal/broken/cyclic/dirty/untracked links fail closed; no live symlink may reach the handoff or model workspace (post-snapshot handoff rejection stays).
A source checkout may be branch-attached because `actions/checkout@v4` checks an external repository's default branch out with `git checkout -B`; exact HEAD equality plus clean pre/post conditions bind that production issue-triage shape, while AgentTask `git.detached` describes the emitted content-addressed snapshot.
See `tests/test_agent_runtime_repo_snapshot.py`.
The model workflow has only `actions: read` and `contents: read`, receives no `FLEET_TOKEN`, and verifies the complete handoff into a fresh workspace.
Its finalizer re-verifies the handoff, normalizes action results or accepts only the direct supervisor's atomic result, enforces task/profile binding, then exposes only a bounded verified result artifact to the trusted caller-side consumer.
The reusable model job owns the task-bound execution timeout; its finalizer normalizes only a task-bound action transcript plus observed enforcement record, or accepts the direct supervisor's atomic result.
Trusted caller-side jobs retain default-token card writes and `FLEET_TOKEN` target operations outside the model boundary and accept only the verified normalized result artifact.

The shared injection model remains unchanged: only trusted workflow prompts and owner/maintainer-authored text are instructions; target content and optional search output are delimited untrusted data; and no model process receives `FLEET_TOKEN`.

- **`triage.yml` - automatic, lightweight, advisory PR-card OR issue-card context.** Triggered by `scan-backstop` / `reconcile.py` and the ingest fast path for pure `needs-decision` pr-review OR issue-triage cards whose current revision (a PR's `head_sha`, or an issue's `updated_at`) does not match `triaged_sha`; if the card is held under `pending-triage`, the update path also publishes its real checkboxes fail-open.
  pr-review is opt-out through `auto_triage`; issue-triage is opt-out through the INDEPENDENT `auto_triage_issues` - both global default true, per-repo override allowed, and both inert unless `CLAUDE_CODE_OAUTH_TOKEN` is present. Neither flag affects the other.
  For a pr-review card it checks out the target PR head read-only with `FLEET_TOKEN`, `persist-credentials: false`, and verifies the head did not move since queueing.
  For an issue-triage card it checks out the repo's DEFAULT branch read-only the same way (same substrate `deep-review.yml` uses for an issue card) - there is no head to verify.
  Both paths then run Claude with lower `--max-turns` than deep-review to produce structured `{summary, product_implications, recommended_action, recommended_reason, evidence}` context; the issue-triage prompt fetches the issue's title/body/comments (no diff), the pr-review prompt the PR title/body/diff, each with its own action set.
  **Pass-by-reference prompt (do not reinline).**
  The runner writes verified target content to bounded `target.txt`, checks out code at `target-src/`, and names those files in a small, target-size-independent prompt for Read/Grep/Glob; target content and `vision.md` must never be copied into the action `prompt:` input.
  `DIFF_COMPLETE` means the whole non-binary/LFS/submodule diff is present within the 1,500,000-byte on-disk cap; truncation fails closed with no auto-merge verdict.
  Required `evidence` is validation-only: `normalize_triage` rejects missing evidence, and `triage-apply` anchor-checks it against `target.txt`, failing open only when that file cannot be read.
  `tests/test_triage_prompt_size.py` owns the structural regression checks.
  Trusted code still renders the visible `### Triage` section, including a human-readable Recommended next step line, with `github.token`, never by Claude directly, and labels known harness polling/status transcript lines as automated status.
  When the structured action is fresh, successful, per-kind allowlisted, and has any required reason text, trusted code persists `triage_recommendation` and may add the `Accept recommendation` checkbox.
  The result is advisory until the owner/maintainer ticks that checkbox, at which point `apply_decision.py` maps it to an existing deterministic action with the same guards.
  Apart from publishing a held card's own `pending-triage` label and placeholder decision section, plus that conditional accept shortcut, it never changes classification, managed labels, merge/close/approve behavior, fork-CI safety, author filtering, or conflict routing.
  Before dispatch, the queueing path writes `triaged_sha=<current revision>` and `triage_status=queued`, so errors and timeouts fail open without retriggering the same revision on every scan.
  Existing open cards of either kind with no `triaged_sha` are intentionally stale and backfill once on the next eligible scan.
  Optional `READONLY_TOKEN` search uses the unchanged `wheelhouse-search` wrapper and remains untrusted evidence only.
  On the exact `triage.pr.search` action, the wrapper's existing bounded anonymous `public_clone` operation lets the model act as the independent reviewer for applicable trusted VISION source criteria. The structured verdict distinguishes local-only policy from external-source-dependent policy. An external-source-positive verdict must match the root-owned same-turn URL/ref/commit/manifest record and exact-file SHA-256 observations; missing, forged, failed, ambiguous, mismatched, or unobserved evidence drops the VISION-positive fields. Contributor assertions are leads, never independent evidence. This is source-only: target, cloned, and package execution remain forbidden, and requirements exceeding that capability fail closed.
  The Claude action allows only `github-actions[bot]`, never `*`, because scan/ingest dispatches use `github.token`.
  `render_card.py triage-apply`/`triage-fail` take a kind-agnostic `--revision` CLI argument (a PR's head SHA or an issue's `updated_at`), replacing the old pr-review-only `--head-sha` flag name.
  Result delivery is independent of transcript retention: `triage-result` extracts the compact final result event before applying the 262144-byte cap solely to the retained debug transcript.
  `tests/test_triage_result_delivery.py` guards this ordering and the uncapped direct extraction in `deep-review.yml`.
  When a DELIVERED result then fails parse/normalize (the #551/#547 schema-miss class - NOT a missing result), a single bounded, tokenless, no-tool `claude_repair` turn reshapes the candidate to the required schema; the repaired output is re-validated (same parse/normalize + evidence-anchor guards) or the card lands on the visible triage-unavailable error carrying the structural reason - see "Bounded schema repair" in Sharp edges and `tests/test_triage_schema_repair.py`.
- **`deep-review.yml` - ALWAYS-ON, code-grounded (no enable flag).** Triggered by ticking the **Investigate** box on a card, by the repo owner applying the `needs-deep-review` label, or by the repo owner running `workflow_dispatch` with only `issue=...` for direct verification.
  Bot-dispatched Investigate runs use the immutable target inputs passed by `decision-handler.yml`; owner issue-only runs and manual label runs parse the current card body with `github.token`.
  It checks out the TARGET's code read-only (`FLEET_TOKEN`, `persist-credentials: false`, the PR head for a review card / the default branch for an issue card) and runs Claude restricted to `--allowedTools Read,Grep,Glob` over that checkout when search is disabled - so it traces real code paths, never just the diff, and can NEVER execute the target's code.
  It uses the same pass-by-reference prompt invariant as `triage.yml`; only the bounded decision-card body remains inline.
  When `READONLY_TOKEN` is absent, this remains the production no-search path: no shell `GH_TOKEN`, no Bash tool, and `github_token: github.token`.
  When `READONLY_TOKEN` is present, Claude also uses that read-only public-scoped token as both the action `github_token` input and shell `GH_TOKEN`, plus `Write` for `search-request.json` and `Bash(wheelhouse-search)`.
  This direct in-process token exposure is deliberate; see `docs/READONLY_TOKEN_DELIVERY.md` for the accepted decision and tradeoff.
  The wrapper is still the existing `scripts/nl_readonly_search.py` install path, scoped to the target repo plus configured fleet repos, so deep-review can cross-reference related, duplicate, or superseding PRs/issues and code context.
  Search output is UNTRUSTED DATA and advisory evidence only; the model still produces only verdict text, and `FLEET_TOKEN` never reaches it.
  No deterministic downstream step reads raw model output because the trusted bridge validates the action `execution_file` and exposes only `AgentResult`.
  Claude does not write a verdict file.
  The Claude action allows only `github-actions[bot]` as a bot actor so the maintainer-gated Investigate dispatch can pass; it must not allow `*` or any external bot actor.
  Its final response is captured by the trusted bridge from the action's `execution_file`, after exact observed-model validation, by preferring the clean `type: "result"` event's `result` string and falling back to the last assistant text.
  The deterministic consumer then labels known harness polling/status transcript lines, qualifies target refs, and posts that text as a card comment with `github.token`.
  If no usable output is present, the workflow posts "Deep review ran but produced no verdict (see the workflow run logs)." and fails the run.
  The ONLY gate is `CLAUDE_CODE_OAUTH_TOKEN`: when it is ABSENT the workflow posts a one-line "Deep-review needs CLAUDE_CODE_OAUTH_TOKEN configured to run." note instead of silently no-opping.
  Manual triggering means there is no runaway-cost reason for a config flag, so the old `deep_review` flag was removed entirely - config, `load_config`, and the `deep-review-enabled` CLI.
- **`nl_decisions`** in `decision-handler.yml`: a plain-English owner/maintainer comment is
  mapped to a structured result (see Sharp edges).
  Opt-in: inert unless `nl_decisions: true` AND `CLAUDE_CODE_OAUTH_TOKEN`
  present.
  `READONLY_TOKEN` is optional.
  Both primary Claude branches pass the exact content-bound canonical `nl-decision-v1` schema through the pinned action's `--json-schema` support. The bridge prefers terminal `structured_output`; when that carrier alone is absent, it may accept the terminal `result` only after strict JSON parsing and validation against the same bound schema.
  The trusted bridge independently enforces the byte bound, schema, task binding, and downstream `nl-route` allowlist before anything can be posted or acted on; native generation and plain terminal JSON are never treated as trusted validation by themselves.
  Absent or genuinely invalid results fail closed into the one bounded `nl-decision.schema-repair` turn, which runs through the direct `claude-cli-pinned` worker on Claude Code `2.1.215` and must itself return native structured output.
  If it is absent, Claude stays in the production no-shell mode (`--allowedTools Read,Grep,Glob`), has no `GH_TOKEN`, and runs no commands. The model is never asked to hand-serialize a decision file; trusted code parses and serializes result objects.
  If it is present, Claude also uses `READONLY_TOKEN` as the action
  `github_token` input and shell `GH_TOKEN`, plus the
  `Bash(wheelhouse-search)` allow-list (tools
  `Read,Grep,Glob,Write,Bash(wheelhouse-search)`) so it can run scoped read-only
  `gh` searches across the target repo and configured fleet repos for related,
  duplicate, or superseding PRs/issues and code context.
  On the exact `nl-decision.search` and `triage.pr.search` actions only, the
  wrapper also accepts one anonymous `public_clone` request with a complete
  public HTTPS Git URL and optional safe ref. It retains
  one bounded, shallow, no-tags/no-submodules/no-LFS/no-hooks data-only clone
  under `RUNNER_TEMP` for Read/Grep/Glob. Root-owned transient provenance state
  binds exact-file digests without becoming model-writable, and the model workflow removes both the
  fixed clone root and provenance state in an `always()` step. Git receives a fresh credential-free
  environment; the authenticated `gh` owner/fleet allowlist is unchanged. See
  `docs/READONLY_TOKEN_DELIVERY.md` for the accepted DNS-rebinding residual and
  `tests/test_public_clone.py` / `tests/test_public_clone_e2e.py` for guards.
  Like `triage.yml`/`deep-review.yml`, the NL prompt is pass-by-reference:
  `nl-fetch` writes target title/body/diff to bounded on-disk `target.txt` with
  an explicit truncation marker, while `apply_decision.build_nl_prompt` only
  names the file.
  Target content must never be inlined into the Claude `prompt:`/`ALL_INPUTS`.
  If the LLM step fails before routing, the final recovery step posts one
  bounded, content-free, marker-keyed card note with `github.token` and changes
  no label, gate, or decision.
  See `tests/test_nl_prompt_size.py`.
  Because `READONLY_TOKEN` is a fine-grained, public-read PAT, it cannot answer
  `claude-code-action`'s own `GET .../collaborators/{actor}/permission`
  triggering-actor check, so that read-only branch also sets
  `allowed_non_write_users: ${{ github.event.sender.login }}` to bypass that
  check - narrowly, for the exact sender the workflow's own `steps.gate`
  (`wheelhouse_core.py authorized`) has already proven is the owner or
  configured maintainer, never `'*'`. That workflow gate remains the real
  trust boundary; the action's built-in check is redundant once it has run.
  This does not touch what token the model can act with - `github_token`/
  `GH_TOKEN` stay `READONLY_TOKEN`, so the model still cannot write anywhere.
  Do not widen `allowed_non_write_users` to `'*'` or drop the `steps.gate`
  authorization it relies on.
  The prompt carries the card's prior thread as owner-scoped conversation history
  so follow-up questions keep continuity (see the conversation-memory bullet in
  Sharp edges for the trusted-author rule).
  Deep-review uses the same wrapper under the same optional `READONLY_TOKEN`
  trust model, but only for advisory verdict context.

## Contributor-facing copy

Messages Wheelhouse posts onto **target repos** (e.g. a rebase nudge on a contributor's PR) speak naturally, like a friendly maintainer bot.
They must not name the product ("Wheelhouse") or use internal-state jargon ("maintainer queue", "resurface", bucket/kind names).
This includes pending-contributor cleanup reminders and close comments.
For a rebase nudge and its rebase-specific pending-contributor repeat reminder, lead with a visible automation disclosure (`Automated reminder:`) - `FLEET_TOKEN` posts as a human login, so without that label the message reads as a personal maintainer ping.
The initial nudge explains that the branch has a merge conflict with the base branch, asks the contributor to rebase onto or merge the latest base branch and push the resolved conflict, and says that checks will re-run and the PR will get looked at again.
The repeat reminder says the PR is still blocked, repeats the rebase and push guidance, and retains its inactivity warning.
Templates live in `wheelhouse_core._rebase_nudge_body` and the `needs-rebase` branch of `_pending_reminder_body`; both the standard conflict path and the CI-approval-noop conflict path share the nudge template.

Owner-facing decision cards and comments on **this repo's** issues are the private queue; those may keep the Wheelhouse name and internal vocabulary.

**The one sanctioned contributor `@`-mention.** `do_merge` in `apply_decision.py` posts a short, friendly thank-you comment on a fleet contributor's PR after a successful card-driven merge (checkbox `merge` or NL "merge it"), `@`-mentioning the contributor by `pr["user"]["login"]`.
This is a deliberate, narrow exception to "never `@`-mention" - that rule is about the owner's private decision cards in *this* repo, never about a comment posted on the *contributor's own* target-repo PR, where a thank-you tag is normal OSS etiquette.
It is gated by `thank_on_merge` (default true, per-repo override via `wheelhouse_core._thank_on_merge_enabled`, mirroring `auto_approve_ci`); no LLM is involved and `CLAUDE_CODE_OAUTH_TOKEN` is irrelevant to it.
The message is either the built-in default or the owner's own `thank_on_merge_message` config (an `{author}` placeholder substituted with the trusted bare login; templates include `@{author}` when they want a GitHub mention, never with untrusted target content); a per-repo message override wins over the global one (`wheelhouse_core._thank_on_merge_message`).
Owner, configured-maintainer, and bot (`*[bot]` login suffix) authors are skipped silently, as is a missing/blank author.
It runs on the same `FLEET_TOKEN` acting path as the merge itself (`_comment_target`, no new token) and strictly AFTER the `PUT .../merge` succeeds - never on already-merged/not-open/head-moved/failed-merge outcomes.
It is best-effort by construction (`_thank_contributor` swallows every exception to a `::warning::` and always leaves `do_merge`'s success result - `("Merged ...", "resolved")` - untouched): a thank-you failure must never flip a successful merge to `error`/`blocked` or trigger a retry.

## Validation

No build step.
Use the authoritative [local validation command list](CONTRIBUTING.md#local-validation).
The notes below record selected non-obvious regression coverage:

- `python tests/test_agent_runtime_contract.py`, `test_agent_runtime_capabilities.py`, `test_agent_runtime_security.py`, `test_agent_runtime_lifecycle.py`, `test_agent_runtime_consumers.py`, `test_agent_runtime_workflows.py`, `test_agent_runtime_repo_snapshot.py`, `test_agent_runtime_admission.py`, `test_agent_runtime_result_binding.py`, and `test_agent_outage_recovery_gate.py` - offline Agent Runtime Contract v1 coverage for strict schemas and hashing, fail-before-spend negotiation, the pinned disabled Codex app-server protocol evidence, exact typed tools and external sandbox boundary, redaction, cancellation and process-group cleanup, malformed/missing/delivered results, all action-profile consumers, explicit Claude production selection wiring with fallback disabled, exact event admission and result/event binding, the complete provider-free eight-path ZIP/fresh-interpreter recovery gate, hosted artifact preservation of signed hidden paths, and Git-object-oracle repository packaging (committed relative symlinks materialize as regular content; absolute/traversal/dirty/gitlink denials; Firstmate-style `CLAUDE.md` / `.claude/skills` fixtures).
- `python tests/test_decision.py` - mocks the LLM, no network, and also covers the non-consuming investigate routing, allow-set, `clear_checkbox`, the pre-merge workflow-touch gate (it inspects net-diff + history `.github/workflows/**`, checks both sides of a rename, returns terminal `blocked` with manual UI-merge guidance, fails closed on incomplete reads, and does not Workflows-gate action.yml), the `thank_on_merge` post-merge thank-you (config on/off, per-repo override, owner/maintainer/bot skip, custom-message substitution, best-effort swallow, and every non-success merge outcome posting none), that `route_decision` qualifies bare cross-repo refs in `answer`/`clarify` replies using `STATE["repo"]` + owner, never the model's own text, and that a HELD card (render_card.py "Held cards") is inert to `cmd_parse` (checkbox tick and slash-command alike) and `cmd_nl_eligible`, while the identical card once published is actionable again. Also covers `request-changes`: it is pr-review-only in `ALLOWED` (not ci-approval/issue-triage) and, unlike `investigate`, IS in `nl_allowed`; `/request-changes <text>` and its `/request_changes` alias slash-parse to the action with the text as free_text (and parse to nothing without text, or when the card's kind doesn't allow it); the `decision:request-changes` label path is ignored because labels cannot carry review text; `route_decision` drives `execute` for a well-formed request-changes action, downgrades to `clarify` when `free_text` is missing or the kind disallows it, and the built NL prompt lists `request-changes` with its judgment guidance for pr-review only; and `do_request_changes` (mocked `gh_rest`) posts exactly one `POST .../pulls/{n}/reviews` with `{"body": text, "event": "REQUEST_CHANGES"}` and a `"none"` (card-stays-open) terminal state, refuses with a clear error (no API call) when the PR author is the repo owner, rejects blank review text before any API call, surfaces a raw API failure as an `"error"` terminal state, and only arms pending-contributor cleanup when config/targets allow it and the target author is a non-maintainer human.
- `python tests/test_nl_decisions_search.py` - offline YAML wiring checks for the optional READONLY_TOKEN search path, scoped actor-check bypass, token isolation, prompt gating, unchanged `nl-route`/`execute` boundary, the `GITHUB_REPOSITORY_OWNER` threading into the `route` step's `env -i` sandbox, the NL prompt's cross-repo-qualification instruction, and that `route_decision` qualification is driven by deterministic state rather than model-claimed repos.
- `python tests/test_nl_schema_repair.py` - native-first NL structured output, schema-valid terminal-result admission, and bounded direct repair, no network: exact canonical `nl-decision-v1` schema binding through the pinned action; trusted acceptance of one terminal `structured_output`; missing, multiple, invalid, and dishonest-native-success denial paths; the malformed `\`-before-backtick regression; bounded, tokenless, one-turn, no-tool repair; success-on-repair routing to a real answer; still-invalid repair denial with precise retryable projection; and static workflow proof that neither runtime branch can recurse into another repair attempt.
- `python tests/test_card_refresh.py` - the card-refresh change-detection, activity-reflection, refreshability-guard, and label-replace logic, pure functions, no network; also covers the `CARD_RENDER_VERSION` 1 -> 2 retroactive triage-ref-qualification propagation and current version stamp: a render-version-behind card with a bare-ref cached `### Triage` section gets it qualified and stamped with the current `render_version` on the next refresh, a render-version-behind card with an older cached automated harness status line gets it labeled exactly once, a card already at the current version with already-qualified triage is a full no-op unless target activity advances, already-qualified refs/URLs/markdown links/non-ref `#` uses in the preserved section are left untouched, and qualification is driven by `GITHUB_REPOSITORY_OWNER` + the card's own state repo rather than the item or model text.
- `python tests/test_target_observation.py` - pure versioned target-observation/action-receipt/projection-reference contracts, tamper-evident identities, approval invalidation effects, and current/pending/unknown projection planning.
- `python tests/test_target_reconcile_transaction.py` - production-composed timed fork-CI regression through `build_repo`, shared exact observer/check reduction/classifier, real reconcile/upsert/render, and the in-memory card boundary: same-scan terminal completion, still-pending current head, incomplete context list, force-push mismatch, persisted as-of identity, and fleet/card token restoration.
- `python tests/test_reconcile.py` - reconcile routing, target-activity state-only reflection, fixed-K adjacent-run soft-close lifecycle/provenance, hard-close and stale-snapshot race safety, and stale-card self-healing, no network. Also covers the #551 approve/wait freeze: a `ci_wait_pr_numbers` PR-kind card is FROZEN (never consumed), its stale-head display is exact-reread and refreshed to pending or explicit unknown via `ci_wait_refresh_items` (anti-masquerade) without ever creating a card, the refresh is a no-op once the semantic projection is already current (no churn), and the intervening workflow run invalidates any prior absence streak before terminal checks release the freeze.
- `python tests/test_card_reuse.py` - deterministic end-to-end coverage through the actual reconcile, renderer/upsert, triage, decision, criteria, and trusted auto-merge indexing modules with an in-memory GitHub boundary: same/new-head and CI-to-PR reuse, strict provenance/actor/identity exclusions, legacy behavior, complete pagination, mutation races, partial failures, global lifecycle serialization, post-open uniqueness rollback, unchanged auto-merge duplicate denial, and the full two-absence waiting/re-entry lifecycle.
- `python tests/test_merge_conflict.py` - mergeability fail-open vs CONFLICTING routing, idempotent rebase nudges, the contributor-fork CI-noop conflict-nudge exception (including UNKNOWN/error no-nudge and no cleanup arming), author-filter nudge skips, optional pending-contributor cleanup arming for normal `needs-rebase` nudges, and reconcile self-healing for conflicted PR cards, no network.
- `python tests/test_ci_autoapprove.py` - the shared `ci_safety` verdict, `pull_request_target` posture detection, and the auto-approve-vs-card routing plus scan-log observability in `build_repo`, all with the network-touching helpers stubbed.
  It covers the completed-context plus separate `action_required` masking regression, fail-closed pending-run discovery, the unchanged draft/fork/author boundaries, and approval of every verified same-workflow duplicate run.
  It also asserts that the risky-file HOLD still short-circuits before run listing or approval, the advisory security summary is attached only to a carded risky contributor PR, and the #551 approve/wait freeze remains limited to freshly approved or running CI.
- `python tests/test_ci_security_summary.py` - the advisory read-only CI-approval security summary (`ci_security_summary`), no network: the HOLD stays effective and the summary CANNOT act (every gh call is a read, `approve_ci` is never invoked); risky patterns are surfaced (`pull_request_target` + PR-head checkout, write permissions, `secrets: inherit`, referenced secret NAMES, unpinned third-party actions) while SHA-pinned actions and benign first-party workflows raise no flags; secret VALUES / verbatim file lines are never echoed and contributor values are sanitized against markdown breakout; it fails closed (unreadable/incomplete file lists and unreadable/unparseable files -> a manual-review note, never raises); composite `action.yml` files are analyzed; and the render side scopes `### Security review (advisory)` to ci-approval, frames it advisory/untrusted, keeps `security_summary` out of the state block, and never triggers a material refresh.
- `python tests/test_check_status.py` - direct, offline unit coverage for the `check_status()` aggregation invariant above, the rollup fail-closed backstop, genuinely green PRs, and card #543's disjoint axi test signals.
- `python tests/test_author_filter.py` - queue author filtering across PR review, CI approval, and issue triage, PR target `updatedAt` propagation for activity sorting, cleanup-closed PR removal before addressed-issue recomputation, plus open-issue/PR/closing-reference pagination guards, no network.
- `python tests/test_pending_contributor_cleanup.py` - offline coverage for the deterministic, fail-open cleanup invariant above, including thresholds, proof and activity handling, timestamp recovery, legacy rebase records, the conflicting ci-noop exception, and the non-conflicting CI-approval clear-only path.
- `python tests/test_auto_triage.py` - automatic PR-card AND issue-card triage: `auto_triage`/`auto_triage_issues` config defaults/overrides/independence, per-revision (`head_sha`/`updated_at`) cache and legacy-card backfill for both kinds, `activity_reflected_at` remaining non-material and being folded into queued writes, rendered section/no-mention behavior for both kinds, deterministic automated-status labeling for the narrow harness-line allowlist, reconcile/ingest dispatch gates including same-pass newly-created-card queueing by issue number, `triage.yml` token isolation including the issue-triage default-branch/no-head-verify path, and cross-repo ref qualification in the rendered `### Triage` section (`triage_section`/`body_with_triage_result` owner threading, the `triage.yml` prompt's qualification instruction, and `GITHUB_REPOSITORY_OWNER` reaching both `triage-apply`/`triage-fail` through the `env -i` sandbox), all offline. Also covers held cards for both kinds: `should_hold` gating parity with `should_auto_triage`, the placeholder render (no `opt:` markers, `pending-triage` label, `held` state key, `needs-decision` retained), `upsert_card` creating held only when triage would actually be queued, preserving held-ness while refresh eligibility still holds, publishing silently when refreshed eligibility turns off, a no-op refresh when unchanged, `update_card_triage` publishing on success AND on failure (fail-open), a stale-revision publish attempt being a no-op, unheld-card behavior staying byte-for-byte unchanged, reconcile self-healing a held card whose target closed, the dispatch-failure fail-open publish added to both `reconcile.py` and the `queue-triage` CLI, and the `triage-recover` fail-open safety net (`triage.yml`'s final `always()` recovery step wiring, and the CLI publishing a card genuinely stuck held+queued for its exact revision while being a no-op for a never-held card, an already-published card, or one queued for a different/superseded revision). Also covers the pass-by-reference `evidence` schema field (`normalize_triage` requires a non-empty string or non-empty string list, rejects missing/blank/malformed lists, and never leaks it into the rendered triage dict) and the `evidence_anchor_ok`/`_triage_evidence_verified` lazy/fabrication guard (genuine single-quoted, double-quoted, markdown-normalized, or conservative fallback target spans verify; fabricated or too-short spans are rejected; and an unreadable `target.txt` fails OPEN).
- `python tests/test_triage_budget.py` - fully offline automatic-triage spend-guard coverage for typed cap and ceiling configuration, strict non-material attempt records, cap exhaustion, trusted by-number daily-ledger creation and verification, UTC rollover, all fail-closed ledger failures, safe reservation leakage, the sealed dispatch boundary, workflow admission, shared concurrency, and the bounded two-call schema-repair amplification.
- `python tests/test_triage_replay.py` - fully offline operator-only replay coverage for exact-number card/source reads, strict exact-revision and identity gates, claim tombstone verification, duplicate-only re-entry, terminal-error and absent-cache recovery, admission-duplicate card projection, fail-closed malformed/mismatch matrices, dry-run zero-write behavior, reviewable wave bounds, bounded dual-written triage result records, and the two exact-cohort one-use evidence-array attempts resets with the global cap pinned at 2.
- `python tests/test_triage_prompt_size.py` - the PASS-BY-REFERENCE prompt architecture (card #517 E2BIG fix), offline static YAML inspection: the load-bearing invariant that neither `triage.yml` nor `deep-review.yml` inlines target content into the Claude `prompt.txt` block (no `cat target.txt`/`cat vision.md`/`gh pr diff`/`gh pr view`/`gh api` there), the prompt stays under a small fixed byte budget and far below `MAX_ARG_STRLEN` raw AND json-escaped, a worst-case synthetic PR (diffs up to 5 MB) never enters the prompt and the prompt size is FLAT regardless of diff size (with a demonstration that the OLD inline design WOULD exceed the limit), the prompt names `target.txt`/`target-src/` and directs Read/Grep/Glob, target.txt is always written and its diff/comments are bounded (deep-review's formerly-UNCAPPED diff is now capped), the untrusted-data framing survives when content is read from files, both the READONLY_TOKEN and no-token Claude steps consume the same by-reference prompt (no-token step is Read/Grep/Glob only), the `DIFF_COMPLETE` fail-closed-on-oversize / complete-on-disk semantics, and the `--target-file` anchor-check wiring.
- `python tests/test_nl_prompt_size.py` - offline guards for the bounded pass-by-reference NL prompt, tool isolation, explicit target truncation, and marker-keyed failure note.
- `python tests/test_triage_result_delivery.py` - card #556 delivered-result-drop regression, no network: a >256KiB Claude transcript that ends in a valid successful `result` event still delivers its verdict (`extract_result_to_file` returns a bounded compact file that flows through `extract_claude_result`->`parse_triage_json`->`normalize_triage`->the visible `### Triage` section with `triage_status:succeeded`), the CLI `extract-result` round-trips and exits non-zero when no result exists, and static YAML checks that `triage.yml`'s `triage-result` step extracts via `render_card.py extract-result` BEFORE the 262144 gate (so the size cap bounds only the retained `transcript.json` copy, never `result_path`) plus the audit that `deep-review.yml` never had a size-cap execution-file drop.
- `python tests/test_triage_schema_repair.py` - cards #551/#547 bounded schema-repair, no network: the TRIGGER discipline (`plan_triage_repair`/`decide_triage_apply` repair a DELIVERED-but-invalid result, but NEVER a missing result - E2BIG/auth/rate-limit/infra - nor an evidence-anchor failure, and a valid result is untouched); success-on-repair (invalid candidate -> valid repair -> the card gets a real `### Triage` section, `triage_status:succeeded`, and `triage_repair_status:repaired`); the repair-failure cap (still-invalid repair, no repair output, or a repair whose evidence no longer anchors all land on the visible triage-unavailable error carrying the STRUCTURAL reason with `triage_repair_status:repair-failed`, exactly one attempt); `triage_schema_reason` being purely structural (field name + defect, never a field value) so no target/comment content leaks into the persisted `triage_error`/`triage_repair_reason` or card body; `build_repair_prompt` naming every required schema field in lockstep with `normalize_triage`, embedding the candidate, byte-bounding a pathological candidate, and forbidding file reads/re-analysis; the NON-MATERIAL telemetry keys (`triage_repair_status`/`triage_repair_reason` absent from `MATERIAL_FIELDS`, a repaired success differing from a normal success ONLY by those two keys, and a later non-repair write clearing them); the `triage-repair-prep` CLI emitting `repair_needed`/`reason`/`repair_prompt` to `$GITHUB_OUTPUT`; the `triage-apply --repair-execution-file` CLI end-to-end with mocked card I/O; and static `triage.yml` wiring (repair-prep -> claude_repair -> repair-result ordered after triage-result and before the card update, the repair turn tokenless with `--max-turns 1`, an empty `--allowedTools`, and a fail-closed `permissions.deny` tool set, gated on `repair_needed`, pass-by-reference with no inlined target content).
- `python tests/test_deep_review.py` - the always-on/code-grounded deep-review and Investigate wiring: render options, the removed enable flag, the token-absent note, the `persist-credentials: false` checkout plus read-only tool isolation, the narrow `allowed_bots`, the optional READONLY_TOKEN-gated `wheelhouse-search` wiring, normalized `AgentResult` verdict capture, issue-only manual dispatch, the handler's immutable-input `workflow_dispatch` trigger, and the "Post the verdict" step's automated-status labeling plus `qualify_issue_refs` call (with the deterministic `TARGET_REPO`/`GITHUB_REPOSITORY_OWNER` inputs) running before the `gh issue comment` post, plus the prompt's qualification instruction, all by inspecting the scripts/YAML, no network.
- `python tests/test_workflow_lint.py` - a regression guard that scans every `.github/workflows/*.yml` `run:` step for a `gh api` invocation combining `--slurp` with `--jq` (mutually exclusive in the installed `gh` CLI - `gh api --slurp` yields an array of per-page arrays and must instead be piped into a standalone `jq`), no network.
- `python tests/test_qualify_refs.py` - direct unit tests for `wheelhouse_core.qualify_issue_refs` (bare `#N` -> `owner/repo#N`, already-qualified/URL/markdown-link/`GH-123`/`#123abc` left untouched, multiple refs in one string, `None`/empty safety, idempotency, and that qualification is driven by the caller-supplied slug rather than any repo the text itself names), no network.
- `python tests/test_scan_reliability.py` - the card #411 scan reliability + correctness hardening, no network: `_gh_graphql_data` retry/backoff (transient-vs-fatal classification, recover-after-5xx, exhaust-then-raise, non-transient fails-fast, transient GraphQL `errors` and unparseable bodies retried, bounded/growing backoff with `_sleep` stubbed); small-page cursor pagination (`_page_open_prs` multi-page assembly and mid-page failure propagating so `build_repo` marks `truncated`); the fleet-scan health ledger (`parse_scan_health`/`update_scan_health`/`render_scan_health_body` increment/reset/carry-forward/legacy-int, and `cmd_scan_health` emitting `::error::` + non-zero exit past threshold, staying green otherwise, and failing OPEN on missing scan.json or ledger I/O error); and the UNKNOWN-mergeability policy (`_settle_mergeable` poll-first-conclusive/fail-open, `_resolve_pr_bucket` polling an explicit `UNKNOWN` and returning `needs-rebase` on settled CONFLICTING / `merge-ready` on settled MERGEABLE / `MERGEABILITY_PENDING` when it never settles, no poll for a known-MERGEABLE PR, plus `build_repo` integration: UNKNOWN->CONFLICTING nudges with no card, UNKNOWN->MERGEABLE emits a merge-ready card, unsettled UNKNOWN emits no item and lands in `indeterminate_pr_numbers` with no nudge, and the #111 acceptance that a statically-conflicting PR never enters the worklist across readable-CONFLICTING and post-base-push-UNKNOWN scans). The reconcile freeze of an `indeterminate_pr_numbers` card (no close, no refresh) and the unchanged non-indeterminate soft-close live in `tests/test_reconcile.py`.
- `python tests/test_config_schema.py` - structural load test that `wheelhouse_core.load_config()` accepts the checked-in `wheelhouse.config.yml`: every `repos:` entry is well-formed (name is trimmed and matches its key; `compliance_check` is null or a trimmed non-empty string; `test_check_patterns` is a list of trimmed non-empty strings; `merge_method` is unset or squash|merge|rebase) and repo names are case-insensitively unique. Deliberately pins no repo names or fleet size, so it keeps guarding the file as the fleet grows/shrinks, no network.
- `python tests/test_auto_merge_v1.py` - scan-time auto-merge (V1), no network and no target-repo writes: the config + exclusion helpers (`_auto_merge_enabled` default-off/overrides, `_auto_merge_exclusions` covering every category incl. VISION.md self-authorization); the pure `verdict_eligible` gate (A/B/C eligibility, class-C opt-in/default-off, malformed/stale/absent verdicts, fail-closed defaults) plus `normalize_automerge_verdict` parsing/coercion; the blast-radius caps at the exact 20-file and 1000-line boundaries; every deterministic gate G0-G6 walked through PASS and HOLD via representative live-card fixtures in `act_on_scan`; the G7 live head + merge-state + same-closing-issue overlap re-check immediately before acting (including scan-clear/act-ambiguous and unreadable evidence); the `wheelhouse:no-auto-merge` escape hatch and global/per-repo kill switches; the ok:false/truncated/indeterminate freeze; base-branch-ONLY VISION.md reads (contents API, no `?ref`); the durable ledger (parse/append/render/cap) + audit comment + `record` CLI resolving the card and appending the ledger (best-effort/no-op paths); the `do_merge` race/error outcomes; the required same-closing-issue ambiguity hold; and the DELIBERATE ABSENCE of an open-PR same-file overlap gate and any per-contributor/per-scan rate cap; the claim-time author-duality normalization (real `get_card`-shaped `{"login": "app/github-actions"}` fixtures normalize and are trusted, `github-actions[bot]` stays trusted, and a human login / a different `app/*` slug / bare `github-actions` / lookalikes all stay fail-closed rejected); plus offline YAML wiring checks (FLEET act step, github.token record step, act->reconcile->record order, the criteria handoff, and the triage.yml base-VISION verdict prompt).
- `python tests/test_automerge_card_ui.py` - authoritative auto-merge criterion UI, no network: every stable row's positive and fail-closed negative state, owner/bot/history distinctions, workflow/security exclusions, dirty/unknown mergeability and checks, verdict freshness and A/B/C details, blast limits, held/claimed card state, the real axi#96 shape through evaluator and renderer, old-card fallback, criterion-only refresh, and proof that persisted display rows cannot grant eligibility.
- `python tests/test_automerge_workflow_hold.py` - durable history-only workflow manual-merge hold, no network: the real two-hour claim/validate/act/record/reconcile lifecycle, structured final-gate evidence, same-head no-reclaim behavior, G7 `UNMET` presentation, clean and still-dirty new heads, G2 net-diff separation, unreadable/incomplete fail-closed paths, malformed/stale state, persistence recovery, same-head maintenance and trusted reuse, immediate hard close, and default-card-token isolation.
YAML-parse `.github/workflows/*.yml` plus `wheelhouse.config.yml` plus `.github/ISSUE_TEMPLATE/*.yml`.
Run `actionlint` if available.
The live LLM paths (auto triage, deep-review, nl_decisions) can only be exercised end-to-end in CI with the token set and, for nl_decisions, the flag on.
Secrets the maintainer must add for the current production selection: `FLEET_TOKEN` always, `CLAUDE_CODE_OAUTH_TOKEN` for auto triage/deep-review and/or nl_decisions, and optionally `READONLY_TOKEN` public-read only for auto triage, nl_decisions, and deep-review search.
Do not add a Codex credential under the current Pro plus public-repository topology; `docs/AGENT_RUNTIME.md` owns the activation prerequisites.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
