"""Minimal stdio MCP bridge for Wheelhouse canonical typed tools.

The bridge is launched with a scrubbed environment by the pinned Claude plan.
It exposes only the exact negotiated tool inventory and has no command-execution
or raw network operation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .contract import load_json_regular
from .tools import CanonicalTools, TOOL_DESCRIPTIONS, TOOL_SCHEMAS, ToolError

NAME_TO_CANONICAL = {
    "fs_read": "fs.read",
    "fs_grep": "fs.grep",
    "fs_glob": "fs.glob",
    "public_search": "public.search",
    "public_fetch": "public.fetch",
    "public_git_snapshot": "public.git_snapshot",
    "public_artifact": "public.artifact",
    "exercise_run": "exercise.run",
}
CANONICAL_TO_NAME = {value: key for key, value in NAME_TO_CANONICAL.items()}


def _write(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _error(request_id: Any, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def serve(plan_path: Path) -> None:
    plan = load_json_regular(plan_path, max_bytes=2 * 1024 * 1024)
    declared = plan.get("tools", {}).get("tools")
    if not isinstance(declared, list):
        raise SystemExit("canonical MCP plan is invalid")
    canonical_names = [row.get("name") for row in declared if isinstance(row, dict)]
    if (
        len(canonical_names) != len(declared)
        or len(set(canonical_names)) != len(canonical_names)
        or any(name not in CANONICAL_TO_NAME for name in canonical_names)
    ):
        raise SystemExit("canonical MCP tool inventory is invalid")
    max_results = {row["name"]: row["maxResultBytes"] for row in declared}
    tools = CanonicalTools(
        os.environ.get("WHEELHOUSE_WORK_ROOT", "/work"),
        canonical_names,
        max_results,
        public_socket=os.environ.get("WHEELHOUSE_PUBLIC_SOCKET", ""),
        exercise_socket=os.environ.get("WHEELHOUSE_EXERCISE_SOCKET", ""),
        execution_id=str(plan.get("executionId") or ""),
        task_sha256=str(plan.get("taskSha256") or ""),
    )
    max_calls = plan.get("limits", {}).get("maxToolCalls")
    if not isinstance(max_calls, int) or isinstance(max_calls, bool) or max_calls < 0:
        raise SystemExit("canonical MCP call bound is invalid")

    for line in sys.stdin:
        if len(line.encode("utf-8")) > 65_536:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if method == "initialize" and "id" in request:
            protocol = str(params.get("protocolVersion") or "2024-11-05")
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": protocol,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "wheelhouse-canonical-tools", "version": "1.0.0"},
                    },
                }
            )
            continue
        if method in ("notifications/initialized", "notifications/cancelled"):
            continue
        if method == "ping" and "id" in request:
            _write({"jsonrpc": "2.0", "id": request_id, "result": {}})
            continue
        if method == "tools/list" and "id" in request:
            rows = [
                {
                    "name": CANONICAL_TO_NAME[name],
                    "description": TOOL_DESCRIPTIONS[name],
                    "inputSchema": TOOL_SCHEMAS[name],
                }
                for name in canonical_names
            ]
            _write({"jsonrpc": "2.0", "id": request_id, "result": {"tools": rows}})
            continue
        if method == "tools/call" and "id" in request:
            requested = params.get("name")
            canonical = NAME_TO_CANONICAL.get(str(requested or ""))
            arguments = params.get("arguments")
            if canonical not in canonical_names:
                _error(request_id, -32602, "canonical tool is not available in this task")
                continue
            if tools.calls >= max_calls:
                _error(request_id, -32000, "canonical tool-call bound exceeded")
                continue
            try:
                value = tools.call(canonical, arguments)
                text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [{"type": "text", "text": text}],
                            "isError": False,
                        },
                    }
                )
            except ToolError:
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": '{"status":"unavailable","reason_code":"request.rejected"}',
                                }
                            ],
                            "isError": True,
                        },
                    }
                )
            continue
        if "id" in request:
            _error(request_id, -32601, "MCP method is not supported")


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--plan", required=True)
    args = parser.parse_args()
    serve(Path(args.plan))


if __name__ == "__main__":
    main()
