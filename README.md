# Wheelhouse

> A ship's **wheelhouse** is where the captain stands to steer. This is your wheelhouse for open-source maintenance: whatever across your repos needs *your* hand surfaces here, and you make the call.

A personal, always-on, cross-repo **"what needs my decision"** command center, built entirely on GitHub Issues + GitHub Actions.
Every issue in this repo is one pending decision about the repositories you maintain - a PR worth merging, a fork-CI run worth approving, an issue worth triaging.
The scheduled scan keeps the queue focused on other people's work: PRs and issues authored by the repo owner, the configured maintainer, or bots stay out of the scan-built worklist, while missing author metadata fails open.
PR-review candidates that GitHub reports as merge-conflicted leave the maintainer worklist until the contributor rebases or merges the base branch and pushes a mergeable head.
If GitHub is still calculating mergeability after a base-branch update, Wheelhouse waits for a conclusive answer without changing that PR's card membership.
You drive cards by ticking a checkbox, replying with a slash-command, or replying in plain English; a workflow executes your call on the real repo and closes the card after a successful resolving action.
If a non-retryable action error occurs, Wheelhouse leaves the card open and marks it `blocked` for manual follow-up.
No server, no database, no bot to host - just this repo and a small set of secrets.

Fork it, edit one config file, add one required secret, and you have your own Wheelhouse.

Changing the Wheelhouse codebase itself goes through [`CONTRIBUTING.md`](CONTRIBUTING.md).
PRs to `main` must be raised by `git push no-mistakes`, which writes the signature checked by the **"PR must be raised via no-mistakes"** workflow.

## How it works

- **The queue is the issue list.** Each open issue is one decision that needs you. Open = pending, closed = consumed.
- **Labels carry state:** `needs-decision` (in the queue), `pending-triage` (first auto-triage attempt is still publishing), `processing` (a handler is acting), `resolved` (a successful resolving action consumed the card), `blocked` (a manual hold or non-retryable action error needs follow-up), `wheelhouse:manual-merge-required` (scan-time auto-merge proved the current head needs a manual workflow-aware merge), plus metadata labels `repo:<name>`, `kind:<pr-review|ci-approval|issue-triage>`, `priority:<high|med|low>`.
- **Each issue body is a decision card:** a link to the target, the target author shown as plain text instead of a notifying `@mention`, the situation, an overlap note, a recommended action, and quick-decision checkboxes.
  A scan-created contributor CI-approval card that holds for changed workflow/action files also includes a deterministic, read-only *Security review (advisory)* section to inform the same manual approval decision.
  A PR-review card whose final auto-merge gate proves a history-only workflow touch includes a head-scoped *Manual merge required* section with bounded evidence.
  When successful auto triage returns a fresh structured recommendation, the card shows an *Accept recommendation* checkbox and hides the older top-level recommended-action text so there is one primary recommendation surface.
  A brand-new PR-review or issue-triage card that is waiting for its first auto-triage attempt temporarily shows a placeholder instead of those checkboxes; the normal checkboxes appear as soon as that attempt completes, even if triage fails.
  A hidden HTML comment holds the machine-readable state.
  The one deliberate exception: merging a fleet contributor's PR through a card posts a friendly, `@`-mentioning thank-you comment *on that PR* (`thank_on_merge`, default on) - good OSS etiquette on the contributor's own PR, distinct from the never-`@`-mention rule for your private decision cards.
- **GitHub Actions are the handlers:** they create cards, refresh pending cards when tracked target state or the rendered card title changes, reflect target activity for recently-updated sorting, execute your decisions, and reconcile the queue against live repo state.
  The hourly scan retries transient GitHub query failures and records repeated unreadable-repo failures in a closed health-ledger issue, so a persistently-dark fleet repo eventually fails the run loudly instead of remaining invisible.

```
 source repos ──dispatch──▶ ingest ─────────┐
                                            ▼
 scheduled scan ──reconcile──▶  this repo's ISSUES  ◀── you tick / comment
 (hourly keep-current path)         (the queue)             │
                                            └── decision-handler ──acts on──▶ your fleet repos
```

The deterministic core (ingest + decision-handler + scan-backstop) runs with a single secret and no LLM.
Three agent-assisted features layer on top: **auto triage** adds lightweight Summary / Product implications / Recommended next step context to PR-review cards (`auto_triage`) and issue-triage cards (the independent `auto_triage_issues`) and can add a deterministic *Accept recommendation* shortcut for fresh structured recommendations, **deep-review** is always available when you tick a card's *Investigate* box for a code-grounded read of the target, and the opt-in `nl_decisions` lets you drive a card in plain English.
All three use one provider-agnostic, versioned runtime contract and can use an optional `READONLY_TOKEN` through the scoped read-only production search path.
The disabled Codex adapter keeps that credential in a typed host broker instead of exposing it to the model worker.
The current production selection is the exact pinned direct Claude Action implementation, executed in a separate read-only reusable model workflow from a bounded content-addressed task handoff.
The local reusable-workflow call resolves at the caller's exact commit, and the model job independently verifies that observed source revision before hydrating inputs or admitting provider spend.
Its runtime proof records the observed read-only job, verified artifact, exact action tools, local no-remote repository, cancellation outcome, and bounded transcript boundary without claiming the stronger disabled Codex worker sandbox.
Unenforceable Claude provider budgets and the exact end-to-end hard deadline are explicit unavailable values, while the child-job timeout plus a durable conservative spend checkpoint constrains each direct invocation.
Claude is the named production primary across every action selection.
Codex CLI app-server remains disabled non-target adapter evidence because the current ChatGPT Pro plus public-repository topology has no supported secure noninteractive subscription-auth path.
OpenCode with Z.AI Coding Plan is a deferred disabled candidate only, with no adapter or credential request authorized in this phase.
No alternative provider is an active target or fallback.
See [Agent runtime operations](docs/AGENT_RUNTIME.md) for selection, provenance, failure recovery, and the provider-neutral adapter boundary.
When model-authored text lands back on a Wheelhouse decision card, bare GitHub `#N` references are qualified to the target repo before posting so they do not autolink to the Wheelhouse repo.
For auto triage and deep review, trusted rendering also preserves known claude-code-action harness polling/status transcript lines but prefixes them with `[automated status]` so they are visible as metadata instead of review substance.
When auto triage will run for a newly created PR-review or issue-triage card, Wheelhouse holds the card under `pending-triage` and publishes the decision boxes only after that first attempt records a result or an unavailable note.

## Setup - a numbered checklist

Follow these top to bottom.
You only ever edit **one file** (`wheelhouse.config.yml`) and add **one required secret** (`FLEET_TOKEN`).

### 1. Fork it

Click **Fork** ▸ **Create a new fork** to copy this repo into your own account.
Keeping it **public** makes your decisions world-readable - a transparency feature; see [Security notes](#security-notes).
A **private** repo works too, in which case `FLEET_TOKEN` must also be able to read this repo's issues.

### 2. Edit `wheelhouse.config.yml`

This is the only file you edit.
The owner is **not** set here - every workflow derives it from `github.repository_owner`, so the file works unchanged on your account.
List the repos you maintain and how to read their checks:

```yaml
repos:
  - name: my-service                      # repo name only (resolved under your owner)
    compliance_check: "required-policy-check"  # exact required gate check, or null
    test_check_patterns: ["test", "build", "e2e"]  # substrings that identify your test/CI checks
    # auto_approve_ci: false              # optional per-repo override
    # auto_merge: true                    # optional scan-time merge opt-in (also needs VISION.md)
    # auto_triage: false                  # optional per-repo LLM spend opt-out (pr-review)
    # auto_triage_issues: false           # optional per-repo LLM spend opt-out (issue-triage)
    # triage_attempt_cap_per_revision: 2  # optional per-repo queued-attempt cap (1..5)
    # pending_contributor_cleanup: false  # optional per-repo stale PR cleanup override
    # pending_contributor_cleanup_days: 14
    # pending_contributor_reminder_days: 10
    # pending_contributor_cleanup_targets: ["pr"]
    # thank_on_merge: false                # optional per-repo opt-out for the merge thank-you comment
    # thank_on_merge_message: "Cheers @{author}, this just merged!"  # optional per-repo wording
  - name: my-cli
    compliance_check: null
    test_check_patterns: ["ci", "test"]

maintainer: ""            # optional extra login allowed to drive decisions and treated as your work
auto_triage: true         # LLM side-job: quick advisory PR-card triage (DEFAULT ON)
auto_triage_issues: true  # LLM side-job: quick advisory issue-card triage (DEFAULT ON, independent of auto_triage)
triage_attempt_cap_per_revision: 2  # queued attempts per card-kind source revision (1..5)
triage_daily_ceiling: 1200           # fleet-wide auto-triage reservations per UTC day (1..2000)
nl_decisions: false       # LLM side-job: reply to a card in plain English (off by default)
card_issues: true         # also scan un-addressed issues, not just PRs; owner/maintainer/bot authors are skipped
auto_approve_ci: true     # auto-approve provably-safe fork-CI runs (DEFAULT ON; see Security notes)
auto_merge: false         # scan-time merge (DEFAULT OFF; requires per-repo VISION.md and a fresh triage verdict)
thank_on_merge: true      # post a friendly @-mention thank-you on a merged contributor PR (DEFAULT ON)
# thank_on_merge_message: "Thanks @{author} - merged! Really appreciate the contribution."  # optional wording override
pending_contributor_cleanup: false       # stale pending-contributor PR cleanup (DEFAULT OFF)
pending_contributor_cleanup_days: 14     # close after this many days, only after a reminder
pending_contributor_reminder_days: 10    # remind once after this many days
pending_contributor_cleanup_targets: ["pr"]  # PR-only MVP
# (Deep review has no flag - it's always available once CLAUDE_CODE_OAUTH_TOKEN is set.)
```

Wheelhouse reads every matching check context.
If GitHub returns duplicate contexts with the same compliance check name, any failing context wins, then any pending context, and only an all-success group passes.
GitHub's own rollup `FAILURE` or `ERROR` also fails closed so an accidental false green cannot make a card look merge-ready.

> **Heads-up - `auto_triage` defaults ON.**
> When this key is absent it is treated as `true`, so a fresh fork with `CLAUDE_CODE_OAUTH_TOKEN` gets lightweight automatic PR-review summaries without another config edit.
> The cache is keyed by PR `head_sha`; existing open cards with no fresh `triaged_sha` marker backfill on the next scan, while later unchanged scans do not spend again unless a trusted recovery path clears the cache and the spend guards allow another queued attempt.
> A brand-new eligible PR-review card is first labeled `pending-triage` and has no decision checkboxes until that first attempt completes.
> Completion publishes the normal card whether triage succeeds, fails, times out, or cannot be started.
> If successful triage also returns a fresh structured recommendation with an allowlisted action, Wheelhouse adds an *Accept recommendation* checkbox that still routes through the deterministic handler.
> Set it to `false` to opt out globally for token-spend control, or add `auto_triage: false` to a single `repos:` entry.
> Auto triage is advisory only: it never changes routing, never acts, and never replaces your checkbox or slash-command decision.

> **Heads-up - `auto_triage_issues` defaults ON, independently of `auto_triage`.**
> Same idea as PR auto triage, but for issue-triage cards, gated by its own flag - toggling either one never affects the other.
> Issues have no head SHA, so the cache is keyed by the issue's GraphQL `updatedAt`, which advances on any edit or new comment; existing open cards with no fresh `triaged_sha` marker backfill on the next scan, while later unchanged scans follow the same spend-guarded recovery rule as PR auto triage.
> Because an issue's `updatedAt` is not a material card field, a new comment can make a card eligible for a fresh spend-guarded triage attempt.
> When that attempt is eligible, its single queued-card write owns the new revision; when it is not eligible, a strictly newer `updatedAt` triggers one deterministic card refresh instead.
> A brand-new eligible issue-triage card uses the same `pending-triage` placeholder and publishes its checkboxes when the first attempt completes, success or failure alike.
> A successful structured issue recommendation can also add the same deterministic *Accept recommendation* checkbox, limited to issue-safe actions.
> Set it to `false` to opt out globally, or add `auto_triage_issues: false` to a single `repos:` entry.
> It checks out the repo's default branch read-only for a little code context (there is no diff to review) and is advisory only, exactly like PR auto triage.

> **Automatic-triage spend bounds.**
> `triage_attempt_cap_per_revision` counts queued attempts for one card kind and source revision, defaults to 2, accepts integers from 1 through 5, and may be overridden per repository.
> An invalid cap fails closed to 1 and logs an error.
> `triage_daily_ceiling` is one global reservation budget per UTC calendar day, defaults to 1200, accepts integers from 1 through 2000, and has no per-repository override.
> An invalid daily ceiling fails closed to 0, so no automatic triage is queued until the configuration is corrected.
> If a reservation is denied because the budget is unavailable, a held card is published with its normal decision controls and a concise deferred-advisory note, without consuming an attempt for that revision.
> Its closed maintenance-ledger issue stores only a version, UTC day, and reserved count; a day rollover resets the count in the same by-number verified write that reserves the new day's first unit.
> Each verified reservation authorizes at most one existing triage dispatch, and that dispatch can make at most two model calls because schema repair is bounded to one additional call.
> The default worst case is therefore 1200 automatic-triage reservations and 2400 model calls per UTC day across scans, ingest runs, and explicitly operator-triggered replay waves.
> Reservations happen before the card is marked queued, so a crash can waste daily capacity but cannot undercount or authorize extra spend.
> Owner-triggered deep review and natural-language decisions are outside this ceiling because they require deliberate owner actions and separate durable claims.

> **Heads-up - `auto_approve_ci` defaults ON.**
> When this key is absent it is treated as `true`, so a fresh fork auto-approves fork-CI runs that the security gate proves safe (no CI-file changes, the PR targets the repo default branch, no `pull_request_target` workflow, and all safety reads succeed) and only raises a card for risky or uncertain contributor-authored runs.
> A run is approved only after Wheelhouse verifies it is the target PR's awaiting `action_required` run: GitHub-populated `workflow_run.pull_requests` must contain exactly that PR, and fork-originated empty associations must match the PR `head_sha` plus `head_branch`.
> If multiple verified pending runs share a stable workflow identity, Wheelhouse approves only the newest run; runs without a stable workflow identity stay distinct.
> After a safe run is approved, Wheelhouse waits for its checks to finish before classifying the PR or creating a card.
> During that wait, an existing pure PR-review card is kept open and the hourly scan refreshes it to the observed head's non-green pending state; no transient-head triage is queued.
> If the approval call verifies that no matching run is awaiting approval, the scan normally emits no card and any stale CI-approval card follows the [scheduled backstop lifecycle](#daily-use).
> For a contributor fork whose safe no-op PR is conclusively `CONFLICTING`, it also posts the existing one-per-head rebase nudge before consuming the card; it never does so for an approved run or unresolved mergeability.
> If the mergeability settlement required for that exception errors, the repo scan is unhealthy and reconcile preserves an existing card instead.
> The scan log records every CI-approval candidate it handles: approved runs and verified no-pending runs emit one `::notice::`, contributor PRs that need a decision emit one `::warning::wheelhouse auto-approve carded <repo>#<pr>: ...` line, and excluded owner, maintainer, or bot PRs that cannot be approved emit one `::warning::wheelhouse auto-approve suppressed-card <repo>#<pr>: ...` line.
> Both warning forms include the safety or uncertainty reason and any approval status/message.
> Set it to `false` to opt out for contributor PRs (every contributor fork-CI candidate raises a card, as you click to approve each), or add `auto_approve_ci: false` to a single `repos:` entry to opt that one repo out.
> Owner, maintainer, and bot-authored fork PRs are excluded from the decision queue, so Wheelhouse still runs the safety-gated approve/noop path for safe CI and suppresses their cards.
> See [Security notes](#security-notes).

> **Heads-up - `auto_merge` is ON fleet-wide in this fork; `VISION.md` is the practical opt-in.**
> The fork-and-own code fallback is OFF: when the key is absent, Wheelhouse treats it as `false`; the generic example above preserves that shipped-default posture by setting it explicitly to `false`.
> This repository's committed `wheelhouse.config.yml` sets `auto_merge: true` globally, so forks of this repository inherit the fleet-wide switch on, while remaining fully inert until a target repository commits a non-empty `VISION.md` on its default branch.
> A target repository becomes practically opted in when it commits a non-empty default-branch `VISION.md`; repositories without one remain inert.
> A per-repo `auto_merge: false` still overrides the fleet-wide setting to opt one repo back out, and applying `wheelhouse:no-auto-merge` to a target PR stops scan-time auto-merge for that PR without affecting manual `/merge`.
> The hourly `scan-backstop`, not an ingest dispatch, can merge an eligible PR.
> It requires a fresh successful PR auto-triage verdict for the current head, base, and `VISION.md` revision that recommends merge, confirms vision alignment, and assigns eligible behavior class A, B, or C (class C must be strictly opt-in and default off).
> Before merging, Wheelhouse also requires a clean, merge-ready live PR with configured compliance and tests green, a returning non-maintainer human contributor, no same-closing-issue ambiguity from another open PR, no excluded sensitive/governance/release/dependency/security/auth/billing/migration/schema/install/default-surface files, and at most 20 changed files and 1,000 changed lines.
> It re-reads the head, base, `VISION.md`, merge state, checks, same-closing-issue overlap, escape-hatch label, and card activity immediately before the existing deterministic merge call, so any uncertainty leaves the PR for normal review.
> Every PR-review card shows each guarded criterion as `MET`, `UNMET`, or `UNAVAILABLE` with concise evidence from the same authoritative evaluator; these rows are read-only context and never authorize a merge.
> When current-head triage succeeds and recommends merge but produces no structured behavior verdict, the eligible behavior class is `UNMET`, while its dependent behavior checks and verdict revision bindings are `UNAVAILABLE` because they were not evaluated; authorization still requires every criterion to be `MET`.
> If the final merge gate finds a workflow-file touch only in commit history after the complete current net diff passed the earlier exclusions, the card gains a `wheelhouse:manual-merge-required` label and a head-scoped manual-merge section with the source PR, commit, and bounded path evidence.
> The matching head is not claimed again on later hourly runs, and its G7 criterion is `UNMET`; a new head clears the hold through normal refresh, requires fresh triage, and still passes through the same live final workflow gate before any merge.
> Wheelhouse never auto-reverts; every automatic merge closes its decision card with an audit record and appends to a closed, durable auto-merge ledger issue in the hub.

> **Heads-up - `thank_on_merge` defaults ON (no Claude token needed).**
> When this key is absent it is treated as `true`, so merging a fleet contributor's PR through a decision card - checkbox *Merge* or a natural-language "merge it" - posts one short, friendly comment on that PR that `@`-mentions the contributor.
> The thank-you side effect itself does not use Claude; a plain-English "merge it" still requires `nl_decisions` and `CLAUDE_CODE_OAUTH_TOKEN`.
> It is the one sanctioned contributor `@`-mention: your own decision cards never `@`-mention a target's author, but this comment is posted on the *contributor's* PR, where a thank-you `@`-mention is normal OSS etiquette.
> Owner, configured-maintainer, and bot authors are never thanked or `@`-mentioned.
> Customize the wording with `thank_on_merge_message` (`{author}` is replaced with the contributor's bare login, so include `@{author}` when you want the thank-you to mention them), globally or per repo; unset it to use the built-in default.
> This never affects the merge itself: if posting the comment fails (or the feature is off), the merge still succeeds exactly as before, with no retry and no reversal.
> Set it to `false` to opt out globally, or add `thank_on_merge: false` to a single `repos:` entry.

> **Heads-up - `pending_contributor_cleanup` defaults OFF.**
> When this key is absent Wheelhouse never auto-closes a target for contributor inactivity.
> When enabled, the scheduled scan watches only PRs with a provable pending-contributor ask created by Wheelhouse: a successful `/request-changes` review or a merge-conflict rebase nudge.
> A provable legacy rebase nudge that predates cleanup arming is eligible too, so an already-nudged conflicting PR can join the same reminder-then-close lifecycle without a new contributor-facing ask.
> The CI-approval security lane remains out of scope except for a cross-repo `needs-ci-approval` ci-noop PR with both a provable rebase nudge and an authoritative current `CONFLICTING` mergeability result.
> The scan re-checks that exception's mergeability immediately before a reminder or close, so a non-conflicting, `UNKNOWN`, or unreadable PR is skipped rather than acted on.
> It posts one reminder at `pending_contributor_reminder_days` and closes at `pending_contributor_cleanup_days` only if the reminder already exists.
> A rebase-specific repeat reminder uses the same visible `Automated reminder:` label as the initial conflict nudge; request-changes reminders retain their distinct wording.
> It skips instead of closing if any required target timeline or PR edit-history read fails, if the ask marker cannot be proven, if the PR head moved, if a non-maintainer human commented, reviewed, left a review comment, edited the PR body, pushed, or performed another target timeline action after the ask, if the target has an unaccounted post-ask update, or if the target has the `wheelhouse:keep-open` label.
> Maintainer and bot activity never reset the clock.
> A missing review timestamp is re-read by review ID when possible; a failed re-read or a genuinely unexplained target update still skips cleanup.
> Set it to `true` globally or on a single repo, and keep `pending_contributor_cleanup_targets: ["pr"]` for the current PR-only behavior.
> The reminder days, cleanup days, and targets can also be overridden per repo.

Not sure what your check names are?
After step 6, run the `scan-backstop` workflow and read its logs, or use the `checks` helper locally:
`GITHUB_REPOSITORY_OWNER=<you> GH_TOKEN=<token> python scripts/wheelhouse_core.py checks my-service`.

### 3. Create a `FLEET_TOKEN`

This is the token the machine uses to act on your other repos.
Only you can mint it (it's tied to your account).

1. GitHub ▸ **Settings** ▸ **Developer settings** ▸ **Personal access tokens** ▸ **Fine-grained tokens** ▸ **Generate new token**.
2. **Repository access** ▸ **Only select repositories** ▸ pick every repo you listed in `wheelhouse.config.yml` (and this repo too, if it is private).
3. **Permissions** ▸ Repository permissions: **Actions → Read and write**, **Contents → Read and write**, **Issues → Read and write**, **Pull requests → Read and write**.
   Leave **Workflows** unset (no access). Card-driven merges of PRs that touch `.github/workflows/**` are deliberately blocked for a manual UI merge instead of granting the fleet token Workflows write.
4. Generate, copy the token.
5. In **this** repo: **Settings** ▸ **Secrets and variables** ▸ **Actions** ▸ **New repository secret** ▸ name it exactly `FLEET_TOKEN`, paste the value.

That is the only secret the deterministic machine needs.

### 4. (Optional) Add the Claude production token for agent-assisted features

Skip this for the deterministic machine.
The direct Claude Action is the explicit production selection behind the shared action boundary.
Trusted jobs prepare immutable inputs and perform all card or target mutations, while the separate reusable model workflow has read-only permissions and returns only a verified normalized result artifact to the trusted consumer.
Do not add `OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, or an `auth.json` blob to this public repository.
Do not add credentials for disabled or deferred provider candidates.
See [Agent runtime operations](docs/AGENT_RUNTIME.md) before changing provider selection.
Three independent Claude-powered features share one token (`CLAUDE_CODE_OAUTH_TOKEN`):
Auto triage and deep review keep their action prompts independent of target size by writing fetched target content to runner files for Claude to read; PR diffs are bounded to 1,500,000 bytes on disk instead of being embedded in the action input.

- **Auto triage (default-on, opt-out)** - when a PR-review card is created or refreshed to a new head, Claude does a quick read-only pass and writes Summary, Product implications, and a Recommended next step into the card.
  The workflow asks for structured `recommended_action` and `recommended_reason` fields; trusted code normalizes them and only adds *Accept recommendation* when the action is safe for that card kind and any required reason text is present.
  It also requires source evidence and checks at least one anchored span against the fetched target text before publishing the advisory result.
  If a captured field is one of the known claude-code-action harness polling/status lines, trusted rendering preserves it and labels it `[automated status]`.
  It is cached by PR head SHA, so an unchanged hourly scan does not re-run it unless a trusted recovery path clears the cache and the spend guards admit another attempt.
  Newly created eligible cards are rendered as `pending-triage` placeholders and queued for that first triage attempt in the same scan or ingest run that creates them.
  The placeholder has no decision checkboxes, and the normal card is published when the attempt succeeds, fails, times out, or cannot be started.
  Existing pure pending PR-review cards that predate the feature backfill once on the next scan.
  Set `auto_triage: false` globally or per repo when you want to control token spend.
  Issue-triage cards get the same treatment under the INDEPENDENT `auto_triage_issues` flag (also default-on): since issues have no head SHA, it caches by the issue's `updatedAt` instead, checks out the repo's default branch read-only for a little code context, uses an issue-safe recommendation action set, and is opt-out the same way with `auto_triage_issues: false`.
  When a repository independently opts into scan-time `auto_merge`, successful PR triage can also return a structured behavior verdict for the deterministic merge gate.
  That verdict is read from the trusted default-branch `VISION.md` and current immutable PR diff, is bound to the PR head and base plus the `VISION.md` revision, and still cannot merge anything by itself.
- **Deep review (always-on)** - tick a card's *Investigate* box and Claude reviews the target's checked-out code without executing it.
  The repo owner can also apply the `needs-deep-review` label or run the `deep-review` workflow with only the decision-card issue number; that manual workflow path fetches the current card body with this repo's token.
  The workflow captures Claude's final response and posts it as the code-grounded merit/triage verdict.
  Known claude-code-action harness polling/status transcript lines are preserved in that verdict but labeled `[automated status]` before posting.
  There is **no flag** - it runs whenever you trigger it, as long as the token is set.
  With the token missing it posts a one-line "needs token" note on the card so you know why nothing ran.
  With an optional public-read `READONLY_TOKEN`, it can use the same scoped `wheelhouse-search` wrapper for advisory related-work and code context.
  Without `READONLY_TOKEN`, it keeps the production no-shell, Read/Grep/Glob-only behavior.
- **`nl_decisions` (opt-in)** - reply to a decision card in plain English and Claude maps it onto the existing actions (see [Daily use](#daily-use)).
  This one stays inert until `nl_decisions: true` **and** the token is present.
  With an optional public-read `READONLY_TOKEN`, its answer mode can search the target repo and configured fleet repos through a read-only `gh` wrapper.
  Without `READONLY_TOKEN`, it keeps the production no-shell behavior.

To set it up:

1. Generate a **Claude subscription** token (requires a Claude Pro/Max subscription): run `claude setup-token` in the Claude Code CLI.
   This is **not** an Anthropic API key - the workflows authenticate `anthropics/claude-code-action` with your subscription only.
   The workflows pin `anthropics/claude-code-action` to `v1.0.161` at `fad22eb3fa582b7357fc0ea48af6645851b884fd` and pass immutable `--model claude-sonnet-4-6` to every Claude step.
   The pinned action resolves `@anthropic-ai/claude-agent-sdk` to `0.3.197`.
   Trusted preflight and post-action bridge steps validate `AgentTask`, observed model identity, and atomic `AgentResult` before deterministic consumers run.
2. Add it as an Actions secret named exactly `CLAUDE_CODE_OAUTH_TOKEN`.
3. Optional: set `auto_triage: false` and/or `auto_triage_issues: false` in `wheelhouse.config.yml` if you do not want automatic PR-card or issue-card triage to spend Claude turns.
4. For the plain-English path, also set `nl_decisions: true` in `wheelhouse.config.yml`.
5. Optional: to let auto triage, deep review, and plain-English answers search related PRs, issues, and code across the target repo and configured fleet repos, add an Actions secret named exactly `READONLY_TOKEN`.
   Scope it for public read only and give it no write permissions.

In every case Claude only treats trusted workflow prompts and owner/maintainer-authored text as instructions; the target diff/issue/code and optional search output are untrusted data, and it never receives `FLEET_TOKEN`.
When `READONLY_TOKEN` is absent, `nl_decisions` runs with Read/Grep/Glob/Write, no shell tools, and no model `GH_TOKEN`.
Auto triage and deep review's no-token branches run with Read/Grep/Glob only, no shell tools, and no model `GH_TOKEN`.
When `READONLY_TOKEN` is present, search-enabled Claude steps receive it as GitHub CLI credentials for the scoped search wrapper.
Auto triage's GitHub write is the workflow-owned card edit, deep review's GitHub write is the workflow-owned card comment, `nl_decisions` actions are performed by the deterministic handler, and `READONLY_TOKEN` never authorizes an action.
Before auto-triage text, deep-review verdicts, or plain-English answer/clarify replies are posted back on a decision card, trusted workflow code rewrites bare GitHub `#N` refs to the target repo's `<owner>/<repo>#N` using deterministic card state, never the model's own text.
For auto-triage text and deep-review verdicts, trusted code also labels known automated harness polling/status lines without stripping them.
Already-qualified refs, URLs, Markdown link destinations, and code spans/blocks are left alone; the prompts ask Claude to write qualified refs too, but the rewrite is the guarantee.
The merge thank-you comment is intentionally outside that rewrite because it is posted on the target PR itself.
Deep review goes a step further: it explores the target's checked-out code without executing it, with **no `FLEET_TOKEN` left on disk** and **no ability to run the target's code**, so even a malicious PR can at worst produce a wrong verdict, never a compromise (see [Security notes](#security-notes)).

### 5. Onboard your repos

Two ways for items to enter the queue, and you can use either or both:

- **Fast path (recommended):** add a small dispatch workflow to each source repo so events push items here in real time.
  Copy-paste instructions are in [`docs/ONBOARDING.md`](docs/ONBOARDING.md).
- **Backstop only:** do nothing in the source repos and rely on the hourly `scan-backstop` to find items and keep pending cards current.

### 6. Verify

1. In this repo, open the **Actions** tab ▸ **scan-backstop** ▸ **Run workflow**.
2. Watch the run. Within a minute, decision-card issues should appear, refresh, or move upward in Recently updated sort for anything in your fleet that needs your call.
3. Tick a consuming decision checkbox on one card and confirm a successful resolving action lands on the target repo and closes the card.
   A non-retryable action error instead leaves the card open with the `blocked` label for manual follow-up.
   If the card is still labeled `pending-triage`, wait for the triage result or unavailable note to publish the checkboxes first.

If nothing appears, see [Troubleshooting](#troubleshooting).

## Daily use

You drive the queue three ways - whichever fits the decision:

- **Read the automatic triage.** PR-review and issue-triage cards can both include a `Triage` section with a quick Summary, Product implications, and Recommended next step.
  For PR-review this is automatic when `auto_triage` is on and `CLAUDE_CODE_OAUTH_TOKEN` exists, with the cache keyed by PR head SHA; for issue-triage it is gated by the INDEPENDENT `auto_triage_issues` and keyed by issue `updatedAt` instead (issues have no head SHA).
  A newly created eligible card may first show `pending-triage` and a placeholder instead of decision boxes; decide after the `Triage` section or unavailable note appears and the boxes publish.
  A fresh successful structured recommendation can prepend *Accept recommendation* to the decision boxes; ticking it maps the recommendation to the same deterministic action that a checkbox or slash-command would have used.
  Auto triage itself is still advisory: it gives you context before deciding and never acts without your tick.
- **Optional scan-time auto-merge.** When a repository has opted into `auto_merge` and committed `VISION.md`, the hourly scan may merge only a PR that clears the separate fail-closed gate described in [step 2](#2-edit-wheelhouseconfigyml).
  It first claims the pure pending decision card, then rechecks the live PR and card before merging, so an owner/maintainer action or a new decision on the card wins instead.
  A `wheelhouse:manual-merge-required` card is a specialized, refreshable auto-merge denial for its current head: commit history touched a workflow file even though the current net diff is clean, so review and merge that PR in the GitHub UI.
  Wheelhouse will not reclaim that same head every hour; a new head removes the hold during normal refresh and must earn a fresh current-head verdict.
  Apply `wheelhouse:no-auto-merge` to the target PR to stop one pending automatic merge; this label does not block your normal `/merge` or *Merge it* decision.
  Each automatic merge resolves the card with its qualifying evidence and adds a row to Wheelhouse's closed auto-merge ledger issue.
- **Quick calls - tick a consuming checkbox.** Each card offers the relevant final-decision boxes (e.g. *Merge it*, *Approve the CI run*, *Close / decline*, *Hold*).
  Tick exactly one; a successful resolving action closes the card, while `/hold` and non-retryable action errors remain open and `blocked` for manual follow-up.
  *Accept recommendation*, when present, is also a checkbox, but it only appears for fresh successful structured auto-triage recommendations on PR-review and issue-triage cards.
  It never appears on CI-approval cards or maps to `/approve-ci`.
  If the accepted recommendation is a non-terminal action such as `comment`, `request-changes`, or `investigate`, the card stays open just as it would for that direct action.
- **Want a deeper look first? - tick *Investigate*.** PR-review and issue-triage cards also offer an *Investigate - deep code-grounded review* box.
  It is the one tick that **does not consume the card**: it kicks off a code-grounded deep review, normalizes Claude's final response into `AgentResult`, posts that merit/triage verdict as a comment, and leaves the card open with the box cleared, so you can investigate again after new commits and still make your real call afterwards.
  (CI-approval cards don't offer it - that's a fast security gate, not a merit review.)
  Under the current production selection it needs `CLAUDE_CODE_OAUTH_TOKEN` (see [step 4](#4-optional-add-the-claude-production-token-for-agent-assisted-features)); without it the card just gets a one-line "needs token" note.
  The repo owner can also apply the `needs-deep-review` label by hand or run the `deep-review` workflow from Actions with only the card issue number; those manual paths parse the current card body before resolving the target.
- **Nuanced calls - comment a slash-command.** Reply on the card with one of:
  - `/merge` - merge the target PR. On success, a friendly `@`-mentioning thank-you comment is posted on the PR (opt-out: `thank_on_merge`).
    PRs that change `.github/workflows/**` (in the net diff **or** in any commit in the PR history) are never API-merged: the card stays open and `blocked` with guidance to review and merge by hand in the GitHub UI (or retry after a rebase drops the workflow touch).
    A rename into or out of `.github/workflows/` also counts as a workflow touch.
    That is intentional - `FLEET_TOKEN` keeps no Workflows write permission.
  - `/approve-ci` - approve the fork-CI run (security-gated; CI/action-file changes are held, while non-default bases and `pull_request_target` posture add warnings).
  - `/close` - close the target PR/issue.
  - `/decline <reason>` - post your reason on the target, then close it.
  - `/hold` - park the card (labels it `blocked`, leaves it for you to handle manually).
  - `/comment <text>` - post your comment to the target and leave the card open.
  - `/request-changes <text>` (pr-review only; `/request_changes <text>` also works) - submit a GitHub "changes requested" review on the target PR with your text as the review body, and leave the card open so the contributor can push again.
    Reuses the same `FLEET_TOKEN` scope as `/merge`/`/comment` (no new secret).
    Security note: a "changes requested" review can put the target PR into a merge-blocked state under branch-protection required-reviews - a slightly larger effect on the target repo than the comment-only path.
    It is a GitHub PR review, not a terminal card decision: submitting more than one just posts another GitHub review (allowed by the API, but noisy), so treat it as one review per push cycle rather than something you repeat.
    If `pending_contributor_cleanup` is enabled for that repo, a successful `/request-changes` also writes a hidden target-side marker and pending label so the scheduled scan can remind once and later close if the contributor never follows up.
- **Plain English - just reply (opt-in).** When you turn on `nl_decisions` (see [step 4](#4-optional-add-the-claude-production-token-for-agent-assisted-features)), reply to a card in normal language and Claude maps what you meant onto the same actions above.
  It does one of three things:
  - **Acts** when you're clearly deciding - "merge it", "close this, it's superseded by #50", "decline because the approach is wrong", or (pr-review only) "request changes, the tests are missing".
    It runs that action on the target exactly as the slash-command would (same guards: per-kind allowlist, head-SHA re-check, fork-CI HOLD), closing the card after a successful terminal decision like merge/close/decline, leaving it open for a non-terminal one like comment/request-changes, or marking it `blocked` after a non-retryable action error.
  - **Answers** when you're asking - "why is this safe to merge?", "what's the risk here?".
    It reads the target (diff/issue) and replies on the card, and **leaves the card open** so you can keep the thread going.
    If `READONLY_TOKEN` is configured, answer mode can also use read-only search across the target repo and configured fleet repos for related, duplicate, or superseding work.
    Without that optional secret, answers use only the prefetched target context and the trusted card conversation.
  - **Asks you to confirm** when it's unsure - so an ambiguous comment gets a reply instead of silence.

  Claude only ever returns structured JSON: an action, an answer, or a clarification request.
  The deterministic handler performs any action, so nothing happens that a slash-command couldn't already do.
  Search output, if any, is evidence only and never an instruction or authorization.
  Only comments from the repository owner or configured maintainer are ever read (a stranger's are ignored).
  A comment that starts with `/` is always treated as a slash-command, never sent to Claude.
  If Claude can't form a useful result, it asks you to rephrase or use a slash-command.

An item is **consumed** when the handler closes its card after a successful resolving action; the card is labeled `resolved` for audit.
A `/hold` or a non-retryable action error leaves the card open with the `blocked` label for manual follow-up.
A workflow-touch or unable-to-verify workflow-history refusal from a direct owner merge decision also lands `blocked` (will not API-merge; merge by hand in the GitHub UI, or retry after a rebase drops the workflow touch).
A retryable merge refusal for a stale head leaves the card as `needs-decision` so you can retry after the card is current.
For the "what changed most recently?" view, use the Issues list sorted by Recently updated, or bookmark `https://github.com/<owner>/<wheelhouse-repo>/issues?q=is%3Aissue%20is%3Aopen%20label%3Aneeds-decision%20sort%3Aupdated-desc`.
Wheelhouse bumps a pure pending card's own updated time when the target PR or issue's GitHub `updatedAt` advances, so recently active targets rise to the top.
That signal is target-level GitHub activity and may include owner, maintainer, or bot activity.
For refresh, auto-triage, and self-healing, a "pure pending" card means it has `needs-decision` and lacks `processing`, `resolved`, or `blocked`.
A `pending-triage` card still counts as pure pending for those maintenance paths, but its `held` state makes checkbox, slash-command, and plain-English decisions inert until Wheelhouse publishes it.
While a card is still a pure `needs-decision` card, a new dispatch or the hourly scan refreshes it in place when the target's material state changes: head SHA, compliance, tests, kind, priority, or checkbox options.
It also refreshes when the exact rendered issue title drifts, or when an issue-triage `updatedAt` is strictly newer and no automatic-triage queued write is eligible to own that revision.
The fork-CI approval wait is a scan-only exception: the hourly scan refreshes an existing same-kind PR-review card to the observed head's pending state without creating a card, posting the usual target-updated comment, or queuing triage; terminal checks return the PR to normal classification and refresh handling.
It also refreshes once when Wheelhouse's internal card render version is stale, so display-only card fixes propagate to existing pure pending cards without a target change.
The current render-version sweep labels known automated harness polling/status lines preserved in older cached `Triage` sections, while keeping the earlier sweeps for conditional *Accept recommendation*, the PR-review `/request-changes <text>` slash hint, and cached target-ref qualification.
A head move also leaves a "target updated" comment so you know to re-review the card.
For PR-review cards, that new head also makes automatic triage stale; the next eligible scan or dispatch may queue a fresh spend-guarded triage attempt for that head.
Issue-triage cards work the same way except the revision is the issue's `updatedAt`, not a head SHA: a new comment or edit alone is not a material change, but it does make the card eligible for a fresh spend-guarded triage attempt.
If that attempt is not eligible, the strictly newer timestamp still refreshes the pure pending card once so its stored revision catches up.
An eligible card created by scan-backstop or ingest is also queued in that same run, so it does not wait for a later backfill scan.
If that newly created card is eligible for auto triage, it starts as `pending-triage`; `triage-apply`, `triage-fail`, dispatch-failure handling, or the recovery step publishes it and removes `pending-triage`.
If auto-triage eligibility turns off while a held card is refreshed, the refresh publishes the normal checkboxes without adding a synthetic triage section.
Pure pending PR-review and issue-triage cards that were already open before auto triage existed have no `triaged_sha` cache yet, so they backfill once on the next eligible scan.
If you act before that refresh lands, a `/merge` (or a "merge it" comment) and `/request-changes` still refuse a stale head with a note.
The scheduled backstop also self-heals: if the underlying PR/issue gets merged or closed elsewhere, its card is closed automatically on the next successful complete scan.
If a repo scan is unreadable or incomplete, Wheelhouse leaves existing cards open because it cannot prove the target disappeared.
After a base-branch push, GitHub can temporarily report a PR's mergeability as `UNKNOWN` while it recalculates it.
Wheelhouse polls an otherwise merge-ready or review-needed PR for a conclusive value before changing its worklist membership.
If it does not settle within the bounded poll, Wheelhouse emits no new item and freezes any existing card unchanged until a later scan can decide it safely.
If an open target no longer needs a maintainer decision, its pure pending card stays open after the first complete, conclusive scheduled workflow run and is soft-closed only when the immediately following workflow run also observes a qualifying absence.
The first absence is hidden card state bound to the trusted workflow run number, and a worklist return clears it while keeping the same card.
If that still-open target returns after the soft close, Wheelhouse reuses an existing closed issue only when it has the exact target identity, trusted automation authorship and close actor, and valid current-schema reconcile soft-close provenance.
If the card was touched after close, reuse also requires a bounded complete timeline proving every post-close event came from trusted Wheelhouse automation and fully explains the issue's `updatedAt`.
If several trusted candidates share the same exact identity, Wheelhouse deterministically reuses the highest issue number and leaves the lower closed candidates unchanged.
The current body and managed labels are prepared while the issue remains closed, then the issue is reopened and uniqueness is verified before triage or any decision can proceed.
Legacy, owner-resolved, blocked, hard-closed, audit-protected, malformed, untrusted, identity-conflicting, or human-touched closed cards are never reopened; an incomplete or ambiguous lookup fails closed instead of creating another card.
The scan, ingest, and decision workflows share one queued card-lifecycle concurrency group so create, reopen, close, and owner decisions cannot race each other.
Any intervening present, manually dispatched, failed, truncated, unresolved-mergeability, or fork-CI-wait run breaks the streak without closing, even when that run cannot safely edit the stored absence record.
An open `blocked` card is not soft-closed merely because its target leaves the worklist, so a non-retryable action error stays visible.
If that target is genuinely merged or closed, the scheduled backstop still hard-closes the `blocked` card.
That includes scan-built targets authored by the repo owner, the configured maintainer, or bots: they remain in the open target set but leave the worklist, so reconcile consumes any old pure pending card for them after two adjacent complete, conclusive workflow runs.
It also includes PR-review candidates whose GraphQL `mergeable` value is `CONFLICTING`.
Those leave the maintainer worklist as `needs-rebase`; contributor-authored PRs get at most one rebase nudge per head SHA, and the backstop consumes any stale pure pending card after the two-adjacent-run soft-close threshold.
There is one fork-CI exception: when safe automatic CI approval verifies that no run is awaiting approval and the contributor PR's mergeability conclusively settles to `CONFLICTING`, Wheelhouse keeps its `needs-ci-approval` classification and emits no card, but posts that same rebase nudge before consuming the PR from the worklist.
An actual CI approval is unchanged and does not post a conflict nudge.
An `UNKNOWN` mergeability value is settled before this exception can nudge, and a missing or still-indeterminate value does not nudge.
If stale pending-contributor cleanup is enabled, a rebase nudge from the normal `needs-rebase` path is also a provable ask for the reminder and close sweep.
The sweep also recognizes an existing provable rebase nudge on a currently conflicting cross-repo `needs-ci-approval` ci-noop PR.
That narrow exception does not change CI routing or create a nudge from the CI-approval state alone.
It re-checks mergeability immediately before a reminder or close, so a non-conflicting, `UNKNOWN`, or unreadable PR is skipped.
Apply `wheelhouse:keep-open` on the target PR when you want to exempt it from that sweep.
By default the scan also **auto-approves fork-CI runs it proves safe** (`auto_approve_ci`, on unless you opt out), so an *Approve the CI run* card now appears only for contributor fork PRs with risky or uncertain cases - a run that changes CI/action files, targets a non-default base branch, has unreadable safety state, hits an approval error, has unknown fork status, or whose repo has a `pull_request_target` workflow (see [Security notes](#security-notes)).
Owner, maintainer, and bot-authored fork PRs follow the same safe approve/noop path, but risky or uncertain cases are logged with `suppressed-card` and do not emit decision cards.
Same-repo PRs with no CI signal are routed to normal PR review, not CI approval.
The approval step still binds each awaiting workflow run to the target PR by PR association, or by exact head SHA plus branch for fork runs where GitHub returns an empty association list.
Verified duplicate pending runs that share a stable workflow identity are collapsed to the newest run before approval, so Wheelhouse does not start two copies of the same workflow and let the older one cancel.
If the approval step verifies that no matching run is awaiting approval, the scan normally emits no worklist item and any stale CI-approval card enters the soft-close lifecycle described above; for a contributor fork that conclusively conflicts, it also posts the rebase nudge described above, and a later pending run re-enters the normal approve, card, or suppressed-card path.
A mergeability-settlement error for that narrow nudge exception marks the repo scan unhealthy instead, so reconcile preserves existing cards.
Each CI-approval candidate the auto path handles also writes exactly one scan-log line, so approved runs, no-pending runs, approval failures, and fail-closed safety reasons are visible in `scan-backstop`.

## Security notes

- **Owner-only acting.** Anyone can open issues or comment on a public repo, but every acting path is owner-gated (`sender == repository_owner`, plus an optional `maintainer` override). Strangers' edits and comments are no-ops.
- **Queue author filter.** The scheduled scan creates decision cards for other people's work.
  PRs and issues authored by the repo owner, the configured `maintainer`, or bots are excluded from the scan-built worklist; bot detection uses GitHub's author type plus the `[bot]` login suffix, and missing author metadata fails open so a real contributor is not silently dropped.
  The explicit dispatch fast path trusts what your source workflow sends, so filter there too if you want it to match the scan.
  If an explicit dispatch creates a PR-review card for your own PR, `/request-changes` refuses to submit the review because GitHub rejects self-review.
- **Merge-conflict routing.** The scheduled scan treats only GitHub's authoritative GraphQL `mergeable: CONFLICTING` value as a merge conflict.
  A conflicting PR that would otherwise become a merge-ready or review-needed PR-review card leaves the maintainer queue as `needs-rebase`.
  Classification never rewrites a fork `needs-ci-approval` target to `needs-rebase`, because CI approval and eventual mergeability are independent.
  However, if automatic approval for a non-excluded contributor fork verifies `noop` because no matching CI run is awaiting approval, and that PR's mergeability is conclusively `CONFLICTING`, the scan posts the same one-per-head rebase nudge while still emitting no decision card.
  This narrow exception does not apply when a CI run was actually approved, and it does not write the structured pending-contributor cleanup state.
  GitHub's explicit `UNKNOWN` value is a pending computation, not a routing answer: Wheelhouse polls an otherwise merge-ready or review-needed candidate and freezes that PR-review card's membership if it cannot settle the result, so `UNKNOWN` never creates, closes, or consumes that card.
  For the fork-CI no-op exception, `UNKNOWN` is likewise only a pending value: the scan settles it before nudging, never nudges an unresolved or missing value, and treats a settlement-query error as an unhealthy scan rather than guessing.
  A missing mergeability value still fails open and routes normally.
  Contributor-authored conflicted PRs get one plain-language rebase nudge per head SHA under `FLEET_TOKEN`: it leads with a visible `Automated reminder:` label, explains the base-branch conflict, asks the contributor to rebase onto or merge the latest base branch, resolve the conflict, and push, then says checks will re-run and the PR will get looked at again.
  A hidden marker in the PR comment prevents duplicates.
  Owner, maintainer, and bot-authored conflicted PRs are not nudged and do not emit decision cards.
- **Token scope.** The default `GITHUB_TOKEN` only reaches this repo and is used for all card activity (so it can't recursively re-trigger the handler).
  Acting on your other repos uses `FLEET_TOKEN`, which is never printed and is only used in cross-repo scan, approval, execution, and read-only fetch steps.
  Scope it to just your fleet with Actions, Contents, Issues, and Pull requests read/write on the target repos.
  Deliberately leave Workflows write unset: a card-driven merge of a PR that touches `.github/workflows/**` leaves the card open and `blocked` with manual GitHub UI merge guidance instead of attempting an API merge that GitHub would reject.
  The optional `READONLY_TOKEN` is used only by search-enabled Claude steps, only when present, and should have public read scope with no write permissions.
- **`request-changes` reuses `FLEET_TOKEN`, no new scope.** `/request-changes <text>` (pr-review only) submits a GitHub "changes requested" review on the target PR through the same `execute`-step `FLEET_TOKEN` wiring `/merge`/`/comment` already use - no new secret, no new token scope.
  It is deterministic, owner-gated, and non-terminal exactly like `/comment`, and it is also selectable by the natural-language intent-mapper (unlike `investigate`, which is checkbox-only).
  A "changes requested" review is a slightly larger effect on the target repo than a plain comment: under branch-protection required-reviews, it can put the target PR into a merge-blocked state until it is dismissed or a new review clears it.
- **Pending-contributor cleanup is deterministic and fail-open.** The scheduled scan runs the stale cleanup sweep under `FLEET_TOKEN`, the same deterministic target-side context that posts normal `needs-rebase` merge-conflict nudges and approves safe fork CI.
  It never runs in a Claude path and never uses `READONLY_TOKEN`.
  It closes only PRs with a structured target-side marker plus active `wheelhouse:pending-contributor-action` label, or legacy rebase nudges whose original hidden per-head marker and timestamp can still be proven.
  A ci-noop fork PR with `needs-ci-approval` is eligible through that legacy-nudge proof only when scan-time and pre-write mergeability reads are both conclusively `CONFLICTING`.
  CI routing alone is never treated as a cleanup ask.
  It also requires a prior visible reminder, an open target, no `wheelhouse:keep-open` label, the same PR head SHA, a verified original ask, and complete target timeline and PR edit-history reads.
  Any uncertainty skips the close.
  Contributor comments, reviews, review comments, edits, or head pushes after the ask stop cleanup and clear the active pending label.
  Owner, configured-maintainer, and bot activity does not reset the clock.
- **Auto triage is advisory, cached, and spend-guarded for PRs and issues alike.** Automatic triage edits only this repo's decision card with the default token, after the scan or ingest path has reserved daily budget and marked `triaged_sha` plus `triage_attempts` for the current revision (a PR's head SHA, or an issue's `updatedAt` - issues have no head SHA).
  That marker is the queue cache: an unchanged revision is not re-triaged unless a trusted recovery path clears the cache and both the per-revision attempt cap and daily ceiling permit another dispatch.
  A brand-new card that is eligible for that attempt is created under an additional `pending-triage` label with no decision checkboxes.
  It keeps `needs-decision` so the triage workflow can still resolve it, but the checkbox, slash-command, and plain-English decision paths ignore it until the attempt completes.
  `triage-apply`, `triage-fail`, dispatch-failure handling, and the recovery step all publish the normal checkboxes fail-open, so a stuck or failed triage run does not permanently hide a card.
  A fresh successful structured recommendation is persisted as hidden `triage_recommendation` state and may add *Accept recommendation* for pr-review or issue-triage only.
  That checkbox is inert for legacy Markdown-only, failed, stale, invalid, non-allowlisted, or missing-required-reason recommendations; it never appears for ci-approval or maps to `approve-ci`.
  When accepted, the deterministic handler maps it to the existing action and keeps the same head-SHA rechecks, token boundaries, terminal/non-terminal behavior, and per-kind allowlist.
  If `auto_triage` (pr-review) or `auto_triage_issues` (issue-triage) is false, or `CLAUDE_CODE_OAUTH_TOKEN` is absent, no workflow is dispatched and cards render without a triage section; the two flags are independent, so disabling one never disables the other.
  For an issue-triage card the target checkout is the repo's default branch, read-only, the same way `deep-review.yml` checks out an issue card.
  The model never receives `FLEET_TOKEN`; target checkout uses `persist-credentials: false`, and optional search uses only `READONLY_TOKEN` through `wheelhouse-search`.
- **Scan-time auto-merge is a narrow, audited opt-in.** `auto_merge` is false unless explicitly enabled globally or per repository, and an enabled repository still needs a default-branch `VISION.md`, a fresh successful behavior verdict, and every deterministic gate to pass.
  The gate rejects sensitive, governance, release, dependency, security/auth, billing, migration, persistence, installation, and public-default surfaces; requires a returning human contributor, clean live mergeability, configured green checks, no same-closing-issue ambiguity from another open PR, and a 20-file/1,000-line cap; and fails closed on every missing or stale read.
  `VISION.md` is fetched only from the default branch and a PR changing it is excluded, so a contribution cannot author the policy used to approve itself.
  Immediately before the merge, Wheelhouse rechecks the head, base, vision revision, live clean state, configured checks, same-closing-issue overlap, target escape-hatch label, card activity, and the unchanged manual workflow-touch merge gate.
  When that final gate proves a history-only workflow touch after a clean complete net diff, Wheelhouse persists a bounded head-scoped manual-merge hold on the decision card with the default card token before releasing the claim.
  The hold can only deny repeated claims; it never skips the final gate or authorizes a later merge, and malformed hold state fails closed until an authoritative refresh.
  PR-review cards display every guarded criterion separately as `MET`, `UNMET`, or `UNAVAILABLE`, using a head-bound result from the same evaluator that enforces G0-G6 and explicitly reserving G7 for the immediate act boundary.
  Displayed rows are non-material, read-only evidence and are never consumed as authorization.
  The cross-repo merge uses `FLEET_TOKEN`, while the claim, audit record, decision-card close, and closed hub ledger issue use the default `GITHUB_TOKEN`.
  Applying `wheelhouse:no-auto-merge` to a target PR is an immediate per-PR kill switch, and an automatic merge is never reverted automatically.
- **Fork-CI / pwn-request HOLD.** Approving a fork PR's CI runs that PR's own workflow/action code with your permissions. Any approval that touches `.github/workflows`, `.github/actions`, or `action.yml`/`action.yaml` is **held** for manual review, never auto-approved (it fails closed if the file list can't be read).
- **CI-approval security review is advisory.** When the scheduled scan creates a contributor CI-approval card for a fork PR that changes those workflow/action files, the card includes a deterministic *Security review (advisory)* section for the affected workflow/action files at the PR head.
  It reports only structured facts, such as triggers, write permissions, secret names or `secrets: inherit`, checkout provenance, action or reusable-workflow pins, action runtimes, and run steps - never source lines or secret values.
  The read-only analysis fails closed to a manual-diff-review note if it cannot analyze a file, and it cannot approve CI, change routing, or soften the pwn-request hold.
  Treat it as focused context and inspect the actual diff before approving CI.
- **Auto-approve of provably-safe fork CI (`auto_approve_ci`, DEFAULT ON).** To kill the repetitive "approve CI" clicks, the scan applies the *same* security gate *before* surfacing a card and auto-approves the runs it proves safe - so only risky or uncertain contributor PRs still raise a card.
  Auto-approve is a strict **subset** of the manual gate: a run emits no card only when there are **no** CI-execution file changes (above), the PR targets the repo default branch, the target repo's default branch runs **no** `pull_request_target` workflow, all safety reads succeed, and the approval call either approves the matching run or verifies that none is awaiting approval.
  After that safety verdict passes, the approval call approves only `action_required` runs for the PR head: when GitHub populates `workflow_run.pull_requests`, it must contain exactly that PR; when fork-originated runs leave that list empty, the run detail must match the PR's head SHA and branch.
  Verified runs are deduped by stable `workflowDatabaseId` when GitHub exposes it, keeping the highest run ID for that workflow; same-named distinct workflows and runs without that identity are not collapsed.
  If no matching run is awaiting approval, Wheelhouse normally emits no worklist item and any stale CI-approval card follows the [scheduled backstop lifecycle](#daily-use); a later pending run re-enters the normal approve, card, or suppressed-card path.
  For a contributor fork whose no-op PR is conclusively `CONFLICTING`, that no-card path also posts the existing fire-once-per-head rebase nudge before it drops the PR from the worklist.
  It does not change the CI-approval bucket, affect an `approved` path with real workflows, or write the structured pending-contributor cleanup state.
  An explicit `UNKNOWN` mergeability is settled before this nudge can be sent, while a missing value or an unresolved value produces no nudge.
  A settlement-query error also produces no nudge, marks the repo scan unhealthy, and preserves existing cards rather than letting reconcile consume them.
  Every contributor uncertainty fails closed to a card (unknown fork status, unreadable PR files, a non-default PR base branch, unreadable workflows, or an approve error).
  The same uncertainty for owner, maintainer, or bot-authored PRs is still not approved, but it is logged and no decision card is emitted because those authors are excluded from the queue.
  It runs in the cross-repo `FLEET_TOKEN` scan step; every approved or no-pending run logs a `::notice::`, every contributor carded run logs a `::warning::wheelhouse auto-approve carded <repo>#<pr>: ...` line, and every excluded-author suppressed card logs a `::warning::wheelhouse auto-approve suppressed-card <repo>#<pr>: ...` line.
  Warning lines include the safety or uncertainty reason and, when an approval was attempted, the `approve_ci` status/message.
  Those log lines are status text only, not token values, and they do not change the approve/card decision or the card body warning.
  Set `auto_approve_ci: false` (globally or per repo) to disable it for contributor PRs; owner, maintainer, and bot-authored fork PRs still run the safety-gated approve/noop path when safe because they are excluded from the decision queue.
  - **The `pull_request_target` caveat (stated plainly).**
    This approval gates the fork's read-only `pull_request` CI run.
    A `pull_request_target` workflow runs **automatically with your repo's secrets regardless of any approval**, so Wheelhouse cannot gate that vector by withholding approval.
    What it *does* is refuse to *silently* auto-clear a repo that has such a workflow (contributor PRs raise a card with a warning, while excluded-author PRs log `suppressed-card`), and it flags **loudly** the genuine exploit shape - a `pull_request_target` workflow that also checks out the PR head (`ref: github.event.pull_request.head.*` / `github.head_ref`), which runs attacker-controlled code with your secrets.
    Treat that flag as a prompt to fix the upstream workflow, not as something this approval can contain.
- **LLM injection defense (all LLM features).** Only trusted workflow prompts and owner/maintainer-authored text reach the LLM as instructions; the target diff/issue/code and optional search output are passed as clearly-delimited untrusted data, and the LLM is never given `FLEET_TOKEN` or write access to a fleet repo.
  For `nl_decisions`, both primary branches use native structured output for the canonical `nl-decision-v1` schema, while `decision.json` is only a bounded portable repair carrier.
  The no-`READONLY_TOKEN` branch can read the bounded on-disk target with `Read`/`Grep`/`Glob`, but has no shell or model `GH_TOKEN`.
  For auto triage and deep review, the no-`READONLY_TOKEN` branch is Read/Grep/Glob only, no shell, and no model `GH_TOKEN`.
  With `READONLY_TOKEN`, Claude receives only that read token as GitHub credentials and may run only `wheelhouse-search` as a shell command, using a wrapper for scoped read-only `gh` lookups across the target repo and configured fleet repos.
  It cannot run arbitrary `gh` or `git` commands.
  For `nl_decisions`, the search-enabled Claude step also passes `allowed_non_write_users` for the exact sender already authorized by Wheelhouse's owner/maintainer gate, because the public-read `READONLY_TOKEN` cannot satisfy `claude-code-action`'s redundant collaborator-permission check.
  For `nl_decisions`, every action-shaped result is normalized into `AgentResult` and re-validated against the per-kind allowlist before the deterministic handler acts.
  Missing, multiple, or invalid native output fails closed into one separate tokenless, no-tool repair attempt, which trusted code re-validates before replying or acting.
  If that repair is still invalid, the card stays open with a content-free retryable note.
  For deep review, the trusted workflow posts only the verdict extracted from a validated `AgentResult`; no deterministic downstream step reads raw model output or model-written files.
- **Cross-repo refs in LLM card text.** Auto triage summaries and structured recommendation reasons, deep-review verdicts, and `nl_decisions` answer/clarify replies are posted on this repo's decision cards or can later be posted to a target through *Accept recommendation* while referring to a different target repo.
  Before those strings are rendered or posted, trusted code qualifies model-written bare GitHub `#N` refs to `<owner>/<repo>#N` with `GITHUB_REPOSITORY_OWNER` and the card state's target repo.
  The model cannot redirect that slug by naming a repo in its output, and the deterministic rewrite is the guarantee even though the prompts also ask for fully-qualified refs.
  Auto-triage rendering and deep-review posting also label a narrow allowlist of claude-code-action harness polling/status transcript lines with `[automated status]`; this is display metadata only and does not strip content or change routing.
  The render-version sweep also re-applies the ref rewrite and automated-status labels to preserved cached `Triage` sections in card bodies; it does not rewrite already-posted card comments.
- **Deep review is code-grounded but sandboxed.** To review the real code, deep review checks out the target repo into the runner using `FLEET_TOKEN` - but only for the clone, with `persist-credentials: false`, so **no token is ever written to disk**.
  The Claude step that follows never gets `FLEET_TOKEN`.
  Without `READONLY_TOKEN`, the action receives this repo's downscoped token as its required GitHub input, while subprocess isolation removes that credential from the model process; Claude is restricted to **read-only** tools (`Read`/`Grep`/`Glob`) with **no shell**.
  With `READONLY_TOKEN`, it gets only that read token for GitHub CLI credentials, may write only the `search-request.json` request file, and may run only `wheelhouse-search` for scoped read-only lookup.
  It cannot build, test, install, or otherwise execute the target's code.
  Because Investigate dispatches this workflow with `github.token`, the Claude action allows only `github-actions[bot]` as a bot actor; wildcard or external bot actors are not allowed.
  The workflow validates and normalizes Claude's final response into `AgentResult`, then posts the extracted verdict from a read-only trusted source snapshot with a scrubbed environment and the default token.
  So a malicious PR that tries to prompt-inject through its own source can at worst produce a wrong verdict comment - never run code or exfiltrate a secret.
  The Investigate trigger is owner/maintainer-gated like every other acting path, while direct manual label and issue-only workflow runs remain repo-owner-only.
- **Public = world-readable.** A public Wheelhouse repo makes your queue and decisions visible to everyone. That transparency is a feature, but state it plainly to yourself before listing private work here; use a private repo if you need it.
- **Least privilege.** Every workflow declares a minimal `permissions:` block, and `ingest`, `decision-handler`, plus `scan-backstop` share the queued `wheelhouse-backstop` concurrency group so card creates, reopens, claims, decisions, and reconciliation cannot race.

## Troubleshooting

- **Nothing shows up in the queue.**
  Check that `FLEET_TOKEN` exists and is scoped to the repos in `wheelhouse.config.yml` (Settings ▸ Secrets and variables ▸ Actions).
  Confirm the repo names in the config are correct (names only, no `owner/` prefix).
  Run `scan-backstop` manually and read the logs - a repo that can't be read is reported as a warning and skipped, not fatal.
  If the target is authored by the repo owner, the configured maintainer, or a bot, the scheduled scan intentionally leaves it out of the queue.
  If an otherwise merge-ready or review-needed PR has a merge conflict, the scheduled scan intentionally leaves it out of the queue until the contributor pushes a mergeable head; contributor-authored PRs get a rebase nudge comment.
  If the log says its mergeability is pending, GitHub has not finished recalculating after a base-branch update; Wheelhouse leaves the PR out of a new card and preserves any existing card until a later scan gets a conclusive result.
- **The `scan-backstop` run failed with `fleet-scan health`.**
  One fleet repo returned `ok:false` for three consecutive scans, so the final health step failed the run after reconcile had already processed the healthy repos.
  Read the scan warnings and restore the repo's readable state, then a successful scan resets its counter in the closed `wheelhouse:scan-health` ledger issue.
  Forks that run the backstop on a different cadence can set a positive `WHEELHOUSE_SCAN_HEALTH_THRESHOLD` environment variable on the workflow's **Check fleet-scan health** step; the default is three consecutive failures.
  Ledger read or write failures only warn and do not fail the run.
- **Items look wrong (a non-compliant PR shows as merge-ready).**
  Your `compliance_check` / `test_check_patterns` don't match your actual check names.
  Wheelhouse aggregates duplicate compliance contexts worst-wins and treats GitHub rollup `FAILURE` or `ERROR` as compliance failure, so also check for a cancelled duplicate run or an untracked required check in the PR's Checks tab.
  Run the `checks` helper (step 2) to see the real names, and the scan logs surface a config warning when a gate-like check is present but unconfigured.
- **A decision didn't execute.**
  Almost always `FLEET_TOKEN` scope: it needs Actions + Contents + Issues + Pull requests (read & write) on the **target** repo.
  The card stays open with an error comment and the `blocked` label after a non-retryable action error, so it remains visible for manual follow-up instead of being soft-closed when an open target leaves the worklist.
  It still closes automatically if that target is later genuinely merged or closed.
  A `/merge` refused with a "head moved" note is working as intended - the PR changed after the card was rendered, so re-scan before merging.
  A `/merge` that leaves the card `blocked` saying the PR changes CI workflow files (or that Wheelhouse could not verify workflow history) is also working as intended: `FLEET_TOKEN` intentionally has no Workflows write, so Wheelhouse never attempts that doomed API merge.
  Review the workflow changes and merge the target PR by hand in the GitHub UI (the card comment includes the PR URL), or after a rebase drops the workflow touch clear the hold and retry `/merge` - the gate re-runs fresh and will merge a workflow-clean PR.
  A `/request-changes` refused for a moved head leaves the card pending; the next scan refreshes it to the new code automatically, then you can re-review and request changes again if needed.
  A `/request-changes` refused because it is your own PR is also working as intended - GitHub does not allow self-review, and scan-built queues normally filter those PRs out.
  A `/merge` that fails with a merge-conflict message means the contributor must rebase or merge the base branch, resolve the conflict, and push before Wheelhouse can merge it.
- **Approve-CI cards appear for PRs that look safe.**
  Open the latest `scan-backstop` run logs and search for `wheelhouse auto-approve carded` or `wheelhouse auto-approve suppressed-card`.
  The line names the repo and PR, includes the safety or uncertainty reason, and includes the `approve_ci` status/message when Wheelhouse tried to approve but had to fail closed.
  A `suppressed-card` line means the PR author is the owner, configured maintainer, or a bot, so Wheelhouse kept the CI approval fail-closed but did not emit a decision card.
  If logs say the fork status is unknown, Wheelhouse could not prove this is a fork PR and left the decision manual.
  If logs say a run could not be verified, Wheelhouse refused because the `action_required` run detail did not bind cleanly to the PR head.
- **Issue cards are missing, or a stale card did not close.**
  Open the latest `scan-backstop` run logs and look for `scan incomplete`.
  Wheelhouse paginates open PRs, open issues, PR closing references, and hub cards; if any repo page or closing-reference page cannot be read completely, it reports the warning, suppresses new issue-triage cards that might be duplicates of PR-addressed issues, and refuses to self-heal close existing cards for that repo until a complete scan succeeds.
- **A pending-contributor PR did not get reminded or closed.**
  Check that `pending_contributor_cleanup` is enabled globally or for that repo, and that the effective `pending_contributor_cleanup_targets` contains `pr`.
  The cleanup sweep is intentionally narrow: it only handles PRs where Wheelhouse can prove a `/request-changes` review or merge-conflict rebase nudge happened, including an unarmed legacy nudge.
  A `needs-ci-approval` fork PR is considered only when it is currently `CONFLICTING` and the nudge is proven; this does not widen the normal CI-approval lane.
  It skips if the target has `wheelhouse:keep-open`, if the PR head changed, if a non-maintainer human commented, reviewed, left a review comment, edited the PR body, pushed, or performed another target timeline action after the ask, if the target has an unaccounted post-ask update, or if any required target timeline or PR edit-history read is missing or ambiguous.
  When a review list item or `reviewed` timeline event lacks a timestamp, the scan re-reads that review by ID; if it still cannot prove the time, it skips cleanup by design.
  Search the latest `scan-backstop` log for `pending-contributor cleanup skipped`, `pending-contributor cleanup reminded`, or `pending-contributor cleanup closed`.
- **An Approve-CI card disappeared before I acted.**
  Search the latest `scan-backstop` logs for `approve_ci noop`.
  That means Wheelhouse verified no matching workflow run was still awaiting approval, emitted no worklist item, and let the stale card follow the [scheduled backstop lifecycle](#daily-use).
  If that safe contributor-fork PR is also conclusively merge-conflicted, Wheelhouse posts one rebase nudge for its current head before consuming the card; an `UNKNOWN` or missing mergeability value does not nudge.
- **Cron lag.**
  The scheduled keep-current path runs hourly, but GitHub cron is best-effort and can be delayed.
  For lower-latency items, wire the dispatch path from [`docs/ONBOARDING.md`](docs/ONBOARDING.md); dispatches nudge the same keep-current logic immediately.
- **A card shows `pending-triage` and no decision checkboxes.**
  Its first auto-triage attempt has been queued but has not published yet.
  Wait for the **triage** workflow to attach either the `Triage` section or an unavailable note; either outcome removes `pending-triage` and restores the normal checkboxes.
  If the configured budget is unavailable before dispatch, the scan or ingest run should publish the card immediately with a deferred-advisory note and leave it eligible for a later retry.
  If the dispatch itself could not be started, the scan or ingest run should publish the card immediately with a "could not be started" note.
  If the triage run failed before its update step, the final recovery step should publish it with a "did not finish" note, or clear the queued cache when trusted source setup was unavailable so a later scan can retry.
- **A PR-review or issue-triage card has no Triage section.**
  For PR-review, auto triage is skipped when `auto_triage: false`, `CLAUDE_CODE_OAUTH_TOKEN` is absent, the card is not a pure `needs-decision` PR-review card, or the card already carries `triaged_sha` for the current head.
  For issue-triage, it's skipped the same way but under the INDEPENDENT `auto_triage_issues`, and the cache is the issue's `updatedAt` revision instead of a head SHA.
  A newly created eligible card should queue in the same **scan-backstop** or **ingest** run that created it.
  Check the latest **scan-backstop**, **ingest**, and **triage** workflow logs.
  On an already-published card, if the triage workflow failed after queuing, the card may show a subtle unavailable note or simply keep the hidden cache so the same revision is not retried every hour.
- **A Triage section appeared but there is no Accept recommendation box.**
  That is expected for legacy Markdown-only recommendations, failed or stale triage, ci-approval cards, non-allowlisted actions, and actions that require text when Claude did not provide a usable reason.
  The box is only a shortcut for a fresh successful structured recommendation; use the normal checkbox or slash-command instead.
- **A plain-English reply did nothing / I only get slash-commands.**
  `nl_decisions` is inert unless `nl_decisions: true` **and** `CLAUDE_CODE_OAUTH_TOKEN` is set; the handler logs `nl path inert (...)` showing which condition is missing.
  Comments from anyone but the owner (or configured `maintainer`) are ignored, and a comment that starts with `/` is always treated as a slash-command.
  A `pending-triage` card is also inert to plain-English replies until the first auto-triage attempt publishes it.
  If plain-English answers work but cannot inspect related work across repos, add the optional `READONLY_TOKEN`; without it the answer path intentionally has no shell or search access.
- **Deep review does nothing.**
  It has no enable flag - it only needs `CLAUDE_CODE_OAUTH_TOKEN`.
  If that secret is missing, the card gets a one-line "Deep-review needs CLAUDE_CODE_OAUTH_TOKEN configured to run." note instead of a verdict; add the secret (see [step 4](#4-optional-add-the-claude-production-token-for-agent-assisted-features)).
  If deep review runs but cannot inspect related work across repos, add the optional `READONLY_TOKEN`; without it the workflow intentionally keeps the production no-shell, no-search posture.
  If the card says "Deep review ran but produced no verdict", the workflow could not extract usable output from Claude's action log and the run fails intentionally; check the **deep-review** workflow logs.
  For direct verification, the repo owner can run **Actions** ▸ **deep-review** ▸ **Run workflow** with just the decision-card issue number; the workflow fetches the current card body before resolving the target.
  If you ticked *Investigate* and nothing happened at all, confirm you're the repo owner or configured `maintainer` and check the **deep-review** workflow run in the Actions tab.

## Repo layout

```
CONTRIBUTING.md               how to submit changes to Wheelhouse itself
wheelhouse.config.yml          the one file you edit
.github/ISSUE_TEMPLATE/
  wheelhouse-decision.yml      schema for machine-rendered card decisions (held cards intentionally render no checkboxes)
.github/workflows/
  ingest.yml                   repository_dispatch / manual -> create, safely reuse, refresh, or activity-reflect a decision card
  decision-handler.yml         your tick / slash-command / plain-English reply -> execute on the target -> close resolved cards, block non-retryable errors, or leave retries actionable
  scan-backstop.yml            hourly scan -> create, safely reuse, refresh, activity-reflect, close cards, run target-side stale pending-contributor cleanup, and surface persistent scan failures
  triage.yml                   automatic lightweight PR/issue card triage -> read-only target pass -> publish held cards / edit card context
  deep-review.yml              always-on, code-grounded: Investigate box / label / manual issue run -> read-only target review -> workflow labels and posts Claude's verdict
  no-mistakes-required.yml     PR-to-main gate requiring the no-mistakes signature
scripts/
  wheelhouse_core.py           resilient GraphQL scan/classify, mergeability settlement, scan-health ledger, author filtering, dedup/overlap, target cleanup, CI safety, auto-approval, read-only CI security summaries, ref qualification, and scan logs
  render_card.py               build decision cards, including held pending-triage placeholders, auto-merge criteria, history-only workflow holds, and advisory CI security reviews; create/reuse/refresh/activity-reflect/close cards; queue/update auto triage; label automated status lines
  apply_decision.py            parse a tick/slash/label/plain-English comment, execute it on the target repo
  auto_merge.py                evaluate criteria, claim, validate, merge, persist final-gate manual-merge holds, and audit strictly eligible scan-time PR auto-merges
  automerge_criteria.py        stable schema for authoritative auto-merge criterion rows
  nl_readonly_search.py        optional READONLY_TOKEN search wrapper for LLM context
  build_item.py                normalize a dispatch payload into a card item
  reconcile.py                 backstop: open new cards, refresh stale pending cards, reflect target activity, close consumed ones
  triage_replay.py             owner-only bounded replay for exact-number auto-triage recovery waves
tests/test_decision.py         offline unit test for parse/route logic, workflow-merge gate, accept-recommendation routing, investigate routing, request-changes routing/execution/cleanup arming, and NL answer ref qualification
tests/test_nl_decisions_search.py offline unit test for optional nl_decisions read-only search, actor-check wiring, and ref-qualification prompt/env wiring
tests/test_nl_schema_repair.py offline unit test for native-first natural-language structured output, bounded one-turn repair, and retryable failure projection
tests/test_card_refresh.py     offline unit test for refresh change detection, activity reflection, guards, labels, render-version triage ref repair, and preserved automated-status labeling
tests/test_reconcile.py        offline unit test for reconcile routing, activity reflection, fixed-K soft-close hysteresis, race guards, and self-healing
tests/test_card_reuse.py       offline end-to-end card soft-close, trusted reuse, ambiguity, and lifecycle serialization tests
tests/test_merge_conflict.py   offline unit test for mergeability routing, rebase nudges, cleanup arming, and stale-card self-healing
tests/test_pending_contributor_cleanup.py offline unit test for deterministic pending-contributor reminders, closing, keep-open, legacy and ci-noop rebase-nudge proof, review-timestamp recovery, and fail-open target-activity proof
tests/test_ci_autoapprove.py   offline unit test for CI safety, scan-time auto-approval, duplicate-run dedup, and logging
tests/test_check_status.py     offline unit test for check_status compliance aggregation and rollup fail-closed backstop
tests/test_author_filter.py    offline unit test for queue author filtering, PR updatedAt propagation, and skipped-card CI handling
tests/test_auto_triage.py      offline unit test for automatic triage config, cache, evidence normalization/anchoring, rendering, structured recommendations, held-card publish/recovery, same-pass new-card dispatch, ref qualification, automated-status labeling, and workflow isolation
tests/test_triage_replay.py    offline unit test for owner-only replay eligibility, claim tombstones, duplicate-only re-entry, sanctioned attempt reset, dry-run behavior, exact-revision gates, and result records
tests/test_triage_prompt_size.py offline regression test for bounded pass-by-reference triage and deep-review prompts
tests/test_auto_merge_v1.py    offline unit test for scan-time auto-merge gates, same-closing-issue overlap holds, claims, live rechecks, audit records, and ledger recovery
tests/test_automerge_card_ui.py offline end-to-end test for authoritative criterion evaluation, fail-closed display states, and real card rendering
tests/test_automerge_workflow_hold.py offline end-to-end test for durable history-only workflow holds, recovery, token isolation, and card lifecycle behavior
tests/test_deep_review.py      offline unit test for the always-on deep-review + Investigate wiring and trusted verdict posting, including ref qualification and automated-status labeling
tests/test_workflow_lint.py    offline regression guard for workflow `gh api --slurp` / `--jq` misuse
tests/test_qualify_refs.py     offline unit test for shared bare `#N` -> `<owner>/<repo>#N` qualification
tests/test_scan_reliability.py offline unit test for scan retry/pagination, scan-health ledger, and UNKNOWN-mergeability safety
tests/test_config_schema.py    offline structural test for checked-in fleet-config entry shape, normalized names/patterns, and case-insensitive name uniqueness
docs/ONBOARDING.md             how to wire a source repo's dispatch (the fast path)
```

## Prior art & lineage

This machine is an **IssueOps** system: GitHub Issues + Actions used as a human-in-the-loop control plane.
It leans on an established pattern rather than inventing one, and credits the people who shaped it.

- **IssueOps** - treat a GitHub issue as a structured request that Actions *parse*, *validate*, and *act* on - was popularized by **Nick Alteen** and GitHub.
  The [`issue-ops`](https://github.com/issue-ops) org ships reusable Actions for it (`parser`, `validator`, `labeler`) and a [docs site](https://issue-ops.github.io/docs/); GitHub's own introduction is [*IssueOps: Automate CI/CD (and more!) with GitHub Issues and Actions*](https://github.blog/engineering/issueops-automate-ci-cd-and-more-with-github-issues-and-actions/).
- **ChatOps ancestry.** IssueOps grew out of **ChatOps** - running ops from a shared, auditable conversation - a term coined by **Jesse Newland** at GitHub around 2013 ([talk](https://speakerdeck.com/jnewland/chatops-at-github)) and built around **Hubot**, GitHub's chat bot (2011).
- Credit honestly: there is no single stamped "who coined IssueOps." Alteen and GitHub are the clear popularizers, and the term itself grew out of ChatOps.

### Where this machine sits in the pattern

Canonical IssueOps is *a human submits a form -> parse -> validate -> act*.
Wheelhouse is the **approval half** of that loop with an **automated front-end**: instead of you filling in a form, the scan/ingest workflows generate the decision cards, and you approve or deny them.
State lives in GitHub exactly as IssueOps intends - an open issue is a pending decision, a closed one is consumed, and labels carry the state in between.

### Lifecycle mapping

Our labels line up conceptually with the IssueOps lifecycle vocabulary - *Parse -> Validate -> Submit -> Approve -> Deny*:

- `needs-decision` - the card has been parsed and validated into the queue and is **awaiting your Approve / Deny**, unless it also has `pending-triage`.
- `pending-triage` - a transient overlay on `needs-decision`: the first auto-triage attempt is still completing, so Approve / Deny controls are hidden until fail-open publish.
- `processing` - **Submit / acting**: a handler is executing your call against the target repo.
- `resolved` - **consumed**: the decision was carried out (merged, approved, or declined) and the card closed.
- `blocked` - **held**: a `/hold`, or a card parked for you to handle manually.

This is a correspondence to orient readers who already know the IssueOps vocabulary, **not** a rename - the labels in this repo are exactly those listed under [How it works](#how-it-works).
