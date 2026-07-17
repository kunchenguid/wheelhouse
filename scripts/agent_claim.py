#!/usr/bin/env python3
"""Durable card-side admission claim for spend-capable agent events."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.admission import event_claim_marker, event_key_sha256, normalized_event_identity
from agent_runtime.contract import ContractError

MAX_COMMENT_BYTES = 16_384
TRIAGE_RECORD_VERSION = 1
TRIAGE_RECORD_PREFIX = "<!-- wheelhouse-triage-record:"
TRIAGE_CLAIM_SUPERSEDED_VERSION = 1
TRIAGE_CLAIM_SUPERSEDED_PREFIX = "<!-- wheelhouse-agent-claim-superseded:"
TRIAGE_ACTIONS = {
    "triage.pr.local",
    "triage.pr.search",
    "triage.issue.local",
    "triage.issue.search",
}


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
    trusted = {"id": comment_id, "body": body}
    for field in ("created_at", "updated_at"):
        if isinstance(value.get(field), str):
            trusted[field] = value[field]
    return trusted


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


def supersede_triage_claim(
    *,
    action: str,
    owner: str,
    repo: str,
    number: int,
    issue: int,
    revision: str,
    repo_slug: str,
) -> dict:
    """Tombstone one exact auto-triage claim before a trusted replay.

    NL and deep-review identities require an event ID, while schema repair has
    its own action key. Restricting this helper to primary triage actions keeps
    replay incapable of superseding either class of claim.
    """
    if action not in TRIAGE_ACTIONS:
        raise ContractError("only a primary triage claim may be superseded")
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_slug)
        or repo_slug.split("/", 1)[0] != owner
    ):
        raise ContractError("triage claim repository was invalid")
    identity = normalized_event_identity(
        action=action,
        owner=owner,
        repo=repo,
        number=number,
        card_issue=issue,
        revision=revision,
    )
    event_key = event_key_sha256(identity)
    marker = event_claim_marker(event_key)
    existing = list_claims(repo_slug, issue, marker)
    if not existing:
        return {"event_key": event_key, "superseded": False}

    claim = existing[0]
    original_updated_at = claim.get("updated_at")
    if _trusted_comment_time(original_updated_at) is None:
        raise ContractError("triage claim timestamp was not trusted")
    superseded_marker = triage_claim_superseded_marker(event_key, original_updated_at)
    body = claim["body"].replace(marker, superseded_marker, 1)
    body = (
        body.rstrip()
        + "\n\nSuperseded by an operator-approved exact-revision auto-triage replay."
    )
    if marker in body or len(body.encode("utf-8")) > MAX_COMMENT_BYTES:
        raise ContractError("superseded triage claim body was invalid")
    updated = gh_json(
        "api",
        "--method",
        "PATCH",
        "repos/%s/issues/comments/%s" % (repo_slug, claim["id"]),
        "-f",
        "body=%s" % body,
    )
    direct = gh_json(
        "api",
        "repos/%s/issues/comments/%s" % (repo_slug, claim["id"]),
    )
    for value in (updated, direct):
        user = value.get("user") if isinstance(value, dict) else None
        comment_id = value.get("id") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or isinstance(comment_id, bool)
            or not isinstance(comment_id, int)
            or comment_id != claim["id"]
            or value.get("body") != body
            or not isinstance(user, dict)
            or user.get("login") != "github-actions[bot]"
        ):
            raise ContractError("superseded triage claim write was not trusted")
    if list_claims(repo_slug, issue, marker):
        raise ContractError("superseded triage claim remained admissibility-visible")
    return {"event_key": event_key, "superseded": True, "comment_id": claim["id"]}


def _trusted_comment_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def triage_claim_superseded_marker(event_key: str, original_updated_at: str) -> str:
    record = {
        "version": TRIAGE_CLAIM_SUPERSEDED_VERSION,
        "event_key": event_key,
        "original_updated_at": original_updated_at,
    }
    return "%s %s -->" % (
        TRIAGE_CLAIM_SUPERSEDED_PREFIX,
        json.dumps(record, sort_keys=True, separators=(",", ":")),
    )


def parse_triage_claim_superseded_marker(body: object) -> dict | None:
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_COMMENT_BYTES:
        return None
    if body.count(TRIAGE_CLAIM_SUPERSEDED_PREFIX) != 1:
        return None
    start = body.find(TRIAGE_CLAIM_SUPERSEDED_PREFIX)
    end = body.find(" -->", start)
    if end < 0:
        return None
    encoded = body[start + len(TRIAGE_CLAIM_SUPERSEDED_PREFIX) : end].strip()

    def no_duplicate_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate superseded claim key")
            value[key] = item
        return value

    try:
        record = json.loads(encoded, object_pairs_hook=no_duplicate_keys)
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict) or set(record) != {
        "version",
        "event_key",
        "original_updated_at",
    }:
        return None
    event_key = record.get("event_key")
    original_updated_at = record.get("original_updated_at")
    if (
        isinstance(record.get("version"), bool)
        or record.get("version") != TRIAGE_CLAIM_SUPERSEDED_VERSION
        or not isinstance(event_key, str)
        or not re.fullmatch(r"[0-9a-f]{64}", event_key)
        or _trusted_comment_time(original_updated_at) is None
    ):
        return None
    return record


def trusted_superseded_triage_claim_comment(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    comment_id = value.get("id")
    body = value.get("body")
    user = value.get("user") or {}
    login = user.get("login") if isinstance(user, dict) else None
    record = parse_triage_claim_superseded_marker(body)
    marker = (
        triage_claim_superseded_marker(
            record["event_key"], record["original_updated_at"]
        )
        if record
        else ""
    )
    terminal_bodies = {
        "Agent triage event finished with consumer.committed. %s\n\n"
        "Superseded by an operator-approved exact-revision auto-triage replay."
        % marker,
        "Agent triage event finished with consumer.rejected. %s\n\n"
        "Superseded by an operator-approved exact-revision auto-triage replay."
        % marker,
    }
    if (
        isinstance(comment_id, bool)
        or not isinstance(comment_id, int)
        or comment_id < 1
        or login != "github-actions[bot]"
        or record is None
        or body not in terminal_bodies
    ):
        return None
    trusted = {"id": comment_id, "body": body, "record": record}
    for field in ("created_at", "updated_at"):
        if isinstance(value.get(field), str):
            trusted[field] = value[field]
    return trusted


def list_superseded_triage_claims(
    repo_slug: str, issue: int, event_key: str
) -> list[dict]:
    pages = gh_json(
        "api",
        "--paginate",
        "--slurp",
        "repos/%s/issues/%s/comments?per_page=100" % (repo_slug, issue),
    )
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ContractError("superseded triage claim pagination was invalid")
    matches = []
    for page in pages:
        for value in page:
            body = value.get("body") if isinstance(value, dict) else None
            if (
                not isinstance(body, str)
                or TRIAGE_CLAIM_SUPERSEDED_PREFIX not in body
            ):
                continue
            user = value.get("user") if isinstance(value, dict) else None
            login = user.get("login") if isinstance(user, dict) else None
            if login != "github-actions[bot]":
                continue
            trusted = trusted_superseded_triage_claim_comment(value)
            if trusted is None:
                raise ContractError("superseded triage claim marker was not trusted")
            if trusted["record"]["event_key"] == event_key:
                matches.append(trusted)
    if len(matches) > 1:
        raise ContractError("triage attempt has duplicate superseded claims")
    return matches


def triage_replay_duplicate_only_evidence(
    *,
    action: str,
    owner: str,
    repo: str,
    number: int,
    issue: int,
    revision: str,
    repo_slug: str,
    replayed_at: str,
) -> bool:
    """Prove an old replay could only have been denied by its stale claim.

    The still-visible primary claim must be a terminal claim written no later
    than the replay marker. Any result record for the same event must likewise
    predate the replay. With claim creation serialized ahead of every model
    path, that exact marker made the replayed delivery inadmissible before task
    construction, so its queued reservation was not a real model attempt.
    """
    if action not in TRIAGE_ACTIONS:
        raise ContractError("duplicate-only evidence is primary-triage scoped")
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_slug)
        or repo_slug.split("/", 1)[0] != owner
    ):
        raise ContractError("triage evidence repository was invalid")
    replay_time = _trusted_comment_time(replayed_at)
    if replay_time is None:
        raise ContractError("replay marker time was invalid")
    identity = normalized_event_identity(
        action=action,
        owner=owner,
        repo=repo,
        number=number,
        card_issue=issue,
        revision=revision,
    )
    event_key = event_key_sha256(identity)
    marker = event_claim_marker(event_key)
    claims = list_claims(repo_slug, issue, marker)
    if len(claims) == 1:
        claim = claims[0]
        claim_time = _trusted_comment_time(claim.get("updated_at"))
        terminal_bodies = {
            "Agent triage event finished with consumer.committed. %s" % marker,
            "Agent triage event finished with consumer.rejected. %s" % marker,
        }
        if (
            claim_time is None
            or claim_time > replay_time
            or claim["body"] not in terminal_bodies
        ):
            return False
    else:
        superseded = list_superseded_triage_claims(repo_slug, issue, event_key)
        if len(superseded) != 1:
            return False
        claim_time = _trusted_comment_time(
            superseded[0]["record"].get("original_updated_at")
        )
        if claim_time is None or claim_time > replay_time:
            return False
    records = list_triage_records(repo_slug, issue, event_key)
    if not records:
        return True
    record_time = _trusted_comment_time(records[0].get("updated_at"))
    return bool(record_time is not None and record_time <= replay_time)


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
    trusted = {"id": comment_id, "body": body, "record": record}
    for field in ("created_at", "updated_at"):
        if isinstance(value.get(field), str):
            trusted[field] = value[field]
    return trusted


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
