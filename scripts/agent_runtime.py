#!/usr/bin/env python3
"""Trusted CLI for Wheelhouse Agent Runtime Contract v1."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.adapters.codex import _load_lock, _protocol_digest
from agent_runtime.claude_bridge import bridge
from agent_runtime.config import ConfigError, resolve_selection
from agent_runtime.consumer import export_value, load_agent_result, result_text
from agent_runtime.contract import ContractError, canonical_sha256, load_json_regular, validate_contract, verify_result_binding
from agent_runtime.events import EventError, verify_result_event_binding
from agent_runtime.supervisor import RuntimeFailure, run
from agent_runtime.task_builder import build_task


def output(name: str, value: object) -> None:
    text = str(value)
    path = os.environ.get("GITHUB_OUTPUT", "")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%s=%s\n" % (name, text.replace("\n", " ")))


def cmd_select(args: argparse.Namespace) -> int:
    selection = resolve_selection(args.action, args.repo)
    output("mode", selection["mode"])
    output("profile", selection["profileName"])
    output("auth_profile", selection["profile"]["auth_profile"])
    output("auth_mechanism", selection["profile"]["auth_mechanism"])
    output("fallback", selection["fallback"])
    if args.json:
        safe = dict(selection)
        print(json.dumps(safe, sort_keys=True))
    else:
        print(selection["mode"])
    return 0


def cmd_auth_status(args: argparse.Namespace) -> int:
    try:
        selection = resolve_selection(args.action, args.repo)
    except ConfigError as error:
        output("enabled", "false")
        output("reason", str(error))
        print("agent runtime auth unavailable: %s" % error)
        return 0
    enabled = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    reason = "" if enabled else "CLAUDE_CODE_OAUTH_TOKEN is absent for the Claude production profile"
    output("enabled", "true" if enabled else "false")
    output("mode", selection["mode"])
    output("reason", reason)
    print("agent runtime auth %s for %s" % ("ready" if enabled else "unavailable", selection["profile"]["auth_profile"]))
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    selection = resolve_selection(args.action, args.repo)
    task = build_task(
        action=args.action,
        selection=selection,
        prompt_path=args.prompt,
        bundle_dir=args.bundle,
        output_path=args.out,
        owner=args.owner,
        repo=args.repo,
        number=args.number,
        target_kind=args.kind,
        revision=args.revision,
        wheelhouse_revision=args.wheelhouse_revision,
        target_file=args.target_file,
        repository_dir=args.repository_dir,
        repository_commit=args.repository_commit,
        vision_file=args.vision_file,
        repair_kind=args.repair_kind,
    )
    output("task", str(Path(args.out).resolve()))
    output("bundle", str(Path(args.bundle).resolve()))
    output("execution_id", task["metadata"]["executionId"])
    print("built AgentTask %s" % task["metadata"]["executionId"])
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    result = run(args.task, args.bundle, args.result, args.events)
    output("result", str(Path(args.result).resolve()))
    output("events", str(Path(args.events).resolve()))
    output("status", result["status"])
    output("error_code", (result.get("error") or {}).get("code", ""))
    print("agent runtime %s" % result["status"])
    return 0 if result["status"] == "succeeded" else 1


def cmd_validate(args: argparse.Namespace) -> int:
    value = load_json_regular(args.path)
    validate_contract(value, args.kind or None)
    print("valid %s" % (args.kind or value["kind"]))
    return 0


def cmd_verify_result(args: argparse.Namespace) -> int:
    task = load_json_regular(args.task)
    result = load_json_regular(args.result)
    verify_result_binding(task, result)
    verify_result_event_binding(result, args.events)
    print("result and terminal event bind to AgentTask")
    return 0


def cmd_bridge_claude(args: argparse.Namespace) -> int:
    result = bridge(args.task, args.bundle, args.execution_file, args.delivered_file, args.enforcement_file, args.handoff_sha256, args.result, args.events)
    output("result", str(Path(args.result).resolve()))
    output("events", str(Path(args.events).resolve()))
    output("status", result["status"])
    output("error_code", (result.get("error") or {}).get("code", ""))
    print("Claude action bridge %s" % result["status"])
    return 0 if result["status"] == "succeeded" else 1


def cmd_export(args: argparse.Namespace) -> int:
    if not export_value(args.result, args.out, require_success=not args.allow_failed_delivered):
        print("agent runtime result has no exportable value", file=sys.stderr)
        return 1
    return 0


def cmd_text(args: argparse.Namespace) -> int:
    text = result_text(args.result, require_success=not args.allow_failed_delivered)
    if not text:
        return 1
    print(text)
    return 0


def cmd_verify_pins(_args: argparse.Namespace) -> int:
    lock = _load_lock()
    digest = _protocol_digest(lock)
    print("Codex %s protocol schemas verified: %s" % (lock["codex"]["binaryVersion"], digest))
    return 0


def _verify_package_tarball(path: str, expected_integrity: str) -> bool:
    candidate = Path(path)
    try:
        if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > 512 * 1024 * 1024:
            return False
        digest = hashlib.sha512()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        observed = "sha512-" + base64.b64encode(digest.digest()).decode("ascii")
        if not hmac.compare_digest(observed, expected_integrity):
            return False
        with tarfile.open(candidate, "r:gz") as archive:
            members = archive.getmembers()
            if not members:
                return False
            for member in members:
                parts = Path(member.name).parts
                if not parts or parts[0] != "package" or any(part in ("", ".", "..") for part in parts) or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    return False
    except (OSError, tarfile.TarError):
        return False
    return True


def cmd_verify_package(args: argparse.Namespace) -> int:
    codex = _load_lock()["codex"]
    if not hmac.compare_digest(args.package, codex["npmPackage"]):
        print("Codex package identity does not match the runtime lock", file=sys.stderr)
        return 1
    platforms = {
        "linux-x64": (codex["linuxX64BinaryPackage"], codex["linuxX64BinaryPackageIntegrity"]),
        "linux-arm64": (codex["linuxArm64BinaryPackage"], codex["linuxArm64BinaryPackageIntegrity"]),
    }
    expected_platform = platforms.get(args.platform)
    if expected_platform is None or not hmac.compare_digest(args.platform_package, expected_platform[0]):
        print("Codex platform package identity does not match the runtime lock", file=sys.stderr)
        return 1
    if not _verify_package_tarball(args.tarball, codex["npmPackageIntegrity"]):
        print("Codex package tarball bytes do not match the runtime lock", file=sys.stderr)
        return 1
    if not _verify_package_tarball(args.platform_tarball, expected_platform[1]):
        print("Codex platform tarball bytes do not match the runtime lock", file=sys.stderr)
        return 1
    print("Codex package integrity verified: %s and %s" % (args.package, args.platform_package))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    result = load_agent_result(args.result)
    if result is None:
        return 1
    selection = result["selection"]
    if selection["actualProvider"]:
        provider_fact = "directly observed `%s`" % selection["actualProvider"]
    elif selection["actualModel"] and selection["adapter"] == "claude-action-compat":
        provider_fact = "inferred `%s` from the pinned Claude action and auth profile" % selection["provider"]
    else:
        provider_fact = "not independently observed"
    lines = [
        "### Wheelhouse agent runtime",
        "",
        "- Status: `%s`" % result["status"],
        "- Adapter: `%s` `%s`" % (selection["adapter"], selection["adapterVersion"]),
        "- Harness: `%s` `%s` (%s)" % (selection["harness"], selection["harnessVersion"] or "unavailable", selection["harnessProvenanceQuality"]),
        "- Provider: selected `%s`; %s (`%s`)" % (selection["provider"], provider_fact, selection["authProfile"]),
        "- Model: selected `%s`, directly observed `%s`" % (selection["requestedModel"], selection["actualModel"] or "unavailable"),
        "- Effort: selected `%s`, directly observed `unavailable`" % selection["requestedEffort"],
        "- Fallback: `disabled`",
        "- Request: `%s`" % result["requestSha256"],
    ]
    if result.get("error"):
        lines.append("- Error: `%s`" % result["error"]["code"])
    text = "\n".join(lines) + "\n"
    summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    else:
        print(text, end="")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select")
    select.add_argument("--action", required=True)
    select.add_argument("--repo", default="")
    select.add_argument("--json", action="store_true")
    select.set_defaults(func=cmd_select)
    auth = commands.add_parser("auth-status")
    auth.add_argument("--action", required=True)
    auth.add_argument("--repo", default="")
    auth.set_defaults(func=cmd_auth_status)
    build = commands.add_parser("build-task")
    build.add_argument("--action", required=True)
    build.add_argument("--prompt", required=True)
    build.add_argument("--bundle", required=True)
    build.add_argument("--out", required=True)
    build.add_argument("--owner", required=True)
    build.add_argument("--repo", required=True)
    build.add_argument("--number", type=int, required=True)
    build.add_argument("--kind", required=True)
    build.add_argument("--revision", required=True)
    build.add_argument("--wheelhouse-revision", required=True)
    build.add_argument("--target-file", default="")
    build.add_argument("--repository-dir", default="")
    build.add_argument("--repository-commit", default="")
    build.add_argument("--vision-file", default="")
    build.add_argument("--repair-kind", choices=("issue", "pr"), default="pr")
    build.set_defaults(func=cmd_build)
    execute = commands.add_parser("run")
    execute.add_argument("--task", required=True)
    execute.add_argument("--bundle", required=True)
    execute.add_argument("--result", required=True)
    execute.add_argument("--events", required=True)
    execute.set_defaults(func=cmd_run)
    validate = commands.add_parser("validate")
    validate.add_argument("--path", required=True)
    validate.add_argument("--kind", choices=("AgentTask", "AgentEvent", "AgentResult"), default="")
    validate.set_defaults(func=cmd_validate)
    verify_result = commands.add_parser("verify-result")
    verify_result.add_argument("--task", required=True)
    verify_result.add_argument("--result", required=True)
    verify_result.add_argument("--events", required=True)
    verify_result.set_defaults(func=cmd_verify_result)
    claude = commands.add_parser("bridge-claude")
    claude.add_argument("--task", required=True)
    claude.add_argument("--bundle", required=True)
    claude.add_argument("--execution-file", default="")
    claude.add_argument("--delivered-file", default="")
    claude.add_argument("--enforcement-file", required=True)
    claude.add_argument("--handoff-sha256", required=True)
    claude.add_argument("--result", required=True)
    claude.add_argument("--events", required=True)
    claude.set_defaults(func=cmd_bridge_claude)
    export = commands.add_parser("export-final")
    export.add_argument("--result", required=True)
    export.add_argument("--out", required=True)
    export.add_argument("--allow-failed-delivered", action="store_true")
    export.set_defaults(func=cmd_export)
    text = commands.add_parser("result-text")
    text.add_argument("--result", required=True)
    text.add_argument("--allow-failed-delivered", action="store_true")
    text.set_defaults(func=cmd_text)
    pins = commands.add_parser("verify-pins")
    pins.set_defaults(func=cmd_verify_pins)
    package = commands.add_parser("verify-package")
    package.add_argument("--package", required=True)
    package.add_argument("--platform", choices=("linux-x64", "linux-arm64"), required=True)
    package.add_argument("--platform-package", required=True)
    package.add_argument("--tarball", required=True)
    package.add_argument("--platform-tarball", required=True)
    package.set_defaults(func=cmd_verify_package)
    summary = commands.add_parser("summary")
    summary.add_argument("--result", required=True)
    summary.set_defaults(func=cmd_summary)
    return root


def main() -> None:
    try:
        code = parser().parse_args()
        status = code.func(code)
    except (ConfigError, ContractError, EventError, RuntimeFailure, ValueError) as error:
        print("agent runtime error: %s" % error, file=sys.stderr)
        status = 1
    raise SystemExit(status)


if __name__ == "__main__":
    main()
