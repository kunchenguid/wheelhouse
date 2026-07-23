# Wheelhouse agent runtime

Wheelhouse has one versioned contract for every agent-assisted task.
The contract covers automatic PR and issue triage with and without search, bounded triage and natural-language schema repair, deep review with and without search, and natural-language decision mapping with and without search.

Claude is the production primary adapter.
The two schema-repair actions resolve to the exact-pin direct Claude CLI profile through the guarded production activation.
The other eight actions remain on the exact pinned Claude Action implementation.
The action path remains present and deployable as the schema-repair rollback target.
Codex CLI app-server remains implemented and tested only as disabled non-target adapter evidence because public GitHub Actions cannot securely authenticate the captain's ChatGPT Pro subscription noninteractively.
Fallback is disabled, so no provider failure invokes a different adapter automatically.

## Current operating state

The checked-in state is intentionally:

- `primary_profile: claude-action-current-pinned`
- `target: claude`
- `fallback: none`
- every action `target: claude`
- every base action profile `claude-action-current-pinned`
- `production_activation` maps only `triage.schema-repair` and `nl-decision.schema-repair` to `claude-cli-pinned`
- `temporary_rollback_profile: null`; setting it to `claude-action-current-pinned` restores both repair actions for an explicit durable replay
- `codex-app-server` recorded only under `disabled_adapters`

This is not selected by secret presence.
Provider environment overrides are rejected.
The current selection cannot target Codex or reach its workflow installation branches.

Both Claude production lanes keep the immutable model identifier, bounded turns, token boundaries, and output behavior.
Trusted parent jobs construct and validate an immutable `AgentTask`, upload a bounded content-addressed handoff with signed hidden paths preserved, and invoke `claude-model.yml` through a local reusable-workflow job.
GitHub resolves that local reusable workflow from the caller's commit, and every caller also passes its exact `github.sha` as the expected source revision.
That separate workflow has only `actions: read` and `contents: read`, receives no `FLEET_TOKEN`, and cannot write cards or target repositories.
Before task construction, every spend-capable event creates a durable default-token claim whose key binds the action, target, decision card, exact target revision, and the trigger identity required for deep review and natural-language decisions.
An eligible natural-language schema repair uses a distinct durable claim bound to the same authorized comment event, so a rerun cannot spend another repair turn. Its visible admission copy identifies the schema-repair phase while the hidden marker preserves the separate repair identity and idempotency key.
Schema-repair actions are structurally ineligible to create another repair task.
Duplicate delivery exits before task construction, and the claim key becomes the AgentTask `idempotencyKey`, so task, result, and terminal event evidence remain bound to the admitted event without retaining prompt or target content in lifecycle records.
An operator-approved exact-revision auto-triage replay first tombstones only the matching primary-triage claim marker and directly verifies that admission can no longer discover it.
The original claim comment remains as a bounded superseded audit record, while schema-repair, deep-review, and natural-language claim identities are outside the supersede operation.
If that tombstone cannot be written and verified, replay refuses the card before queueing or reservation.
The only replay marker that can re-enter its once guard is the proven `admission.duplicate`-only cohort: its terminal primary claim and any claim-keyed result record must both predate the replay marker, proving denial before task construction.
That exception removes only the duplicate queued reservation from the per-revision attempt count and replay-marker guard; the daily ledger reservation and all other guards remain intact.
An admission duplicate for an exact queued revision is projected as a terminal card error without clearing the queue cache key, making the denial visible without enabling an hourly retry loop.
Automatic triage also dual-writes one bounded claim-keyed `wheelhouse-triage-record` hidden comment for each admitted attempt, containing only version, event key, revision, structural status, and structural consumer code.
Normal triage card consumers do not read that migration record, but replay may read it only as bounded duplicate-only evidence.
Automatic triage also reserves from the closed UTC daily budget ledger before its verified queued-card checkpoint.
The default `triage_daily_ceiling` is 1200 reservations per UTC day, and each reservation can reach at most one primary call plus one bounded schema-repair call, for a 2400-model-call daily worst case.
The finite default lets approved replay waves complete without cost throttling while preserving a hard runaway-containment bound.
The per-card `triage_attempt_cap_per_revision` defaults to two queued attempts for one card-kind source revision.
Malformed cap configuration fails closed to one, while malformed ceiling or ledger state fails closed to zero new reservations.
Deep-review and natural-language decision events remain outside this automatic-triage ceiling because each requires a deliberate owner action and its own durable claim.
The model job verifies the complete handoff before hydrating a fresh workspace and initializes a local repository without a remote or network fetch.
The action lane applies its exact action tool allowlist and leaves only a bounded transcript plus observed enforcement record for its finalizer.
The direct repair lane pins the model job to `ubuntu-24.04`, installs and verifies Bubblewrap `0.9.0-1ubuntu0.1`, exercises a real namespace before provider admission, verifies Claude CLI `2.1.215` against the platform digest in `runtime.lock.json`, binds the OAuth credential through one mode-0600 file, and launches the existing supervisor and worker inside the Bubblewrap provider-only sandbox. Bubblewrap is launched through the runner's passwordless `sudo` solely because Ubuntu 24.04 denies unprivileged loopback setup in a new network namespace; it clears the environment and drops all worker capabilities before executing the model process. The root-mapped worker receives only a write-only output mount; after exit, the trusted supervisor restores ownership only on regular expected result and diagnostic files before validating them.
Sandbox-prerequisite failure is recorded separately from Claude download or digest failure, and both remain pre-spend with zero provider requests.
The action lane revalidates the signed target inputs after invocation and accepts success only when the post-action observation is non-null and exactly matches the pre-action observation for `target.txt`, `target-src/`, and `repository-provenance.json`.
Declared outputs, `.git/**`, `vision.md`, and unrelated workspace scratch are outside that signed-input immutability proof; unexpected scratch can be diagnostic, but it does not by itself invalidate the read-only target-input proof.
The reusable model workflow validates its observed `GITHUB_SHA` against the expected caller commit before hydration or provider admission.
Its separately permissioned finalizer verifies the handoff again, binds the observed source revision into the enforcement proof, and atomically emits `AgentResult` plus content-free events as a bounded artifact for the trusted consumer.
Triage and schema-repair claims record `consumer-committed` only after the trusted card projection reports an actual exact-revision update or held-card recovery; a successful no-op or stale projection remains `consumer-rejected`.
Every task limit carries provider-neutral enforcement evidence as `externally-enforced`, `adapter-enforced`, or `unavailable`, and an unavailable value is explicitly `null`.
Claude records the exact end-to-end hard deadline as unavailable because GitHub can delay a reusable job.
The obsolete API dispatch deadline is unavailable because the model job is part of the caller's workflow graph, while the child-job execution timeout remains externally enforced.
Trusted artifact, transcript, event, and final-output bounds remain explicit.
The model workflow uploads a content-free `spendStarted: true` checkpoint immediately before either invocation lane, so cancellation or a harness crash cannot downgrade a possibly spent attempt.
The Claude Action bridge profile does not claim the disabled Codex worker's network namespace, capability dropping, no-new-privileges, environment denial, or host-home denial.
Its proof level is `github-readonly-artifact-bridge-v1`, distinct from `sandboxed-adapter-worker-v1` used by adapters actually launched through the stronger worker boundary.
The action lane records the pinned action source commit and a checked-out action metadata digest when the runner exposes it; a successful direct repair records its verified Claude executable version and digest instead.

## Direct Claude schema-repair production profile

`agent_runtime/adapters/claude.py` implements the minimum direct Claude CLI boundary used by both schema-repair actions.
It accepts only the `anthropic-subscription` profile and a private file handoff for the `CLAUDE_CODE_OAUTH_TOKEN` process binding, rejects ambient API, cloud, GitHub, alternate-provider, and fallback configuration, and verifies one regular executable against the exact `2.1.215` platform digest before spend.
The runtime lock records the official release commit, immutable download URLs, Linux x64 and arm64 plus Darwin arm64 digests, and a checked protocol fixture digest.

The adapter validates the bound action schema against the small pinned-CLI subset before exposing `output.structured: native-schema`.
It compiles one shell-free argv, keeps the prompt on standard input, and requires exactly one matching `system/init.model` plus one final successful `structured_output` result from a bounded UTF-8 stream.
Its cancellation primitive is `SIGTERM` to the Claude process group, with the runtime retaining grace and hard-kill ownership.
The trusted core still owns the sandbox, provider proxy, deadlines, content-addressed handoff, independent schema and evidence validation, result binding, events, retention, and secret scanning.

The direct schema-repair profile intentionally has an empty tool inventory, one turn, no shell, and no fallback model.
Successful results must contain terminal native `structured_output`; the trusted core revalidates it and records `native-schema` plus `json-schema` validation, exact observed model, execution profile, sandbox implementation, and credential isolation in `AgentResult`.
`verify_result_binding` rejects any result whose execution profile differs from its admitted task.
The pinned action, `claude_bridge.py`, their workflow steps, and their tests remain present for the rollback window.
After rollback, replay is explicit through the existing marker-versioned durable triage replay path; automatic hourly cache retry remains disabled.
Before another profile is promoted, production observation uses the durable result and stage records for at least 20 successful or expected-failure executions over at least seven days.

## Operator replay exact-card selector

The supported replay owner remains an owner-started `scan-backstop.yml` `workflow_dispatch` with a non-empty validated `replay_wave`. Scheduled runs cannot reach replay, and the default empty `replay_exact_cards` input preserves the legacy sorted-prefix behavior.

Any raw non-empty `replay_exact_cards` input puts that workflow run into replay-only posture before the selector is validated. Checkout, runtime setup, and the exact replay owner may run, but open-card listing, fleet scan, stale-contributor maintenance, auto-approval, all auto-merge phases, queue reconcile, generic auto-triage queueing, card refresh/activity reflection/closure, and scan-health bookkeeping are skipped. Malformed selectors, a missing or invalid wave, count/limit mismatch, and incompatible reset input therefore fail in the replay owner without falling through to routine maintenance. A write-enabled exact run can mutate only through the existing exact replay path. An exact dry-run performs only its exact-card/source planning reads and reports zero writes. Leaving `replay_exact_cards` empty preserves scheduled maintenance, ordinary manual maintenance, legacy generic replay, and generic replay dry-run behavior except for the reserved card 1585 incident wave below, which remains replay-only and fails closed without its exact selector.

For a reviewed non-prefix cohort, set `replay_exact_cards` to the versioned contract `v1:N[,N...]`. Each `N` is a positive decimal card number from 1 through 9,007,199,254,740,991 without leading zeroes. The selector accepts at most 25 unique numbers, canonicalizes them into ascending order, rejects whitespace, empty elements, ranges, wildcards, duplicates, and trailing data, and requires `replay_limit` to equal the selector count. It cannot be combined with `replay_attempts_reset_cards`.

For example, plan six exact cards with zero writes:

```bash
gh-axi workflow run scan-backstop.yml \
  --repo OWNER/wheelhouse \
  --ref main \
  --field replay_wave=reviewed-wave \
  --field replay_limit=6 \
  --field replay_exact_cards='v1:1483,1584,1585,1586,1594,1598' \
  --field replay_dry_run=true
```

When this selector is present, the candidate listing is not used for discovery. Every requested card and source is still read by exact number and must pass the existing trusted identity, exact-revision, pure-card, terminal-error, attempt-cap, replay-marker, claim, daily-budget, sealed-permit, and idempotency checks. Selection grants no admission or authority. The sole code-defined exception to the normal attempt cap is the separately bound, one-use card 1585 incident permit below.

The planner reports the canonical selector and one `exact-selector/v1 admitted` line per card containing its revision. Dry-run and write-enabled modes use that same planner. If any requested card is missing, ineligible, changed during the full second-read preflight, already recovered, or the complete cohort exceeds remaining daily budget, the wave fails before writes and no generic candidate is substituted. After mutation starts, an unavoidable later GitHub race or write failure stops the wave immediately; already queued cards remain independently safe, no other card is substituted, and the operator must freeze and dry-run an explicit remaining cohort before another write-enabled dispatch.

### One-use card 1585 incident permit

`card-1585-anchor-fix-r3-final` is a code-defined, one-use permit for card 1585 only. It is not a general reset. It requires `replay_exact_cards='v1:1585'`, limit 1, the owner actor, the search action and its exact event key, the existing 2/2 attempt record, reviewed prior replay marker, exact terminal claim and result records, and the landed escaped-quote anchor behavior. Every planning and mutation reread rebuilds the approved source-review binding: `kunchenguid/no-mistakes#547` at head `0f29152c44b808064f9a2a2621c9bde6456f6262`, base `3d4691aedba97d9f877c073e3e652a8fde69d574`, target-facts digest `c8308310c07e85d840ea41785f78786a04d181bcf25c1b2ae6dbe4db278f6ea9`, immutable title/body/update snapshot digest `a0dd38be93e516c4bd3c376993d2dc3eee89f6e90638f63c55017aac808661a6` at `2026-07-23T04:56:49Z`, default-branch VISION blob `08077197b28d5f6b5b74b405d4617f066f620e33`, and VISION content digest `be04f798e4e616390c87a7fd21db7a3f656a4a7077b897c6a8aeb5cb49721b43`. Any mismatch stops before reservation.

The permit first writes and verifies its version 3 replay marker while leaving the terminal 2/2 cache scheduler-inert. A marker-write failure leaves the prior claim and permit retryable. Once verified, the marker permanently consumes the permit; claim tombstoning, ordinary budget reservation, the atomic queued checkpoint, or sealed dispatch may proceed, and any later failure remains consumed and cannot be picked up by scheduled reconciliation. The replay-owned queued checkpoint alone clears the terminal cache and makes one counter slot available without changing the effective cap of 2. Other cards, revisions, waves, selectors, reset inputs, and ordinary replay remain under the existing cap. The incident wave itself enters replay-only workflow posture before selector validation, as does any non-empty exact selector, so a missing or malformed incident selector still runs no scan, reconciliation, auto-merge, target action, or ordinary maintenance.

First run the exact zero-write plan:

```bash
gh-axi workflow run scan-backstop.yml \
  --repo OWNER/wheelhouse \
  --ref main \
  --field replay_wave=card-1585-anchor-fix-r3-final \
  --field replay_limit=1 \
  --field replay_exact_cards='v1:1585' \
  --field replay_dry_run=true
```

Only after that run is green and its admitted binding exactly matches the values above, consume the permit once with the identical invocation except for `replay_dry_run=false`:

```bash
gh-axi workflow run scan-backstop.yml \
  --repo OWNER/wheelhouse \
  --ref main \
  --field replay_wave=card-1585-anchor-fix-r3-final \
  --field replay_limit=1 \
  --field replay_exact_cards='v1:1585' \
  --field replay_dry_run=false
```

## Disabled and investigated adapters

The authentication audit found no officially supported secure noninteractive way to use the current ChatGPT Pro subscription from this public GitHub Actions repository.

Do not add any of these to target the disabled Codex adapter:

- `OPENAI_API_KEY`
- `CODEX_API_KEY`
- `CODEX_ACCESS_TOKEN`
- a copied `auth.json` blob

The official Codex GitHub Action uses OpenAI Platform API-key billing.
That is not ChatGPT subscription authentication and is forbidden unless the captain separately changes the billing decision.

Managed `auth.json` is officially documented only for trusted private CI.
It is forbidden by the official guidance for public or open-source repositories.
It contains refreshable OAuth material and requires one serialized consumer plus secure mutable persistence of every refreshed replacement.
A repository secret cannot safely implement that write-back lifecycle.

The runtime's disabled Codex adapter does not accept ambient Codex credentials, and no public workflow creates a credential handoff.
OpenCode with Z.AI Coding Plan is a deferred disabled candidate only.
No purchase decision, credential request, provider call, workflow target, or OpenCode adapter is authorized in this phase.
Provider-specific OpenCode or Z.AI policy must not enter runtime core schemas, lifecycle, tools, or consumers.
The provider-neutral adapter interface remains the only future seam.

## Runtime boundary

Trusted Wheelhouse steps continue to authorize events, fetch immutable target inputs, bind revisions, and perform every GitHub mutation.
The selected harness runs in a distinct disposable GitHub Actions job whose token permissions are read-only and whose workspace is hydrated only from the verified task handoff.

The Claude Action compatibility boundary receives only:

- bounded prompt and input files represented by the immutable task
- the exact action-specific tool allowlist
- the selected Claude subscription credential
- the optional read-only search credential on search-enabled paths
- one fresh workspace with read-only task inputs and bounded action output

The Claude model subprocess never receives `FLEET_TOKEN` or another GitHub credential with write or acting authority.
The action no-search path receives only the model job's downscoped default token because the pinned action requires a GitHub token input.
Search-enabled action paths may receive only the optional `READONLY_TOKEN` and the narrow `wheelhouse-search` command.
For the exact `nl-decision.search` and `triage.pr.search` actions only, that command also exposes the bounded anonymous `public_clone` request operation. It accepts a complete public HTTPS Git URL plus an optional safe ref, retains one data-only shallow clone under `RUNNER_TEMP` for Read/Grep/Glob, and removes it in an `always()` cleanup step. Initial PR triage explicitly distinguishes local-only VISION criteria from criteria that require external source inspection. External-source-positive verdicts require matching exact-file SHA-256 observations and clone provenance captured in root-owned transient state outside the model's writable identity. The declared tool remains exactly `Bash(wheelhouse-search)`; cloned and package execution remain forbidden.
Trusted card writes and target operations remain outside the model subprocess.

The disabled Codex adapter keeps `READONLY_TOKEN` in a trusted host broker.
Its model can call `github.search.readonly`, but it receives only the bounded broker result and never receives the token or a shell.

Codex built-in shell, web search, apps, connectors, memories, plugins, hooks, and multi-agent features are disabled.
The app-server receives only task-declared dynamic tools.
Unregistered app-server requests are denied.

The disabled Codex worker network runs through a Unix-socket CONNECT proxy with an auth-profile endpoint allowlist.
Its sandbox has a separate network namespace and no direct network route.
Its tool network is either absent or the read-only broker socket.

## Contract and pins

The public contract version is `wheelhouse.agent-runtime/v1alpha1`.
The public documents are:

- `AgentTask`
- monotonic NDJSON `AgentEvent`
- atomic `AgentResult`

The schemas live under `agent_runtime/schemas/`.
Unknown fields are rejected.
Canonical contract and proof hashes use deterministic JSON plus SHA-256.
The terminal event's `resultSha256` uses the explicit `agent-result-without-artifacts/v1` projection so the normalized-event artifact cannot create a cyclic or order-dependent digest.

Codex is pinned to CLI `0.144.0`, source commit `767822446c7a594caa19609ca435281a9ec67e0d`, npm package integrity, architecture-specific Linux executable-package integrity, and vendored app-server schema digests.
The direct Claude CLI adapter is pinned separately to CLI `2.1.215`, release commit `316ce99628e89900bf0b1328fed3b8fec0c0c92d`, official platform URLs and SHA-256 digests, and its bounded stream-JSON fixture digest.
The model-free `Agent Runtime canary` job runs the same signed installer script on `ubuntu-24.04`, including the exact package preflight, a minimal Bubblewrap namespace command, binary download, digest check, and `--version` probe without any model credential.
Run `python scripts/agent_runtime.py verify-pins` to verify the protocol files.
Offline evidence verifies the exact wrapper and selected Linux executable tarballs against the committed SHA-512 integrity pins.
Current production selection cannot reach the disabled Codex installation branches in workflows.

The app-server is driven over stdio JSONL.
Human terminal output is never scraped.
The worker performs `initialize`, `account/read` with `refreshToken:false`, model listing, provider capability reading, quota reading when available, `thread/start`, `turn/start`, and `turn/interrupt`.

The disabled Codex worker requires observed account type `chatgpt`.
Its offline adapter evidence also requires an eligible Business or Enterprise plan for the access-token mechanism.
Its generated test configuration forces `chatgpt` login and an explicitly supplied workspace ID.
Ambient `OPENAI_API_KEY`, `CODEX_API_KEY`, and `CODEX_ACCESS_TOKEN` are rejected before the worker starts.
An undeclared provider, model reroute, model mismatch, or effort mismatch fails closed.

## Tools and outputs

Canonical tools are:

- `fs.read`
- `fs.grep`
- `fs.glob`
- `github.search.readonly`
- typed `final.*` schemas for adapters that need terminating final tools

Codex uses its native `turn/start.outputSchema` mechanism.
The fake adapter and future adapters use the same action schemas and trusted validation.
Natural-language primary calls pass the canonical content-bound `nl-decision-v1` schema to the pinned Claude action and prefer one terminal `structured_output` value.
That canonical schema declares JSON Schema draft-07 for Claude CLI 2.1.215 compatibility. The direct adapter also accepts the previous draft 2020-12 declaration through the same restricted keyword subset, so the trusted validation language and primary or repair behavior do not change with the dialect declaration.
If that carrier alone is absent, the bridge may parse the plain terminal `result`, but accepts it only when the object passes the same byte bound, task binding, and exact bound schema. The resulting proof records `schema-validated-terminal-result` rather than claiming native delivery. Neither native generation nor terminal JSON alone can authorize a reply or action.

Path tools reject absolute paths, traversal, symlinks, devices, sockets, and escaping canonical paths.
Results and call counts are bounded, including rejected tool attempts.
Filesystem result bounds apply to the complete canonical serialized response, including paths and envelope fields.
Read and search payloads truncate deterministically to fit that complete envelope.
Search keeps the existing repository allowlist and operation semantics but no longer needs model-facing Write or Bash on Codex.

Repository inputs are derived from the exact bound Git commit after exact-HEAD and clean index/worktree checks, rather than copied from the live filesystem shape.
Committed regular and executable blobs are packaged from the Git object database.
Safe committed relative symlinks are materialized as regular files or bounded alias trees with content-free provenance, while absolute, escaping, broken, cyclic, dirty, changing, or over-limit links and gitlinks fail closed.
The source checkout may remain branch-attached, as it is for an external default-branch `actions/checkout@v4` checkout, because AgentTask `git.detached` describes the emitted content-addressed snapshot rather than the source checkout.
Committed hidden roots such as `.agents`, `.claude`, `.github`, and `.gitignore` are ordinary signed inputs and must remain present through the hosted artifact transport.
No symlink may reach the signed handoff or hydrated model workspace.

Final-result delivery is independent of transcript retention.
A bounded Claude transcript is transferred once within the read-only reusable workflow for trusted normalization with one-day artifact retention, then only the verified normalized result artifact crosses to the trusted consumer.
The finalizer always uploads that normalized result before reporting a missing result or a pre-model harness, lifecycle, or sandbox failure as unhealthy, preserving cleanup and failure evidence without presenting the run as healthy.
Triage normalization and the trusted card consumer share one compact-object extractor and one `target-anchor/v1` implementation. After JSON decoding, that anchor parser recognizes straight single- and double-quoted spans. Within a span, an odd run of backslashes before the matching delimiter removes only its final escape slash; all preceding slashes, mismatched quote characters, other Unicode characters, and source text remain significant. Even slash runs remain literal, malformed or mismatched delimiters cannot fall back to unquoted anchoring, and the resulting span must still occur verbatim in the immutable target after the existing case, whitespace, backtick, and asterisk normalization. Provider prose around one schema-valid compact triage object is transport metadata, not a schema failure. A VISION evidence list may be empty when no declared criterion matches, but every supplied criterion remains strictly schema-validated and downstream VISION/source binding still fails closed. A structurally valid candidate with unanchored evidence is `output.evidence_invalid`, never `output.schema_invalid` or success. A genuinely schema-invalid but delivered triage candidate remains available to its one-turn repair policy.

`AgentResult.status` and normalized output events describe trusted transport, schema, and evidence validation. The later `consumer.committed` stage describes a separately bound card projection, whose triage status and behavior verdict remain independent. Each layer emits one terminal result for its own execution ID or event key rather than overwriting the other layer's evidence.

Natural-language primary calls fail closed when neither native output nor a schema-valid plain terminal result is available. A missing native carrier by itself no longer discards an otherwise schema-valid terminal result, covering the pre-2.1.205 carrier-omission class without weakening schema validation.
Genuinely absent or invalid results receive one separately claimed repair task. Candidate precedence is bounded native output, then a JSON-parseable terminal result, then a legacy raw file or terminal prose. The live prompt never asks the model to hand-serialize `decision.json`.
Natural-language repair has no GitHub or search token exposed to the model, is no-tool and single-turn, and cannot recurse. It still uses the Claude subscription credential and model tokens when it starts. Its output must pass the unchanged `nl-decision-v1` parser and schema before trusted code can reply or act.
Missing triage output and evidence failure do not trigger schema repair.
Trusted code still performs normalized triage, evidence anchoring, cross-repository reference qualification, natural-language action allowlisting, card claims, revision checks, PR head checks, and auto-merge G0-G7 checks.

No model output directly authorizes or performs a GitHub action.

## Deadlines, cancellation, and retry

Sandboxed worker actions have a soft deadline, a cancellation grace interval, and a hard deadline.
For Claude, the separately permissioned reusable job has its own task-bound GitHub Actions timeout.
The end-to-end Claude hard deadline is unavailable because GitHub may queue the reusable job, but a delayed job still cannot execute beyond its own job timeout.
Cancellation or timeout leaves the pre-invocation checkpoint available to the always-running finalizer, which emits a conservative normalized failure instead of trusting missing output.
The worker counts every logical provider request and turn before it can proceed, including continuations after rejected tool calls, disables provider and stream retries, and interrupts before continuation at an observed token ceiling or after any observed overrun.
Codex receives the task input ceiling through its pinned app-server context configuration and additional native output-schema string ceilings before the first provider request.
Durable worker checkpoints preserve observed spend, usage, and model provenance if the worker crashes or is killed after spend begins.
At the soft deadline the supervisor writes a cancellation request.
The Codex adapter sends `turn/interrupt` and waits for an interrupted terminal event.
After the grace interval the supervisor sends `SIGTERM` to the process group.
At the hard deadline it sends `SIGKILL`.

A partial final is never accepted.
Results are written to a temporary file, flushed, validated, and atomically renamed.

Current actions permit one primary candidate attempt and no runtime retry.
The exactly-one schema repair is a separate task, not a provider retry; an invalid repair result is terminal for that event.
Fallback remains `none`.

Stable error families distinguish contract, config, selection, capability, auth, quota, provider, transport, input, provenance, tool, sandbox, lifecycle, harness, output, stale-target, source-revision, consumer, and internal failures.
Persisted messages are bounded and content-free.

## Provenance and diagnostics

Every result records:

- adapter and harness versions and digests
- protocol and schema pins
- provider and named auth profile
- auth mechanism and expected-workspace hash
- requested and observed model and effort
- cost class and data boundary
- request, capability, policy, prompt, input, output-schema, and sandbox hashes
- exact tool names
- retry and fallback decisions
- usage when available
- terminal status and stable error code

The GitHub job summary is generated by trusted code.
The model cannot author or suppress it.

Raw prompts, target inputs, tool results, app-server traffic, and auth state are not retained as diagnostics.
The content-addressed Claude input handoff, bounded transcript artifact, and normalized result artifact exist only for isolated reusable-workflow job transfer and use the minimum one-day retention supported by the artifact service.
Diagnostics are scanned for GitHub tokens, model keys, bearer values, private keys, and sensitive auth fields.
The worker also compares diagnostics and final output against the exact injected credential values in memory without printing them.
Only a content-free redaction count may be retained.

If a secret exposure is suspected, disable the credential-bearing workflow first, revoke the affected credential or OAuth session, invalidate every stale runner copy, and rotate before resuming.

## Failure recovery

For triage, revision freshness and held-card recovery remain product-level safeguards outside the runtime.
A failed, cancelled, or missing result publishes an eligible held card through the existing exact-revision fail-open path.
A stale attempt cannot publish over a newer revision.
The retryable `source.revision_mismatch` code publishes the bounded "Wheelhouse updated while this request waited; please retry." explanation instead of being collapsed into a provider or schema failure.

For deep review, missing output posts the existing fixed no-verdict note and leaves the card open.

For natural-language mapping, missing or invalid output cannot produce an action.
The primary call is native first, and only bridge-validated terminal `structured_output` is a native success.
When that carrier alone is absent, a plain terminal result is accepted without repair only if it passes the unchanged byte bound, task binding, and exact `nl-decision-v1` validation; its proof records `schema-validated-terminal-result` rather than native delivery.
An absent native carrier without such a valid plain terminal result, a multiple or invalid native carrier, or a result that fails strict JSON or `nl-decision-v1` validation, receives one separately claimed repair attempt; a still-invalid repair leaves the card open with a bounded, content-free retryable failure note.
The marker-keyed failure note remains bounded and fire-once.
A normalized `source.revision_mismatch` result uses the same precise retry explanation, while unknown failures keep the generic note.
A successful mapped action still enters the existing card claim and deterministic executor.

To inspect a failure:

1. Read selection and capability negotiation before model text.
2. Confirm requested and observed provenance match.
3. Use the stable error code to identify the phase.
4. Fix configuration or the named auth profile instead of weakening a capability.
5. Never replay a natural-language action against a changed card or PR head.

## Provider changes

Claude remains the production primary until the captain approves a supported, subscription-funded, secure, and behaviorally compatible alternative.
Provider changes require an explicit reviewed plan covering credentials, billing, data boundaries, every production action path, and deterministic consumer parity.
They must preserve `fallback: none` and cannot be selected by secret presence or an environment override.

Codex is not an active target or expected future primary under the current plan.
OpenCode with Z.AI Coding Plan is deferred and disabled, with no adapter implemented.
Neither status authorizes a credential request, paid proof, workflow target, fallback, or production promise.
The provider-neutral adapter contract should be extended only after a new captain decision and without embedding provider-specific policy in runtime core.

## One-call canary and natural rollout draft

This is a plan only and does not authorize a provider call, deployment, replay, fallback, secret change, or workflow change.
Execution requires the captain to approve the exact canary task and its evidence location in a separate decision made after provider-free validation passes.

The canary uses one naturally admitted, low-risk `triage.issue.local` event whose exact event identity and target revision are cryptographically bound to its AgentTask before invocation.
Immediately before invocation, the operator must verify the durable claim is unique, the target revision is still current, the selected provider and immutable model match policy, and fallback remains `none`.
The canary permits exactly one provider request and one turn, with provider retries, schema repair, continuation, replay, and alternate-provider routing disabled.
If freshness is lost before projection, the worker must cancel when possible, publish the bound terminal stale-target result, and make no target mutation.

Success requires one spend checkpoint, one immutable AgentResult bound to the approved AgentTask, one matching terminal event projection, exact provider and model provenance, a still-current target revision at projection, one expected card update, and no duplicate claim, request, result, or target mutation.
Abort on any preflight, capability, authentication, quota, provenance, checkpoint, freshness, schema, lifecycle, cancellation, consumer, or binding discrepancy.
An aborted or failed canary is not replayed, repaired, or routed to a fallback provider under this plan.

Evidence must retain the approved action, target revision, trigger identity, AgentTask and AgentResult digests, claim and run identifiers, request and turn counts, spend checkpoint, observed provider and model provenance, bounded usage and timing, freshness checks, terminal projection digest, consumer outcome, and target mutation audit.
Credentials, raw prompts, raw transcripts, target contents, and provider responses outside the bounded AgentResult are excluded from the evidence package.
The captain must review this evidence and separately approve natural rollout before any further provider-backed event is admitted.

Natural rollout uses only newly arriving eligible events and never synthesizes or replays an event.
It admits one action family at a time in this order: issue triage, PR triage, schema repair when naturally triggered, deep review, then natural-language decisions.
Local profiles precede search-enabled profiles within each applicable family.
Each stage remains limited to its first naturally admitted event until its AgentTask, checkpoint, AgentResult, terminal projection, freshness behavior, consumer effect, and mutation audit satisfy the canary success criteria.
Promotion to the next stage requires explicit captain approval of the accumulated evidence.
Any abort criterion stops further admission, preserves existing terminal evidence, and leaves fallback and alternate providers disabled.

## Local verification

No paid model call is required for local validation.
The authoritative command list is [Local validation](../CONTRIBUTING.md#local-validation).

The fake adapter exercises all action profiles without network or credentials.
Do not run a paid live proof or mutate repository secrets without explicit approval.
