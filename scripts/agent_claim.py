#!/usr/bin/env python3
"""Durable card-side admission claim for spend-capable agent events."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.admission import event_claim_marker, event_key_sha256, normalized_event_identity
from agent_runtime.contract import ContractError

MAX_COMMENT_BYTES = 16_384
TRIAGE_RECORD_VERSION = 1
TRIAGE_RECORD_PREFIX = "<!-- wheelhouse-triage-record:"


def output(name: str, value: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT", "")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%s=%s\n" % (name, str(value).replace("\n", " ")))


def gh_json(*args: str) -> object:
    result = subprocess.run(
        ("gh", *args),
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def trusted_claim_comment(value: object, marker: str) -> dict | None:
    if not isinstance(value, dict):
        return None
    comment_id = value.get("id")
    body = value.get("body")
    login = ((value.get("user") or {}).get("login") if isinstance(value.get("user"), dict) else None)
    if (
        isinstance(comment_id, bool)
        or not isinstance(comment_id, int)
        or comment_id < 1
        or not isinstance(body, str)
        or len(body.encode("utf-8")) > MAX_COMMENT_BYTES
        or body.count(marker) != 1
        or login != "github-actions[bot]"
    ):
        return None
    return {"id": comment_id, "body": body}


def list_claims(repo_slug: str, issue: int, marker: str) -> list[dict]:
    pages = gh_json(
        "api",
        "--paginate",
        "--slurp",
        "repos/%s/issues/%s/comments?per_page=100" % (repo_slug, issue),
    )
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ContractError("agent claim comment pagination was invalid")
    matches = []
    for page in pages:
        for value in page:
            body = value.get("body") if isinstance(value, dict) else None
            if isinstance(body, str) and marker in body:
                user = value.get("user") if isinstance(value, dict) else None
                login = user.get("login") if isinstance(user, dict) else None
                if login != "github-actions[bot]":
                    continue
                trusted = trusted_claim_comment(value, marker)
                if trusted is None:
                    raise ContractError("agent claim marker was not trusted")
                matches.append(trusted)
    if len(matches) > 1:
        raise ContractError("agent event has duplicate durable claims")
    return matches


def triage_record_body(
    event_key: str, revision: str, status: str, code: str
) -> str:
    record = {
        "version": TRIAGE_RECORD_VERSION,
        "event_key": event_key,
        "revision": revision,
        "status": status,
        "code": code,
    }
    return "<!-- wheelhouse-triage-record: %s -->" % json.dumps(
        record, sort_keys=True, separators=(",", ":")
    )


def parse_triage_record(body: object) -> dict | None:
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_COMMENT_BYTES:
        return None
    if body.count(TRIAGE_RECORD_PREFIX) != 1 or not body.endswith(" -->"):
        return None
    encoded = body[len(TRIAGE_RECORD_PREFIX) : -4].strip()

    def no_duplicate_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate triage record key")
            value[key] = item
        return value

    try:
        record = json.loads(encoded, object_pairs_hook=no_duplicate_keys)
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict) or set(record) != {
        "version",
        "event_key",
        "revision",
        "status",
        "code",
    }:
        return None
    event_key = record.get("event_key")
    revision = record.get("revision")
    code = record.get("code")
    if (
        isinstance(record.get("version"), bool)
        or record.get("version") != TRIAGE_RECORD_VERSION
        or not isinstance(event_key, str)
        or not re.fullmatch(r"[0-9a-f]{64}", event_key)
        or not isinstance(revision, str)
        or not revision
        or len(revision) > 128
        or "\n" in revision
        or record.get("status") not in {"succeeded", "error"}
        or not isinstance(code, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", code)
    ):
        return None
    return record


def trusted_triage_record_comment(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    comment_id = value.get("id")
    body = value.get("body")
    user = value.get("user") or {}
    login = user.get("login") if isinstance(user, dict) else None
    record = parse_triage_record(body)
    if (
        isinstance(comment_id, bool)
        or not isinstance(comment_id, int)
        or comment_id < 1
        or login != "github-actions[bot]"
        or record is None
    ):
        return None
    return {"id": comment_id, "body": body, "record": record}


def list_triage_records(repo_slug: str, issue: int, event_key: str) -> list[dict]:
    pages = gh_json(
        "api",
        "--paginate",
        "--slurp",
        "repos/%s/issues/%s/comments?per_page=100" % (repo_slug, issue),
    )
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ContractError("triage record comment pagination was invalid")
    matches = []
    for page in pages:
        for value in page:
            body = value.get("body") if isinstance(value, dict) else None
            if not isinstance(body, str) or TRIAGE_RECORD_PREFIX not in body:
                continue
            user = value.get("user") if isinstance(value, dict) else None
            login = user.get("login") if isinstance(user, dict) else None
            if login != "github-actions[bot]":
                continue
            trusted = trusted_triage_record_comment(value)
            if trusted is None:
                raise ContractError("triage record marker was not trusted")
            if trusted["record"]["event_key"] == event_key:
                matches.append(trusted)
    if len(matches) > 1:
        raise ContractError("triage attempt has duplicate result records")
    return matches


def record_triage_result(args: argparse.Namespace) -> int:
    if (
        isinstance(args.issue, bool)
        or not isinstance(args.issue, int)
        or args.issue < 1
        or not isinstance(args.repo_slug, str)
        or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo_slug
        )
    ):
        raise ContractError("triage result record destination was invalid")
    body = triage_record_body(args.event_key, args.revision, args.status, args.code)
    if parse_triage_record(body) is None:
        raise ContractError("triage result record fields were invalid")
    existing = list_triage_records(args.repo_slug, args.issue, args.event_key)
    if existing and existing[0]["body"] == body:
        comment = existing[0]
        action = "unchanged"
    elif existing:
        comment = trusted_triage_record_comment(
            gh_json(
                "api",
                "--method",
                "PATCH",
                "repos/%s/issues/comments/%s" % (args.repo_slug, existing[0]["id"]),
                "-f",
                "body=%s" % body,
            )
        )
        action = "updated"
    else:
        comment = trusted_triage_record_comment(
            gh_json(
                "api",
                "--method",
                "POST",
                "repos/%s/issues/%s/comments" % (args.repo_slug, args.issue),
                "-f",
                "body=%s" % body,
            )
        )
        action = "created"
    if comment is None or comment["body"] != body:
        raise ContractError("triage result record write response was invalid")
    direct = trusted_triage_record_comment(
        gh_json(
            "api",
            "repos/%s/issues/comments/%s" % (args.repo_slug, comment["id"]),
        )
    )
    if direct is None or direct["body"] != body:
        raise ContractError("triage result record was not authoritatively readable")
    print("triage result record %s for %s" % (action, args.event_key))
    return 0


def claim(args: argparse.Namespace) -> int:
    identity = normalized_event_identity(
        action=args.action,
        owner=args.owner,
        repo=args.repo,
        number=args.number,
        card_issue=args.issue,
        revision=args.revision,
        event_id=args.event_id,
    )
    event_key = event_key_sha256(identity)
    marker = event_claim_marker(event_key)
    existing = list_claims(args.repo_slug, args.issue, marker)
    admitted = not existing
    if existing:
        comment = existing[0]
    else:
        body = "Agent event admitted and is being processed.\n\n%s" % marker
        created = gh_json(
            "api",
            "--method",
            "POST",
            "repos/%s/issues/%s/comments" % (args.repo_slug, args.issue),
            "-f",
            "body=%s" % body,
        )
        comment = trusted_claim_comment(created, marker)
        if comment is None:
            raise ContractError("agent claim create response was invalid")
        direct = gh_json(
            "api",
            "repos/%s/issues/comments/%s" % (args.repo_slug, comment["id"]),
        )
        comment = trusted_claim_comment(direct, marker)
        if comment is None:
            raise ContractError("agent claim was not authoritatively readable")
    output("admitted", "true" if admitted else "false")
    output("event_key", event_key)
    output("comment_id", comment["id"])
    output("marker", marker)
    print("agent event %s %s" % ("admitted" if admitted else "duplicate", event_key))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("--action", required=True)
    root.add_argument("--owner", required=True)
    root.add_argument("--repo", required=True)
    root.add_argument("--number", type=int, required=True)
    root.add_argument("--issue", type=int, required=True)
    root.add_argument("--revision", required=True)
    root.add_argument("--event-id", default="")
    root.add_argument("--repo-slug", required=True)
    return root


def record_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("record")
    root.add_argument("--issue", type=int, required=True)
    root.add_argument("--repo-slug", required=True)
    root.add_argument("--event-key", required=True)
    root.add_argument("--revision", required=True)
    root.add_argument("--status", choices=("succeeded", "error"), required=True)
    root.add_argument("--code", required=True)
    return root


def main() -> None:
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "record":
            raise SystemExit(record_triage_result(record_parser().parse_args()))
        raise SystemExit(claim(parser().parse_args()))
    except (ContractError, json.JSONDecodeError, OSError, subprocess.SubprocessError, ValueError) as error:
        print("agent claim error: %s" % error, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
