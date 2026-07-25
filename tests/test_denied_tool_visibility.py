#!/usr/bin/env python3
"""Regression coverage for bounded denied-tool evidence and split triage truth."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_runtime.retention import (  # noqa: E402
    MAX_DENIED_EVIDENCE_BYTES,
    reduce_execution,
    retained_tool_denials,
)
from agent_runtime.claude_handoff import PACKAGED_RUNTIME_FILES  # noqa: E402
sys.path.insert(0, str(ROOT / "scripts"))
import render_card as rc  # noqa: E402


FAILURES = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def raw_events(denied=True):
    events = [
        {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "denied-1",
                        "name": "Bash",
                        "input": {
                            "command": "wheelhouse-search --token=ghp_abcdefghijklmnopqrstuvwxyz1234567890 --request-file search-request.json",
                            "content": "SECRET FILE BODY MUST NOT SURVIVE",
                            "env": {"GH_TOKEN": "github_pat_abcdefghijklmnopqrstuvwxyz1234567890"},
                        },
                    },
                    {
                        "type": "tool_use",
                        "id": "denied-2",
                        "name": "Bash",
                        "input": {"command": "wheelhouse-search search-request.json"},
                    }
                ]
            },
        },
    ]
    if denied:
        events.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "denied-1",
                            "is_error": True,
                            "content": "Permission denied: invocation did not match the configured allowlist.",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "denied-2",
                            "is_error": True,
                            "content": "Permission denied: invocation did not match the configured allowlist.",
                        }
                    ]
                },
            }
        )
    else:
        events.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "denied-1",
                            "is_error": False,
                            "content": "ok",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "denied-2",
                            "is_error": False,
                            "content": "ok",
                        }
                    ]
                },
            }
        )
    events.append(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": '{"summary":"bounded"}',
            "duration_ms": 10,
            "num_turns": 2,
            "permission_denials_count": 2 if denied else 0,
        }
    )
    return events


def terminal_denial_events(unmatched_count):
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "ordinary-error",
                        "name": "Read",
                        "input": {"file_path": "missing.txt"},
                    },
                    *[
                        {
                            "type": "tool_use",
                            "id": f"omitted-denial-{index}",
                            "name": "Bash",
                            "input": {"command": f"wheelhouse-search request-{index}.json"},
                        }
                        for index in range(unmatched_count)
                    ],
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "ordinary-error",
                        "is_error": True,
                        "content": "File does not exist.",
                    }
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "permission_denials_count": 1,
        },
    ]
    return events


def card_body():
    item = {
        "repo": "wheelhouse",
        "number": 1676,
        "kind": "pr-review",
        "head_sha": "a" * 40,
        "title": "Denied invocation fixture",
        "author": "contributor",
        "bucket": "review-needed",
        "comp": "pass",
        "tests": "green",
        "url": "https://github.com/example/axi/pull/114",
        "summary": "checks are green",
        "recommendation": "Review this change.",
        "priority": "med",
    }
    body = rc.render(item)["body"]
    return rc.body_with_triage_queued(body, item)


def main():
    # Exact production-shaped retained input from run 30138731020: the first
    # divergent assistant tool event has only a type marker, with no name/input.
    production_retained = [
        {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6"},
        {"type": "assistant", "message": {"content": []}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "candidate", "num_turns": 10},
    ]
    first_tool = production_retained[2]["message"]["content"][0]
    check("repro: production retained tool event loses invocation identity", set(first_tool) == {"type"})
    check("repro: production retained artifact cannot recover command shape", "command" not in json.dumps(production_retained))
    check(
        "production: retention helper ships in the signed runtime",
        "retention.py" in PACKAGED_RUNTIME_FILES,
    )

    bounded = reduce_execution(raw_events(), "triage.pr.search")
    evidence = retained_tool_denials(bounded)
    encoded = json.dumps(bounded, sort_keys=True, separators=(",", ":"))
    check("deny: retained event contains the two bounded denial records", evidence is not None and evidence["denialCount"] == 2 and len(evidence["invocations"]) == 2)
    check("deny: tool identity and command shape survive", evidence["invocations"][0]["tool"] == "Bash" and "command" in evidence["invocations"][0]["request"])
    check("deny: allowlist mismatch is diagnosable", "wheelhouse-search" in evidence["invocations"][0]["request"]["command"])
    check("deny: secret values are redacted", "ghp_abcdefghijklmnopqrstuvwxyz" not in encoded and "github_pat_abcdefghijklmnopqrstuvwxyz" not in encoded and "REDACTED_SECRET" in encoded)
    check("deny: unrelated payloads are omitted", "SECRET FILE BODY" not in encoded and "GH_TOKEN" not in encoded and "env" not in encoded)
    check("deny: evidence has deterministic bound", len(encoded.encode()) <= MAX_DENIED_EVIDENCE_BYTES)

    compact_success = reduce_execution(raw_events(denied=False), "triage.pr.search")
    success_encoded = json.dumps(compact_success, sort_keys=True, separators=(",", ":"))
    check("success: no denied evidence is retained", retained_tool_denials(compact_success) is None)
    check("success: tool input is not retained unnecessarily", "wheelhouse-search" not in success_encoded and "SECRET FILE BODY" not in success_encoded)

    inferred = retained_tool_denials(
        reduce_execution(terminal_denial_events(1), "triage.pr.search")
    )
    check(
        "deny: omitted result selects the only unmatched invocation",
        inferred is not None
        and [row["tool"] for row in inferred["invocations"]] == ["Bash"],
    )
    ambiguous = retained_tool_denials(
        reduce_execution(terminal_denial_events(2), "triage.pr.search")
    )
    check(
        "deny: ambiguous omitted results retain count without attribution",
        ambiguous is not None
        and ambiguous["denialCount"] == 1
        and ambiguous["invocations"] == [],
    )

    body = card_body()
    verdict = {
        "summary": "A bounded advisory result.",
        "product_implications": "No authority change.",
        "recommended_action": "hold",
        "recommended_reason": "The denied invocation must be retried.",
        "evidence": "target.txt: \"bounded\"",
    }
    revision = "a" * 40
    visible = rc.body_with_triage_result(
        body,
        revision,
        triage=verdict,
        primary_error_code="output.schema_invalid",
    )
    state = rc.parse_state_block(visible)
    check("card: advisory result remains consumable", state.get("triage_status") == "succeeded")
    check("card: primary failure is independently stateful", state.get("triage_primary_status") == "failed" and state.get("triage_primary_error_code") == "output.schema_invalid")
    check("card: consumption split is explicit", state.get("triage_consumption") == "advisory")
    check("card: primary failure is not green-masked", "Primary model validation failed (`output.schema_invalid`)" in visible and "not a primary validation success" in visible)

    if FAILURES:
        raise SystemExit("failed checks: " + ", ".join(FAILURES))


if __name__ == "__main__":
    main()
