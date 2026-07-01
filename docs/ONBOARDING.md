# Onboarding a source repo (the fast path)

The scheduled `scan-backstop` already finds and refreshes items in your fleet hourly with **no** changes to your other repos.
This doc is the optional **fast path**: add a tiny dispatch workflow to a source repo so events show up or refresh in your queue in real time instead of waiting for the next hourly scan.

Nothing here is required to run the machine, and nothing here changes how Wheelhouse classifies items - a dispatch is just a low-latency nudge that creates a card or refreshes a pure pending card when material state has changed; the backstop still reconciles everything later.
The scheduled scan applies Wheelhouse's owner/maintainer/bot author filter, but this explicit dispatch path trusts the source workflow and does not re-check author type, so only dispatch items you want carded.
The scheduled scan also applies merge-conflict `needs-rebase` routing and rebase nudges; explicit dispatches do not, so the backstop may later consume a dispatched PR-review card for a PR GitHub reports as `CONFLICTING`.

> You add these files to **your source repos**, not to Wheelhouse.
> The hub only ever reads; it never pushes to your source repos except to execute a decision you made.

## The dispatch contract

A source repo notifies the hub by sending a `repository_dispatch` event with **event type `wheelhouse-item`** to the hub repo, with a `client_payload` describing the item:

| field            | required | meaning                                                            |
| ---------------- | -------- | ------------------------------------------------------------------ |
| `repo`           | yes      | the source repo **name** (no owner), e.g. `my-service`             |
| `number`         | yes      | the PR or issue number                                             |
| `kind`           | no       | `pr-review` (default), `ci-approval`, or `issue-triage`            |
| `head_sha`       | no       | the PR head SHA - recommended; lets the hub refuse a stale merge   |
| `title`          | no       | short title of the target                                          |
| `author`         | no       | the PR/issue author's login                                        |
| `comp`           | no       | compliance status shown on the card                               |
| `tests`          | no       | test status shown on the card                                     |
| `summary`        | no       | one-line situation summary                                         |
| `recommendation` | no       | recommended action shown on the card                              |
| `priority`       | no       | `high` / `med` / `low`                                             |
| `options`        | no       | comma-separated checkbox option keys (defaults follow `kind`; see below) |

The `author` field is display data for dispatched cards.
It is rendered as plain text (`by <login>`), not as a GitHub `@mention`, so a dispatched card does not notify the target author.
The scan-built worklist has richer GitHub author metadata and skips PRs or issues from the repo owner, the configured maintainer, or bots; dispatch payloads are explicit card requests and are not filtered again by the hub.

Default checkbox sets are `pr-review`: `merge,close,investigate,hold`; `ci-approval`: `approve-ci,close,hold`; and `issue-triage`: `close,investigate,hold`.
`investigate` is non-consuming: it triggers the code-grounded deep-review workflow, clears the box, and leaves the card open for the real decision.
If you override `options`, include `investigate` only on `pr-review` or `issue-triage` cards when you want that box.

The hub's `ingest` workflow dedupes by target: a second dispatch for the same `repo`+`number` creates nothing new.
If the existing card is still a pure `needs-decision` card and a material field changed (`head_sha`, `comp`, `tests`, `kind`, `priority`, or `options`), the hub refreshes it in place.
Title, summary, and recommendation updates ride along with a material refresh, but do not rewrite an existing card by themselves.
Cards already labeled `processing`, `resolved`, or `blocked` are left untouched so a refresh cannot clobber an in-flight or consumed decision.

> **Legacy event type.** Before the rename to Wheelhouse the event type was `triage-item`. `ingest.yml` still listens for both (`types: [wheelhouse-item, triage-item]`), so a source repo wired up before the rename keeps working - but new dispatchers should send `wheelhouse-item`.

## Token for the source side

Sending a `repository_dispatch` to the hub requires a token with write access to the **hub** repo.

- If your `FLEET_TOKEN` already includes the hub repo in its scope, you can reuse it.
- Otherwise mint a fine-grained PAT scoped to **only the hub repo** with **Contents → Read and write**, and add it to the source repo as an Actions secret named `WHEELHOUSE_DISPATCH_TOKEN`.

## Copy-paste: source-repo workflow

Add this as `.github/workflows/notify-wheelhouse.yml` **in the source repo**.
It fires when a non-draft PR is opened, marked ready, or labeled, and nudges the hub.
Tune the trigger to match when *you* actually want to be asked (for example, only on a `ready-to-merge` label).

```yaml
name: notify-wheelhouse

on:
  pull_request:
    types: [opened, ready_for_review, labeled, reopened]

permissions:
  contents: read

jobs:
  notify:
    # Match the scan's default "other people's work" queue for owner and bot PRs.
    # If you configured an extra Wheelhouse maintainer, add their login here too.
    if: >
      github.event.pull_request.draft == false &&
      github.event.pull_request.user.login != github.repository_owner &&
      github.event.pull_request.user.type != 'Bot'
    runs-on: ubuntu-latest
    steps:
      - name: Dispatch to Wheelhouse
        env:
          # A token that can write to the hub repo (reuse FLEET_TOKEN, or a
          # hub-scoped PAT). Stored as a secret in THIS source repo.
          GH_TOKEN: ${{ secrets.WHEELHOUSE_DISPATCH_TOKEN }}
          # The hub repo. "wheelhouse" is the default name - change it if you
          # renamed your fork. Owner is derived, so this stays account-agnostic.
          HUB: ${{ github.repository_owner }}/wheelhouse
          # Pass GitHub context via env (never inline into the shell) so a PR
          # title containing quotes/backticks can't break or inject the command.
          P_REPO: ${{ github.event.repository.name }}
          P_NUMBER: ${{ github.event.pull_request.number }}
          P_SHA: ${{ github.event.pull_request.head.sha }}
          P_TITLE: ${{ github.event.pull_request.title }}
          P_AUTHOR: ${{ github.event.pull_request.user.login }}
        run: |
          payload="$(jq -nc \
            --arg repo "$P_REPO" \
            --arg number "$P_NUMBER" \
            --arg sha "$P_SHA" \
            --arg title "$P_TITLE" \
            --arg author "$P_AUTHOR" \
            '{event_type:"wheelhouse-item", client_payload:{
                repo:$repo, number:($number|tonumber), kind:"pr-review",
                head_sha:$sha, title:$title, author:$author }}')"
          echo "$payload" | gh api "repos/$HUB/dispatches" --input -
```

### Notes

- **Injection-safe by construction.** GitHub context values are passed through `env:` and read by `jq --arg`, never interpolated into the shell - a hostile PR title cannot break out.
- **PR-review merge conflicts.** Ingest dispatches create cards from the payload; they do not read GraphQL `mergeable` or post rebase nudges.
  The scheduled scan later treats `CONFLICTING` PRs as `needs-rebase`, posts any contributor nudge, and consumes stale pure pending cards.
- **`ci-approval` items.** If you want every fork-CI approval to surface fast, add a job that dispatches with `kind:"ci-approval"` when a run reaches `action_required` (e.g. on `workflow_run`).
  Ingest dispatches create or refresh a card immediately; they do not run the scan-time `auto_approve_ci` path or the scan author filter.
  If you want provably-safe runs auto-cleared instead of carded, rely on `scan-backstop` for CI approvals.
  If the scan later verifies that no matching run is awaiting approval, it emits no worklist item and reconcile consumes any stale CI-approval card.
  The `scan-backstop` logs emit one notice for each approved or no-pending run, one `wheelhouse auto-approve carded ...` warning for each contributor run that becomes a card, or one `wheelhouse auto-approve suppressed-card ...` warning for each owner, maintainer, or bot run that cannot be approved and does not emit a card.
  Warnings include the safety or uncertainty reason and any approval status/message.
  When you do approve a card, the hub still applies the same gate: CI/action-file changes are held, and non-default bases or `pull_request_target` posture are surfaced as warnings.
  It also approves only `action_required` workflow runs bound to the target PR: populated `workflow_run.pull_requests` must name exactly that PR, while fork-originated empty associations must match the PR head SHA plus head branch.
- **Issues.** To push issue triage, dispatch with `kind:"issue-triage"` from an `issues` trigger. (The hub also cards issues from the backstop when `card_issues: true`, skipping owner, maintainer, and bot-authored issues in the scan-built worklist.)
- **Third-party alternative.** If you prefer, `peter-evans/repository-dispatch` does the same dispatch as an action; the `gh api` form above keeps you dependency-free.

## Manual test (no source-repo changes)

You can exercise the whole path without touching a source repo:

1. In the hub, **Actions** ▸ **ingest** ▸ **Run workflow**.
2. Fill in `repo`, `number`, and (recommended) `head_sha`.
3. A decision card appears in the hub's issues; if one already exists, material changes refresh it in place.
4. Tick a consuming decision box to confirm the handler acts on the target.

This is the quickest way to validate `FLEET_TOKEN` scope before wiring real dispatches.
