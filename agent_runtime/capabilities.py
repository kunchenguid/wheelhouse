"""Capability descriptors and fail-before-spend negotiation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contract import canonical_sha256


class CapabilityError(ValueError):
    pass


@dataclass(frozen=True)
class Negotiation:
    descriptor: dict[str, Any]
    proof: dict[str, Any]


def codex_descriptor(binary_version: str, binary_digest: str, protocol_schema_sha256: str) -> dict[str, Any]:
    return {
        "adapter": "codex-app-server",
        "adapterVersion": "1.0.0",
        "harness": "codex-cli",
        "harnessVersion": binary_version,
        "harnessDigest": binary_digest,
        "protocol": "app-server-v2-experimental",
        "protocolSchemaSha256": protocol_schema_sha256,
        "capabilities": {
            "input.text": {"mechanism": "turn-input", "maxBytes": 2000000},
            "fs.read": {"mechanism": "dynamic-tool", "writes": False},
            "fs.grep": {"mechanism": "dynamic-tool"},
            "fs.glob": {"mechanism": "dynamic-tool"},
            "github.search.readonly": {"mechanism": "dynamic-tool-broker"},
            "process.exec": {"mechanism": "external-deny", "mode": "none"},
            "tool.network": {"mechanism": "external-deny", "modes": ["none", "broker-only"]},
            "output.structured": {"mechanism": "native-schema", "strict": True, "maxSchemaBytes": 65536},
            "lifecycle.cancel": {"mechanism": "turn/interrupt+process-group", "ackMs": 10000},
            "provenance.actual-model": {"mechanism": "thread-start-response+reroute-rejection"},
            "provenance.actual-provider": {"mechanism": "thread-start-response"},
            "usage.tokens": {"mechanism": "thread/tokenUsage/updated"},
            "quota.snapshot": {"mechanism": "account/rateLimits/read"},
            "isolation.external": {"mechanism": "bubblewrap+network-proxy", "worker": "sandboxed-adapter-worker"},
        },
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CapabilityError(message)


def negotiate(task: dict[str, Any], descriptor: dict[str, Any], host_proof: dict[str, Any]) -> Negotiation:
    candidate = task["spec"]["selection"]["candidates"][0]
    _require(task["spec"]["isolation"]["profile"] == "sandboxed-worker-v1", "task did not request the sandboxed worker profile")
    _require(len(task["spec"]["selection"]["candidates"]) == 1, "fallback candidates are not enabled")
    _require(task["spec"]["selection"]["fallback"]["mode"] == "none", "automatic fallback is forbidden")
    _require(candidate["adapter"] == descriptor["adapter"], "adapter does not match the selected plan")
    _require(candidate["harness"] == descriptor["harness"], "harness does not match the selected plan")
    _require(candidate["allowModelAlias"] is False, "model aliases are forbidden")
    _require(candidate["costClass"] == "subscription", "Codex must not change the billing class")
    _require(host_proof.get("externalSandbox") is True, "external sandbox is unavailable")
    _require(host_proof.get("networkProxy") is True, "provider-only network enforcement is unavailable")
    _require(host_proof.get("denyHostHome") is True, "host home denial is unavailable")
    _require(host_proof.get("processGroupCleanup") is True, "process-group cleanup is unavailable")

    available = descriptor["capabilities"]
    proven_optional = []
    for requirement in task["spec"]["capabilities"]["required"]:
        name = requirement["name"]
        constraints = requirement.get("constraints") or {}
        _require(name in available, "required capability %s is unavailable" % name)
        capability = available[name]
        if name == "process.exec":
            _require(constraints.get("mode") == "none" and capability.get("mode") == "none", "process execution is not externally denied")
        elif name == "tool.network":
            _require(constraints.get("mode") in capability.get("modes", []), "tool network boundary is unavailable")
        elif name == "output.structured":
            mechanisms = constraints.get("mechanismAnyOf") or []
            _require(capability.get("mechanism") in mechanisms, "structured output mechanism is not accepted")
            _require(capability.get("strict") is True, "strict structured output is unavailable")
        elif name == "lifecycle.cancel":
            _require(capability.get("ackMs", 10**9) <= constraints.get("ackMs", 0), "cancel acknowledgement bound is too weak")
        elif name == "fs.read":
            _require(capability.get("writes") is False and constraints.get("writes") is False, "read capability could write")
    for requirement in task["spec"]["capabilities"]["optional"]:
        if requirement["name"] in available:
            proven_optional.append(requirement["name"])

    requested_tools = [tool["name"] for tool in task["spec"]["tools"]["tools"]]
    allowed_tools = {"fs.read", "fs.grep", "fs.glob"}
    if task["metadata"]["action"].endswith(".search"):
        allowed_tools.add("github.search.readonly")
    _require(set(requested_tools).issubset(allowed_tools), "task requested a tool outside its action profile")
    _require(len(requested_tools) == len(set(requested_tools)), "task requested a duplicate tool")
    _require(task["spec"]["tools"]["default"] == "deny", "tool default must be deny")
    _require(task["spec"]["tools"]["parallel"] is False, "parallel tool calls are not enabled")

    proof = {
        "descriptorSha256": canonical_sha256(descriptor),
        "hostProofSha256": canonical_sha256(host_proof),
        "required": [item["name"] for item in task["spec"]["capabilities"]["required"]],
        "optionalProven": proven_optional,
        "exactTools": requested_tools,
        "structuredOutputMechanism": "native-schema",
        "fallback": "none",
    }
    return Negotiation(descriptor=descriptor, proof=proof)
