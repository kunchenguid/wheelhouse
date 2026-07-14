"""Trusted Agent Runtime v1 supervisor."""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import API_VERSION
from .adapters import ADAPTERS
from .brokers import ProviderProxy, SearchBroker
from .capabilities import CapabilityError, negotiate
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
from .redaction import contains_secret, sanitize_message
from .sandbox import SandboxError, build_command, host_proof


class RuntimeFailure(Exception):
    def __init__(self, code: str, phase: str, message: str, spend_started: bool = False, adapter_code: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.message = message
        self.spend_started = spend_started
        self.adapter_code = adapter_code


FALLBACK_ELIGIBLE = {
    "provider.quota_exhausted",
    "provider.rate_limited",
    "provider.overloaded",
    "provider.unavailable",
    "transport.connection",
    "transport.stream_interrupted",
}
RETRYABLE = FALLBACK_ELIGIBLE


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _phase_for(code: str) -> str:
    if code.startswith("contract") or code.startswith("input"):
        return "validating"
    if code.startswith("config") or code.startswith("selection"):
        return "selecting"
    if code.startswith("auth") or code.startswith("capability"):
        return "probing"
    if code.startswith("sandbox"):
        return "sandboxing"
    if code.startswith("output") or code in ("model.mismatch", "effort.mismatch"):
        return "validating-output"
    if code.startswith("lifecycle"):
        return "cancelling"
    return "running"


def _error(code: str, message: str, phase: str = "", spend_started: bool = False, adapter_code: str | None = None) -> dict[str, Any]:
    return {
        "code": code,
        "phase": phase or _phase_for(code),
        "message": sanitize_message(message),
        "retryable": code in RETRYABLE,
        "fallbackEligible": code in FALLBACK_ELIGIBLE,
        "spendStarted": bool(spend_started),
        "safeToResume": False,
        "adapterCode": sanitize_message(adapter_code, fallback="", max_chars=120) or None,
        "httpStatus": None,
        "detailsArtifact": None,
    }


def _verify_artifacts(task: dict[str, Any], bundle: Path) -> None:
    references = [task["spec"]["prompt"]["userArtifact"], task["spec"]["output"]["schemaArtifact"]]
    references.extend(item["artifact"] for item in task["spec"]["inputs"])
    for reference in references:
        if reference.startswith("/") or ".." in Path(reference).parts:
            raise RuntimeFailure("contract.invalid", "validating", "Artifact path escaped its content-addressed bundle.")
        path = bundle / reference
        try:
            info = path.lstat()
        except OSError as error:
            raise RuntimeFailure("contract.invalid", "validating", "Required content-addressed artifact is missing.") from error
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeFailure("contract.invalid", "validating", "Artifact symlinks are forbidden.")
        expected = reference.rsplit("/", 1)[-1]
        if path.is_file():
            if file_sha256(path) != expected:
                raise RuntimeFailure("contract.invalid", "validating", "Artifact digest does not match its reference.")
        elif path.is_dir():
            item = next((row for row in task["spec"]["inputs"] if row["artifact"] == reference), None)
            if not item or item["sha256"] != expected:
                raise RuntimeFailure("contract.invalid", "validating", "Directory artifact manifest is invalid.")
            manifest = []
            total = 0
            for base, dirs, files in os.walk(path, followlinks=False):
                dirs[:] = sorted(dirs)
                for name in dirs:
                    child = Path(base) / name
                    if child.is_symlink():
                        raise RuntimeFailure("contract.invalid", "validating", "Directory artifact contains a symlink.")
                for name in sorted(files):
                    child = Path(base) / name
                    child_info = child.lstat()
                    if stat.S_ISLNK(child_info.st_mode) or not stat.S_ISREG(child_info.st_mode):
                        raise RuntimeFailure("contract.invalid", "validating", "Directory artifact contains a non-regular file.")
                    total += child_info.st_size
                    manifest.append({"path": str(child.relative_to(path)), "bytes": child_info.st_size, "sha256": file_sha256(child)})
            if total != item["bytes"] or len(manifest) != item["git"]["fileCount"] or canonical_sha256(manifest) != expected:
                raise RuntimeFailure("contract.invalid", "validating", "Directory artifact changed after task construction.")
        else:
            raise RuntimeFailure("contract.invalid", "validating", "Artifact must be a regular file or directory.")
    for item in task["spec"]["inputs"]:
        if item["bytes"] > item["maxBytes"]:
            raise RuntimeFailure("input.too_large", "validating", "Input artifact exceeds its declared byte bound.")


def _materialize_fake_work(task: dict[str, Any], bundle: Path) -> None:
    work = bundle / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(mode=0o700)
    for item in task["spec"]["inputs"]:
        source = bundle / item["artifact"]
        destination = work / item["logicalPath"]
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copyfile(source, destination)


def _adapter_digest(adapter_id: str) -> str:
    root = Path(__file__).resolve().parent
    files = [root / "adapters" / "base.py", root / "adapters" / ("codex.py" if adapter_id == "codex-app-server" else "fake.py"), root / "worker.py"]
    return canonical_sha256({path.name: file_sha256(path) for path in files})


def _selection(task: dict[str, Any], probe: Any, worker: dict[str, Any] | None = None) -> dict[str, Any]:
    candidate = task["spec"]["selection"]["candidates"][0]
    descriptor = probe.descriptor.value
    worker = worker or {}
    return {
        "candidateIndex": 0,
        "harness": candidate["harness"],
        "adapter": candidate["adapter"],
        "adapterVersion": descriptor["adapterVersion"],
        "adapterDigest": _adapter_digest(candidate["adapter"]),
        "harnessVersion": descriptor["harnessVersion"],
        "harnessDigest": descriptor["harnessDigest"],
        "harnessProvenanceQuality": "test-fixture" if candidate["adapter"] == "fake" else "verified-executable",
        "harnessSourceCommit": None,
        "harnessMetadataSha256": None,
        "protocol": descriptor["protocol"],
        "protocolSchemaSha256": descriptor["protocolSchemaSha256"],
        "provider": candidate["provider"],
        "actualProvider": str(worker.get("actualProvider") or ""),
        "authProfile": candidate["authProfile"],
        "authMechanism": candidate["authMechanism"],
        "expectedWorkspaceIdSha256": canonical_sha256(candidate["expectedWorkspaceId"]) if candidate.get("expectedWorkspaceId") else None,
        "requestedModel": candidate["model"],
        "actualModel": str(worker.get("actualModel") or ""),
        "requestedEffort": candidate["effort"],
        "actualEffort": str(worker.get("actualEffort") or ""),
        "costClass": candidate["costClass"],
        "dataBoundary": candidate["dataBoundary"],
        "fallbackUsed": False,
        "fallbackReason": None,
    }


def _usage(worker: dict[str, Any], duration_ms: int) -> dict[str, Any]:
    usage = worker.get("usage") if isinstance(worker.get("usage"), dict) else {}
    cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {"amount": None, "currency": None, "quality": "unavailable"}
    quota = usage.get("quota") if isinstance(usage.get("quota"), dict) else {}
    snapshot = quota.get("snapshotSha256")
    quota_available = quota.get("available") is True and isinstance(snapshot, str) and len(snapshot) == 64 and all(character in "0123456789abcdef" for character in snapshot)

    def count(name: str) -> int | None:
        value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def count_or_zero(name: str) -> int:
        value = count(name)
        return value if value is not None else 0

    def percent(name: str) -> int | None:
        value = quota.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100 else None

    amount = cost.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
        amount = None
    return {
        "inputTokens": count("inputTokens"),
        "outputTokens": count("outputTokens"),
        "cacheReadTokens": count("cacheReadTokens"),
        "cacheWriteTokens": count("cacheWriteTokens"),
        "providerRequests": count("providerRequests"),
        "toolCalls": count_or_zero("toolCalls"),
        "turns": count_or_zero("turns"),
        "durationMs": max(0, duration_ms),
        "quota": {
            "available": quota_available,
            "snapshotSha256": snapshot if quota_available else None,
            "observedAt": _now() if quota_available else None,
            "primaryUsedPercent": percent("primaryUsedPercent") if quota_available else None,
            "secondaryUsedPercent": percent("secondaryUsedPercent") if quota_available else None,
        },
        "cost": {"amount": amount, "currency": cost.get("currency") if isinstance(cost.get("currency"), str) else None, "quality": cost.get("quality") if cost.get("quality") in ("actual", "estimated", "unavailable") else "unavailable"},
    }


def _proof(task: dict[str, Any], descriptor: dict[str, Any], negotiation: dict[str, Any], host: dict[str, Any]) -> dict[str, Any]:
    return {
        "contractMajor": 1,
        "isolationLevel": "sandboxed-adapter-worker-v1",
        "capabilitySnapshotSha256": canonical_sha256(descriptor),
        "negotiationSha256": canonical_sha256(negotiation),
        "policySha256": canonical_sha256({"isolation": task["spec"]["isolation"], "limits": task["spec"]["limits"], "retention": task["spec"]["retention"], "retry": task["spec"]["retry"]}),
        "compiledPromptSha256": task["spec"]["prompt"]["segments"][0]["sha256"],
        "inputManifestSha256": canonical_sha256(task["spec"]["inputs"]),
        "outputSchemaSha256": task["spec"]["output"]["schemaSha256"],
        "sandboxPolicySha256": canonical_sha256(host),
    }


def _read_worker_events(path: Path, events: EventWriter) -> None:
    def warn(code: str, message: str) -> None:
        try:
            events.warning(code, message)
        except (OSError, ValueError):
            pass

    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
            return
        allowed = {
            "capabilities.probed",
            "model.request.started",
            "message.delta",
            "message.completed",
            "tool.started",
            "tool.completed",
            "usage.updated",
            "warning",
            "cancellation.requested",
            "adapter.codex.turn.started",
            "adapter.codex.turn.completed",
            "adapter.fake.scripted",
        }
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if events.written >= events.max_bytes - 16384:
                    return
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    warn("adapter-events-invalid", "Adapter event record was malformed.")
                    continue
                event_type = row.get("type") if isinstance(row, dict) else ""
                if isinstance(event_type, str) and (event_type in allowed or event_type.startswith("adapter.")) and isinstance(row.get("data"), dict):
                    try:
                        events.emit(event_type, row["data"])
                    except (OSError, ValueError):
                        return
    except (OSError, UnicodeError):
        warn("adapter-events-unavailable", "Adapter event diagnostics were unavailable.")


def _anchor_ok(value: Any, task: dict[str, Any], bundle: Path) -> bool:
    if task["spec"]["output"]["evidencePolicy"] != "target-anchor/v1":
        return True
    if not isinstance(value, dict) or not isinstance(value.get("evidence"), str):
        return False
    target = next((row for row in task["spec"]["inputs"] if row["id"] == "target"), None)
    if not target:
        return False
    try:
        text = (bundle / target["artifact"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    import re
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    for quote in re.findall(r'"([^"\n]{1,240})"', value["evidence"]):
        needle = re.sub(r"\s+", " ", quote).strip().lower()
        if len(needle) >= 12 and needle in normalized:
            return True
    return False


def _validate_worker(task: dict[str, Any], bundle: Path, worker: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    candidate = task["spec"]["selection"]["candidates"][0]
    delivered_value = worker.get("final") if "final" in worker else worker.get("delivered")
    delivered = None
    secret_match = False
    if delivered_value is not None:
        try:
            encoded = canonical_json_bytes(delivered_value)
            retained_value = delivered_value
        except ContractError:
            try:
                raw = json.dumps(delivered_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError):
                raw = ""
            retained_value = raw
            encoded = canonical_json_bytes(raw)
        secret_match = contains_secret(encoded.decode("utf-8", "replace"))
        if not secret_match and len(encoded) <= task["spec"]["limits"]["maxFinalBytes"]:
            delivered = {"value": retained_value, "valueSha256": canonical_sha256(retained_value), "bytes": len(encoded)}
    if secret_match:
        return None, None, _error("sandbox.violation", "Secret scanner rejected the delivered result.", spend_started=bool(worker.get("spendStarted")))
    usage = _usage(worker, 0)
    limits = task["spec"]["limits"]
    if (
        usage["turns"] > limits["maxTurns"]
        or (usage["providerRequests"] is not None and usage["providerRequests"] > limits["maxProviderRequests"])
        or (usage["inputTokens"] is not None and usage["inputTokens"] > limits["maxInputTokens"])
        or (usage["outputTokens"] is not None and usage["outputTokens"] > limits["maxOutputTokens"])
    ):
        return None, delivered, _error("context.exceeded", "Sandboxed adapter worker exceeded a task usage limit.", spend_started=bool(worker.get("spendStarted")))
    if worker.get("status") != "succeeded":
        error = worker.get("error") or {}
        return None, delivered, _error(str(error.get("code") or "internal.error"), str(error.get("message") or "Sandboxed adapter worker failed."), spend_started=bool(worker.get("spendStarted")), adapter_code=error.get("adapterCode"))
    if worker.get("actualModel") != candidate["model"] or worker.get("actualProvider") != candidate["provider"]:
        return None, delivered, _error("model.mismatch", "Observed model or provider did not match the selected candidate.", spend_started=True)
    if worker.get("actualEffort") != candidate["effort"]:
        return None, delivered, _error("effort.mismatch", "Observed effort did not match the selected candidate.", spend_started=True)
    if delivered is None:
        return None, None, _error("output.missing", "Adapter completed without a bounded final result.", spend_started=True)
    schema = load_json_regular(bundle / task["spec"]["output"]["schemaArtifact"], max_bytes=65536)
    try:
        validate_schema(delivered_value, schema)
    except ContractError:
        return None, delivered, _error("output.schema_invalid", "Delivered result failed the trusted output schema.", spend_started=True)
    if not _anchor_ok(delivered_value, task, bundle):
        return None, delivered, _error("output.evidence_invalid", "Delivered evidence did not anchor to the immutable target input.", spend_started=True)
    final = {
        "schemaId": task["spec"]["output"]["schemaId"],
        "value": delivered_value,
        "valueSha256": delivered["valueSha256"],
        "bytes": delivered["bytes"],
        "validation": [
            {"name": "json-schema", "status": "passed"},
            {"name": task["spec"]["output"]["evidencePolicy"], "status": "passed" if task["spec"]["output"]["evidencePolicy"] != "none" else "not-applicable"},
            {"name": "observed-provenance", "status": "passed"},
        ],
    }
    return final, delivered, None


def _merge_worker_checkpoint(worker: Any, checkpoint: dict[str, Any], protocol_code: str = "harness.protocol", protocol_message: str = "Sandboxed adapter worker result was missing or malformed.", adapter_code: str | None = None) -> dict[str, Any]:
    if isinstance(worker, dict):
        merged = dict(worker)
    else:
        merged = {
            "status": "failed",
            "error": {"code": protocol_code, "message": protocol_message, "adapterCode": adapter_code},
        }
    merged["spendStarted"] = bool(merged.get("spendStarted")) or bool(checkpoint.get("spendStarted"))
    for name in ("actualModel", "actualProvider", "actualEffort"):
        value = merged.get(name)
        checkpoint_value = checkpoint.get(name)
        if not isinstance(value, str) or not value or len(value) > 256:
            merged[name] = checkpoint_value if isinstance(checkpoint_value, str) and len(checkpoint_value) <= 256 else ""
    worker_usage = merged.get("usage") if isinstance(merged.get("usage"), dict) else {}
    checkpoint_usage = checkpoint.get("usage") if isinstance(checkpoint.get("usage"), dict) else {}
    usage = dict(worker_usage)
    for name in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "providerRequests", "toolCalls", "turns"):
        values = [value for value in (worker_usage.get(name), checkpoint_usage.get(name)) if isinstance(value, int) and not isinstance(value, bool) and value >= 0]
        usage[name] = max(values) if values else None
    if not isinstance(usage.get("quota"), dict) and isinstance(checkpoint_usage.get("quota"), dict):
        usage["quota"] = checkpoint_usage["quota"]
    if not isinstance(usage.get("cost"), dict) and isinstance(checkpoint_usage.get("cost"), dict):
        usage["cost"] = checkpoint_usage["cost"]
    merged["usage"] = usage
    return merged


def _run(task_path: str, bundle_dir: str, result_path: str, events_path: str, recovery: dict[str, Any]) -> dict[str, Any]:
    task = load_json_regular(task_path, max_bytes=16 * 1024 * 1024)
    validate_contract(task, "AgentTask")
    bundle = Path(bundle_dir).resolve()
    output_dir = bundle / "output"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(mode=0o700)
    _verify_artifacts(task, bundle)
    started_at = _now()
    started = time.monotonic()
    execution_id = task["metadata"]["executionId"]
    request_sha256 = canonical_sha256(task)
    candidate = task["spec"]["selection"]["candidates"][0]
    adapter_class = ADAPTERS.get(candidate["adapter"])
    if adapter_class is None:
        raise RuntimeFailure("selection.no_candidate", "selecting", "Selected adapter is not in the trusted allowlist.")
    adapter = adapter_class()
    host = host_proof(adapter.id)
    probe = adapter.probe(task)
    negotiated = negotiate(task, probe.descriptor.value, host)
    plan = adapter.compile(task, negotiated.proof, probe)
    attempt_id = secrets.token_hex(16)
    plan["attemptId"] = attempt_id
    plan_path = bundle / "adapter-plan.json"
    atomic_write_json(plan_path, plan)
    if host.get("testOnly"):
        _materialize_fake_work(task, bundle)

    provider_proxy = None
    search_broker = None
    provider_socket = ""
    search_socket = ""
    broker_dir = Path(tempfile.mkdtemp(prefix="wha-"))
    try:
        if adapter.id == "codex-app-server":
            provider_socket = str(broker_dir / "provider.sock")
            provider_proxy = ProviderProxy(provider_socket, task["spec"]["isolation"]["modelNetwork"]["allowedHosts"])
            provider_proxy.start()
        if task["metadata"]["action"].endswith(".search"):
            token = os.environ.get("READONLY_TOKEN", "")
            search_socket = str(broker_dir / "search.sock")
            scripts = str(Path(__file__).resolve().parents[1] / "scripts")
            if scripts not in sys.path:
                sys.path.insert(0, scripts)
            import wheelhouse_core
            search_broker = SearchBroker(search_socket, task["metadata"]["target"]["owner"], task["metadata"]["target"]["repo"], token, wheelhouse_core.load_config())
            search_broker.start()

        worker_command = adapter.worker_command(str(plan_path), str(output_dir))
        command, environment = build_command(
            task=task,
            bundle=str(bundle),
            plan_path=str(plan_path),
            output_dir=str(output_dir),
            auth_source=probe.auth_source,
            binary_path=probe.binary_path,
            provider_socket=provider_socket,
            search_socket=search_socket,
            worker_command=worker_command,
            proof=host,
        )
        recovery.update(
            {
                "executionId": execution_id,
                "requestSha256": request_sha256,
                "attemptId": attempt_id,
                "startedAt": started_at,
                "startedMonotonicMs": int(started * 1000),
                "selection": _selection(task, probe),
                "proof": _proof(task, probe.descriptor.value, negotiated.proof, host),
            }
        )
        with EventWriter(events_path, execution_id, task["spec"]["limits"]["maxEventBytes"]) as events:
            events.emit("execution.accepted", {"requestSha256": request_sha256})
            events.emit("selection.resolved", {"candidateIndex": 0, "adapter": candidate["adapter"], "harness": candidate["harness"], "provider": candidate["provider"], "model": candidate["model"], "effort": candidate["effort"], "fallback": "none"})
            events.emit("capabilities.probed", {"descriptorSha256": canonical_sha256(probe.descriptor.value)})
            events.emit("capabilities.negotiated", {"proofSha256": canonical_sha256(negotiated.proof), "exactTools": negotiated.proof["exactTools"]})
            events.emit("sandbox.started", {"implementation": host["implementation"], "policySha256": canonical_sha256(host)})
            events.emit("attempt.started", {"attempt": 1, "sameCandidateMaxAttempts": 1})
            process = subprocess.Popen(command, env=environment, start_new_session=True, cwd=str(bundle), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            cancel_path = output_dir / "cancel.request"
            soft = task["spec"]["limits"]["softDeadlineMs"] / 1000
            hard = task["spec"]["limits"]["hardDeadlineMs"] / 1000
            grace = task["spec"]["limits"]["cancelGraceMs"] / 1000
            cancel_at: float | None = None
            killed_code = ""
            cancellation_requested = False

            def request_cancel(_signum: int, _frame: Any) -> None:
                nonlocal cancellation_requested
                cancellation_requested = True

            old_term = signal.signal(signal.SIGTERM, request_cancel)
            old_int = signal.signal(signal.SIGINT, request_cancel)
            try:
                while process.poll() is None:
                    elapsed = time.monotonic() - started
                    if (elapsed >= soft or cancellation_requested) and cancel_at is None:
                        cancel_path.write_text("cancel\n", encoding="utf-8")
                        cancel_at = time.monotonic()
                        events.emit("cancellation.requested", {"reason": "runner-signal" if cancellation_requested else "soft-deadline", "mechanism": adapter.cancel_protocol()})
                    if cancel_at is not None and time.monotonic() - cancel_at >= grace and process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        killed_code = "lifecycle.timeout"
                    if elapsed >= hard and process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        killed_code = "lifecycle.hard_kill"
                    time.sleep(0.05)
                process.wait(timeout=5)
            finally:
                signal.signal(signal.SIGTERM, old_term)
                signal.signal(signal.SIGINT, old_int)

            worker_path = output_dir / "worker-result.json"
            state_path = output_dir / "worker-state.json"
            try:
                checkpoint = load_json_regular(state_path, max_bytes=65536)
                if not isinstance(checkpoint, dict):
                    checkpoint = {}
            except ContractError:
                checkpoint = {}
            if killed_code:
                worker_value: Any = {"status": "failed", "spendStarted": True, "error": {"code": killed_code, "message": "Sandboxed adapter worker exceeded its deadline.", "adapterCode": None}}
            elif process.returncode != 0 and not worker_path.exists():
                worker_value = {"status": "failed", "error": {"code": "harness.crash", "message": "Sandboxed adapter worker exited without an atomic result.", "adapterCode": str(process.returncode)}}
            else:
                try:
                    worker_value = load_json_regular(worker_path, max_bytes=2 * 1024 * 1024)
                except ContractError:
                    worker_value = None
            worker = _merge_worker_checkpoint(worker_value, checkpoint)
            _read_worker_events(output_dir / "adapter-events.ndjson", events)
            try:
                final, delivered, validation_error = _validate_worker(task, bundle, worker)
            except Exception:
                final = None
                delivered = None
                validation_error = _error(
                    "internal.error",
                    "Trusted output validation failed after model spend.",
                    phase="validating-output",
                    spend_started=bool(worker.get("spendStarted")),
                )
            duration = int((time.monotonic() - started) * 1000)
            status = "succeeded" if final is not None else ("cancelled" if validation_error and validation_error["code"] == "lifecycle.cancelled" else "failed")
            result = {
                "apiVersion": API_VERSION,
                "kind": "AgentResult",
                "executionId": execution_id,
                "requestSha256": request_sha256,
                "status": status,
                "selection": _selection(task, probe, worker),
                "proof": _proof(task, probe.descriptor.value, negotiated.proof, host),
                "usage": _usage(worker, duration),
                "artifacts": [],
                "startedAt": started_at,
                "completedAt": _now(),
            }
            if delivered is not None:
                result["delivered"] = delivered
            if final is not None:
                result["final"] = final
            else:
                result["error"] = validation_error or _error("internal.error", "Agent runtime failed without a classified error.")
            events.emit("attempt.completed", {"status": status, "attempt": 1})
            events.emit("validation.completed", {"status": "passed" if final else "failed", "errorCode": None if final else result["error"]["code"]})
            events.emit("execution.completed", {"status": status, "resultSha256": result_projection_sha256(result), "projection": "agent-result-without-artifacts/v1"})
        event_file = Path(events_path)
        if event_file.is_file():
            result["artifacts"].append({"role": "normalized-events", "sha256": file_sha256(event_file), "mediaType": "application/x-ndjson", "bytes": event_file.stat().st_size, "retentionDays": task["spec"]["retention"]["normalizedEventsDays"], "redaction": "wheelhouse-agent/v1"})
        validate_contract(result, "AgentResult")
        atomic_write_json(result_path, result)
        return result
    finally:
        if search_broker:
            search_broker.close()
        if provider_proxy:
            provider_proxy.close()
        shutil.rmtree(broker_dir, ignore_errors=True)


def _preflight_code(error: Exception) -> tuple[str, str]:
    name = type(error).__name__
    message = str(error)
    lowered = message.lower()
    if isinstance(error, ContractError):
        return "contract.invalid", "validating"
    if isinstance(error, SandboxError):
        return "sandbox.violation", "sandboxing"
    if isinstance(error, CapabilityError):
        return "capability.unsatisfied", "probing"
    if isinstance(error, RuntimeFailure):
        return error.code, error.phase
    if name == "CodexProbeError":
        if "missing" in lowered or "unavailable" in lowered or "not configured" in lowered:
            return "auth.missing", "probing"
        if "credential" in lowered or "auth" in lowered or "api-key" in lowered:
            return "auth.invalid", "probing"
        return "capability.unsatisfied", "probing"
    return "internal.error", "validating"


def _write_rejected(task_path: str, bundle_dir: str, result_path: str, events_path: str, error: Exception, recovery: dict[str, Any]) -> dict[str, Any]:
    task = load_json_regular(task_path, max_bytes=16 * 1024 * 1024)
    validate_contract(task, "AgentTask")
    candidate = task["spec"]["selection"]["candidates"][0]
    execution_id = task["metadata"]["executionId"]
    request_sha256 = canonical_sha256(task)
    code, phase = _preflight_code(error)
    checkpoint: dict[str, Any] = {}
    try:
        checkpoint_value = load_json_regular(Path(bundle_dir) / "output" / "worker-state.json", max_bytes=65536)
        if (
            isinstance(checkpoint_value, dict)
            and checkpoint_value.get("spendStarted") is True
            and checkpoint_value.get("executionId") == execution_id
            and checkpoint_value.get("requestSha256") == request_sha256
            and checkpoint_value.get("attemptId") == recovery.get("attemptId")
            and recovery.get("executionId") == execution_id
            and recovery.get("requestSha256") == request_sha256
            and isinstance(recovery.get("selection"), dict)
            and isinstance(recovery.get("proof"), dict)
        ):
            checkpoint = checkpoint_value
    except Exception:
        pass
    spend_started = bool(checkpoint)
    if spend_started:
        code = "internal.error"
        phase = "running"
    started = recovery.get("startedAt") if spend_started and isinstance(recovery.get("startedAt"), str) else _now()
    adapter_id = candidate["adapter"]
    if adapter_id == "codex-app-server":
        lock_path = Path(__file__).resolve().parent / "runtime.lock.json"
        lock = load_json_regular(lock_path)
        codex = lock["codex"]
        harness_version = codex["binaryVersion"]
        adapter_version = codex["adapterVersion"]
        protocol = codex["protocol"]
        protocol_digest = canonical_sha256(codex["protocolSchemas"])
    else:
        harness_version = "1.0.0"
        adapter_version = "1.0.0"
        protocol = "fake-script-v1"
        protocol_digest = canonical_sha256({"fake-script": 1})
    selection = {
        "candidateIndex": 0,
        "harness": candidate["harness"],
        "adapter": adapter_id,
        "adapterVersion": adapter_version,
        "adapterDigest": _adapter_digest(adapter_id),
        "harnessVersion": None,
        "harnessDigest": None,
        "harnessProvenanceQuality": "unavailable",
        "harnessSourceCommit": None,
        "harnessMetadataSha256": None,
        "protocol": protocol,
        "protocolSchemaSha256": protocol_digest,
        "provider": candidate["provider"],
        "actualProvider": str(checkpoint.get("actualProvider") or "")[:256],
        "authProfile": candidate["authProfile"],
        "authMechanism": candidate["authMechanism"],
        "expectedWorkspaceIdSha256": canonical_sha256(candidate["expectedWorkspaceId"]) if candidate.get("expectedWorkspaceId") else None,
        "requestedModel": candidate["model"],
        "actualModel": str(checkpoint.get("actualModel") or "")[:256],
        "requestedEffort": candidate["effort"],
        "actualEffort": str(checkpoint.get("actualEffort") or "")[:256],
        "costClass": candidate["costClass"],
        "dataBoundary": candidate["dataBoundary"],
        "fallbackUsed": False,
        "fallbackReason": None,
    }
    if spend_started:
        selection = dict(recovery["selection"])
        selection["actualModel"] = str(checkpoint.get("actualModel") or "")[:256]
        selection["actualProvider"] = str(checkpoint.get("actualProvider") or "")[:256]
        selection["actualEffort"] = str(checkpoint.get("actualEffort") or "")[:256]
    rejected_error = _error(
        code,
        "Agent runtime failed after model spend." if spend_started else str(error) or "Agent runtime preflight failed.",
        phase=phase,
        spend_started=spend_started,
    )
    result = {
        "apiVersion": API_VERSION,
        "kind": "AgentResult",
        "executionId": execution_id,
        "requestSha256": request_sha256,
        "status": "failed" if spend_started else "rejected",
        "selection": selection,
        "proof": recovery["proof"] if spend_started else {
            "contractMajor": 1,
            "isolationLevel": "sandboxed-adapter-worker-v1",
            "capabilitySnapshotSha256": canonical_sha256({"status": "unavailable", "code": code}),
            "negotiationSha256": canonical_sha256({"status": "failed-after-spend" if spend_started else "rejected-before-spend", "code": code}),
            "policySha256": canonical_sha256({"isolation": task["spec"]["isolation"], "limits": task["spec"]["limits"], "retention": task["spec"]["retention"], "retry": task["spec"]["retry"]}),
            "compiledPromptSha256": task["spec"]["prompt"]["segments"][0]["sha256"],
            "inputManifestSha256": canonical_sha256(task["spec"]["inputs"]),
            "outputSchemaSha256": task["spec"]["output"]["schemaSha256"],
            "sandboxPolicySha256": canonical_sha256({"status": "not-started"}),
        },
        "error": rejected_error,
        "usage": _usage(checkpoint, max(0, int(time.monotonic() * 1000) - int(recovery.get("startedMonotonicMs") or 0))) if spend_started else {
            "inputTokens": None,
            "outputTokens": None,
            "cacheReadTokens": None,
            "cacheWriteTokens": None,
            "providerRequests": 0,
            "toolCalls": 0,
            "turns": 0,
            "durationMs": 0,
            "quota": {"available": False, "snapshotSha256": None, "observedAt": None, "primaryUsedPercent": None, "secondaryUsedPercent": None},
            "cost": {"amount": None, "currency": None, "quality": "unavailable"},
        },
        "artifacts": [],
        "startedAt": started,
        "completedAt": _now(),
    }
    with EventWriter(events_path, execution_id, task["spec"]["limits"]["maxEventBytes"]) as events:
        events.emit("execution.accepted", {"requestSha256": request_sha256})
        events.emit("selection.resolved", {"candidateIndex": 0, "adapter": adapter_id, "harness": candidate["harness"], "provider": candidate["provider"], "model": candidate["model"], "effort": candidate["effort"], "fallback": "none"})
        events.emit("validation.completed", {"status": "failed", "errorCode": code, "spendStarted": spend_started})
        events.emit("execution.completed", {"status": result["status"], "resultSha256": result_projection_sha256(result), "projection": "agent-result-without-artifacts/v1"})
    event_file = Path(events_path)
    result["artifacts"].append({"role": "normalized-events", "sha256": file_sha256(event_file), "mediaType": "application/x-ndjson", "bytes": event_file.stat().st_size, "retentionDays": task["spec"]["retention"]["normalizedEventsDays"], "redaction": "wheelhouse-agent/v1"})
    validate_contract(result, "AgentResult")
    atomic_write_json(result_path, result)
    return result


def run(task_path: str, bundle_dir: str, result_path: str, events_path: str) -> dict[str, Any]:
    recovery: dict[str, Any] = {}
    try:
        return _run(task_path, bundle_dir, result_path, events_path, recovery)
    except Exception as error:
        try:
            return _write_rejected(task_path, bundle_dir, result_path, events_path, error, recovery)
        except Exception:
            raise error
