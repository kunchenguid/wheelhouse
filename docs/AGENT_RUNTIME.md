# Wheelhouse agent runtime

Wheelhouse has one versioned contract for every agent-assisted task.
The contract covers automatic PR and issue triage with and without search, bounded schema repair, deep review with and without search, and natural-language decision mapping with and without search.

Codex CLI app-server is the selected future primary adapter.
It is fully implemented but deliberately inactive.
The current direct Claude Action remains the explicit production and rollback bridge while the Codex authentication and activation gates are unmet.
Fallback is disabled, so a Codex failure never invokes Claude automatically.

## Current operating state

The checked-in state is intentionally:

- `primary_profile: codex-subscription-pinned`
- `rollout: legacy`
- `production_activation: false`
- `fallback: none`
- every action `rollout: legacy`

This is not selected by secret presence.
Only a reviewed `wheelhouse.config.yml` change can activate Codex.
The emergency environment override can select `legacy` only.
It cannot select a provider, model, effort, credential, or stronger tool policy.

The temporary Claude bridge keeps the exact reviewed action commit, model alias, turn limits, token boundaries, and output fallback behavior.
Every direct Claude step is guarded by an explicit `legacy` selection.
A failed Codex task cannot make one of those steps run.

## Why Codex is inactive

The authentication audit found no officially supported secure noninteractive way to use the current ChatGPT Pro subscription from this public GitHub Actions repository.

Do not add any of these merely to unblock activation:

- `OPENAI_API_KEY`
- `CODEX_API_KEY`
- `CODEX_ACCESS_TOKEN`
- a copied `auth.json` blob

The official Codex GitHub Action uses OpenAI Platform API-key billing.
That is not ChatGPT subscription authentication and is forbidden unless the captain separately changes the billing decision.

A Codex access token is the preferred future noninteractive subscription credential, but it is available only to ChatGPT Business and Enterprise workspaces and is intended for trusted private automation.
It is a Codex-scoped credential rather than a full ChatGPT browser session, but it can spend the creating user's or workspace's Codex entitlement and must be treated as a high-impact credential.

Managed `auth.json` is officially documented only for trusted private CI.
It is forbidden by the official guidance for public or open-source repositories.
It contains refreshable OAuth material and requires one serialized consumer plus secure mutable persistence of every refreshed replacement.
A repository secret cannot safely implement that write-back lifecycle.

The runtime does not accept ambient Codex credentials.
A future approved private credential boundary must provide one mode-`0600` named handoff file to the trusted supervisor.
The public workflows do not create that file.
Managed `auth.json` activation remains fail-closed even after a policy change until serialized secure refresh persistence and stale-seed rejection are implemented and approved.

## Runtime boundary

Trusted Wheelhouse steps continue to authorize events, fetch immutable target inputs, bind revisions, and perform every GitHub mutation.
The selected harness runs in a disposable sandboxed adapter worker on the GitHub Actions runner.

The worker receives only:

- content-addressed bounded prompt and input artifacts
- exact typed tool schemas
- a provider-only network channel
- the selected model credential
- one writable temporary filesystem and result directory

The worker never receives `FLEET_TOKEN`, `github.token`, `READONLY_TOKEN`, a repository credential, the runner home, or another workspace.

`READONLY_TOKEN` stays in a trusted host broker.
The model can call `github.search.readonly`, but it receives only the bounded broker result.
It never receives the token or a shell.

Codex built-in shell, web search, apps, connectors, memories, plugins, hooks, and multi-agent features are disabled.
The app-server receives only task-declared dynamic tools.
Unregistered app-server requests are denied.

The model network runs through a Unix-socket CONNECT proxy with an auth-profile endpoint allowlist.
The sandbox has a separate network namespace and no direct network route.
Tool network is either absent or the read-only broker socket.

## Contract and pins

The public contract version is `wheelhouse.agent-runtime/v1alpha1`.
The public documents are:

- `AgentTask`
- monotonic NDJSON `AgentEvent`
- atomic `AgentResult`

The schemas live under `agent_runtime/schemas/`.
Unknown fields are rejected.
Canonical contract and proof hashes use deterministic JSON plus SHA-256.

Codex is pinned to CLI `0.144.0`, source commit `767822446c7a594caa19609ca435281a9ec67e0d`, npm package integrity, Linux binary-package integrity, and vendored app-server schema digests.
Run `python scripts/agent_runtime.py verify-pins` to verify the protocol files.
Each workflow also compares `npm view @openai/codex@0.144.0 dist.integrity` with the locked package integrity before installation.

The app-server is driven over stdio JSONL.
Human terminal output is never scraped.
The worker performs `initialize`, `account/read` with `refreshToken:false`, model listing, provider capability reading, quota reading when available, `thread/start`, `turn/start`, and `turn/interrupt`.

The worker requires observed account type `chatgpt`.
A future access-token profile also requires an eligible Business or Enterprise plan.
The generated config forces `chatgpt` login and the captain-approved workspace ID.
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
Results and call counts are bounded.
Search keeps the existing repository allowlist and operation semantics but no longer needs model-facing Write or Bash on Codex.

Final-result delivery is independent of transcript retention.
Raw transcripts are discarded by default.
A schema-invalid but delivered triage candidate remains available to the existing one-turn repair policy.
Missing output and evidence failure do not trigger schema repair.
Trusted code still performs normalized triage, evidence anchoring, cross-repository reference qualification, natural-language action allowlisting, card claims, revision checks, PR head checks, and auto-merge G0-G7 checks.

No model output directly authorizes or performs a GitHub action.

## Deadlines, cancellation, and retry

Each action has a soft deadline, a cancellation grace interval, and a hard deadline.
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

## Rollback

The current production selection is already the temporary exact Claude bridge.
After Codex activation, set the allowlisted emergency rollout profile to `legacy` or commit the action rollout back to `legacy`.

Rollback does not edit secrets, models, providers, tools, or output consumers.
It does not run automatically after a Codex error.

The direct bridge must remain until Codex has production evidence.
The dependency-ordered follow-up is to add the pinned Claude Agent SDK as another adapter behind this same contract, then remove all workflow-specific direct Claude Action calls.
That follow-up must not create automatic fallback.

## Production activation prerequisites

Codex activation requires every item below.
The runtime rejects selection before model spend when any item is missing.

1. The captain approves one authentication and topology alternative.
2. Security review approves a private credential-bearing runner or workflow boundary.
3. A manually approved non-production proof passes and its proof credential is revoked.
4. A separate production credential has an owner, finite expiry, rotation date, revocation owner, and incident runbook.
5. All seven paths pass contract, sandbox, cancellation, malformed-output, provenance, and consumer-parity tests.
6. ChatGPT auth, exact provider, exact model, exact effort, and no API-key substitution are proven.
7. Claude remains explicit rollback and automatic fallback remains disabled.
8. A captain-approved config commit sets the selected action rollouts and `production_activation: true`.
9. Alternate PR triage is separately approved before its verdict may influence auto-merge.

For the preferred future path, the captain must first choose a Business or Enterprise workspace and a trusted private automation boundary.
Only after that decision should an operator create a finite-expiry `CODEX_ACCESS_TOKEN` owned by a dedicated least-privilege workflow identity.
No credential request is appropriate under the current Pro plus public-repository topology.

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
