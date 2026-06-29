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
  manual review and fails closed.

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
  is a full no-op when nothing changed), `deep-review` (phase 2, inert),
  `no-mistakes-required` (PR-to-`main` gate: the job `name:` MUST stay exactly
  `PR must be raised via no-mistakes` - it is the check name the fleet convention
  and this repo's own `wheelhouse.config.yml compliance_check` reference - and it
  passes only when the PR body carries the no-mistakes signature
  `Updates from [git push no-mistakes](https://github.com/kunchenguid/no-mistakes)`,
  with bot authors skipped; Wheelhouse dogfoods on itself the same gate it enforces
  on the fleet, so contributions go through `git push no-mistakes` - see
  `CONTRIBUTING.md`).
- **Scripts:** `wheelhouse_core.py` (scan/classify/dedup/security gate + shared utils
  `parse_state_block`, `authorized`, `state`, `nl-decisions-enabled`),
  `render_card.py` (render + card CRUD), `apply_decision.py` (deterministic
  `parse` then `execute`, plus the natural-language `nl-eligible`/`nl-prompt`/
  `nl-route` that map an owner's free-text comment to a structured intent),
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
- Token discipline per step: scan/execute and the read-only target fetch for the
  LLM (`deep-review` prepare, decision-handler `nl-fetch`) use `FLEET_TOKEN`; all
  card writes - including every `issue-ops/labeler` step (its `github_token`
  defaults to `github.token`, passed explicitly here) - use `github.token`. The
  card's own comment thread is also this repo's data, so the NL `nl-comments`
  fetch uses `github.token`, NOT `FLEET_TOKEN`. Mixing them either breaks
  cross-repo acting or creates a re-trigger loop. The LLM step itself never gets
  `FLEET_TOKEN`; target content reaches it only as pre-fetched, delimited
  untrusted data inside the prompt.
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
- The `repository_dispatch` event type is `wheelhouse-item`, but `ingest.yml`
  also listens for the legacy `triage-item` (`types: [wheelhouse-item,
  triage-item]`). It is a cross-repo wire contract: source repos onboarded before
  the rename still send `triage-item`, so the alias must stay until every source
  dispatcher is updated. Same idea as the state-marker back-compat - rename the
  name, keep accepting the old one.

## LLM side-jobs (both opt-in, both off by default)

Two independent LLM features share the same auth (a Claude **subscription** token
from `claude setup-token` via `anthropics/claude-code-action` - NOT an Anthropic
API key) and the same injection model (only owner-authored text is an
instruction; target content is delimited untrusted data; the LLM gets only this
repo's token):

- **`deep_review`** + `deep-review.yml`: label `needs-deep-review` -> Claude
  posts a read-only merit/triage verdict. Inert unless `deep_review: true` AND
  `CLAUDE_CODE_OAUTH_TOKEN` present.
- **`nl_decisions`** in `decision-handler.yml`: a plain-English owner comment is
  mapped to a structured intent (see Sharp edges). Inert unless
  `nl_decisions: true` AND `CLAUDE_CODE_OAUTH_TOKEN` present. Claude is restricted
  to the `Write` tool (`claude_args: --allowedTools Write`) - it writes
  `decision.json` and runs no commands. The prompt carries the card's prior
  thread as owner-scoped conversation history so follow-up questions keep
  continuity (see the conversation-memory bullet in Sharp edges for the
  trusted-author rule).

## Validation

No build step. Validate with `python -m py_compile scripts/*.py tests/*.py`, run
the unit tests (`python tests/test_decision.py` - mocks the LLM, no network,
`python tests/test_card_refresh.py` - the card-refresh change-detection /
refreshability-guard / label-replace logic, pure functions, no network, and
`python tests/test_reconcile.py` - reconcile routing and stale-card self-healing,
no network), and
YAML-parse `.github/workflows/*.yml` + `wheelhouse.config.yml` +
`.github/ISSUE_TEMPLATE/*.yml` (run `actionlint` if available; fetch the binary
via its `download-actionlint.bash` if not). The live LLM paths (deep-review,
nl_decisions) can only be exercised end-to-end in CI with the flag on and the
token set. Secrets the maintainer must add: `FLEET_TOKEN` (always) and
`CLAUDE_CODE_OAUTH_TOKEN` (deep_review and/or nl_decisions only).
