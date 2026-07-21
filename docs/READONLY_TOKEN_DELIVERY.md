# READONLY_TOKEN delivery to model actions

Status: Accepted

## Decision

Production search-enabled Claude actions receive `READONLY_TOKEN` in process,
both as the action's `github_token` input and as `GH_TOKEN` in the step
environment. This is the intended credential boundary: model tooling, including
`gh` through `wheelhouse-search`, may use the token directly for anything its
permissions allow. The search steps in
[`.github/workflows/claude-model.yml`](../.github/workflows/claude-model.yml)
are the authoritative wiring.

`READONLY_TOKEN` must be a fine-grained PAT limited to public repository reads,
with no write permissions and no access to private repositories. Wheelhouse does
not manage private repositories, so no private or cross-repository private-read
credential is needed. The setup requirement is also documented in the
[`README`](../README.md#setup).

## Alternative declined

A broker-only design exists in [`agent_runtime/brokers.py`](../agent_runtime/brokers.py):
`SearchBroker` keeps the token in the trusted host and serves bounded requests
over a Unix socket to a sandboxed model adapter. That design provides stronger
credential isolation, but it was considered and declined as a replacement for
production direct delivery. Do not recommend it as one.

## Accepted tradeoff

In-process model tooling can reach the token value. GitHub log masking may
replace ordinary appearances with `***`, but it is not exfiltration-proof: a
compromised or prompt-injected task could encode the value to evade masking.
This residual exposure is understood and accepted because the credential is
limited to public reads and has no write or private-repository access. Existing
environment scrubbing and the scoped `wheelhouse-search` wrapper remain
defense-in-depth controls, not a claim that the token is unavailable in process.

## Anonymous public clone child

The owner/maintainer-gated `nl-decision.search` path may ask `wheelhouse-search`
to clone one complete public HTTPS Git URL. This is separate from the existing
authenticated `gh` allowlist. The wrapper resolves the URL's host and rejects
loopback, link-local, private, reserved, metadata, or otherwise non-public
addresses before it starts Git. Git receives an explicit credential-free
environment with a fresh home and configuration, prompting and credential
helpers disabled, and no model, GitHub, cloud, or runner credentials inherited.
The shallow data-only clone is retained outside the target workspace only for
the model step, then a trusted `always()` step removes it.

### DNS rebinding residual

For arbitrary public custom Git hosts, Git resolves the hostname again between
Wheelhouse's validation and Git's connection. A host can theoretically change
from the validated public address to a non-public address in that narrow window.
Wheelhouse does not add a custom HTTP stack, address-pinning proxy, or provider
proxy to close that gap. This is the explicitly accepted residual of aligning
with the official `claude-code-action` posture. Redirect following is disabled,
metadata and other non-public answers are rejected at validation, the child has
no credentials, the hosted runner has no Wheelhouse-internal service network,
and clone time and retained data remain bounded.
