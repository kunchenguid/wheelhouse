#!/usr/bin/env python3
"""Durable card-side admission claim for spend-capable agent events."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.admission import event_claim_marker, event_key_sha256, normalized_event_identity
from agent_runtime.contract import ContractError

MAX_COMMENT_BYTES = 16_384


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
                trusted = trusted_claim_comment(value, marker)
                if trusted is None:
                    raise ContractError("agent claim marker was not trusted")
                matches.append(trusted)
    if len(matches) > 1:
        raise ContractError("agent event has duplicate durable claims")
    return matches


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


def main() -> None:
    try:
        raise SystemExit(claim(parser().parse_args()))
    except (ContractError, json.JSONDecodeError, OSError, subprocess.SubprocessError, ValueError) as error:
        print("agent claim error: %s" % error, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
