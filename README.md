# Wheelhouse

> A ship's **wheelhouse** is where the captain stands to steer. This is your wheelhouse for open-source maintenance: whatever across your repos needs *your* hand surfaces here, and you make the call.

A personal, always-on, cross-repo **"what needs my decision"** command center, built entirely on GitHub Issues + GitHub Actions.
Every issue in this repo is one pending decision about the repositories you maintain - a PR worth merging, a fork-CI run worth approving, an issue worth triaging.
The scheduled scan keeps the queue focused on other people's work: PRs and issues authored by the repo owner, the configured maintainer, or bots stay out of the scan-built worklist, while missing author metadata fails open.
PR-review candidates that GitHub reports as merge-conflicted leave the maintainer worklist until the contributor rebases or merges the base branch and pushes a mergeable head.
You make final decisions by ticking a checkbox or replying in plain English; a workflow executes your call on the real repo and closes the card.
No server, no database, no bot to host - just this repo and a small set of secrets.

Fork it, edit one config file, add one required secret, and you have your own Wheelhouse.

Changing the Wheelhouse codebase itself goes through [`CONTRIBUTING.md`](CONTRIBUTING.md).
PRs to `main` must be raised by `git push no-mistakes`, which writes the signature checked by the **"PR must be raised via no-mistakes"** workflow.

## How it works

- **The queue is the issue list.** Each open issue is one decision that needs you. Open = pending, closed = consumed.
- **Labels carry state:** `needs-decision` (in the queue), `processing` (a handler is acting), `resolved`, `blocked`, plus metadata labels `repo:<name>`, `kind:<pr-review|ci-approval|issue-triage>`, `priority:<high|med|low>`.
- **Each issue body is a decision card:** a link to the target, the situation, an overlap note, a recommended action, and quick-decision checkboxes. A hidden HTML comment holds the machine-readable state.
- **GitHub Actions are the handlers:** they create cards, refresh pending cards when their targets change, execute your decisions, and reconcile the queue against live repo state.

```
 source repos ──dispatch──▶ ingest ─────────┐
                                            ▼
 scheduled scan ──reconcile──▶  this repo's ISSUES  ◀── you tick / comment
 (hourly keep-current path)         (the queue)             │
                                            └── decision-handler ──acts on──▶ your fleet repos
```

The deterministic core (ingest + decision-handler + scan-backstop) runs with a single secret and no LLM.
Two Claude-powered features layer on top, both gated by a Claude subscription token: **deep-review** is always available (tick a card's *Investigate* box for a code-grounded read of the target), and the opt-in `nl_decisions` lets you drive a card in plain English.
Both LLM features can also use an optional `READONLY_TOKEN` for scoped read-only search across the target repo and configured fleet repos.

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
    compliance_check: "required-policy-check"  # exact name of a required gate check, or null
    test_check_patterns: ["test", "build", "e2e"]  # substrings that identify your test/CI checks
    # auto_approve_ci: false              # optional per-repo override
  - name: my-cli
    compliance_check: null
    test_check_patterns: ["ci", "test"]

maintainer: ""         # optional extra login allowed to drive decisions and treated as your work
nl_decisions: false    # LLM side-job: reply to a card in plain English (off by default)
card_issues: false     # also scan un-addressed issues, not just PRs; owner/maintainer/bot authors are skipped
auto_approve_ci: true  # auto-approve provably-safe fork-CI runs (DEFAULT ON; see Security notes)
# (Deep review has no flag - it's always available once CLAUDE_CODE_OAUTH_TOKEN is set.)
```

> **Heads-up - `auto_approve_ci` defaults ON.**
> When this key is absent it is treated as `true`, so a fresh fork auto-approves fork-CI runs that the security gate proves safe (no CI-file changes, the PR targets the repo default branch, no `pull_request_target` workflow, and all safety reads succeed) and only raises a card for risky or uncertain contributor-authored runs.
> A run is approved only after Wheelhouse verifies it is the target PR's awaiting `action_required` run: GitHub-populated `workflow_run.pull_requests` must contain exactly that PR, and fork-originated empty associations must match the PR `head_sha` plus `head_branch`.
> If the approval call verifies that no matching run is awaiting approval, the scan emits no card and the backstop consumes any stale CI-approval card.
> The scan log records every CI-approval candidate it handles: approved runs and verified no-pending runs emit one `::notice::`, contributor PRs that need a decision emit one `::warning::wheelhouse auto-approve carded <repo>#<pr>: ...` line, and excluded owner, maintainer, or bot PRs that cannot be approved emit one `::warning::wheelhouse auto-approve suppressed-card <repo>#<pr>: ...` line.
> Both warning forms include the safety or uncertainty reason and any approval status/message.
> Set it to `false` to opt out for contributor PRs (every contributor fork-CI candidate raises a card, as you click to approve each), or add `auto_approve_ci: false` to a single `repos:` entry to opt that one repo out.
> Owner, maintainer, and bot-authored fork PRs are excluded from the decision queue, so Wheelhouse still runs the safety-gated approve/noop path for safe CI and suppresses their cards.
> See [Security notes](#security-notes).

Not sure what your check names are?
After step 6, run the `scan-backstop` workflow and read its logs, or use the `checks` helper locally:
`GITHUB_REPOSITORY_OWNER=<you> GH_TOKEN=<token> python scripts/wheelhouse_core.py checks my-service`.

### 3. Create a `FLEET_TOKEN`

This is the token the machine uses to act on your other repos.
Only you can mint it (it's tied to your account).

1. GitHub ▸ **Settings** ▸ **Developer settings** ▸ **Personal access tokens** ▸ **Fine-grained tokens** ▸ **Generate new token**.
2. **Repository access** ▸ **Only select repositories** ▸ pick every repo you listed in `wheelhouse.config.yml` (and this repo too, if it is private).
3. **Permissions** ▸ Repository permissions: **Actions → Read and write**, **Contents → Read and write**, **Issues → Read and write**, **Pull requests → Read and write**.
4. Generate, copy the token.
5. In **this** repo: **Settings** ▸ **Secrets and variables** ▸ **Actions** ▸ **New repository secret** ▸ name it exactly `FLEET_TOKEN`, paste the value.

That is the only secret the deterministic machine needs.

### 4. (Optional) Add the Claude token for the LLM features

Skip this for the deterministic machine.
Two independent Claude-powered features share one token (`CLAUDE_CODE_OAUTH_TOKEN`):

- **Deep review (always-on)** - tick a card's *Investigate* box and Claude reviews the target's checked-out code without executing it.
  The repo owner can also apply the `needs-deep-review` label or run the `deep-review` workflow with only the decision-card issue number; that manual workflow path fetches the current card body with this repo's token.
  The workflow captures Claude's final response and posts it as the code-grounded merit/triage verdict.
  There is **no flag** - it runs whenever you trigger it, as long as the token is set.
  With the token missing it posts a one-line "needs token" note on the card so you know why nothing ran.
  With an optional public-read `READONLY_TOKEN`, it can use the same scoped `wheelhouse-search` wrapper for advisory related-work and code context.
  Without `READONLY_TOKEN`, it keeps the legacy no-shell, Read/Grep/Glob-only behavior.
- **`nl_decisions` (opt-in)** - reply to a decision card in plain English and Claude maps it onto the existing actions (see [Daily use](#daily-use)).
  This one stays inert until `nl_decisions: true` **and** the token is present.
  With an optional public-read `READONLY_TOKEN`, its answer mode can search the target repo and configured fleet repos through a read-only `gh` wrapper.
  Without `READONLY_TOKEN`, it keeps the legacy no-shell behavior.

To set it up:

1. Generate a **Claude subscription** token (requires a Claude Pro/Max subscription): run `claude setup-token` in the Claude Code CLI.
   This is **not** an Anthropic API key - the workflows authenticate `anthropics/claude-code-action` with your subscription only.
2. Add it as an Actions secret named exactly `CLAUDE_CODE_OAUTH_TOKEN`.
3. For the plain-English path, also set `nl_decisions: true` in `wheelhouse.config.yml`.
4. Optional: to let deep review and plain-English answers search related PRs, issues, and code across the target repo and configured fleet repos, add an Actions secret named exactly `READONLY_TOKEN`.
   Scope it for public read only and give it no write permissions.

In every case Claude only ever reads your own text as instructions; the target diff/issue/code and optional search output are untrusted data, and it never receives `FLEET_TOKEN`.
When `READONLY_TOKEN` is absent, `nl_decisions` runs with the `Write` tool only, no shell tools, and no model `GH_TOKEN`.
Deep review's no-token branch runs with Read/Grep/Glob only, no shell tools, and no model `GH_TOKEN`.
When `READONLY_TOKEN` is present, search-enabled Claude steps receive it as GitHub CLI credentials for the scoped search wrapper.
Deep review's GitHub write is the workflow-owned card comment; `nl_decisions` actions are performed by the deterministic handler, and `READONLY_TOKEN` never authorizes an action.
Deep review goes a step further: it explores the target's checked-out code without executing it, with **no `FLEET_TOKEN` left on disk** and **no ability to run the target's code**, so even a malicious PR can at worst produce a wrong verdict, never a compromise (see [Security notes](#security-notes)).

### 5. Onboard your repos

Two ways for items to enter the queue, and you can use either or both:

- **Fast path (recommended):** add a small dispatch workflow to each source repo so events push items here in real time.
  Copy-paste instructions are in [`docs/ONBOARDING.md`](docs/ONBOARDING.md).
- **Backstop only:** do nothing in the source repos and rely on the hourly `scan-backstop` to find items and keep pending cards current.

### 6. Verify

1. In this repo, open the **Actions** tab ▸ **scan-backstop** ▸ **Run workflow**.
2. Watch the run. Within a minute, decision-card issues should appear or refresh for anything in your fleet that needs your call.
3. Tick a consuming decision checkbox on one card and confirm the action lands on the target repo and the card closes.

If nothing appears, see [Troubleshooting](#troubleshooting).

## Daily use

You drive the queue three ways - whichever fits the decision:

- **Quick calls - tick a consuming checkbox.** Each card offers the relevant final-decision boxes (e.g. *Merge it*, *Approve the CI run*, *Close / decline*, *Hold*). Tick exactly one; the handler executes it and closes the card.
- **Want a deeper look first? - tick *Investigate*.** PR-review and issue-triage cards also offer an *Investigate - deep code-grounded review* box.
  It is the one tick that **does not consume the card**: it kicks off a code-grounded deep review, captures Claude's final response from the action output, posts that merit/triage verdict as a comment, and leaves the card open with the box cleared, so you can investigate again after new commits and still make your real call afterwards.
  (CI-approval cards don't offer it - that's a fast security gate, not a merit review.)
  It needs `CLAUDE_CODE_OAUTH_TOKEN` (see [step 4](#4-optional-add-the-claude-token-for-the-llm-features)); without it the card just gets a one-line "needs token" note.
  The repo owner can also apply the `needs-deep-review` label by hand or run the `deep-review` workflow from Actions with only the card issue number; those manual paths parse the current card body before resolving the target.
- **Nuanced calls - comment a slash-command.** Reply on the card with one of:
  - `/merge` - merge the target PR.
  - `/approve-ci` - approve the fork-CI run (security-gated; CI/action-file changes are held, while non-default bases and `pull_request_target` posture add warnings).
  - `/close` - close the target PR/issue.
  - `/decline <reason>` - post your reason on the target, then close it.
  - `/hold` - park the card (labels it `blocked`, leaves it for you to handle manually).
  - `/comment <text>` - post your comment to the target and leave the card open.
- **Plain English - just reply (opt-in).** When you turn on `nl_decisions` (see [step 4](#4-optional-add-the-claude-token-for-the-llm-features)), reply to a card in normal language and Claude maps what you meant onto the same actions above.
  It does one of three things:
  - **Acts** when you're clearly deciding - "merge it", "close this, it's superseded by #50", "decline because the approach is wrong".
    It runs that action on the target and closes the card, exactly as the slash-command would (same guards: per-kind allowlist, head-SHA re-check, fork-CI HOLD).
  - **Answers** when you're asking - "why is this safe to merge?", "what's the risk here?".
    It reads the target (diff/issue) and replies on the card, and **leaves the card open** so you can keep the thread going.
    If `READONLY_TOKEN` is configured, answer mode can also use read-only search across the target repo and configured fleet repos for related, duplicate, or superseding work.
    Without that optional secret, answers use only the prefetched target context and the trusted card conversation.
  - **Asks you to confirm** when it's unsure - so an ambiguous comment gets a reply instead of silence.

  Claude only ever returns structured JSON: an action, an answer, or a clarification request.
  The deterministic handler performs any action, so nothing happens that a slash-command couldn't already do.
  Search output, if any, is evidence only and never an instruction or authorization.
  Only your own comments are ever read (a stranger's are ignored).
  A comment that starts with `/` is always treated as a slash-command, never sent to Claude.
  If Claude can't form a useful result, it asks you to rephrase or use a slash-command.

An item is **consumed** when the handler closes its card after acting; the card is labeled `resolved` (or `blocked` for a hold) for audit.
While a card is still a pure `needs-decision` card, a new dispatch or the hourly scan refreshes it in place when the target's material state changes: head SHA, compliance, tests, kind, priority, or checkbox options.
A head move also leaves a "target updated" comment so you know to re-review the card.
If you act before that refresh lands, a `/merge` (or a "merge it" comment) still refuses a stale head with a note.
The scheduled backstop also self-heals: if the underlying PR/issue gets merged or closed elsewhere, its card is closed automatically on the next scan.
If an open target no longer needs a maintainer decision, its pure pending card is closed too.
That includes scan-built targets authored by the repo owner, the configured maintainer, or bots: they remain in the open target set but leave the worklist, so reconcile consumes any old pure pending card for them after a successful scan.
It also includes PR-review candidates whose GraphQL `mergeable` value is `CONFLICTING`.
Those leave the maintainer worklist as `needs-rebase`; contributor-authored PRs get at most one rebase nudge per head SHA, and the backstop consumes any stale pure pending card.
By default the scan also **auto-approves fork-CI runs it proves safe** (`auto_approve_ci`, on unless you opt out), so an *Approve the CI run* card now appears only for contributor fork PRs with risky or uncertain cases - a run that changes CI/action files, targets a non-default base branch, has unreadable safety state, hits an approval error, has unknown fork status, or whose repo has a `pull_request_target` workflow (see [Security notes](#security-notes)).
Owner, maintainer, and bot-authored fork PRs follow the same safe approve/noop path, but risky or uncertain cases are logged with `suppressed-card` and do not emit decision cards.
Same-repo PRs with no CI signal are routed to normal PR review, not CI approval.
The approval step still binds each awaiting workflow run to the target PR by PR association, or by exact head SHA plus branch for fork runs where GitHub returns an empty association list.
If the approval step verifies that no matching run is awaiting approval, the scan emits no worklist item and the backstop consumes any stale CI-approval card; a later pending run re-enters the normal approve, card, or suppressed-card path.
Each CI-approval candidate the auto path handles also writes exactly one scan-log line, so approved runs, no-pending runs, approval failures, and fail-closed safety reasons are visible in `scan-backstop`.

## Security notes

- **Owner-only acting.** Anyone can open issues or comment on a public repo, but every acting path is owner-gated (`sender == repository_owner`, plus an optional `maintainer` override). Strangers' edits and comments are no-ops.
- **Queue author filter.** The scheduled scan creates decision cards for other people's work.
  PRs and issues authored by the repo owner, the configured `maintainer`, or bots are excluded from the scan-built worklist; bot detection uses GitHub's author type plus the `[bot]` login suffix, and missing author metadata fails open so a real contributor is not silently dropped.
  The explicit dispatch fast path trusts what your source workflow sends, so filter there too if you want it to match the scan.
- **Merge-conflict routing.** The scheduled scan treats only GitHub's authoritative GraphQL `mergeable: CONFLICTING` value as a merge conflict.
  A conflicting PR that would otherwise become a merge-ready or review-needed PR-review card leaves the maintainer queue as `needs-rebase`; `UNKNOWN` or missing mergeability fails open and routes normally.
  Contributor-authored conflicted PRs get one Wheelhouse rebase nudge per head SHA under `FLEET_TOKEN`, with a hidden marker in the PR comment to prevent duplicates.
  Owner, maintainer, and bot-authored conflicted PRs are not nudged and do not emit decision cards.
- **Token scope.** The default `GITHUB_TOKEN` only reaches this repo and is used for all card activity (so it can't recursively re-trigger the handler).
  Acting on your other repos uses `FLEET_TOKEN`, which is never printed and is only used in cross-repo scan, approval, execution, and read-only fetch steps.
  Scope it to just your fleet with Actions, Contents, Issues, and Pull requests read/write on the target repos.
  The optional `READONLY_TOKEN` is used only by search-enabled Claude steps, only when present, and should have public read scope with no write permissions.
- **Fork-CI / pwn-request HOLD.** Approving a fork PR's CI runs that PR's own workflow/action code with your permissions. Any approval that touches `.github/workflows`, `.github/actions`, or `action.yml`/`action.yaml` is **held** for manual review, never auto-approved (it fails closed if the file list can't be read).
- **Auto-approve of provably-safe fork CI (`auto_approve_ci`, DEFAULT ON).** To kill the repetitive "approve CI" clicks, the scan applies the *same* security gate *before* surfacing a card and auto-approves the runs it proves safe - so only risky or uncertain contributor PRs still raise a card.
  Auto-approve is a strict **subset** of the manual gate: a run emits no card only when there are **no** CI-execution file changes (above), the PR targets the repo default branch, the target repo's default branch runs **no** `pull_request_target` workflow, all safety reads succeed, and the approval call either approves the matching run or verifies that none is awaiting approval.
  After that safety verdict passes, the approval call approves only `action_required` runs for the PR head: when GitHub populates `workflow_run.pull_requests`, it must contain exactly that PR; when fork-originated runs leave that list empty, the run detail must match the PR's head SHA and branch.
  If no matching run is awaiting approval, Wheelhouse emits no worklist item and lets reconcile consume any stale CI-approval card; a later pending run re-enters the normal approve, card, or suppressed-card path.
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
- **LLM injection defense (both LLM features).** Only your own text ever reaches the LLM as instructions; the target diff/issue and optional search output are passed as clearly-delimited untrusted data, and the LLM is never given `FLEET_TOKEN` or write access to a fleet repo.
  For `nl_decisions`, the no-`READONLY_TOKEN` branch keeps the legacy posture: one file-writing tool, no shell, and no model `GH_TOKEN`.
  For deep review, the no-`READONLY_TOKEN` branch keeps the legacy posture: Read/Grep/Glob only, no shell, and no model `GH_TOKEN`.
  With `READONLY_TOKEN`, Claude receives only that read token as GitHub credentials and may run only `wheelhouse-search` as a shell command, using a wrapper for scoped read-only `gh` lookups across the target repo and configured fleet repos.
  It cannot run arbitrary `gh` or `git` commands.
  For `nl_decisions`, every action-shaped result is re-validated against the per-kind allowlist before the deterministic handler acts, and the workflow preserves only `decision.json` before routing/executing from a read-only trusted source copy.
  For deep review, the trusted workflow posts the action-output verdict and no deterministic downstream step reads model-written files.
- **Deep review is code-grounded but sandboxed.** To review the real code, deep review checks out the target repo into the runner using `FLEET_TOKEN` - but only for the clone, with `persist-credentials: false`, so **no token is ever written to disk**.
  The Claude step that follows never gets `FLEET_TOKEN`.
  Without `READONLY_TOKEN`, it gets this repo's token and is restricted to **read-only** tools (`Read`/`Grep`/`Glob`) with **no shell**.
  With `READONLY_TOKEN`, it gets only that read token for GitHub CLI credentials, may write only the `search-request.json` request file, and may run only `wheelhouse-search` for scoped read-only lookup.
  It cannot build, test, install, or otherwise execute the target's code.
  Because Investigate dispatches this workflow with `github.token`, the Claude action allows only `github-actions[bot]` as a bot actor; wildcard or external bot actors are not allowed.
  The workflow captures Claude's final response from the action output and posts the verdict with the default token.
  So a malicious PR that tries to prompt-inject through its own source can at worst produce a wrong verdict comment - never run code or exfiltrate a secret.
  The Investigate trigger is owner/maintainer-gated like every other acting path, while direct manual label and issue-only workflow runs remain repo-owner-only.
- **Public = world-readable.** A public Wheelhouse repo makes your queue and decisions visible to everyone. That transparency is a feature, but state it plainly to yourself before listing private work here; use a private repo if you need it.
- **Least privilege.** Every workflow declares a minimal `permissions:` block, and each card is serialized with per-issue `concurrency` so concurrent ticks can't race.

## Troubleshooting

- **Nothing shows up in the queue.**
  Check that `FLEET_TOKEN` exists and is scoped to the repos in `wheelhouse.config.yml` (Settings ▸ Secrets and variables ▸ Actions).
  Confirm the repo names in the config are correct (names only, no `owner/` prefix).
  Run `scan-backstop` manually and read the logs - a repo that can't be read is reported as a warning and skipped, not fatal.
  If the target is authored by the repo owner, the configured maintainer, or a bot, the scheduled scan intentionally leaves it out of the queue.
  If an otherwise merge-ready or review-needed PR has a merge conflict, the scheduled scan intentionally leaves it out of the queue until the contributor pushes a mergeable head; contributor-authored PRs get a rebase nudge comment.
- **Items look wrong (a non-compliant PR shows as merge-ready).**
  Your `compliance_check` / `test_check_patterns` don't match your actual check names.
  Run the `checks` helper (step 2) to see the real names, and the scan logs surface a config warning when a gate-like check is present but unconfigured.
- **A decision didn't execute.**
  Almost always `FLEET_TOKEN` scope: it needs Actions + Contents + Issues + Pull requests (read & write) on the **target** repo. The card stays open with an error comment when an action fails.
  A `/merge` that's refused with a "head moved" note is working as intended - re-scan and decide again.
  A `/merge` that fails with a merge-conflict message means the contributor must rebase or merge the base branch, resolve the conflict, and push before Wheelhouse can merge it.
- **Approve-CI cards appear for PRs that look safe.**
  Open the latest `scan-backstop` run logs and search for `wheelhouse auto-approve carded` or `wheelhouse auto-approve suppressed-card`.
  The line names the repo and PR, includes the safety or uncertainty reason, and includes the `approve_ci` status/message when Wheelhouse tried to approve but had to fail closed.
  A `suppressed-card` line means the PR author is the owner, configured maintainer, or a bot, so Wheelhouse kept the CI approval fail-closed but did not emit a decision card.
  If logs say the fork status is unknown, Wheelhouse could not prove this is a fork PR and left the decision manual.
  If logs say a run could not be verified, Wheelhouse refused because the `action_required` run detail did not bind cleanly to the PR head.
- **An Approve-CI card disappeared before I acted.**
  Search the latest `scan-backstop` logs for `approve_ci noop`.
  That means Wheelhouse verified no matching workflow run was still awaiting approval, emitted no worklist item, and let reconcile consume the stale card.
- **Cron lag.**
  The scheduled keep-current path runs hourly, but GitHub cron is best-effort and can be delayed.
  For lower-latency items, wire the dispatch path from [`docs/ONBOARDING.md`](docs/ONBOARDING.md); dispatches nudge the same card-refresh logic immediately.
- **A plain-English reply did nothing / I only get slash-commands.**
  `nl_decisions` is inert unless `nl_decisions: true` **and** `CLAUDE_CODE_OAUTH_TOKEN` is set; the handler logs `nl path inert (...)` showing which condition is missing.
  Comments from anyone but the owner (or configured `maintainer`) are ignored, and a comment that starts with `/` is always treated as a slash-command.
  If plain-English answers work but cannot inspect related work across repos, add the optional `READONLY_TOKEN`; without it the answer path intentionally has no shell or search access.
- **Deep review does nothing.**
  It has no enable flag - it only needs `CLAUDE_CODE_OAUTH_TOKEN`.
  If that secret is missing, the card gets a one-line "Deep-review needs CLAUDE_CODE_OAUTH_TOKEN configured to run." note instead of a verdict; add the secret (see [step 4](#4-optional-add-the-claude-token-for-the-llm-features)).
  If deep review runs but cannot inspect related work across repos, add the optional `READONLY_TOKEN`; without it the workflow intentionally keeps the legacy no-shell, no-search posture.
  If the card says "Deep review ran but produced no verdict", the workflow could not extract usable output from Claude's action log and the run fails intentionally; check the **deep-review** workflow logs.
  For direct verification, the repo owner can run **Actions** ▸ **deep-review** ▸ **Run workflow** with just the decision-card issue number; the workflow fetches the current card body before resolving the target.
  If you ticked *Investigate* and nothing happened at all, confirm you're the repo owner or configured `maintainer` and check the **deep-review** workflow run in the Actions tab.

## Repo layout

```
CONTRIBUTING.md               how to submit changes to Wheelhouse itself
wheelhouse.config.yml          the one file you edit
.github/ISSUE_TEMPLATE/
  wheelhouse-decision.yml      schema for the machine-rendered cards (lets issue-ops/parser read the checkboxes)
.github/workflows/
  ingest.yml                   repository_dispatch / manual -> create or refresh a decision card
  decision-handler.yml         your tick / slash-command / plain-English reply -> execute on the target -> close the card
  scan-backstop.yml            hourly scan -> create, refresh, or close cards against live repo state
  deep-review.yml              always-on, code-grounded: Investigate box / label / manual issue run -> read-only target review -> workflow posts Claude's verdict
  no-mistakes-required.yml     PR-to-main gate requiring the no-mistakes signature
scripts/
  wheelhouse_core.py           GraphQL scan, classify, author filtering, dedup/overlap, merge-conflict nudges, CI safety, auto-approval, and scan logs
  render_card.py               build the decision card; create/refresh/close cards in this repo
  apply_decision.py            parse a tick/slash/label/plain-English comment, execute it on the target repo
  nl_readonly_search.py        optional READONLY_TOKEN search wrapper for LLM context
  build_item.py                normalize a dispatch payload into a card item
  reconcile.py                 backstop: open new cards, refresh stale pending cards, close consumed ones
tests/test_decision.py         offline unit test for the parse/route logic (mocks the LLM), incl. investigate routing
tests/test_nl_decisions_search.py offline unit test for optional nl_decisions read-only search wiring
tests/test_card_refresh.py     offline unit test for refresh change detection, guards, and labels
tests/test_reconcile.py        offline unit test for reconcile routing and self-healing
tests/test_merge_conflict.py   offline unit test for mergeability routing, rebase nudges, and stale-card self-healing
tests/test_ci_autoapprove.py   offline unit test for CI safety, scan-time auto-approval, and logging
tests/test_author_filter.py    offline unit test for queue author filtering and skipped-card CI handling
tests/test_deep_review.py      offline unit test for the always-on deep-review + Investigate wiring
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

- `needs-decision` - the card has been parsed and validated into the queue and is **awaiting your Approve / Deny**.
- `processing` - **Submit / acting**: a handler is executing your call against the target repo.
- `resolved` - **consumed**: the decision was carried out (merged, approved, or declined) and the card closed.
- `blocked` - **held**: a `/hold`, or a card parked for you to handle manually.

This is a correspondence to orient readers who already know the IssueOps vocabulary, **not** a rename - the labels in this repo are exactly those listed under [How it works](#how-it-works).
