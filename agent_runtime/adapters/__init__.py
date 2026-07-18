"""Trusted adapter registry.

New adapters implement :class:`AgentAdapterV1` and enter this explicit allowlist
after their binary, auth profile, tools, events, errors, cancellation, and
contract fixtures are reviewed.
"""

from .base import AgentAdapterV1, AdapterDescriptor, AdapterProbe
from .claude import ClaudeCliAdapter
from .codex import CodexAppServerAdapter
from .fake import FakeAdapter

ADAPTERS = {
    "claude-cli": ClaudeCliAdapter,
    "codex-app-server": CodexAppServerAdapter,
    "fake": FakeAdapter,
}

__all__ = ["ADAPTERS", "AgentAdapterV1", "AdapterDescriptor", "AdapterProbe"]
