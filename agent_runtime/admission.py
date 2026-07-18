"""Action-specific event identity, claims, and content-free stage evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from typing import Any

from .contract import ContractError, canonical_json_bytes, load_json_regular, validate_contract

ACTION = re.compile(r"^(?:triage\.(?:pr|issue)\.(?:local|search)|triage\.schema-repair|deep-review\.(?:local|search)|nl-decision\.(?:local|search|schema-repair))$")
REVISION = re.compile(r"^[A-Za-z0-9:._+-]{1,160}$")
EVENT_ID = re.compile(r"^[A-Za-z0-9:._+-]{1,200}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
EXECUTION_ID = re.compile(r"^[0-9a-f-]{36}$")

STAGES = {
    "admitted",
    "denied",
    "stale",
    "task-built",
    "task-failed",
    "handoff-packed",
    "child-dispatched",
    "child-correlated",
    "hydrated",
    "checkpoint",
    "provider-started",
    "output-captured",
    "result-normalized",
    "consumer-committed",
    "consumer-rejected",
}
STATUSES = {"ok", "failed", "skipped", "possible-spend"}
CODE = re.compile(r"^[a-z][a-z0-9_.-]{2,80}$")


def normalized_event_identity(
    *,
    action: str,
    owner: str,
    repo: str,
    number: int,
    card_issue: int,
    revision: str,
    event_id: str = "",
) -> dict[str, Any]:
    if not ACTION.fullmatch(action):
        raise ContractError("agent event action is invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo):
        raise ContractError("agent event target is invalid")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ContractError("agent event target number is invalid")
    if isinstance(card_issue, bool) or not isinstance(card_issue, int) or card_issue < 1:
        raise ContractError("agent event card number is invalid")
    if not REVISION.fullmatch(revision):
        raise ContractError("agent event revision is invalid")
    requires_event = action.startswith("nl-decision.") or action.startswith("deep-review.")
    if requires_event != bool(event_id):
        raise ContractError("agent event trigger identity is invalid")
    if event_id and not EVENT_ID.fullmatch(event_id):
        raise ContractError("agent event trigger identity is invalid")
    return {
        "version": 1,
        "action": action,
        "target": {"owner": owner, "repo": repo, "number": number},
        "cardIssue": card_issue,
        "revision": revision,
        "eventId": event_id or None,
    }


def event_key_sha256(identity: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def event_claim_marker(event_key: str) -> str:
    if not DIGEST.fullmatch(event_key):
        raise ContractError("agent event key hash is invalid")
    return "<!-- wheelhouse-agent-claim:v1:%s -->" % event_key


def stage_record(
    *,
    action: str,
    source_sha: str,
    event_key: str,
    stage: str,
    status: str,
    code: str,
    execution_id: str = "",
    deadline_ms: int | None = None,
) -> dict[str, Any]:
    if not ACTION.fullmatch(action):
        raise ContractError("agent stage action is invalid")
    if not SHA.fullmatch(source_sha):
        raise ContractError("agent stage source SHA is invalid")
    if not DIGEST.fullmatch(event_key):
        raise ContractError("agent stage event key is invalid")
    if stage not in STAGES or status not in STATUSES or not CODE.fullmatch(code):
        raise ContractError("agent stage classification is invalid")
    if execution_id and not EXECUTION_ID.fullmatch(execution_id):
        raise ContractError("agent stage execution id is invalid")
    if deadline_ms is not None and (
        isinstance(deadline_ms, bool)
        or not isinstance(deadline_ms, int)
        or not 1_000 <= deadline_ms <= 3_600_000
    ):
        raise ContractError("agent stage deadline is invalid")
    return {
        "version": 1,
        "action": action,
        "sourceSha": source_sha,
        "eventKeySha256": event_key,
        "executionId": execution_id or None,
        "stage": stage,
        "status": status,
        "code": code,
        "deadlineMs": deadline_ms,
    }


def stage_line(record: dict[str, Any]) -> str:
    return "wheelhouse-agent-stage " + json.dumps(record, sort_keys=True, separators=(",", ":"))


def stage_from_task(
    task: dict[str, Any],
    *,
    stage: str,
    status: str,
    code: str,
    deadline_ms: int | None = None,
) -> dict[str, Any]:
    validate_contract(task, "AgentTask")
    return stage_record(
        action=task["metadata"]["action"],
        source_sha=task["metadata"]["wheelhouseRevision"],
        event_key=task["metadata"]["idempotencyKey"],
        execution_id=task["metadata"]["executionId"],
        stage=stage,
        status=status,
        code=code,
        deadline_ms=deadline_ms,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--code", required=True)
    parser.add_argument("--deadline-ms", type=int)
    args = parser.parse_args()
    task = load_json_regular(args.task)
    print(
        stage_line(
            stage_from_task(
                task,
                stage=args.stage,
                status=args.status,
                code=args.code,
                deadline_ms=args.deadline_ms,
            )
        )
    )


if __name__ == "__main__":
    main()
