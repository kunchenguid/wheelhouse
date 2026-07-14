"""Strict contract validation, canonical hashing, and atomic JSON delivery."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from . import API_VERSION

ROOT = Path(__file__).resolve().parent
SCHEMA_ROOT = ROOT / "schemas"
SCHEMAS = {
    "AgentTask": SCHEMA_ROOT / "v1alpha1" / "agent-task.schema.json",
    "AgentEvent": SCHEMA_ROOT / "v1alpha1" / "agent-event.schema.json",
    "AgentResult": SCHEMA_ROOT / "v1alpha1" / "agent-result.schema.json",
}


class ContractError(ValueError):
    """A stable, content-free contract validation failure."""


class ArtifactError(ContractError):
    """An artifact failed a path, type, size, or digest check."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON suitable for the v1 hash contract.

    v1 data contains only integral numbers. Rejecting non-integral floats avoids
    pretending Python's float renderer implements every RFC 8785 edge case.
    """

    def check(item: Any, path: str = "$") -> None:
        if isinstance(item, float):
            if not math.isfinite(item) or not item.is_integer():
                raise ContractError("non-integral number at %s" % path)
        elif isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ContractError("non-string object key at %s" % path)
            for key, child in item.items():
                check(child, "%s.%s" % (path, key))
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                check(child, "%s[%d]" % (path, index))

    check(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def result_projection_sha256(result: dict[str, Any]) -> str:
    return canonical_sha256({key: value for key, value in result.items() if key != "artifacts"})


def file_sha256(path: os.PathLike[str] | str, max_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ArtifactError("artifact exceeds its declared byte limit")
            digest.update(chunk)
    return digest.hexdigest()


def _is_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ContractError("validator does not support schema type %r" % expected)


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$", root: dict[str, Any] | None = None) -> None:
    """Validate the strict JSON Schema subset used by Wheelhouse.

    The vendored public schemas are authoritative. Keeping this small validator
    in trusted source avoids package installs in the adapter worker.
    """

    root = root or schema
    if "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/"):
            raise ContractError("external schema references are forbidden")
        target: Any = root
        for part in reference[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        validate_schema(value, target, path, root)
        return
    if "const" in schema and value != schema["const"]:
        raise ContractError("%s must equal the contract constant" % path)
    if "enum" in schema and value not in schema["enum"]:
        raise ContractError("%s is not an allowed value" % path)
    expected = schema.get("type")
    if expected is not None:
        choices = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(value, choice) for choice in choices):
            raise ContractError("%s has the wrong type" % path)
    if "oneOf" in schema:
        successes = 0
        for option in schema["oneOf"]:
            try:
                validate_schema(value, option, path, root)
                successes += 1
            except ContractError:
                pass
        if successes != 1:
            raise ContractError("%s does not match exactly one schema alternative" % path)
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                validate_schema(value, option, path, root)
                break
            except ContractError:
                continue
        else:
            raise ContractError("%s does not match a schema alternative" % path)
    if "allOf" in schema:
        for option in schema["allOf"]:
            if "if" in option:
                try:
                    validate_schema(value, option["if"], path, root)
                except ContractError:
                    if "else" in option:
                        validate_schema(value, option["else"], path, root)
                else:
                    if "then" in option:
                        validate_schema(value, option["then"], path, root)
            else:
                validate_schema(value, option, path, root)
    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise ContractError("%s is missing required field %s" % (path, missing[0]))
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ContractError("%s has unknown field %s" % (path, unknown[0]))
        max_properties = schema.get("maxProperties")
        if max_properties is not None and len(value) > max_properties:
            raise ContractError("%s has too many fields" % path)
        for name, child in value.items():
            child_schema = properties.get(name)
            if child_schema is None:
                additional = schema.get("additionalProperties")
                child_schema = additional if isinstance(additional, dict) else None
            if child_schema:
                validate_schema(child, child_schema, "%s.%s" % (path, name), root)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ContractError("%s has too few items" % path)
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ContractError("%s has too many items" % path)
        item_schema = schema.get("items")
        if item_schema:
            for index, child in enumerate(value):
                validate_schema(child, item_schema, "%s[%d]" % (path, index), root)
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ContractError("%s is too short" % path)
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ContractError("%s is too long" % path)
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ContractError("%s has an invalid format" % path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ContractError("%s is below its minimum" % path)
        if "maximum" in schema and value > schema["maximum"]:
            raise ContractError("%s exceeds its maximum" % path)


def load_schema(kind: str) -> dict[str, Any]:
    try:
        path = SCHEMAS[kind]
    except KeyError as error:
        raise ContractError("unknown contract kind") from error
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def validate_contract(document: dict[str, Any], kind: str | None = None) -> None:
    if not isinstance(document, dict):
        raise ContractError("contract document must be an object")
    actual_kind = kind or document.get("kind")
    if actual_kind not in SCHEMAS:
        raise ContractError("unknown contract kind")
    if document.get("apiVersion") != API_VERSION:
        raise ContractError("unsupported agent runtime contract version")
    validate_schema(document, load_schema(actual_kind))
    if actual_kind == "AgentTask":
        for item in document["spec"]["inputs"]:
            logical = Path(item["logicalPath"])
            if logical.is_absolute() or any(part in ("", ".", "..") for part in logical.parts):
                raise ContractError("$.spec.inputs logicalPath has an invalid format")
        limits = document["spec"]["limits"]
        enforcement = limits["enforcement"]
        for name, quality in enforcement.items():
            if (limits[name] is None) != (quality == "unavailable"):
                raise ContractError("limit values must match their enforcement availability")
        if limits["hardDeadlineMs"] is not None and limits["softDeadlineMs"] is not None and limits["cancelGraceMs"] is not None and limits["hardDeadlineMs"] <= limits["softDeadlineMs"] + limits["cancelGraceMs"]:
            raise ContractError("hard deadline must exceed soft deadline plus cancellation grace")
    if actual_kind == "AgentResult":
        if document["proof"]["limitEnforcementSha256"] != canonical_sha256(document["proof"]["limitEnforcement"]):
            raise ContractError("limit enforcement evidence digest does not match")
        revision_binding = document["proof"].get("revisionBinding")
        if revision_binding is not None and (
            revision_binding["expectedCommitSha"] == revision_binding["observedCommitSha"]
            or (revision_binding["cancellationConfirmed"] != (revision_binding["cancellationError"] is None))
            or document["proof"]["sandboxPolicySha256"] != canonical_sha256(revision_binding)
            or document["status"] != "failed"
            or document.get("error", {}).get("code") != "target.stale"
            or document.get("error", {}).get("spendStarted") is not True
            or document["selection"]["actualProvider"]
            or document["selection"]["actualModel"]
            or document["selection"]["actualEffort"]
            or any(document["usage"][name] is not None for name in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "providerRequests", "toolCalls", "turns", "durationMs"))
        ):
            raise ContractError("revision mismatch evidence is inconsistent")
        selection = document["selection"]
        quality = selection["harnessProvenanceQuality"]
        if quality == "verified-executable" and (selection["harnessVersion"] is None or selection["harnessDigest"] is None):
            raise ContractError("verified executable provenance requires an observed version and digest")
        if quality == "verified-action-metadata" and (selection["harnessSourceCommit"] is None or selection["harnessMetadataSha256"] is None):
            raise ContractError("verified action provenance requires source and metadata identities")
        if quality == "pinned-action-reference" and (selection["harnessSourceCommit"] is None or selection["harnessMetadataSha256"] is not None):
            raise ContractError("pinned action provenance must not claim observed metadata")
        if quality == "unavailable" and any(selection[name] is not None for name in ("harnessVersion", "harnessDigest", "harnessSourceCommit", "harnessMetadataSha256")):
            raise ContractError("unavailable harness provenance cannot carry observed identities")


def load_json_regular(path: os.PathLike[str] | str, max_bytes: int = 16 * 1024 * 1024) -> Any:
    candidate = Path(path)
    try:
        info = candidate.lstat()
    except OSError as error:
        raise ContractError("contract file is unavailable") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ContractError("contract path must be a regular file")
    if info.st_size > max_bytes:
        raise ContractError("contract file is too large")
    try:
        with candidate.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ContractError("contract file is not valid UTF-8 JSON") from error


def atomic_write_json(path: os.PathLike[str] | str, value: Any, mode: int = 0o600) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(value) + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def verify_result_binding(task: dict[str, Any], result: dict[str, Any]) -> None:
    validate_contract(task, "AgentTask")
    validate_contract(result, "AgentResult")
    if result["executionId"] != task["metadata"]["executionId"]:
        raise ContractError("result execution id does not match its task")
    if result["requestSha256"] != canonical_sha256(task):
        raise ContractError("result request hash does not match its task")
