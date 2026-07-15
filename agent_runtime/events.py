"""Bounded monotonic normalized event stream."""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any

from . import API_VERSION
from .contract import canonical_json_bytes, result_projection_sha256, validate_contract
from .redaction import sanitize_message


class EventError(ValueError):
    pass


class EventWriter:
    def __init__(self, path: str, execution_id: str, max_bytes: int) -> None:
        self.path = Path(path)
        self.execution_id = execution_id
        self.max_bytes = max_bytes
        self.seq = 0
        self.written = 0
        self.started = time.monotonic()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("wb")

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.seq += 1
        event = {
            "apiVersion": API_VERSION,
            "kind": "AgentEvent",
            "executionId": self.execution_id,
            "seq": self.seq,
            "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "monotonicMs": int((time.monotonic() - self.started) * 1000),
            "type": event_type,
            "data": data or {},
        }
        validate_contract(event, "AgentEvent")
        line = canonical_json_bytes(event) + b"\n"
        if self.written + len(line) > self.max_bytes:
            raise EventError("normalized event stream exceeded its byte bound")
        self.handle.write(line)
        self.handle.flush()
        self.written += len(line)
        return event

    def warning(self, code: str, message: str) -> None:
        self.emit("warning", {"code": code, "message": sanitize_message(message)})

    def close(self) -> None:
        if not self.handle.closed:
            self.handle.flush()
            os.fsync(self.handle.fileno())
            self.handle.close()

    def __enter__(self) -> "EventWriter":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def read_events(path: str) -> list[dict[str, Any]]:
    events = []
    expected = 1
    terminal = 0
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line)
                validate_contract(event, "AgentEvent")
                if event["seq"] != expected:
                    raise EventError("event sequence is not monotonic")
                expected += 1
                if event["type"] == "execution.completed":
                    terminal += 1
                events.append(event)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        if isinstance(error, EventError):
            raise
        raise EventError("event stream is malformed") from error
    if terminal > 1:
        raise EventError("event stream has duplicate terminal events")
    return events


def verify_result_event_binding(result: dict[str, Any], path: str) -> None:
    """Require one terminal event to project the selected AgentResult exactly."""

    events = read_events(path)
    if not events or events[-1]["type"] != "execution.completed":
        raise EventError("event stream is missing its terminal projection")
    if any(event["executionId"] != result["executionId"] for event in events):
        raise EventError("event stream execution id does not match its result")
    terminal = events[-1]
    if (
        terminal["data"].get("projection") != "agent-result-without-artifacts/v1"
        or terminal["data"].get("resultSha256") != result_projection_sha256(result)
        or terminal["data"].get("status") != result["status"]
    ):
        raise EventError("terminal event projection does not match its result")
