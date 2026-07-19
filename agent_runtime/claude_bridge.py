"""Trusted Agent Runtime Contract bridge for the pinned Claude Action."""

from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import API_VERSION
from .contract import (
    ContractError,
    atomic_write_json,
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    load_json_regular,
    validate_contract,
    validate_schema,
    result_projection_sha256,
)
from .events import EventWriter
from .supervisor import _anchor_ok, _error, _verify_artifacts
from .task_builder import (
    claude_capabilities,
    claude_declared_outputs,
    claude_declared_tools,
    claude_isolation,
    claude_limit_enforcement,
    claude_native_structured_output,
)

ACTION_COMMIT = "fad22eb3fa582b7357fc0ea48af6645851b884fd"
ACTION_VERSION = "1.0.161"
CLAUDE_CODE_VERSION = "2.1.197"
IMMUTABLE_MODEL = "claude-sonnet-4-6"
PROTOCOL = "claude-agent-sdk-json-v1"


def _transcript(path: str) -> list[dict[str, Any]]:
    candidate = Path(path)
    if not path or candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > 8 * 1024 * 1024:
        return []
    value = load_json_regular(candidate, max_bytes=8 * 1024 * 1024)
    if not isinstance(value, list):
        raise ContractError("Claude action transcript was not an event array")
    if any(not isinstance(row, dict) for row in value):
        raise ContractError("Claude action transcript contained an invalid event")
    return value


def _spend_started(path: str) -> bool:
    candidate = Path(path)
    try:
        return bool(path) and not candidate.is_symlink() and candidate.is_file() and candidate.stat().st_size > 0
    except OSError:
        return False


def _observed_model(rows: list[dict[str, Any]]) -> str:
    models = [
        row["model"]
        for row in rows
        if row.get("type") == "system"
        and row.get("subtype") == "init"
        and isinstance(row.get("model"), str)
        and row.get("model")
    ]
    return models[0] if len(models) == 1 else ""


def _terminal_result(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    results = [row for row in rows if row.get("type") == "result"]
    if len(results) != 1 or not rows or rows[-1] is not results[0]:
        return None
    return results[0]


def _result_text(terminal: dict[str, Any] | None) -> str:
    if terminal is None or terminal.get("is_error") is not False:
        return ""
    text = terminal.get("result")
    return text.strip() if isinstance(text, str) else ""


def _delivered(action: str, terminal: dict[str, Any], delivered_file: str) -> Any:
    if claude_native_structured_output(action):
        if "structured_output" not in terminal:
            raise ContractError(
                "Claude action omitted negotiated native structured output"
            )
        return terminal["structured_output"]
    if delivered_file:
        return load_json_regular(delivered_file, max_bytes=131072)
    text = _result_text(terminal)
    if not text:
        raise ContractError("Claude action delivered no final result")
    if action.startswith("deep-review"):
        return {"text": text}
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ContractError("Claude action result was not an object")
    return value


def _raw_delivered_file(path: str, max_bytes: int) -> str:
    """Retain bounded malformed declared output as repair-only delivered data."""
    if not path:
        return ""
    candidate = Path(path)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                return ""
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return ""
        return raw.decode("utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _repair_candidate(
    action: str,
    terminal: dict[str, Any],
    delivered_file: str,
    max_bytes: int,
) -> tuple[bool, Any]:
    """Return bounded untrusted data for the separate portable repair task."""
    if (
        claude_native_structured_output(action)
        and "structured_output" in terminal
    ):
        value = terminal["structured_output"]
        try:
            if len(canonical_json_bytes(value)) <= max_bytes:
                return True, value
        except (ContractError, RecursionError, TypeError, ValueError):
            pass
    raw = _raw_delivered_file(delivered_file, max_bytes) or _result_text(terminal)
    return (bool(raw), raw)


def _usage(rows: list[dict[str, Any]], terminal: dict[str, Any] | None, duration_ms: int) -> dict[str, Any]:
    turns = 0
    token_usage: dict[str, Any] = {}
    tool_calls = 0
    for row in rows:
        if row.get("type") == "assistant" and isinstance(row.get("message"), dict):
            content = row["message"].get("content")
            if isinstance(content, list):
                tool_calls += sum(1 for item in content if isinstance(item, dict) and item.get("type") == "tool_use")
    if terminal is not None:
        value = terminal.get("num_turns")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            turns = value
        usage = terminal.get("usage")
        if isinstance(usage, dict):
            token_usage = usage

    def token(name: str) -> int | None:
        value = token_usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    return {
        "inputTokens": token("input_tokens"),
        "outputTokens": token("output_tokens"),
        "cacheReadTokens": token("cache_read_input_tokens"),
        "cacheWriteTokens": token("cache_creation_input_tokens"),
        "providerRequests": None,
        "toolCalls": tool_calls,
        "turns": turns,
        "durationMs": max(0, duration_ms),
        "quota": {"available": False, "snapshotSha256": None, "observedAt": None, "primaryUsedPercent": None, "secondaryUsedPercent": None},
        "cost": {"amount": None, "currency": None, "quality": "unavailable"},
    }


def _attempt_timing(execution_file: str, terminal: dict[str, Any] | None, max_duration_ms: int) -> tuple[str, int]:
    duration = terminal.get("duration_ms") if terminal is not None else None
    if not isinstance(duration, int) or isinstance(duration, bool) or not 0 <= duration <= max_duration_ms:
        duration = 0
    completed = time.time()
    try:
        observed = Path(execution_file).stat().st_mtime
        if 0 < observed <= completed:
            completed = observed
    except OSError:
        pass
    started = datetime.fromtimestamp(completed - duration / 1000, tz=timezone.utc)
    return started.isoformat(timespec="milliseconds").replace("+00:00", "Z"), duration


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validated_cancellation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("controller cancellation evidence was invalid")
    expected = {
        "requestStatus",
        "requestReturnCode",
        "terminalStatus",
        "terminalConclusion",
        "cancellationConfirmed",
    }
    request_status = value.get("requestStatus")
    return_code = value.get("requestReturnCode")
    terminal_status = value.get("terminalStatus")
    terminal_conclusion = value.get("terminalConclusion")
    confirmed = value.get("cancellationConfirmed")
    if (
        set(value) != expected
        or request_status not in ("not-requested", "accepted", "failed", "unavailable")
        or (return_code is not None and (not isinstance(return_code, int) or isinstance(return_code, bool) or not 0 <= return_code <= 255))
        or terminal_status not in ("", "completed")
        or terminal_conclusion not in ("", "success", "failure", "cancelled", "timed_out", "skipped", "neutral", "action_required", "startup_failure", "stale")
        or not isinstance(confirmed, bool)
        or confirmed != (terminal_status == "completed" and terminal_conclusion == "cancelled")
        or (request_status == "not-requested" and return_code is not None)
    ):
        raise ContractError("controller cancellation evidence was invalid")
    return {
        "requestStatus": request_status,
        "requestReturnCode": return_code,
        "terminalStatus": terminal_status,
        "terminalConclusion": terminal_conclusion,
        "cancellationConfirmed": confirmed,
    }


def _enforcement(path: str, task: dict[str, Any], execution_file: str, handoff_sha256: str) -> dict[str, Any] | None:
    try:
        proof = load_json_regular(path, max_bytes=65536)
    except (ContractError, OSError, RecursionError, ValueError):
        return None
    controller = proof.get("controller") if isinstance(proof, dict) else None
    transcript = Path(execution_file)
    try:
        transcript_sha = file_sha256(transcript) if execution_file and transcript.is_file() and not transcript.is_symlink() else None
    except OSError:
        return None
    expected_permissions = {"actions": "read", "contents": "read", "issues": "none"}
    action = task["metadata"]["action"]
    expected_readonly_token = "in-process" if action.endswith(".search") else "absent"
    action_metadata_sha = proof.get("actionMetadataSha256")
    action_metadata_quality = proof.get("actionMetadataQuality")
    observation_before = proof.get("preActionInputObservationSha256")
    observation_after = proof.get("postActionInputObservationSha256")
    termination_reason = controller.get("terminationReason") if isinstance(controller, dict) else None
    conclusion = controller.get("conclusion") if isinstance(controller, dict) else None
    inputs_verified = proof.get("targetInputsReadOnly") is True and observation_after == observation_before
    inputs_unavailable = proof.get("targetInputsReadOnly") is False and observation_after is None
    externally_enforced_limits = {
        name: task["spec"]["limits"][name]
        for name, quality in task["spec"]["limits"]["enforcement"].items()
        if quality == "externally-enforced"
    }
    cancellation = controller.get("cancellation") if isinstance(controller, dict) else None
    try:
        if cancellation is not None:
            _validated_cancellation(cancellation)
    except ContractError:
        return None
    if (
        not isinstance(handoff_sha256, str)
        or len(handoff_sha256) != 64
        or any(character not in "0123456789abcdef" for character in handoff_sha256)
        or proof.get("version") != 1
        or proof.get("boundary") != "separate-read-only-github-job"
        or proof.get("jobPermissions") != expected_permissions
        or proof.get("writeCapableGithubTokenAvailable") is not False
        or proof.get("fleetTokenAvailable") is not False
        or proof.get("readonlyTokenBoundary") != expected_readonly_token
        or not isinstance(proof.get("spendStarted"), bool)
        or proof.get("isolationLevel") != "github-readonly-artifact-bridge-v1"
        or proof.get("artifactHydration") != "content-addressed-bounded-verified"
        or not isinstance(observation_before, str)
        or len(observation_before) != 64
        or (conclusion == "success" and not inputs_verified)
        or (conclusion != "success" and not (inputs_verified or inputs_unavailable))
        or proof.get("declaredOutputPaths") != claude_declared_outputs(action)
        or proof.get("workspaceRepository") != "local-no-remote"
        or proof.get("declaredTools") != claude_declared_tools(action)
        or proof.get("actionSourceCommit") != ACTION_COMMIT
        or action_metadata_quality not in ("verified-action-metadata", "pinned-action-reference")
        or (action_metadata_quality == "verified-action-metadata" and (not isinstance(action_metadata_sha, str) or len(action_metadata_sha) != 64))
        or (isinstance(action_metadata_sha, str) and any(character not in "0123456789abcdef" for character in action_metadata_sha))
        or (action_metadata_quality == "pinned-action-reference" and action_metadata_sha is not None)
        or task["spec"]["capabilities"] != claude_capabilities(action, task["spec"]["output"]["schemaSha256"])
        or task["spec"]["tools"] != {"default": "deny", "parallel": False, "tools": []}
        or task["spec"]["isolation"] != claude_isolation(action)
        or task["spec"]["limits"]["enforcement"] != claude_limit_enforcement()
        or proof.get("taskSha256") != canonical_sha256(task)
        or proof.get("handoffManifestSha256") != handoff_sha256
        or proof.get("action") != task["metadata"]["action"]
        or proof.get("transcriptSha256") != transcript_sha
        or not isinstance(controller, dict)
        or controller.get("hardDeadlineMs") is not None
        or controller.get("dispatchDeadlineMs") != task["spec"]["limits"]["dispatchDeadlineMs"]
        or controller.get("childExecutionTimeoutMs") != task["spec"]["limits"]["childExecutionTimeoutMs"]
        or proof.get("childExecutionTimeoutMs") != task["spec"]["limits"]["childExecutionTimeoutMs"]
        or conclusion not in ("success", "failure", "timed_out", "cancelled")
        or termination_reason not in ("completed", "child-timeout", "parent-sigterm", "controller-failure")
        or (termination_reason == "completed" and controller.get("conclusion") not in ("success", "failure"))
        or (termination_reason == "child-timeout" and controller.get("conclusion") != "timed_out")
        or (termination_reason == "parent-sigterm" and controller.get("conclusion") != "cancelled")
        or (termination_reason == "controller-failure" and controller.get("conclusion") != "failure")
        or controller.get("enforcedLimits") != externally_enforced_limits
        or controller.get("expectedCommitSha") != task["metadata"]["wheelhouseRevision"]
        or controller.get("observedCommitSha") != task["metadata"]["wheelhouseRevision"]
        or not isinstance(controller.get("dispatchRef"), str)
        or not controller.get("dispatchRef")
        or not isinstance(controller.get("correlationId"), str)
        or len(controller.get("correlationId")) != 32
        or any(character not in "0123456789abcdef" for character in controller.get("correlationId"))
        or not isinstance(controller.get("parentRunId"), str)
        or not isinstance(controller.get("parentRunAttempt"), str)
        or not isinstance(controller.get("modelRunId"), str)
    ):
        return None
    return proof


def _write_parent_failure_result(
    task_path: str,
    bundle_dir: str,
    evidence: dict[str, Any],
    proof_extension: dict[str, Any],
    error_code: str,
    message: str,
    adapter_code: str,
    result_path: str,
    events_path: str,
    cancellation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task = load_json_regular(task_path, max_bytes=16 * 1024 * 1024)
    validate_contract(task, "AgentTask")
    _verify_artifacts(task, Path(bundle_dir).resolve())
    candidate = task["spec"]["selection"]["candidates"][0]
    selection = {
        "profile": task["spec"]["selection"]["profile"],
        "candidateIndex": 0,
        "harness": candidate["harness"],
        "adapter": candidate["adapter"],
        "adapterVersion": "1.0.0",
        "adapterDigest": file_sha256(Path(__file__)),
        "harnessVersion": None,
        "harnessDigest": None,
        "harnessProvenanceQuality": "unavailable",
        "harnessSourceCommit": None,
        "harnessMetadataSha256": None,
        "protocol": PROTOCOL,
        "protocolSchemaSha256": canonical_sha256({"protocol": PROTOCOL, "systemInitModel": True}),
        "provider": candidate["provider"],
        "actualProvider": "",
        "authProfile": candidate["authProfile"],
        "authMechanism": candidate["authMechanism"],
        "expectedWorkspaceIdSha256": canonical_sha256(candidate["expectedWorkspaceId"]) if candidate.get("expectedWorkspaceId") else None,
        "requestedModel": candidate["model"],
        "actualModel": "",
        "requestedEffort": candidate["effort"],
        "actualEffort": "",
        "costClass": candidate["costClass"],
        "dataBoundary": candidate["dataBoundary"],
        "fallbackUsed": False,
        "fallbackReason": None,
    }
    proof = {
        "contractMajor": 1,
        "executionProfile": task["spec"]["selection"]["profile"],
        "isolationLevel": "github-readonly-artifact-bridge-v1",
        "sandboxImplementation": "github-readonly-artifact-bridge-v1",
        "credentialIsolation": "action-input+subprocess-env-scrub",
        "structuredOutputMechanism": "unavailable-after-controller-failure",
        "capabilitySnapshotSha256": canonical_sha256(task["spec"]["capabilities"]),
        "negotiationSha256": canonical_sha256({"status": "failed-with-possible-spend", "code": error_code}),
        "policySha256": canonical_sha256({name: task["spec"][name] for name in ("isolation", "limits", "retention", "retry")}),
        "compiledPromptSha256": task["spec"]["prompt"]["segments"][0]["sha256"],
        "inputManifestSha256": canonical_sha256(task["spec"]["inputs"]),
        "outputSchemaSha256": task["spec"]["output"]["schemaSha256"],
        "sandboxPolicySha256": canonical_sha256(evidence),
        "limitEnforcement": task["spec"]["limits"]["enforcement"],
        "limitEnforcementSha256": canonical_sha256(task["spec"]["limits"]["enforcement"]),
    }
    proof.update(proof_extension)
    result = {
        "apiVersion": API_VERSION,
        "kind": "AgentResult",
        "executionId": task["metadata"]["executionId"],
        "requestSha256": canonical_sha256(task),
        "status": "failed",
        "selection": selection,
        "proof": proof,
        "error": _error(
            error_code,
            message,
            phase="validating",
            spend_started=True,
            adapter_code=adapter_code,
        ),
        "usage": {
            "inputTokens": None,
            "outputTokens": None,
            "cacheReadTokens": None,
            "cacheWriteTokens": None,
            "providerRequests": None,
            "toolCalls": None,
            "turns": None,
            "durationMs": None,
            "quota": {"available": False, "snapshotSha256": None, "observedAt": None, "primaryUsedPercent": None, "secondaryUsedPercent": None},
            "cost": {"amount": None, "currency": None, "quality": "unavailable"},
        },
        "artifacts": [],
        "startedAt": _now(),
        "completedAt": _now(),
    }
    with EventWriter(events_path, result["executionId"], task["spec"]["limits"]["maxEventBytes"]) as events:
        events.emit("execution.accepted", {"requestSha256": result["requestSha256"]})
        events.emit("selection.resolved", {"candidateIndex": 0, "adapter": candidate["adapter"], "harness": candidate["harness"], "provider": candidate["provider"], "model": candidate["model"], "effort": candidate["effort"], "fallback": "none"})
        if cancellation is not None:
            events.emit("adapter.controller.lifecycle", _validated_cancellation(cancellation))
        events.emit("validation.completed", {"status": "failed", "errorCode": error_code, "spendStarted": True})
        events.emit("execution.completed", {"status": "failed", "resultSha256": result_projection_sha256(result), "projection": "agent-result-without-artifacts/v1"})
    event_file = Path(events_path)
    result["artifacts"].append({"role": "normalized-events", "sha256": file_sha256(event_file), "mediaType": "application/x-ndjson", "bytes": event_file.stat().st_size, "retentionDays": task["spec"]["retention"]["normalizedEventsDays"], "redaction": "wheelhouse-agent/v1"})
    validate_contract(result, "AgentResult")
    atomic_write_json(result_path, result)
    return result


def write_revision_mismatch_result(
    task_path: str,
    bundle_dir: str,
    expected_sha: str,
    observed_sha: str,
    run_id: str,
    dispatch_ref: str,
    correlation_id: str,
    cancellation: dict[str, Any],
    result_path: str,
    events_path: str,
) -> dict[str, Any]:
    task = load_json_regular(task_path, max_bytes=16 * 1024 * 1024)
    if (
        expected_sha != task.get("metadata", {}).get("wheelhouseRevision")
        or len(observed_sha) != 40
        or any(character not in "0123456789abcdef" for character in observed_sha)
        or observed_sha == expected_sha
        or not run_id
        or not dispatch_ref
        or len(correlation_id) != 32
        or any(character not in "0123456789abcdef" for character in correlation_id)
    ):
        raise ContractError("trusted revision mismatch metadata was invalid")
    cancellation = _validated_cancellation(cancellation)
    binding = {
        "quality": "trusted-parent-run-metadata",
        "status": "mismatched",
        "spendEvidenceQuality": "conservative-possible-spend",
        "cancellationConfirmed": cancellation["cancellationConfirmed"],
        "cancellationError": None if cancellation["cancellationConfirmed"] else "lifecycle.cancel_unconfirmed",
        "expectedCommitSha": expected_sha,
        "observedCommitSha": observed_sha,
        "modelRunId": run_id,
        "dispatchRef": dispatch_ref,
        "correlationId": correlation_id,
    }
    return _write_parent_failure_result(
        task_path,
        bundle_dir,
        binding,
        {"revisionBinding": binding},
        "source.revision_mismatch",
        "Wheelhouse model workflow revision did not match the trusted caller.",
        "revision-mismatch",
        result_path,
        events_path,
        cancellation,
    )


def write_controller_failure_result(
    task_path: str,
    bundle_dir: str,
    run_id: str,
    dispatch_ref: str,
    correlation_id: str,
    cancellation: dict[str, Any],
    result_path: str,
    events_path: str,
    *,
    reason: str = "malformed-run-metadata",
) -> dict[str, Any]:
    cancellation = _validated_cancellation(cancellation)
    if reason not in ("malformed-run-metadata", "child-artifact-unavailable"):
        raise ContractError("controller failure reason was invalid")
    evidence = {
        "quality": "trusted-parent-controller",
        "reason": reason,
        "spendEvidenceQuality": "conservative-possible-spend",
        "cancellation": cancellation,
        "modelRunId": run_id or None,
        "dispatchRef": dispatch_ref,
        "correlationId": correlation_id,
    }
    return _write_parent_failure_result(
        task_path,
        bundle_dir,
        evidence,
        {},
        "harness.protocol",
        "Claude model workflow returned malformed run metadata." if reason == "malformed-run-metadata" else "Claude model child ended without a trusted final or checkpoint artifact.",
        reason,
        result_path,
        events_path,
        cancellation,
    )


def bridge(task_path: str, bundle_dir: str, execution_file: str, delivered_file: str, enforcement_file: str, handoff_sha256: str, result_path: str, events_path: str) -> dict[str, Any]:
    task = load_json_regular(task_path, max_bytes=16 * 1024 * 1024)
    validate_contract(task, "AgentTask")
    bundle = Path(bundle_dir).resolve()
    _verify_artifacts(task, bundle)
    candidate = task["spec"]["selection"]["candidates"][0]
    if candidate["adapter"] != "claude-action-compat" or candidate["provider"] != "anthropic" or candidate["model"] != IMMUTABLE_MODEL or candidate["allowModelAlias"]:
        raise ContractError("Claude production selection violates the pinned adapter contract")
    transcript_error = False
    try:
        rows = _transcript(execution_file)
    except (ContractError, OSError, RecursionError, UnicodeError, ValueError):
        rows = []
        transcript_error = True
    actual_model = _observed_model(rows)
    terminal = _terminal_result(rows)
    enforcement = _enforcement(enforcement_file, task, execution_file, handoff_sha256)
    spend_started = _spend_started(execution_file) or bool(enforcement and enforcement["spendStarted"])
    error = None
    delivered = None
    final = None
    if transcript_error:
        error = _error("harness.protocol", "Claude action execution data failed bounded protocol validation.", spend_started=spend_started)
    elif enforcement is None:
        error = _error("sandbox.violation", "Claude action job enforcement proof was missing or invalid.", spend_started=spend_started)
    elif enforcement["controller"]["terminationReason"] == "child-timeout":
        error = _error("lifecycle.timeout", "Claude action exceeded the externally enforced child execution timeout.", spend_started=spend_started)
    elif enforcement["controller"]["terminationReason"] == "parent-sigterm":
        error = _error("lifecycle.cancelled", "Claude action was cancelled with its trusted parent.", spend_started=spend_started)
    elif enforcement["controller"]["terminationReason"] == "controller-failure":
        error = _error("harness.protocol", "Claude model controller rejected malformed workflow metadata.", spend_started=spend_started)
    elif not rows:
        error = _error("output.missing", "Claude action delivered no execution data.", spend_started=spend_started)
    elif terminal is None:
        error = _error("harness.protocol", "Claude action execution did not contain exactly one terminal result event.", spend_started=spend_started)
    elif actual_model != candidate["model"]:
        error = _error("model.mismatch", "Observed Claude model did not match the immutable requested model.", spend_started=spend_started)
    elif terminal.get("is_error") is not False or terminal.get("subtype") != "success":
        error = _error("harness.crash", "Claude action reported an unsuccessful execution.", spend_started=spend_started)
    elif not isinstance(terminal.get("duration_ms"), int) or isinstance(terminal.get("duration_ms"), bool) or not 0 <= terminal["duration_ms"] <= task["spec"]["limits"]["childExecutionTimeoutMs"]:
        error = _error("harness.protocol", "Claude action terminal result omitted valid attempt timing.", spend_started=spend_started)
    else:
        try:
            value = _delivered(task["metadata"]["action"], terminal, delivered_file)
            encoded = canonical_json_bytes(value)
            if len(encoded) > task["spec"]["limits"]["maxFinalBytes"]:
                raise ContractError("Claude action result exceeded its byte bound")
            delivered = {"value": value, "valueSha256": canonical_sha256(value), "bytes": len(encoded)}
            schema = load_json_regular(bundle / task["spec"]["output"]["schemaArtifact"], max_bytes=65536)
            validate_schema(value, schema)
            if not _anchor_ok(value, task, bundle):
                error = _error("output.evidence_invalid", "Delivered evidence did not anchor to the immutable target input.", spend_started=True)
            else:
                validation = []
                if (
                    claude_native_structured_output(task["metadata"]["action"])
                ):
                    validation.append({"name": "native-schema", "status": "passed"})
                validation.extend(
                    [
                        {"name": "json-schema", "status": "passed"},
                        {"name": task["spec"]["output"]["evidencePolicy"], "status": "passed" if task["spec"]["output"]["evidencePolicy"] != "none" else "not-applicable"},
                        {"name": "observed-provenance", "status": "passed"},
                    ]
                )
                final = {
                    "schemaId": task["spec"]["output"]["schemaId"],
                    "value": value,
                    "valueSha256": delivered["valueSha256"],
                    "bytes": delivered["bytes"],
                    "validation": validation,
                }
        except (ContractError, json.JSONDecodeError, OSError, RecursionError, UnicodeError, ValueError):
            # Native NL failures retain the bounded structured value when one
            # exists, then the portable decision.json carrier, then terminal
            # prose. The candidate remains failed and can never become `final`
            # until the separate repair task passes the same trusted validation.
            has_candidate, repair_value = _repair_candidate(
                task["metadata"]["action"],
                terminal,
                delivered_file,
                task["spec"]["limits"]["maxFinalBytes"],
            )
            if has_candidate:
                encoded = canonical_json_bytes(repair_value)
                if len(encoded) <= task["spec"]["limits"]["maxFinalBytes"]:
                    delivered = {"value": repair_value, "valueSha256": canonical_sha256(repair_value), "bytes": len(encoded)}
            error = _error("output.schema_invalid" if rows or delivered_file else "output.missing", "Claude action output failed trusted contract validation.", spend_started=spend_started)
    started_at, duration = _attempt_timing(execution_file, terminal, task["spec"]["limits"]["childExecutionTimeoutMs"])
    status = "succeeded" if final is not None else ("cancelled" if error and error["code"] == "lifecycle.cancelled" else "failed")
    selection = {
        "profile": task["spec"]["selection"]["profile"],
        "candidateIndex": 0,
        "harness": candidate["harness"],
        "adapter": candidate["adapter"],
        "adapterVersion": "1.0.0",
        "adapterDigest": file_sha256(Path(__file__)),
        "harnessVersion": None,
        "harnessDigest": None,
        "harnessProvenanceQuality": enforcement["actionMetadataQuality"] if enforcement else "unavailable",
        "harnessSourceCommit": enforcement["actionSourceCommit"] if enforcement else None,
        "harnessMetadataSha256": enforcement["actionMetadataSha256"] if enforcement else None,
        "protocol": PROTOCOL,
        "protocolSchemaSha256": canonical_sha256({"protocol": PROTOCOL, "systemInitModel": True}),
        "provider": candidate["provider"],
        # The action transcript directly reports the model, not provider identity.
        "actualProvider": "",
        "authProfile": candidate["authProfile"],
        "authMechanism": candidate["authMechanism"],
        "expectedWorkspaceIdSha256": canonical_sha256(candidate["expectedWorkspaceId"]) if candidate.get("expectedWorkspaceId") else None,
        "requestedModel": candidate["model"],
        "actualModel": actual_model,
        "requestedEffort": candidate["effort"],
        # The action transcript does not directly report an effort setting.
        "actualEffort": "",
        "costClass": candidate["costClass"],
        "dataBoundary": candidate["dataBoundary"],
        "fallbackUsed": False,
        "fallbackReason": None,
    }
    result = {
        "apiVersion": API_VERSION,
        "kind": "AgentResult",
        "executionId": task["metadata"]["executionId"],
        "requestSha256": canonical_sha256(task),
        "status": status,
        "selection": selection,
        "proof": {
            "contractMajor": 1,
            "executionProfile": task["spec"]["selection"]["profile"],
            "isolationLevel": "github-readonly-artifact-bridge-v1",
            "sandboxImplementation": "github-readonly-artifact-bridge-v1",
            "credentialIsolation": "action-input+subprocess-env-scrub",
            "structuredOutputMechanism": "native-schema"
            if claude_native_structured_output(task["metadata"]["action"])
            else "trusted-post-action-bridge",
            "capabilitySnapshotSha256": canonical_sha256(task["spec"]["capabilities"]),
            "negotiationSha256": canonical_sha256({"candidate": candidate, "tools": task["spec"]["tools"], "limitEnforcement": task["spec"]["limits"]["enforcement"], "fallback": "none"}),
            "policySha256": canonical_sha256({name: task["spec"][name] for name in ("isolation", "limits", "retention", "retry")}),
            "compiledPromptSha256": task["spec"]["prompt"]["segments"][0]["sha256"],
            "inputManifestSha256": canonical_sha256(task["spec"]["inputs"]),
            "outputSchemaSha256": task["spec"]["output"]["schemaSha256"],
            "sandboxPolicySha256": canonical_sha256(enforcement or {"status": "unverified"}),
            "limitEnforcement": task["spec"]["limits"]["enforcement"],
            "limitEnforcementSha256": canonical_sha256(task["spec"]["limits"]["enforcement"]),
        },
        "usage": _usage(rows, terminal, duration),
        "artifacts": [],
        "startedAt": started_at,
        "completedAt": _now(),
    }
    if delivered is not None:
        result["delivered"] = delivered
    if final is not None:
        result["final"] = final
    else:
        result["error"] = error or _error("output.missing", "Claude action delivered no result.", spend_started=spend_started)
    with EventWriter(events_path, result["executionId"], task["spec"]["limits"]["maxEventBytes"]) as events:
        events.emit("execution.accepted", {"requestSha256": result["requestSha256"]})
        events.emit("selection.resolved", {"candidateIndex": 0, "adapter": candidate["adapter"], "harness": candidate["harness"], "provider": candidate["provider"], "model": candidate["model"], "effort": candidate["effort"], "fallback": "none"})
        events.emit("capabilities.negotiated", {"proofSha256": result["proof"]["negotiationSha256"], "exactTools": [row["name"] for row in task["spec"]["tools"]["tools"]]})
        events.emit("validation.completed", {"status": "passed" if final else "failed", "errorCode": None if final else result["error"]["code"], "spendStarted": spend_started})
        events.emit("execution.completed", {"status": status, "resultSha256": result_projection_sha256(result), "projection": "agent-result-without-artifacts/v1"})
    event_file = Path(events_path)
    result["artifacts"].append({"role": "normalized-events", "sha256": file_sha256(event_file), "mediaType": "application/x-ndjson", "bytes": event_file.stat().st_size, "retentionDays": task["spec"]["retention"]["normalizedEventsDays"], "redaction": "wheelhouse-agent/v1"})
    validate_contract(result, "AgentResult")
    atomic_write_json(result_path, result)
    return result
