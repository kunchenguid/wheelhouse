"""Deterministic no-network adapter for contract and workflow tests."""

from __future__ import annotations

import os
from typing import Any

from ..contract import canonical_sha256, load_json_regular
from .base import AdapterDescriptor, AdapterProbe, AgentAdapterV1


class FakeAdapter(AgentAdapterV1):
    id = "fake"
    adapter_version = "1.0.0"

    def probe(self, task: dict[str, Any], schema_bytes: bytes | None = None) -> AdapterProbe:
        script_path = os.environ.get("WHEELHOUSE_FAKE_ADAPTER_SCRIPT", "")
        if not script_path:
            raise ValueError("fake adapter script is missing")
        script = load_json_regular(script_path, max_bytes=1024 * 1024)
        if not isinstance(script, dict):
            raise ValueError("fake adapter script must be an object")
        descriptor = {
            "adapter": "fake",
            "adapterVersion": self.adapter_version,
            "harness": "fake-harness",
            "harnessVersion": "1.0.0",
            "harnessDigest": canonical_sha256({"fake": 1}),
            "protocol": "fake-script-v1",
            "protocolSchemaSha256": canonical_sha256({"fake-script": 1}),
            "capabilities": {
                "input.text": {"mechanism": "script", "maxBytes": 2000000},
                "fs.read": {"mechanism": "dynamic-tool", "writes": False},
                "fs.grep": {"mechanism": "dynamic-tool"},
                "fs.glob": {"mechanism": "dynamic-tool"},
                "github.search.readonly": {"mechanism": "dynamic-tool-broker"},
                "process.exec": {"mechanism": "external-deny", "mode": "none"},
                "tool.network": {"mechanism": "external-deny", "modes": ["none", "broker-only"]},
                "output.structured": {"mechanism": "native-schema", "strict": True, "maxSchemaBytes": 65536},
                "lifecycle.cancel": {"mechanism": "fake-cancel", "ackMs": 10},
                "provenance.actual-model": {"mechanism": "script"},
                "provenance.actual-provider": {"mechanism": "script"},
                "usage.tokens": {"mechanism": "script"},
                "quota.snapshot": {"mechanism": "script"},
                "isolation.external": {"mechanism": "test-sandbox", "worker": "sandboxed-adapter-worker"},
            },
        }
        return AdapterProbe(
            descriptor=AdapterDescriptor(descriptor),
            binary_path="python3",
            auth_source="",
            supplemental={"script": script, "scriptSha256": canonical_sha256(script)},
        )

    def compile(self, task: dict[str, Any], proof: dict[str, Any], probe: AdapterProbe) -> dict[str, Any]:
        return {
            "planVersion": "wheelhouse.agent-runtime/adapter-plan-v1",
            "executionId": task["metadata"]["executionId"],
            "action": task["metadata"]["action"],
            "taskSha256": canonical_sha256(task),
            "candidate": task["spec"]["selection"]["candidates"][0],
            "descriptor": probe.descriptor.value,
            "negotiation": proof,
            "prompt": task["spec"]["prompt"],
            "inputs": task["spec"]["inputs"],
            "tools": task["spec"]["tools"],
            "limits": task["spec"]["limits"],
            "output": task["spec"]["output"],
            "retention": task["spec"]["retention"],
            "isolation": task["spec"]["isolation"],
            "fakeScript": probe.supplemental["script"],
        }

    def worker_command(self, plan_path: str, output_dir: str) -> list[str]:
        return ["python3", "-m", "agent_runtime.worker", "--plan", plan_path, "--output-dir", output_dir]
