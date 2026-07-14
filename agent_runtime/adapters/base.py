"""Future-adapter interface for Agent Runtime Contract v1."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AdapterDescriptor:
    value: dict[str, Any]


@dataclass(frozen=True)
class AdapterProbe:
    descriptor: AdapterDescriptor
    binary_path: str
    auth_source: str
    supplemental: dict[str, Any]


class AgentAdapterV1(ABC):
    """Host-side adapter interface.

    A future adapter must supply an immutable descriptor and these operations.
    The core remains the only owner of sequencing, deadlines, result validation,
    retention, and normalized terminal status.
    """

    id: str
    adapter_version: str
    supported_contract_majors: tuple[int, ...] = (1,)

    @abstractmethod
    def probe(self, task: dict[str, Any]) -> AdapterProbe:
        """Prove pins, auth type, and static capabilities without generation."""

    @abstractmethod
    def compile(self, task: dict[str, Any], proof: dict[str, Any], probe: AdapterProbe) -> dict[str, Any]:
        """Compile one immutable sandboxed adapter worker plan."""

    @abstractmethod
    def worker_command(self, plan_path: str, output_dir: str) -> list[str]:
        """Return the command executed only inside the external sandbox."""

    def cancel_protocol(self) -> str:
        """Return the adapter-native cancellation primitive."""
        return "process-group"
