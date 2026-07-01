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
  consumed. Labels are state (`needs-decision`, `processing`, `resolved`,
  `blocked`, `repo:*`, `kind:*`, `priority:*`). A hidden
  `<!-- wheelhouse-state: {...} -->` block in each card body carries
  `{repo, number, kind, head_sha, options}` plus the material fields
  `{comp, tests, priority}` (the latter three added so a refresh can cheaply and
  deterministically decide "did this target materially change?" - see "Card
  refresh" in Sharp edges). `options` is also material for refresh comparison,
  but is normalized as a sorted set so checkbox reordering alone does not
  refresh the card. Automatic PR triage adds non-material cache fields such as
  `triaged_sha` and `triage_status`; those are deliberately outside
  `MATERIAL_FIELDS` so a triage result never changes classification or forces a
  card refresh. The state block also carries `render_version`, another
  non-material field alongside `triaged_sha`: it is a one-time re-render
  trigger stamped by `render()` (see "Card refresh" in Sharp edges) that exists
  purely so a display-only fix (e.g. the author `@mention` drop) propagates to
  already-open cards; it is never a `MATERIAL_FIELDS` member and never
  influences classification. `render_card.py` writes that marker, but
  `parse_state_block` also accepts the legacy `<!-- triage-state: ... -->`
  marker (cards rendered before the rename) - back-compat that must stay so a live
  queue keeps working. It also tolerates old `wheelhouse-state` cards that lack
  the material fields: a missing field reads as "unknown", so such a card is seen
  as changed exactly once and refreshes itself (backfilling the fields), then
  no-ops. The local lock/board/ledger from the original `triage.py`
  are intentionally dropped (replaced by Actions
  `concurrency` + issues/labels/comments).
- **Workflows:** `ingest` (dispatch/manual -> upsert a card), `decision-handler`
  (tick/slash/**plain-English** -> act on target -> consume card), `scan-backstop`
  (hourly scan -> reconcile: create/refresh/close - the primary keep-current path
  now that cards refresh on material change; safe to run hourly because reconcile
  is a full no-op when nothing changed, and queues automatic PR triage when the
  current head lacks a fresh `triaged_sha` cache), `triage` (automatic,
  lightweight, advisory PR-card context; gated on `auto_triage` and
  `CLAUDE_CODE_OAUTH_TOKEN`; cached once per PR head), `deep-review` (ALWAYS-ON, code-grounded;
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
  auto-approve in `build_repo`, plus shared utils
  `parse_state_block`, `authorized`, `state`, `nl-decisions-enabled`,
  `auto-triage-enabled`),
  `render_card.py` (render + card CRUD; `CHECKBOX_OPTIONS`/`OPTION_LABELS` carry
  the per-kind checkboxes, including the non-consuming `investigate` box on
  pr-review/issue-triage; automatic triage section rendering, `triaged_sha`
  cache updates, and trusted triage-result card edits), `apply_decision.py` (deterministic `parse` then
  `execute`; the NON-CONSUMING `investigate` routing + `clear-checkbox`; plus the
  natural-language `nl-eligible`/`nl-prompt`/`nl-route` that map an owner's
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
  refresh is a same-`head_sha` cosmetic refresh: it reuses the same
  `_preserve_same_head_triage` path as a material same-head refresh (an
  existing `### Triage` section and its `triaged_sha`/`triage_status` cache
  survive untouched, no re-triage for that head), and it does NOT drop the
  "target updated" comment (that stays gated strictly on `head_sha` actually
  changing). The shared pure helpers live in `render_card.py`
  (`material_changed`, `render_stale`, `is_refreshable`, `plan_label_update`); `reconcile.py`
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
  - **No-op when neither trigger fires.** A card that is both materially
    unchanged AND render-fresh gets no body edit, no label churn, and no
    comment - never rewrite a card just to put back an identical body. The
    material check is a cheap dict compare of the state block's material
    fields, which is why those fields are carried in the state JSON; the
    render-staleness check is the same kind of cheap compare against
    `render_version`.
  - **Replace the managed labels, don't just add.** `upsert_card` removes
    `repo:*`/`kind:*`/`priority:*`/`target:*` labels that no longer apply
    (`plan_label_update`), so a changed priority/kind doesn't leave both the old
    and new label stuck on the card. `needs-decision` and any human-added label
    are never removed.
  When `head_sha` changed the refresh also drops a short "target updated" card
  comment so the owner sees a re-review is warranted rather than being silently
  swapped underneath. All of this stays on the ambient `GH_TOKEN` (= default
  `GITHUB_TOKEN`) like every other card write, so a refresh never re-triggers the
  handler and never runs under `FLEET_TOKEN`. reconcile only ever refreshes from
  scanned `items`, which exist solely for `ok:true` repos, so an `ok:false` repo
  (state unknown) is never refreshed - the same invariant that bars closing its
  cards.
- **Automatic PR triage is a cached card-side side job, not routing.**
  It applies only to pure `needs-decision` pr-review cards when the effective
  `auto_triage` setting is true and `CLAUDE_CODE_OAUTH_TOKEN` is present.
  For explicit ingest payloads, `auto_triage:false` is an item-level opt-out only;
  it cannot force triage on when the global or per-repo config disables it.
  The cache key is the card state's `triaged_sha`, compared to the current
  `head_sha`.
  Missing `triaged_sha` on an existing open card counts as stale, so legacy
  pr-review cards backfill exactly once on the next eligible scan.
  Before dispatching `triage.yml`, `reconcile.py` / the ingest fast path edits
  the card state to set `triaged_sha=<current head>` and
  `triage_status=queued`; this intentionally spends at most one Claude attempt
  per head even if the asynchronous workflow errors, times out, or cannot parse
  a result.
  `triaged_sha` and the visible `### Triage` section are non-material: they must
  never affect `classify`, `material_changed`, decision parsing, merge execution,
  fork-CI approval, author filtering, or conflict routing.
  If `head_sha` changes, normal material refresh removes stale triage context and
  the fresh head becomes eligible for one new triage attempt.
  If config is off or the token is absent, no dispatch happens and cards render
  exactly as the deterministic card did before this feature.
- Natural-language decisions are owner-comment-only and structured: the LLM
  returns `{mode: action|answer|clarify, action?, free_text?, answer?}` to
  `decision.json` and nothing else. `apply_decision.py nl-route` is the trust
  boundary - it validates `action` against the per-kind allowlist and only then
  sets the `decision` output that makes the SAME deterministic `execute` run
  (so every guard - allowlist, head-SHA re-check, fork-CI HOLD, token isolation,
  concurrency - applies unchanged). `answer`/`clarify` only post a card comment
  and leave the card open.
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
  `READONLY_TOKEN` is never used by `execute` and never gates or authorizes an
  action.
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
- NL conversation memory is owner-scoped, and the scoping IS the security
  boundary. `decision-handler.yml` fetches the card's thread (`nl-comments`,
  `github.token`) and `apply_decision.py assemble_history` renders it as a
  "Conversation so far" block of trusted context - but ONLY comments authored by
  a maintainer or by the workflow bot (`github-actions[bot]`, the assistant's own
  prior turns) survive. The maintainer set is exactly `wheelhouse_core.maintainers()`
  (repo owner + optional configured `maintainer`) - the SAME notion the
  `gate`/`authorized` path uses; do not invent a second rule. Every other author
  (a contributor, a third-party bot) is dropped ENTIRELY so non-owner text can
  never enter the LLM's instruction context. The triggering comment is excluded
  from history by id (`github.event.comment.id`) because it is still passed
  separately as the single new instruction; the history is context only. None of
  this widens the acting trust model: optional `READONLY_TOKEN` search output is
  also untrusted reference data, the LLM still never gets `FLEET_TOKEN`, and
  `nl-route`'s allowlist re-validation is unchanged.
- `wheelhouse_core.py scan` is resilient: a repo that fails to read is reported as a
  warning (`ok:false`) and skipped, and `reconcile.py` must never close cards for
  an `ok:false` repo (state unknown).
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

## LLM side-jobs

Three independent LLM features share the same auth (a Claude **subscription** token
from `claude setup-token` via `anthropics/claude-code-action` - NOT an Anthropic
API key) and the same injection model (only trusted workflow prompts and
owner/maintainer-authored text are instructions; target content and optional
search output are delimited untrusted data; the LLM never gets `FLEET_TOKEN`):
Every `anthropics/claude-code-action` LLM step is pinned to `v1.0.161` at commit `fad22eb3fa582b7357fc0ea48af6645851b884fd` and passes `--model sonnet`.
The pinned release resolves `@anthropic-ai/claude-agent-sdk` to `0.3.197`; on the Anthropic API, Claude Code versions v2.1.197 and later resolve `sonnet` to Sonnet 5.

- **`triage.yml` - automatic, lightweight, advisory PR-card context.** Triggered by `scan-backstop` / `reconcile.py` and the ingest fast path for pure `needs-decision` pr-review cards whose current `head_sha` does not match `triaged_sha`.
  It is opt-out through `auto_triage` (global default true, per-repo override allowed) and inert unless `CLAUDE_CODE_OAUTH_TOKEN` is present.
  It checks out the target PR head read-only with `FLEET_TOKEN`, `persist-credentials: false`, then runs Claude with lower `--max-turns` than deep-review to produce only structured `{summary, product_implications, recommended_next_step}` context.
  It writes the visible `### Triage` section by a trusted workflow step with `github.token`, never by Claude, and the result is advisory only.
  It never changes classification, labels, checkbox decisions, `apply_decision.py`, merge/close/approve behavior, fork-CI safety, author filtering, or conflict routing.
  Before dispatch, the queueing path writes `triaged_sha=<head_sha>` and `triage_status=queued`, so errors and timeouts fail open without retriggering the same head on every scan.
  Existing open pr-review cards with no `triaged_sha` are intentionally stale and backfill once on the next eligible scan.
  Optional `READONLY_TOKEN` search uses the unchanged `wheelhouse-search` wrapper and remains untrusted evidence only.
  The Claude action allows only `github-actions[bot]`, never `*`, because scan/ingest dispatches use `github.token`.
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
  Its final response is captured from the action's `execution_file` output by preferring the clean `type: "result"` event's `result` string, falling back to the last assistant text, and the trusted workflow step posts that text as a card comment with `github.token`.
  If no usable output is present, the workflow posts "Deep review ran but produced no verdict (see the workflow run logs)." and fails the run.
  The ONLY gate is `CLAUDE_CODE_OAUTH_TOKEN`: when it is ABSENT the workflow posts a one-line "Deep-review needs CLAUDE_CODE_OAUTH_TOKEN configured to run." note instead of silently no-opping.
  Manual triggering means there is no runaway-cost reason for a config flag, so the old `deep_review` flag was removed entirely - config, `load_config`, and the `deep-review-enabled` CLI.
- **`nl_decisions`** in `decision-handler.yml`: a plain-English owner comment is
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
  The prompt carries the card's prior thread as owner-scoped conversation history
  so follow-up questions keep continuity (see the conversation-memory bullet in
  Sharp edges for the trusted-author rule).
  Deep-review uses the same wrapper under the same optional `READONLY_TOKEN`
  trust model, but only for advisory verdict context.

## Contributor-facing copy

Messages Wheelhouse posts onto **target repos** (e.g. a rebase nudge on a contributor's PR) speak naturally, like a friendly maintainer bot.
They must not name the product ("Wheelhouse") or use internal-state jargon ("maintainer queue", "resurface", bucket/kind names).

Owner-facing decision cards and comments on **this repo's** issues are the private queue; those may keep the Wheelhouse name and internal vocabulary.

## Validation

No build step.
Validate with `python -m py_compile scripts/*.py tests/*.py`.
Run the unit tests:
- `python tests/test_decision.py` - mocks the LLM, no network, and also covers the non-consuming investigate routing, allow-set, and `clear_checkbox`.
- `python tests/test_nl_decisions_search.py` - offline YAML wiring checks for the optional READONLY_TOKEN search path, token isolation, prompt gating, and unchanged `nl-route`/`execute` boundary.
- `python tests/test_card_refresh.py` - the card-refresh change-detection, refreshability-guard, and label-replace logic, pure functions, no network.
- `python tests/test_reconcile.py` - reconcile routing and stale-card self-healing, no network.
- `python tests/test_merge_conflict.py` - mergeability fail-open vs CONFLICTING routing, idempotent rebase nudges, author-filter nudge skips, and reconcile self-healing for conflicted PR cards, no network.
- `python tests/test_ci_autoapprove.py` - the shared `ci_safety` verdict, `pull_request_target` posture detection, and the auto-approve-vs-card routing plus scan-log observability in `build_repo`, all with the network-touching helpers stubbed.
- `python tests/test_author_filter.py` - queue author filtering across PR review, CI approval, and issue triage, no network.
- `python tests/test_auto_triage.py` - automatic PR-card triage config defaults/overrides, per-head cache and legacy-card backfill, rendered section/no-mention behavior, reconcile/ingest dispatch gates, and `triage.yml` token isolation, all offline.
- `python tests/test_deep_review.py` - the always-on/code-grounded deep-review and Investigate wiring: render options, the removed enable flag, the token-absent note, the `persist-credentials: false` checkout plus read-only tool isolation, the narrow `allowed_bots`, the optional READONLY_TOKEN-gated `wheelhouse-search` wiring, the action-output verdict capture, issue-only manual dispatch, and the handler's immutable-input `workflow_dispatch` trigger, all by inspecting the scripts/YAML, no network.
YAML-parse `.github/workflows/*.yml` plus `wheelhouse.config.yml` plus `.github/ISSUE_TEMPLATE/*.yml`.
Run `actionlint` if available; fetch the binary via its `download-actionlint.bash` if not.
The live LLM paths (auto triage, deep-review, nl_decisions) can only be exercised end-to-end in CI with the token set and, for nl_decisions, the flag on.
Secrets the maintainer must add: `FLEET_TOKEN` always, `CLAUDE_CODE_OAUTH_TOKEN` for auto triage/deep-review and/or nl_decisions, and optionally `READONLY_TOKEN` public-read only for auto triage, nl_decisions, and deep-review search.
