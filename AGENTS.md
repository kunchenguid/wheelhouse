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
  `resolved`, `blocked`, `repo:*`, `kind:*`, `priority:*`). A hidden
  `<!-- wheelhouse-state: {...} -->` block in each card body carries
  `{repo, number, kind, head_sha, options}` plus the material fields
  `{comp, tests, priority}` (the latter three added so a refresh can cheaply and
  deterministically decide "did this target materially change?" - see "Card
  refresh" in Sharp edges). `options` is also material for refresh comparison,
  but is normalized as a sorted set so checkbox reordering alone does not
  refresh the card. The state block also carries `updated_at` unconditionally
  (populated for issue-triage items, empty for pr-review) - it is NON-material,
  existing purely as the issue-triage auto-triage cache key, mirroring how
  `head_sha` doubles as the pr-review cache key. Automatic triage (pr-review
  AND issue-triage) adds non-material cache fields such as
  `triaged_sha`, `triage_status`, and `triage_recommendation`; those are
  deliberately outside `MATERIAL_FIELDS` so a triage result never changes
  classification or forces a card refresh. The auto-inserted
  `accept-recommendation` option is stripped from material option comparisons
  because it is derived from non-material triage state, not from source-provided
  checkbox options. A held card also carries non-material `held: true` until its
  first auto-triage attempt publishes the normal decision controls. The state
  block also carries `render_version`, another
  non-material field alongside `triaged_sha`: it is a one-time re-render
  trigger stamped by `render()` (see "Card refresh" in Sharp edges) that exists
  purely so a display-only fix (e.g. the author `@mention` drop or automated
  status labeling) propagates to already-open cards; it is never a
  `MATERIAL_FIELDS` member and never
  influences classification. `render_card.py` writes that marker, but
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
  (tick/slash/**plain-English** -> act on target -> consume terminal cards or
  leave non-terminal cards open), `scan-backstop`
  (hourly scan -> deterministic target-side cleanup plus reconcile:
  create/refresh/close - the primary keep-current path
  now that cards refresh on material change, render-version staleness, or a
  held-card publish trigger; safe to run hourly because reconcile is a full
  no-op when none of those triggers fires, and queues automatic PR or issue
  triage when the
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
  requiring `CLAUDE_CODE_OAUTH_TOKEN`; cached once per revision), `deep-review` (ALWAYS-ON, code-grounded;
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
  auto-approve in `build_repo`, stale pending-contributor cleanup
  (`sweep_pending_contributor_actions`, target-side markers/labels, and the
  rebase-nudge arming path), plus shared utils
  `parse_state_block`, `authorized`, `state`, `nl-decisions-enabled`,
  `auto-triage-enabled`, `auto-triage-issues-enabled`, `qualify_issue_refs`
  (rewrites a bare GitHub-autolink `#N` in model text to `owner/repo#N` - see
  "Cross-repo reference qualification" in Sharp edges)),
  `render_card.py` (render + card CRUD; `CHECKBOX_OPTIONS`/`OPTION_LABELS` carry
  the per-kind checkboxes, including the non-consuming `investigate` box on
  pr-review/issue-triage; held `pending-triage` placeholder rendering;
  automatic triage section rendering, structured recommendation persistence,
  conditional `Accept recommendation` checkbox rendering, `triaged_sha` cache
  updates, automated-status labeling for known harness transcript lines, and
  trusted triage-result card edits that publish held cards),
  `apply_decision.py` (deterministic `parse` then
  `execute`; non-checkbox actions including `comment`, `decline`, and
  pr-review-only `request-changes` with optional cleanup arming after a successful
  GitHub review; the virtual `accept-recommendation` checkbox
  routing into existing deterministic actions; the NON-CONSUMING `investigate`
  routing + `clear-checkbox`; plus the natural-language `nl-eligible`/`nl-prompt`/`nl-route` that map an owner's
  free-text comment to a structured result), `nl_readonly_search.py` (installs
  the optional `wheelhouse-search` wrapper for READONLY_TOKEN-backed LLM
  context),
  `build_item.py` (normalize ingest payload), `reconcile.py` (backstop
  create/**refresh**/close and automatic triage dispatch). `apply_decision` imports `wheelhouse_core` and
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

- **`check_status()`'s `comp`/`tests` are worst-wins aggregates, never scalar
  overwrites - card #392 was a false green from getting this wrong.**
  GitHub's GraphQL `statusCheckRollup.contexts` can return more than one
  check-run with the SAME name (e.g. Wheelhouse's own `approve_ci` approving
  two duplicate pending runs of one workflow, one of which then gets
  cancelled by the workflow's own `concurrency: cancel-in-progress` group).
  `check_status()` (`scripts/wheelhouse_core.py`) collects every context
  matching `cfg["compliance_check"]` into a list and reduces it after the
  loop, exactly like it already did for `tests`: any terminal non-`SUCCESS`
  conclusion anywhere in the group -> `"fail"`; else any non-`COMPLETED`
  context -> `"pending"`; only if every matching context is a completed
  `SUCCESS` -> `"pass"`. A scalar last-write-wins assignment inside the loop
  (the original bug) makes the result depend on GraphQL array order instead
  of policy. As a fail-toward-safe backstop, `check_status()` also clamps
  `compliance` to `"fail"` whenever GitHub's own authoritative
  `statusCheckRollup.state` is `"FAILURE"`/`"ERROR"` and the per-context read
  would otherwise say `"pass"`/`"n/a"` - deliberately conservative (it can
  hold a card over an untracked/optional check the rollup counts but this
  config doesn't), because a false hold is recoverable and a false green is
  not. `classify()` is correct given correct inputs and was not touched; the
  defect was entirely in how `check_status()` derived those inputs. See
  `tests/test_check_status.py`. Relatedly, `approve_ci()` dedups its
  `action_required` run list by stable `workflowDatabaseId` when GitHub exposes
  one, keeping the highest `databaseId` per head_sha, and leaves runs without a
  stable workflow identity distinct before approving, purely so Wheelhouse itself
  stops manufacturing the duplicate-pending-run race that started the card #392
  incident - this dedup runs strictly after the risky-files/posture HOLD check
  and never weakens it.
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
  card current: when a target's MATERIAL state changes - `head_sha`, compliance
  (`comp`), tests (`tests`), `kind`, `priority`, or checkbox `options` - the
  card is re-rendered in place; title/summary/recommendation re-render naturally
  and are NOT change triggers. Option comparisons use set equality; display
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
  it is not a material field). `CARD_RENDER_VERSION` is currently `5`: the
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
  stamped with the current version, and the next scan is a full no-op.
  The `TRIAGE_START`/`### Triage`/`TRIAGE_END` markers contain no
  `#N` and do not match the automated-status allowlist, so repairing the whole
  section string leaves them intact.
  The shared pure helpers live in `render_card.py`
  (`material_changed`, `render_stale`, `held_publish_needed`, `refresh_needed`,
  `is_refreshable`, `plan_label_update`); `reconcile.py`
  pre-checks them (using the card row it already listed) so the common
  no-change case never hits the API, and `upsert_card` re-checks them before it
  edits (defense in depth for the event path). Three rules are load-bearing and
  must not be loosened:
  - **Only refresh a pure `needs-decision` card.** A re-render resets the card's
    checkboxes, so a card already `processing`/`resolved`/`blocked` is left
    completely untouched - refreshing one would clobber an in-flight decision or
    race the decision-handler. (`is_refreshable` is the guard; the lock set is
    `NON_REFRESHABLE_LABELS`.) This is the chosen safe rule, and it gates the
    `render_version` trigger exactly the same way - a mid-decision card is
    never refreshed just because it is render-stale.
    A held `pending-triage` card deliberately stays refreshable because it keeps
    `needs-decision` and carries no non-refreshable lock label.
  - **No-op when neither trigger fires.** A card that is both materially
    unchanged AND render-fresh, and does not need held-state publishing, gets no
    body edit, no label churn, and no comment - never rewrite a card just to
    put back an identical body. The
    material check is a cheap dict compare of the state block's material
    fields, which is why those fields are carried in the state JSON; the
    render-staleness check is the same kind of cheap compare against
    `render_version`, and `held_publish_needed` is the same kind of cheap
    predicate for a held card whose auto-triage path is no longer available.
  - **Replace the managed labels, don't just add.** `upsert_card` removes
    `repo:*`/`kind:*`/`priority:*`/`target:*` labels that no longer apply
    (`plan_label_update`), so a changed priority/kind doesn't leave both the old
    and new label stuck on the card. It also syncs the exact `pending-triage`
    label to the current `held` state. `needs-decision` and any human-added
    label are never removed.
  When `head_sha` changed the refresh also drops a short "target updated" card
  comment so the owner sees a re-review is warranted rather than being silently
  swapped underneath. All of this stays on the ambient `GH_TOKEN` (= default
  `GITHUB_TOKEN`) like every other card write, so a refresh never re-triggers the
  handler and never runs under `FLEET_TOKEN`. reconcile only ever refreshes from
  scanned `items`, which exist solely for `ok:true` repos, so an `ok:false` repo
  (state unknown) is never refreshed - the same invariant that bars closing its
  cards.
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
  `triage_status=queued`; this intentionally spends at most one Claude attempt
  per revision even if the asynchronous workflow errors, times out, or cannot
  parse a result.
  **A just-created card must be read back BY NUMBER, never via
  `find_card`'s label-filtered `gh issue list`.** That listing is not
  read-after-write consistent immediately after `gh issue create`, so reading
  it back milliseconds later can silently miss the card and skip queuing its
  first auto-triage attempt (only a later scan's pre-existing-card backfill
  path would then catch it). `_create_card`/`upsert_card` therefore always
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
  the card and makes the fresh head eligible for one new triage attempt in the
  same pass. For an issue-triage card, `updated_at` is NOT material (an issue's
  title/comp/tests/kind/priority/options rarely change on a new comment), so a
  new comment/edit can make the card eligible for one new triage attempt
  WITHOUT any card refresh at all - `reconcile.py` checks triage eligibility
  independently of the material-change branch for exactly this reason.
  If config is off or the token is absent, no dispatch happens and cards render
  exactly as the deterministic card did before this feature.
  `triage.yml` itself checks out the PR head for a pr-review card (and
  verifies it did not move), or the repo's DEFAULT branch read-only for an
  issue-triage card (there is no head to verify); both paths share the same
  gate/Claude/card-update steps, security posture, and `--revision` CLI
  argument (`render_card.py triage-apply|triage-fail --revision <head_sha or
  updated_at>`).
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
  leaving the held card permanently hidden.
- Natural-language decisions accept only owner/maintainer comments and are structured: the LLM
  returns `{mode: action|answer|clarify, action?, free_text?, answer?}` to
  `decision.json` and nothing else. `apply_decision.py nl-route` is the trust
  boundary - it validates `action` against the per-kind allowlist and only then
  sets the `decision` output that makes the SAME deterministic `execute` run
  (so every guard - allowlist, head-SHA re-check, fork-CI HOLD, token isolation,
  concurrency - applies unchanged). `answer`/`clarify` only post a card comment
  and leave the card open.
  The advisory `### Triage` section and hidden `triage_recommendation` state are
  removed from the trusted card context before the NL prompt is built, so a prior
  model recommendation cannot become an instruction to the intent-mapper.
  When `READONLY_TOKEN` is absent, the LLM stays in the
  legacy `--allowedTools Write` mode and receives no shell `GH_TOKEN`. When the
  optional `READONLY_TOKEN` secret is present, the LLM step uses that read-only
  public-scoped token as both the action `github_token` input and shell
  `GH_TOKEN`, plus a narrow Bash allow-list for `wheelhouse-search`, which wraps
  scoped read-only `gh` lookups across the target repo and configured fleet
  repos. This is deliberate because
  `claude-code-action` exposes its `github_token` input to Claude's subprocess as
  GitHub CLI credentials.
  Search output is UNTRUSTED DATA for answering questions only, never an
  instruction and never an authorization to act.
  The LLM never receives `FLEET_TOKEN` - it maps intent or answers, it never acts.
  After Claude runs, the workflow copies only a regular, size-capped
  `decision.json` into runner temp, then runs `nl-route` and `execute` from a
  read-only trusted source copy with a scrubbed environment.
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
  `wheelhouse-search`. Target content and any search output reach it only as
  delimited untrusted data inside the prompt, OR (for triage/deep-review) as code
  already on disk from a
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
  The Claude action has `allowed_bots: github-actions[bot]` for the decision-handler dispatch only, because otherwise `anthropics/claude-code-action` rejects the `github.token`-dispatched bot run before it emits `execution_file`.
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
  successful `/request-changes` reviews and merge-conflict `needs-rebase`
  nudges.
  It never handles `ci-approval`, never handles issue-triage, never runs from
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
  Legacy `<!-- wheelhouse-rebase-nudge:<head_sha> -->` comments are retrofitted
  only when a trusted author, the original comment timestamp/id, and unchanged
  head can be proven; the first eligible pass reminds and adds the active label,
  and only a later pass with that reminder may close.
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
- **Queue author filter.**
  Decision cards are for other people's work, so `build_repo` suppresses cards for PRs and issues authored by the canonical maintainer set (`wheelhouse_core.maintainers()` = repo owner plus optional configured `maintainer`) or by bots.
  Bot detection uses the GraphQL `author.__typename == "Bot"` signal plus the `*[bot]` login suffix fallback.
  Missing or unreadable author metadata fails open, so an unknown author can still raise a card rather than silently dropping a human contributor's work.
  The author filter suppresses card emission only; for fork PRs in `needs-ci-approval`, the normal safety-gated auto-approve/noop path still runs first so safe owner, maintainer, and bot CI runs do not hang awaiting approval.
  This deliberately bypasses the global or per-repo `auto_approve_ci: false` opt-out only for those author-excluded ci-approval PRs; contributor PRs still honor the opt-out and card as before.
  Unsafe, uncertain, or failed owner, maintainer, and bot CI-approval targets still do not emit cards, but they keep the scan-log warning.
  Skipped targets still remain in `open_pr_numbers` / `open_issue_numbers` but are absent from the `items` worklist, so `reconcile.py` consumes any existing pure `needs-decision` owner, maintainer, or bot card on the next successful scan.
- **Merge conflicts leave the maintainer queue.**
  `wheelhouse_core.py` fetches GraphQL `pullRequests.nodes.mergeable` and treats only `CONFLICTING` as authoritative.
  `UNKNOWN` or missing mergeability fails open because GitHub computes it asynchronously, so the PR classifies normally until a later scan can prove the conflict.
  A conflicting PR that would otherwise route to `merge-ready` or `review-needed` becomes waiting-on-contributor `needs-rebase`, which is intentionally absent from `NEEDS_MAINTAINER`.
  This never rewrites `needs-ci-approval`: fork CI approval is independent of whether the eventual merge would conflict, and issue triage is unrelated.
  On the `ok:true` scan path, `build_repo` posts a contributor nudge under `FLEET_TOKEN` for non-owner/non-maintainer/non-bot `needs-rebase` PRs.
  The nudge body carries hidden marker `<!-- wheelhouse-rebase-nudge:<head_sha> -->`; before posting, Wheelhouse paginates the PR comments and skips if that marker already exists, so it posts at most once per conflicted head SHA and can nudge again only after a new push creates a new head.
  If stale pending-contributor cleanup is active for PRs, a newly posted nudge is
  patched with a structured `wheelhouse-pending-contributor-action` marker and
  the target gets `wheelhouse:pending-contributor-action`; if the nudge comment
  response lacks a comment id or timestamp, the cleanup arming fails open with a
  warning and the nudge remains posted.
  If comment lookup or posting fails, the scan logs a warning and still emits no card; it never posts without first checking for the current marker.
  The PR stays in `open_pr_numbers` but drops out of `items`, so `reconcile.py` consumes any existing pure `needs-decision` card on the next successful scan.
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
  An `approve_ci` `noop` is a verified "nothing awaiting approval" state, so the
  scan emits no worklist item and reconcile consumes any stale card; if a real
  pending run appears on a later scan, the normal approve/card/suppressed-card
  path runs again.
  Fork-originated `action_required` workflow runs are expected to have an empty `workflow_run.pull_requests` list, so `approve_ci` verifies that fork case with the already-filtered run's exact `head_sha` plus `head_branch`; non-empty `pull_requests` stays strict and must contain exactly the target PR.
  After verification, `approve_ci` dedups matching pending runs by stable `workflowDatabaseId` when GitHub exposes it, keeps the highest `databaseId`, and leaves same-named distinct workflows or runs without workflow identity distinct.
  This dedup happens after the risky-files/posture safety gate, so it never weakens the HOLD path.
  **Observability (every outcome is logged, never silent).** `_auto_approve_or_card`
  returns `(handled, card_note, log_note)` and `build_repo` emits exactly ONE
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

Three independent LLM features share the same auth (a Claude **subscription** token
from `claude setup-token` via `anthropics/claude-code-action` - NOT an Anthropic
API key) and the same injection model (only trusted workflow prompts and
owner/maintainer-authored text are instructions; target content and optional
search output are delimited untrusted data; the LLM never gets `FLEET_TOKEN`):
Every `anthropics/claude-code-action` LLM step is pinned to `v1.0.161` at commit `fad22eb3fa582b7357fc0ea48af6645851b884fd` and passes `--model sonnet`.
The pinned release resolves `@anthropic-ai/claude-agent-sdk` to `0.3.197`; on the Anthropic API, Claude Code versions v2.1.197 and later resolve `sonnet` to Sonnet 5.

- **`triage.yml` - automatic, lightweight, advisory PR-card OR issue-card context.** Triggered by `scan-backstop` / `reconcile.py` and the ingest fast path for pure `needs-decision` pr-review OR issue-triage cards whose current revision (a PR's `head_sha`, or an issue's `updated_at`) does not match `triaged_sha`; if the card is held under `pending-triage`, the update path also publishes its real checkboxes fail-open.
  pr-review is opt-out through `auto_triage`; issue-triage is opt-out through the INDEPENDENT `auto_triage_issues` - both global default true, per-repo override allowed, and both inert unless `CLAUDE_CODE_OAUTH_TOKEN` is present. Neither flag affects the other.
  For a pr-review card it checks out the target PR head read-only with `FLEET_TOKEN`, `persist-credentials: false`, and verifies the head did not move since queueing.
  For an issue-triage card it checks out the repo's DEFAULT branch read-only the same way (same substrate `deep-review.yml` uses for an issue card) - there is no head to verify.
  Both paths then run Claude with lower `--max-turns` than deep-review to produce structured `{summary, product_implications, recommended_action, recommended_reason}` context; the issue-triage prompt fetches the issue's title/body/comments (no diff) and asks for an issue-appropriate action set, while the pr-review prompt keeps the PR title/body/diff and a PR action set.
  Trusted code still renders the visible `### Triage` section, including a human-readable Recommended next step line, with `github.token`, never by Claude directly, and labels known harness polling/status transcript lines as automated status.
  When the structured action is fresh, successful, per-kind allowlisted, and has any required reason text, trusted code persists `triage_recommendation` and may add the `Accept recommendation` checkbox.
  The result is advisory until the owner/maintainer ticks that checkbox, at which point `apply_decision.py` maps it to an existing deterministic action with the same guards.
  Apart from publishing a held card's own `pending-triage` label and placeholder decision section, plus that conditional accept shortcut, it never changes classification, managed labels, merge/close/approve behavior, fork-CI safety, author filtering, or conflict routing.
  Before dispatch, the queueing path writes `triaged_sha=<current revision>` and `triage_status=queued`, so errors and timeouts fail open without retriggering the same revision on every scan.
  Existing open cards of either kind with no `triaged_sha` are intentionally stale and backfill once on the next eligible scan.
  Optional `READONLY_TOKEN` search uses the unchanged `wheelhouse-search` wrapper and remains untrusted evidence only.
  The Claude action allows only `github-actions[bot]`, never `*`, because scan/ingest dispatches use `github.token`.
  `render_card.py triage-apply`/`triage-fail` take a kind-agnostic `--revision` CLI argument (a PR's head SHA or an issue's `updated_at`), replacing the old pr-review-only `--head-sha` flag name.
- **`deep-review.yml` - ALWAYS-ON, code-grounded (no enable flag).** Triggered by ticking the **Investigate** box on a card, by the repo owner applying the `needs-deep-review` label, or by the repo owner running `workflow_dispatch` with only `issue=...` for direct verification.
  Bot-dispatched Investigate runs use the immutable target inputs passed by `decision-handler.yml`; owner issue-only runs and manual label runs parse the current card body with `github.token`.
  It checks out the TARGET's code read-only (`FLEET_TOKEN`, `persist-credentials: false`, the PR head for a review card / the default branch for an issue card) and runs Claude restricted to `--allowedTools Read,Grep,Glob` over that checkout when search is disabled - so it traces real code paths, never just the diff, and can NEVER execute the target's code.
  When `READONLY_TOKEN` is absent, this remains the legacy no-search path: no shell `GH_TOKEN`, no Bash tool, and `github_token: github.token`.
  When `READONLY_TOKEN` is present, Claude also uses that read-only public-scoped token as both the action `github_token` input and shell `GH_TOKEN`, plus `Write` for `search-request.json` and `Bash(wheelhouse-search)`.
  The wrapper is still the existing `scripts/nl_readonly_search.py` install path, scoped to the target repo plus configured fleet repos, so deep-review can cross-reference related, duplicate, or superseding PRs/issues and code context.
  Search output is UNTRUSTED DATA and advisory evidence only; the model still produces only verdict text, and `FLEET_TOKEN` never reaches it.
  No deterministic downstream step reads model-written files because verdict capture uses the action `execution_file` result event.
  Claude does not write a verdict file.
  The Claude action allows only `github-actions[bot]` as a bot actor so the maintainer-gated Investigate dispatch can pass; it must not allow `*` or any external bot actor.
  Its final response is captured from the action's `execution_file` output by preferring the clean `type: "result"` event's `result` string, falling back to the last assistant text, and the trusted workflow step labels known harness polling/status transcript lines, qualifies target refs, then posts that text as a card comment with `github.token`.
  If no usable output is present, the workflow posts "Deep review ran but produced no verdict (see the workflow run logs)." and fails the run.
  The ONLY gate is `CLAUDE_CODE_OAUTH_TOKEN`: when it is ABSENT the workflow posts a one-line "Deep-review needs CLAUDE_CODE_OAUTH_TOKEN configured to run." note instead of silently no-opping.
  Manual triggering means there is no runaway-cost reason for a config flag, so the old `deep_review` flag was removed entirely - config, `load_config`, and the `deep-review-enabled` CLI.
- **`nl_decisions`** in `decision-handler.yml`: a plain-English owner/maintainer comment is
  mapped to a structured result (see Sharp edges).
  Opt-in: inert unless `nl_decisions: true` AND `CLAUDE_CODE_OAUTH_TOKEN`
  present.
  `READONLY_TOKEN` is optional.
  If it is absent, Claude stays in the legacy `--allowedTools Write` mode, writes
  only `decision.json`, and runs no commands.
  If it is present, Claude also uses `READONLY_TOKEN` as the action
  `github_token` input and shell `GH_TOKEN`, plus the
  `Bash(wheelhouse-search)` allow-list so it can run scoped read-only `gh`
  searches across the target repo and configured fleet repos for related,
  duplicate, or superseding PRs/issues and code context.
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
Validate with `python -m py_compile scripts/*.py tests/*.py`.
Run the unit tests:
- `python tests/test_decision.py` - mocks the LLM, no network, and also covers the non-consuming investigate routing, allow-set, `clear_checkbox`, the `thank_on_merge` post-merge thank-you (config on/off, per-repo override, owner/maintainer/bot skip, custom-message substitution, best-effort swallow, and every non-success merge outcome posting none), that `route_decision` qualifies bare cross-repo refs in `answer`/`clarify` replies using `STATE["repo"]` + owner, never the model's own text, and that a HELD card (render_card.py "Held cards") is inert to `cmd_parse` (checkbox tick and slash-command alike) and `cmd_nl_eligible`, while the identical card once published is actionable again. Also covers `request-changes`: it is pr-review-only in `ALLOWED` (not ci-approval/issue-triage) and, unlike `investigate`, IS in `nl_allowed`; `/request-changes <text>` and its `/request_changes` alias slash-parse to the action with the text as free_text (and parse to nothing without text, or when the card's kind doesn't allow it); the `decision:request-changes` label path is ignored because labels cannot carry review text; `route_decision` drives `execute` for a well-formed request-changes action, downgrades to `clarify` when `free_text` is missing or the kind disallows it, and the built NL prompt lists `request-changes` with its judgment guidance for pr-review only; and `do_request_changes` (mocked `gh_rest`) posts exactly one `POST .../pulls/{n}/reviews` with `{"body": text, "event": "REQUEST_CHANGES"}` and a `"none"` (card-stays-open) terminal state, refuses with a clear error (no API call) when the PR author is the repo owner, rejects blank review text before any API call, surfaces a raw API failure as an `"error"` terminal state, and only arms pending-contributor cleanup when config/targets allow it and the target author is a non-maintainer human.
- `python tests/test_nl_decisions_search.py` - offline YAML wiring checks for the optional READONLY_TOKEN search path, scoped actor-check bypass, token isolation, prompt gating, unchanged `nl-route`/`execute` boundary, the `GITHUB_REPOSITORY_OWNER` threading into the `route` step's `env -i` sandbox, the NL prompt's cross-repo-qualification instruction, and that `route_decision` qualification is driven by deterministic state rather than model-claimed repos.
- `python tests/test_card_refresh.py` - the card-refresh change-detection, refreshability-guard, and label-replace logic, pure functions, no network; also covers the `CARD_RENDER_VERSION` 1 -> 2 retroactive triage-ref-qualification propagation and current version stamp: a render-version-behind card with a bare-ref cached `### Triage` section gets it qualified and stamped with the current `render_version` on the next refresh, a render-version-behind card with an older cached automated harness status line gets it labeled exactly once, a card already at the current version with already-qualified triage is a full no-op, already-qualified refs/URLs/markdown links/non-ref `#` uses in the preserved section are left untouched, and qualification is driven by `GITHUB_REPOSITORY_OWNER` + the card's own state repo rather than the item or model text.
- `python tests/test_reconcile.py` - reconcile routing and stale-card self-healing, no network.
- `python tests/test_merge_conflict.py` - mergeability fail-open vs CONFLICTING routing, idempotent rebase nudges, author-filter nudge skips, optional pending-contributor cleanup arming for rebase nudges, and reconcile self-healing for conflicted PR cards, no network.
- `python tests/test_ci_autoapprove.py` - the shared `ci_safety` verdict, `pull_request_target` posture detection, and the auto-approve-vs-card routing plus scan-log observability in `build_repo`, all with the network-touching helpers stubbed. Also covers `approve_ci`'s dedup-by-`workflowDatabaseId`: two `action_required` runs of the same workflow for one head_sha approve exactly one (the higher/newer run id), same-named distinct workflows or runs without workflow identity stay distinct, and the risky-file HOLD still short-circuits before dedup/run-list/approve even when duplicates are present.
- `python tests/test_check_status.py` - direct unit tests for `check_status()`'s `compliance` aggregation: two check-run contexts sharing the `compliance_check` name (one `CANCELLED`, one `SUCCESS`) yield `comp == "fail"` in both array orders (the card #392 incident - worst-wins, not last-write-wins), the `statusCheckRollup.state == "FAILURE"` backstop refuses to report `pass` even when every per-context read is `SUCCESS`, and a genuinely-green PR still classifies `comp == "pass"` / `tests == "green"`, no network.
- `python tests/test_author_filter.py` - queue author filtering across PR review, CI approval, and issue triage, cleanup-closed PR removal before addressed-issue recomputation, plus open-issue/PR/closing-reference pagination guards, no network.
- `python tests/test_pending_contributor_cleanup.py` - deterministic stale pending-contributor cleanup: config defaults/overrides, PR-only scope, reminder and close thresholds, visible-reminder requirement, close-comment wording, idempotent reminder/close behavior, keep-open, contributor activity detection, maintainer/bot non-reset behavior, head-move cleanup, fail-open timeline/edit-history/proof cases, legacy rebase marker retrofit, and CI/disabled-target exclusions, no network.
- `python tests/test_auto_triage.py` - automatic PR-card AND issue-card triage: `auto_triage`/`auto_triage_issues` config defaults/overrides/independence, per-revision (`head_sha`/`updated_at`) cache and legacy-card backfill for both kinds, rendered section/no-mention behavior for both kinds, deterministic automated-status labeling for the narrow harness-line allowlist, reconcile/ingest dispatch gates including same-pass newly-created-card queueing by issue number, `triage.yml` token isolation including the issue-triage default-branch/no-head-verify path, and cross-repo ref qualification in the rendered `### Triage` section (`triage_section`/`body_with_triage_result` owner threading, the `triage.yml` prompt's qualification instruction, and `GITHUB_REPOSITORY_OWNER` reaching both `triage-apply`/`triage-fail` through the `env -i` sandbox), all offline. Also covers held cards for both kinds: `should_hold` gating parity with `should_auto_triage`, the placeholder render (no `opt:` markers, `pending-triage` label, `held` state key, `needs-decision` retained), `upsert_card` creating held only when triage would actually be queued, preserving held-ness while refresh eligibility still holds, publishing silently when refreshed eligibility turns off, a no-op refresh when unchanged, `update_card_triage` publishing on success AND on failure (fail-open), a stale-revision publish attempt being a no-op, unheld-card behavior staying byte-for-byte unchanged, reconcile self-healing a held card whose target closed, the dispatch-failure fail-open publish added to both `reconcile.py` and the `queue-triage` CLI, and the `triage-recover` fail-open safety net (`triage.yml`'s final `always()` recovery step wiring, and the CLI publishing a card genuinely stuck held+queued for its exact revision while being a no-op for a never-held card, an already-published card, or one queued for a different/superseded revision).
- `python tests/test_deep_review.py` - the always-on/code-grounded deep-review and Investigate wiring: render options, the removed enable flag, the token-absent note, the `persist-credentials: false` checkout plus read-only tool isolation, the narrow `allowed_bots`, the optional READONLY_TOKEN-gated `wheelhouse-search` wiring, the action-output verdict capture, issue-only manual dispatch, the handler's immutable-input `workflow_dispatch` trigger, and the "Post the verdict" step's automated-status labeling plus `qualify_issue_refs` call (with the deterministic `TARGET_REPO`/`GITHUB_REPOSITORY_OWNER` inputs) running before the `gh issue comment` post, plus the prompt's qualification instruction, all by inspecting the scripts/YAML, no network.
- `python tests/test_workflow_lint.py` - a regression guard that scans every `.github/workflows/*.yml` `run:` step for a `gh api` invocation combining `--slurp` with `--jq` (mutually exclusive in the installed `gh` CLI - `gh api --slurp` yields an array of per-page arrays and must instead be piped into a standalone `jq`), no network.
- `python tests/test_qualify_refs.py` - direct unit tests for `wheelhouse_core.qualify_issue_refs` (bare `#N` -> `owner/repo#N`, already-qualified/URL/markdown-link/`GH-123`/`#123abc` left untouched, multiple refs in one string, `None`/empty safety, idempotency, and that qualification is driven by the caller-supplied slug rather than any repo the text itself names), no network.
YAML-parse `.github/workflows/*.yml` plus `wheelhouse.config.yml` plus `.github/ISSUE_TEMPLATE/*.yml`.
Run `actionlint` if available; fetch the binary via its `download-actionlint.bash` if not.
The live LLM paths (auto triage, deep-review, nl_decisions) can only be exercised end-to-end in CI with the token set and, for nl_decisions, the flag on.
Secrets the maintainer must add: `FLEET_TOKEN` always, `CLAUDE_CODE_OAUTH_TOKEN` for auto triage/deep-review and/or nl_decisions, and optionally `READONLY_TOKEN` public-read only for auto triage, nl_decisions, and deep-review search.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
