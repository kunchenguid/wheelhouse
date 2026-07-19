# READONLY_TOKEN delivery to model actions

Status: Accepted

## Decision

Search-enabled model actions receive `READONLY_TOKEN` in process, both as the
action's `github_token` input and as `GH_TOKEN` in the step environment. This is
the intended credential boundary: model tooling, including `gh` through
`wheelhouse-search`, may use the token directly for anything its permissions
allow. The search steps in [`.github/workflows/claude-model.yml`](../.github/workflows/claude-model.yml)
are the authoritative wiring.

`READONLY_TOKEN` must be a fine-grained PAT limited to public repository reads,
with no write permissions and no access to private repositories. Wheelhouse does
not manage private repositories, so no private or cross-repository private-read
credential is needed. The setup requirement is also documented in the
[`README`](../README.md#setup).

## Alternative declined

A broker-only design exists in [`agent_runtime/brokers.py`](../agent_runtime/brokers.py):
`SearchBroker` keeps the token in the trusted host and serves bounded requests
over a Unix socket to the sandboxed `claude-cli` adapter. That design provides
stronger credential isolation, but it is not the accepted delivery mechanism
for search-enabled model actions. Direct in-process use was chosen deliberately.

## Accepted tradeoff

In-process model tooling can reach the token value. GitHub log masking may
replace ordinary appearances with `***`, but it is not exfiltration-proof: a
compromised or prompt-injected task could encode the value to evade masking.
This residual exposure is understood and accepted because the credential is
limited to public reads and has no write or private-repository access. Existing
environment scrubbing and the scoped `wheelhouse-search` wrapper remain
defense-in-depth controls, not a claim that the token is unavailable in process.
