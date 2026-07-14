"""Trusted compatibility helpers for existing Wheelhouse output consumers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .contract import ContractError, load_json_regular, validate_contract


def load_agent_result(path: str) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        value = load_json_regular(path, max_bytes=2 * 1024 * 1024)
        validate_contract(value, "AgentResult")
    except (ContractError, OSError, ValueError):
        return None
    return value


def delivered_value(path: str, require_success: bool = False) -> Any:
    result = load_agent_result(path)
    if result is None:
        return None
    if require_success and result.get("status") != "succeeded":
        return None
    final = result.get("final")
    if isinstance(final, dict) and "value" in final:
        return final["value"]
    if not require_success:
        delivered = result.get("delivered")
        if isinstance(delivered, dict) and "value" in delivered:
            return delivered["value"]
    return None


def result_text(path: str, require_success: bool = False) -> str:
    value = delivered_value(path, require_success=require_success)
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict) and isinstance(value.get("text"), str) and len(value) == 1:
        return value["text"].strip()
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def export_value(path: str, output: str, require_success: bool = True) -> bool:
    value = delivered_value(path, require_success=require_success)
    if value is None:
        return False
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
    return True
