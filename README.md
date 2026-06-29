# Wheelhouse

> A ship's **wheelhouse** is where the captain stands to steer. This is your wheelhouse for open-source maintenance: whatever across your repos needs *your* hand surfaces here, and you make the call.

A personal, always-on, cross-repo **"what needs my decision"** command center, built entirely on GitHub Issues + GitHub Actions.
Every issue in this repo is one pending decision about the repositories you maintain - a PR worth merging, a fork-CI run worth approving, an issue worth triaging.
You answer by ticking a checkbox or replying in plain English; a workflow executes your call on the real repo and closes the card.
No server, no database, no bot to host - just this repo and a couple of secrets.

Fork it, edit one config file, add one secret, and you have your own Wheelhouse.

Changing the Wheelhouse codebase itself goes through [`CONTRIBUTING.md`](CONTRIBUTING.md).
PRs to `main` must be raised by `git push no-mistakes`, which writes the signature checked by the **"PR must be raised via no-mistakes"** workflow.

## How it works

- **The queue is the issue list.** Each open issue is one decision that needs you. Open = pending, closed = consumed.
- **Labels carry state:** `needs-decision` (in the queue), `processing` (a handler is acting), `resolved`, `blocked`, plus metadata labels `repo:<name>`, `kind:<pr-review|ci-approval|issue-triage>`, `priority:<high|med|low>`.
- **Each issue body is a decision card:** a link to the target, the situation, an overlap note, a recommended action, and quick-decision checkboxes. A hidden HTML comment holds the machine-readable state.
- **GitHub Actions are the handlers:** they create cards, execute your decisions, and reconcile the queue against live repo state.

```
 source repos ──dispatch──▶ ingest ─────────┐
                                            ▼
 scheduled scan ──reconcile──▶  this repo's ISSUES  ◀── you tick / comment
 (backstop, self-heals)             (the queue)             │
                                            └── decision-handler ──acts on──▶ your fleet repos
```

The deterministic core (ingest + decision-handler + scan-backstop) runs with a single secret and no LLM.
Two optional LLM side-jobs (both off by default) bring Claude in: `nl_decisions` lets you drive a card in plain English, and `deep_review` adds code-grounded review.

## Setup - a numbered checklist

Follow these top to bottom.
You only ever edit **one file** (`wheelhouse.config.yml`) and add **one secret** (`FLEET_TOKEN`).

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
  - name: my-cli
    compliance_check: null
    test_check_patterns: ["ci", "test"]

maintainer: ""        # optional extra login allowed to drive decisions; default = repo owner
deep_review: false    # LLM side-job: label -> read-only review verdict (off by default)
nl_decisions: false   # LLM side-job: reply to a card in plain English (off by default)
card_issues: false    # also card un-addressed issues, not just PRs (default: PRs only)
```

Not sure what your check names are?
After step 6, run the `scan-backstop` workflow and read its logs, or use the `checks` helper locally:
`GITHUB_REPOSITORY_OWNER=<you> GH_TOKEN=<token> python scripts/wheelhouse_core.py checks my-service`.

### 3. Create a `FLEET_TOKEN`

This is the token the machine uses to act on your other repos.
Only you can mint it (it's tied to your account).

1. GitHub ▸ **Settings** ▸ **Developer settings** ▸ **Personal access tokens** ▸ **Fine-grained tokens** ▸ **Generate new token**.
2. **Repository access** ▸ **Only select repositories** ▸ pick every repo you listed in `wheelhouse.config.yml` (and this repo too, if it is private).
3. **Permissions** ▸ Repository permissions: **Contents → Read and write**, **Issues → Read and write**, **Pull requests → Read and write**.
4. Generate, copy the token.
5. In **this** repo: **Settings** ▸ **Secrets and variables** ▸ **Actions** ▸ **New repository secret** ▸ name it exactly `FLEET_TOKEN`, paste the value.

That is the only secret the deterministic machine needs.

### 4. (Optional) Enable the LLM side-jobs

Skip this for the deterministic machine.
Two independent Claude-powered features share one token, and both are **off** until you opt in:

- **`nl_decisions`** - reply to a decision card in plain English and Claude maps it onto the existing actions (see [Daily use](#daily-use)).
- **`deep_review`** - apply the `needs-deep-review` label to a card and Claude posts a read-only merit/triage verdict.

To enable either (or both):

1. Set `nl_decisions: true` and/or `deep_review: true` in `wheelhouse.config.yml`.
2. Generate a **Claude subscription** token (requires a Claude Pro/Max subscription): run `claude setup-token` in the Claude Code CLI.
   This is **not** an Anthropic API key - the workflows authenticate `anthropics/claude-code-action` with your subscription only.
3. Add it as an Actions secret named exactly `CLAUDE_CODE_OAUTH_TOKEN`.

Each feature stays completely inert until **both** its flag is `true` **and** the token is present.
In either case Claude only ever reads your own text as instructions, the target content is passed to it as untrusted data, and it is given only this repo's token (never `FLEET_TOKEN`) - it proposes; the deterministic handler disposes.

### 5. Onboard your repos

Two ways for items to enter the queue, and you can use either or both:

- **Fast path (recommended):** add a small dispatch workflow to each source repo so events push items here in real time.
  Copy-paste instructions are in [`docs/ONBOARDING.md`](docs/ONBOARDING.md).
- **Backstop only:** do nothing in the source repos and rely on the scheduled `scan-backstop` to find items a few times a day.

### 6. Verify

1. In this repo, open the **Actions** tab ▸ **scan-backstop** ▸ **Run workflow**.
2. Watch the run. Within a minute, decision-card issues should appear for anything in your fleet that needs your call.
3. Tick a checkbox on one card and confirm the action lands on the target repo and the card closes.

If nothing appears, see [Troubleshooting](#troubleshooting).

## Daily use

You drive the queue three ways - whichever fits the decision:

- **Quick calls - tick a checkbox.** Each card offers the relevant boxes (e.g. *Merge it*, *Approve the CI run*, *Close / decline*, *Hold*). Tick exactly one; the handler executes it and closes the card.
- **Nuanced calls - comment a slash-command.** Reply on the card with one of:
  - `/merge` - merge the target PR.
  - `/approve-ci` - approve the fork-CI run (security-gated; auto-held if it touches CI files).
  - `/close` - close the target PR/issue.
  - `/decline <reason>` - post your reason on the target, then close it.
  - `/hold` - park the card (labels it `blocked`, leaves it for you to handle manually).
  - `/comment <text>` - post your comment to the target and leave the card open.
- **Plain English - just reply (opt-in).** When you turn on `nl_decisions` (see [step 4](#4-optional-enable-the-llm-side-jobs)), reply to a card in normal language and Claude maps what you meant onto the same actions above. It does one of three things:
  - **Acts** when you're clearly deciding - "merge it", "close this, it's superseded by #50", "decline because the approach is wrong". It runs that action on the target and closes the card, exactly as the slash-command would (same guards: per-kind allowlist, head-SHA re-check, fork-CI HOLD).
  - **Answers** when you're asking - "why is this safe to merge?", "what's the risk here?". It reads the target (diff/issue) and replies on the card, and **leaves the card open** so you can keep the thread going.
  - **Asks you to confirm** when it's unsure - so an ambiguous comment gets a reply instead of silence.

  Claude only ever *maps* your comment to a structured choice; the deterministic handler performs any action, so nothing happens that a slash-command couldn't already do. Only your own comments are ever read (a stranger's are ignored). A comment that starts with `/` is always treated as a slash-command, never sent to Claude. If Claude can't form a useful result, it asks you to rephrase or use a slash-command.

An item is **consumed** when the handler closes its card after acting; the card is labeled `resolved` (or `blocked` for a hold) for audit.
If a PR's head moves after a card is created, a `/merge` (or a "merge it" comment) is safely refused with a note so you re-check before merging.
The scheduled backstop also self-heals: if the underlying PR/issue gets merged or closed elsewhere, its card is closed automatically on the next scan.

## Security notes

- **Owner-only acting.** Anyone can open issues or comment on a public repo, but every acting path is owner-gated (`sender == repository_owner`, plus an optional `maintainer` override). Strangers' edits and comments are no-ops.
- **Token scope.** The default `GITHUB_TOKEN` only reaches this repo and is used for all card activity (so it can't recursively re-trigger the handler). Acting on your other repos uses `FLEET_TOKEN`, which is never printed and only ever used in the one cross-repo step. Scope it to just your fleet.
- **Fork-CI / pwn-request HOLD.** Approving a fork PR's CI runs that PR's own workflow/action code with your permissions. Any approval that touches `.github/workflows`, `.github/actions`, or `action.yml`/`action.yaml` is **held** for manual review, never auto-approved (it fails closed if the file list can't be read).
- **LLM injection defense (both LLM side-jobs).** Only your own text ever reaches the LLM as instructions; the target diff/issue is passed as clearly-delimited untrusted data, and the LLM is never given `FLEET_TOKEN` or write access to a fleet repo. For `nl_decisions` the LLM only *maps* your comment to a structured choice that is re-validated against the per-kind action allowlist before the deterministic handler acts - so a prompt-injection in a target diff cannot make it merge or close anything you didn't ask for, and it is further restricted to a single file-writing tool (no shell, no `gh`).
- **Public = world-readable.** A public Wheelhouse repo makes your queue and decisions visible to everyone. That transparency is a feature, but state it plainly to yourself before listing private work here; use a private repo if you need it.
- **Least privilege.** Every workflow declares a minimal `permissions:` block, and each card is serialized with per-issue `concurrency` so concurrent ticks can't race.

## Troubleshooting

- **Nothing shows up in the queue.**
  Check that `FLEET_TOKEN` exists and is scoped to the repos in `wheelhouse.config.yml` (Settings ▸ Secrets and variables ▸ Actions).
  Confirm the repo names in the config are correct (names only, no `owner/` prefix).
  Run `scan-backstop` manually and read the logs - a repo that can't be read is reported as a warning and skipped, not fatal.
- **Items look wrong (a non-compliant PR shows as merge-ready).**
  Your `compliance_check` / `test_check_patterns` don't match your actual check names.
  Run the `checks` helper (step 2) to see the real names, and the scan logs surface a config warning when a gate-like check is present but unconfigured.
- **A decision didn't execute.**
  Almost always `FLEET_TOKEN` scope: it needs Contents + Issues + Pull requests (read & write) on the **target** repo. The card stays open with an error comment when an action fails.
  A `/merge` that's refused with a "head moved" note is working as intended - re-scan and decide again.
- **Cron lag.**
  Scheduled runs are best-effort and can be delayed by GitHub. For real-time items, wire the dispatch path from [`docs/ONBOARDING.md`](docs/ONBOARDING.md); the backstop is only the safety net.
- **A plain-English reply did nothing / I only get slash-commands.**
  `nl_decisions` is inert unless `nl_decisions: true` **and** `CLAUDE_CODE_OAUTH_TOKEN` is set; the handler logs `nl path inert (...)` showing which condition is missing. Comments from anyone but the owner (or configured `maintainer`) are ignored, and a comment that starts with `/` is always treated as a slash-command.
- **Deep review does nothing.**
  It's inert unless `deep_review: true` **and** `CLAUDE_CODE_OAUTH_TOKEN` is set. The gate step logs which condition is missing.

## Repo layout

```
CONTRIBUTING.md               how to submit changes to Wheelhouse itself
wheelhouse.config.yml          the one file you edit
.github/ISSUE_TEMPLATE/
  wheelhouse-decision.yml      schema for the machine-rendered cards (lets issue-ops/parser read the checkboxes)
.github/workflows/
  ingest.yml                   repository_dispatch / manual -> create or update a decision card
  decision-handler.yml         your tick / slash-command / plain-English reply -> execute on the target -> close the card
  scan-backstop.yml            scheduled scan -> reconcile the queue against live repo state
  deep-review.yml              (LLM side-job, inert) label -> Claude reviews the target -> comments back
  no-mistakes-required.yml     PR-to-main gate requiring the no-mistakes signature
scripts/
  wheelhouse_core.py           GraphQL scan, classify, dedup/overlap, security-gated CI approval
  render_card.py               build the decision card; create/update/close cards in this repo
  apply_decision.py            parse a tick/slash/label/plain-English comment, execute it on the target repo
  build_item.py                normalize a dispatch payload into a card item
  reconcile.py                 backstop: open new cards, close stale ones
tests/test_decision.py         offline unit test for the parse/route logic (mocks the LLM)
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
