# Wheelhouse agent runtime

Wheelhouse has one versioned contract for every agent-assisted task.
The contract covers automatic PR and issue triage with and without search, bounded schema repair, deep review with and without search, and natural-language decision mapping with and without search.

Claude is the production primary adapter.
Every current action resolves to the exact pinned direct Claude Action implementation through the shared selection boundary.
Codex CLI app-server remains implemented and tested only as disabled non-target adapter evidence because public GitHub Actions cannot securely authenticate the captain's ChatGPT Pro subscription noninteractively.
Fallback is disabled, so no provider failure invokes a different adapter automatically.

## Current operating state

The checked-in state is intentionally:

- `primary_profile: claude-action-current-pinned`
- `target: claude`
- `fallback: none`
- every action `target: claude`
- `codex-app-server` recorded only under `disabled_adapters`

This is not selected by secret presence.
Provider environment overrides are rejected.
The current selection cannot target Codex or reach its workflow installation branches.

The Claude production path keeps the exact reviewed action commit, immutable model identifier, turn limits, token boundaries, and output behavior.
Every direct Claude step is guarded by an explicit `claude` selection.
Each direct action is preceded by immutable `AgentTask` construction and followed by a trusted bridge that validates the transcript's observed model and emits atomic `AgentResult` plus content-free events.
Mandatory bubblewrap subprocess isolation removes GitHub credentials from the Claude model process while trusted workflow steps retain their existing card and target mutation tokens outside that process.

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
The selected harness runs behind an externally enforced disposable subprocess boundary on the GitHub Actions runner.

The active Claude compatibility boundary receives only:

- bounded prompt and input files represented by the immutable task
- the exact action-specific tool allowlist
- the selected Claude subscription credential
- the optional read-only search credential on search-enabled paths
- one writable temporary filesystem for action output

The Claude model subprocess never receives `FLEET_TOKEN`, `github.token`, or another GitHub credential with acting authority.
Search-enabled paths may receive only the optional `READONLY_TOKEN` and the narrow `wheelhouse-search` command.
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

Codex is pinned to CLI `0.144.0`, source commit `767822446c7a594caa19609ca435281a9ec67e0d`, npm package integrity, architecture-specific Linux executable-package integrity, and vendored app-server schema digests.
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

Path tools reject absolute paths, traversal, symlinks, devices, sockets, and escaping canonical paths.
Results and call counts are bounded, including rejected tool attempts.
Filesystem result bounds apply to the complete canonical serialized response, including paths and envelope fields.
Read and search payloads truncate deterministically to fit that complete envelope.
Search keeps the existing repository allowlist and operation semantics but no longer needs model-facing Write or Bash on Codex.

Final-result delivery is independent of transcript retention.
Raw transcripts are discarded by default.
A schema-invalid but delivered triage candidate remains available to the existing one-turn repair policy.
Missing output and evidence failure do not trigger schema repair.
Trusted code still performs normalized triage, evidence anchoring, cross-repository reference qualification, natural-language action allowlisting, card claims, revision checks, PR head checks, and auto-merge G0-G7 checks.

No model output directly authorizes or performs a GitHub action.

## Deadlines, cancellation, and retry

Each action has a soft deadline, a cancellation grace interval, and a hard deadline.
The worker counts every logical provider request and turn before it can proceed, including continuations after rejected tool calls, disables provider and stream retries, and interrupts before continuation at an observed token ceiling or after any observed overrun.
Codex receives the task input ceiling through its pinned app-server context configuration and additional native output-schema string ceilings before the first provider request.
Durable worker checkpoints preserve observed spend, usage, and model provenance if the worker crashes or is killed after spend begins.
At the soft deadline the supervisor writes a cancellation request.
The Codex adapter sends `turn/interrupt` and waits for an interrupted terminal event.
After the grace interval the supervisor sends `SIGTERM` to the process group.
At the hard deadline it sends `SIGKILL`.

A partial final is never accepted.
Results are written to a temporary file, flushed, validated, and atomically renamed.

Current actions permit one candidate attempt and no runtime retry.
The existing exactly-one schema repair is a separate task, not a provider retry.
Fallback remains `none`.

Stable error families distinguish contract, config, selection, capability, auth, quota, provider, transport, input, provenance, tool, sandbox, lifecycle, harness, output, stale-target, consumer, and internal failures.
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

Raw prompts, target inputs, tool results, app-server traffic, auth state, and transcripts are not uploaded.
Diagnostics are scanned for GitHub tokens, model keys, bearer values, private keys, and sensitive auth fields.
The worker also compares diagnostics and final output against the exact injected credential values in memory without printing them.
Only a content-free redaction count may be retained.

If a secret exposure is suspected, disable the credential-bearing workflow first, revoke the affected credential or OAuth session, invalidate every stale runner copy, and rotate before resuming.

## Failure recovery

For triage, revision freshness and held-card recovery remain product-level safeguards outside the runtime.
A failed, cancelled, or missing result publishes an eligible held card through the existing exact-revision fail-open path.
A stale attempt cannot publish over a newer revision.

For deep review, missing output posts the existing fixed no-verdict note and leaves the card open.

For natural-language mapping, missing or invalid output cannot produce an action.
The marker-keyed failure note remains bounded and fire-once.
A successful mapped action still enters the existing card claim and deterministic executor.

To inspect a failure:

1. Read selection and capability negotiation before model text.
2. Confirm requested and observed provenance match.
3. Use the stable error code to identify the phase.
4. Fix configuration or the named auth profile instead of weakening a capability.
5. Never replay a natural-language action against a changed card or PR head.

## Provider changes

Claude remains the production primary until the captain approves a supported, subscription-funded, secure, and behaviorally compatible alternative.
Provider changes require an explicit reviewed plan covering credentials, billing, data boundaries, all seven action paths, and deterministic consumer parity.
They must preserve `fallback: none` and cannot be selected by secret presence or an environment override.

Codex is not an active target or expected future primary under the current plan.
OpenCode with Z.AI Coding Plan is deferred and disabled, with no adapter implemented.
Neither status authorizes a credential request, paid proof, workflow target, fallback, or production promise.
The provider-neutral adapter contract should be extended only after a new captain decision and without embedding provider-specific policy in runtime core.

## Local verification

No paid model call is required for local validation.

Run:

```bash
python scripts/agent_runtime.py verify-pins
python tests/test_agent_runtime_contract.py
python tests/test_agent_runtime_capabilities.py
python tests/test_agent_runtime_security.py
python tests/test_agent_runtime_lifecycle.py
python tests/test_agent_runtime_consumers.py
python tests/test_agent_runtime_workflows.py
```

The fake adapter exercises all action profiles without network or credentials.
Do not run a paid live proof or mutate repository secrets without explicit approval.
