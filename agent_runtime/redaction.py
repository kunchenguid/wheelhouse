"""Content-class diagnostics redaction for the agent runtime."""

from __future__ import annotations

import re
from typing import Iterable

REDACTED = "[REDACTED_SECRET]"
_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bat-[A-Za-z0-9._~-]{12,}\b"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:Bearer|token)\s+[A-Za-z0-9._~+/-]{16,}=*\b", re.I),
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|personal[_-]?access[_-]?token|agent[_-]?private[_-]?key|client[_-]?secret)\s*[=:]\s*[^\s,;]{8,}"),
    re.compile(r'(?i)"(?:OPENAI_API_KEY|CODEX_API_KEY|CODEX_ACCESS_TOKEN|access_token|refresh_token|id_token|personal_access_token|agent_private_key|client_secret)"\s*:\s*(?:"[^"\\]*(?:\\.[^"\\]*)*"|[^,}\s]+)'),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


class SecretDetected(ValueError):
    """Retained diagnostics still contained a secret after redaction."""


def redact_text(text: str, max_chars: int = 8192) -> tuple[str, int]:
    value = str(text or "")[:max_chars]
    matches = 0
    for pattern in _PATTERNS:
        value, count = pattern.subn(REDACTED, value)
        matches += count
    return value, matches


def contains_secret(text: str) -> bool:
    return any(pattern.search(str(text or "")) for pattern in _PATTERNS)


def sanitize_message(text: str, fallback: str = "Agent runtime operation failed.", max_chars: int = 500) -> str:
    clean, _ = redact_text(text, max_chars=max_chars)
    clean = re.sub(r"[\r\n\t]+", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or fallback


def redact_lines(lines: Iterable[str], max_chars: int = 32768) -> tuple[str, int]:
    return redact_text("\n".join(lines), max_chars=max_chars)
