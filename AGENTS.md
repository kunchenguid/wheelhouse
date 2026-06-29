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
  refresh the card. `render_card.py` writes that marker, but
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
  is a full no-op when nothing changed), `deep-review` (ALWAYS-ON, code-grounded;
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
  `parse_state_block`, `authorized`, `state`, `nl-decisions-enabled`),
  `render_card.py` (render + card CRUD; `CHECKBOX_OPTIONS`/`OPTION_LABELS` carry
  the per-kind checkboxes, including the non-consuming `investigate` box on
  pr-review/issue-triage), `apply_decision.py` (deterministic `parse` then
  `execute`; the NON-CONSUMING `investigate` routing + `clear-checkbox`; plus the
  natural-language `nl-eligible`/`nl-prompt`/`nl-route` that map an owner's
  free-text comment to a structured intent),
  `build_item.py` (normalize ingest payload), `reconcile.py` (backstop
  create/**refresh**/close). `apply_decision`/`reconcile`/`render_card` import
  `wheelhouse_core` (and `build_item` imports `render_card`) via
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

- Decision cards are machine-created. The card body's hidden state block and the
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
  order remains the order provided in the card body/state. The shared pure
  helpers live in `render_card.py`
  (`material_changed`, `is_refreshable`, `plan_label_update`); `reconcile.py`
  pre-checks them (using the card row it already listed) so the common
  no-change case never hits the API, and `upsert_card` re-checks them before it
  edits (defense in depth for the event path). Three rules are load-bearing and
  must not be loosened:
  - **Only refresh a pure `needs-decision` card.** A re-render resets the card's
    checkboxes, so a card already `processing`/`resolved`/`blocked` is left
    completely untouched - refreshing one would clobber an in-flight decision or
    race the decision-handler. (`is_refreshable` is the guard; the lock set is
    `NON_REFRESHABLE_LABELS`.) This is the chosen safe rule.
  - **No-op when nothing material changed.** An unchanged card gets no body edit,
    no label churn, and no comment - never rewrite a card just to put back an
    identical body. The check is a cheap dict compare of the state block's
    material fields, which is why those fields are carried in the state JSON.
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
- Natural-language decisions are owner-comment-only and structured: the LLM
  returns `{mode: action|answer|clarify, action?, free_text?, answer?}` to
  `decision.json` and nothing else. `apply_decision.py nl-route` is the trust
  boundary - it validates `action` against the per-kind allowlist and only then
  sets the `decision` output that makes the SAME deterministic `execute` run
  (so every guard - allowlist, head-SHA re-check, fork-CI HOLD, token isolation,
  concurrency - applies unchanged). `answer`/`clarify` only post a card comment
  and leave the card open. The LLM is restricted to the `Write` tool and gets
  only this repo's token, never `FLEET_TOKEN` - it maps intent, it never acts.
- Token discipline per step: scan/execute and the read-only target reads for the
  LLM (`deep-review` prepare + its target-code checkout, decision-handler
  `nl-fetch`) use `FLEET_TOKEN`; all
  card writes - including every `issue-ops/labeler` step (its `github_token`
  defaults to `github.token`, passed explicitly here) - use `github.token`. The
  card's own comment thread is also this repo's data, so the NL `nl-comments`
  fetch uses `github.token`, NOT `FLEET_TOKEN`. Mixing them either breaks
  cross-repo acting or creates a re-trigger loop. The LLM step itself never gets
  `FLEET_TOKEN`; target content reaches it only as pre-fetched, delimited
  untrusted data inside the prompt, OR (for deep-review) as code already on disk
  from a `persist-credentials: false` checkout, so NO token is left on disk for
  the LLM to read.
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
  `actions: write`. The manual `needs-deep-review` label path is unchanged (a
  human applying it raises the `labeled` event normally). `investigate` is in the
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
  this widens the trust model: the LLM is still `--allowedTools Write`, still gets
  only this repo's token (never `FLEET_TOKEN`), and `nl-route`'s allowlist
  re-validation is unchanged.
- `wheelhouse_core.py scan` is resilient: a repo that fails to read is reported as a
  warning (`ok:false`) and skipped, and `reconcile.py` must never close cards for
  an `ok:false` repo (state unknown).
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
  `build_repo` (the `FLEET_TOKEN` scan context), for each `needs-ci-approval` PR:
  if auto-approve is enabled AND the verdict is `safe` (no risky files, no
  posture, no read error), call `approve_ci` and emit NO card (log a `::notice::`
  to stderr - never stdout, which carries scan.json); otherwise emit the
  `ci-approval` card exactly as before, carrying the safety warning. **Fail closed
  everywhere**: an unsafe verdict, a `hold`/`error` from the approve, or an
  approve that throws all fall back to a card - nothing is silently dropped.
  Idempotent by construction: once approved the next scan sees CI running/results
  (not `needs-ci-approval`), so it is not re-approved; a later push that adds a
  workflow file or flips the posture routes the PR back to a card. The auto path
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

Two independent LLM features share the same auth (a Claude **subscription** token
from `claude setup-token` via `anthropics/claude-code-action` - NOT an Anthropic
API key) and the same injection model (only owner-authored text is an
instruction; target content is delimited untrusted data; the LLM gets only this
repo's token, never `FLEET_TOKEN`):

- **`deep-review.yml` - ALWAYS-ON, code-grounded (no enable flag).** Triggered by
  ticking the **Investigate** box on a card or applying the `needs-deep-review`
  label. It checks out the TARGET's code read-only (`FLEET_TOKEN`,
  `persist-credentials: false`, the PR head for a review card / the default branch
  for an issue card) and runs Claude restricted to `--allowedTools
  Read,Grep,Glob,Write` over that checkout - so it traces real code paths, never
  just the diff, and can NEVER execute the target's code (no Bash, no build/test).
  Claude writes `verdict.md`; the workflow posts it as a card comment with
  `github.token`. The ONLY gate is `CLAUDE_CODE_OAUTH_TOKEN`: when it is ABSENT the
  workflow posts a one-line "Deep-review needs CLAUDE_CODE_OAUTH_TOKEN configured
  to run." note instead of silently no-opping. (Manual triggering means there is
  no runaway-cost reason for a config flag, so the old `deep_review` flag was
  removed entirely - config, `load_config`, and the `deep-review-enabled` CLI.)
- **`nl_decisions`** in `decision-handler.yml`: a plain-English owner comment is
  mapped to a structured intent (see Sharp edges). Opt-in: inert unless
  `nl_decisions: true` AND `CLAUDE_CODE_OAUTH_TOKEN` present. Claude is restricted
  to the `Write` tool (`claude_args: --allowedTools Write`) - it writes
  `decision.json` and runs no commands. The prompt carries the card's prior
  thread as owner-scoped conversation history so follow-up questions keep
  continuity (see the conversation-memory bullet in Sharp edges for the
  trusted-author rule).

## Validation

No build step. Validate with `python -m py_compile scripts/*.py tests/*.py`, run
the unit tests (`python tests/test_decision.py` - mocks the LLM, no network, and
now also the non-consuming investigate routing / allow-set / `clear_checkbox`,
`python tests/test_card_refresh.py` - the card-refresh change-detection /
refreshability-guard / label-replace logic, pure functions, no network,
`python tests/test_reconcile.py` - reconcile routing and stale-card self-healing,
no network, `python tests/test_ci_autoapprove.py` - the shared `ci_safety`
verdict, `pull_request_target` posture detection, and the auto-approve-vs-card
routing in `build_repo`, all with the network-touching helpers stubbed, and
`python tests/test_deep_review.py` - the always-on/code-grounded deep-review +
Investigate wiring: render options, the removed enable flag, the token-absent
note, the `persist-credentials: false` checkout + read-only tool isolation, and
the handler's `workflow_dispatch` trigger, all by inspecting the scripts/YAML, no
network), and
YAML-parse `.github/workflows/*.yml` + `wheelhouse.config.yml` +
`.github/ISSUE_TEMPLATE/*.yml` (run `actionlint` if available; fetch the binary
via its `download-actionlint.bash` if not). The live LLM paths (deep-review,
nl_decisions) can only be exercised end-to-end in CI with the token set (and, for
nl_decisions, the flag on). Secrets the maintainer must add: `FLEET_TOKEN`
(always) and `CLAUDE_CODE_OAUTH_TOKEN` (for deep-review and/or nl_decisions).
