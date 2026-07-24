#!/usr/bin/env python3
"""Bounded recovery for a decision label erased by a PR projection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import apply_decision  # noqa: E402
import render_card  # noqa: E402

VERSION = 2
PREFIX = "<!-- wheelhouse-decision-label-recovery:"
MAX_COMMENT_BYTES = 16_384
HISTORY_PAGE_SIZE = 100
MAX_HISTORY_PAGES = 10
TRUSTED_AUTOMATION = frozenset({"github-actions[bot]", "app/github-actions"})
LOCK_LABELS = frozenset(
    {
        "processing",
        "resolved",
        "blocked",
        "wheelhouse:auto-merge-claim",
    }
)


class RecoveryError(RuntimeError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _output(name, value):
    path = os.environ.get("GITHUB_OUTPUT", "")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%s=%s\n" % (name, str(value).replace("\n", " ")))


def _labels(value):
    if not isinstance(value, list):
        return None
    names = []
    for label in value:
        name = label if isinstance(label, str) else (label or {}).get("name")
        if not isinstance(name, str) or not name or len(name) > 100:
            return None
        names.append(name)
    if len(names) != len(set(names)):
        return None
    return set(names)


def _login(value):
    actor = value if isinstance(value, dict) else {}
    login = actor.get("login")
    return login if isinstance(login, str) else ""


def _event_time(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
    ):
        return None
    return value


def _flatten_pages(value):
    if not isinstance(value, list) or any(not isinstance(page, list) for page in value):
        return None
    return [row for page in value for row in page]


def _state_identity(body):
    state = render_card._unique_state_block(body)
    if not state or state.get("kind") != "pr-review":
        return None
    observation = render_card.target_contracts.normalize_review_observation(
        state.get(render_card.REVIEW_OBSERVATION_FIELD)
    )
    context = render_card.context_contracts.normalize_decision_context(
        state.get(render_card.DECISION_CONTEXT_FIELD)
    )
    number = state.get("number")
    if (
        state.get(render_card.PROJECTION_OWNER_FIELD) != render_card.PROJECTION_OWNER
        or not isinstance(state.get("repo"), str)
        or not state["repo"]
        or isinstance(number, bool)
        or not isinstance(number, int)
        or number < 1
        or not isinstance(state.get("head_sha"), str)
        or not state["head_sha"]
        or observation is None
        or context is None
    ):
        return None
    return {
        "repo": state["repo"],
        "number": number,
        "head_sha": state["head_sha"],
        "observation_id": observation["observation_id"],
        "context_id": context["context_id"],
    }


def _relevant_label_events(events, label):
    rows = []
    for event in events:
        if not isinstance(event, dict):
            return None
        if event.get("event") not in {"labeled", "unlabeled"}:
            continue
        event_label = event.get("label")
        if not isinstance(event_label, dict) or event_label.get("name") != label:
            continue
        event_id = event.get("id")
        created_at = _event_time(event.get("created_at"))
        actor = _login(event.get("actor"))
        if (
            isinstance(event_id, bool)
            or not isinstance(event_id, int)
            or event_id < 1
            or created_at is None
            or not actor
        ):
            return None
        rows.append(
            {
                "id": event_id,
                "event": event["event"],
                "created_at": created_at,
                "actor": actor,
            }
        )
    rows.sort(key=lambda row: (row["created_at"], row["id"]))
    return rows


def _record_key(record):
    return hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()


def _body_digest(body):
    if not isinstance(body, str):
        return None
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def marker(record):
    return "%s %s -->" % (PREFIX, _canonical(record))


def parse_marker(body):
    if (
        not isinstance(body, str)
        or len(body.encode("utf-8")) > MAX_COMMENT_BYTES
        or body.count(PREFIX) != 1
    ):
        return None
    start = body.find(PREFIX)
    end = body.find(" -->", start)
    if end < 0:
        return None
    def no_duplicate_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate recovery claim key")
            value[key] = item
        return value

    try:
        record = json.loads(
            body[start + len(PREFIX) : end].strip(),
            object_pairs_hook=no_duplicate_keys,
        )
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict) or set(record) != {
        "version",
        "event_key",
        "labeled_event_id",
        "erased_event_id",
        "erased_at",
        "repo_slug",
        "card_issue",
        "card_node_id",
        "label",
        "target_repo",
        "target_number",
        "head_sha",
        "observation_id",
        "context_id",
        "body_sha256",
    }:
        return None
    integer_fields = (
        "labeled_event_id",
        "erased_event_id",
        "card_issue",
        "card_node_id",
        "target_number",
    )
    if (
        isinstance(record.get("version"), bool)
        or record.get("version") != VERSION
        or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("event_key") or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("body_sha256") or ""))
        or any(
            isinstance(record.get(field), bool)
            or not isinstance(record.get(field), int)
            or record[field] < 1
            for field in integer_fields
        )
        or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}",
            str(record.get("repo_slug") or ""),
        )
        or not re.fullmatch(
            r"decision:[a-z][a-z0-9-]{0,40}", str(record.get("label") or "")
        )
        or any(
            not isinstance(record.get(field), str) or not record[field]
            for field in (
                "target_repo",
                "head_sha",
                "observation_id",
                "context_id",
            )
        )
        or _event_time(record.get("erased_at")) is None
    ):
        return None
    unsigned = dict(record)
    claimed = unsigned.pop("event_key")
    if claimed != _record_key(unsigned):
        return None
    return record


def admission_record(
    event_payload,
    current,
    event_pages,
    *,
    repo_slug,
    issue,
    sender,
    authorized,
    processing="forbidden",
):
    if authorized is not True or processing not in {
        "forbidden",
        "required",
    }:
        return None, "actor.unauthorized"
    if not isinstance(event_payload, dict) or event_payload.get("action") != "labeled":
        return None, "event.unsupported"
    repository = event_payload.get("repository") or {}
    trigger_issue = event_payload.get("issue") or {}
    trigger_label = event_payload.get("label") or {}
    trigger_sender = _login(event_payload.get("sender"))
    label = trigger_label.get("name")
    if (
        repository.get("full_name") != repo_slug
        or trigger_sender != sender
        or not sender
        or not isinstance(label, str)
        or not label.startswith("decision:")
        or not isinstance(current, dict)
    ):
        return None, "event.identity"
    current_number = current.get("number")
    current_id = current.get("id")
    if (
        trigger_issue.get("number") != issue
        or current_number != issue
        or trigger_issue.get("id") != current_id
        or isinstance(current_id, bool)
        or not isinstance(current_id, int)
        or current_id < 1
        or str(trigger_issue.get("state") or "").lower() != "open"
        or str(current.get("state") or "").lower() != "open"
    ):
        return None, "card.identity"
    trigger_body = trigger_issue.get("body")
    current_body = current.get("body")
    trigger_identity = _state_identity(trigger_body)
    current_identity = _state_identity(current_body)
    if (
        trigger_identity is None
        or current_identity is None
        or trigger_identity != current_identity
        or not render_card.owner_projection_race_recoverable(
            trigger_body, current_body
        )
    ):
        return None, "projection.binding"
    decision = apply_decision.parse_label(
        label, apply_decision.ALLOWED.get("pr-review", set())
    )
    if decision is None:
        return None, "label.unsupported"
    trigger_labels = _labels(trigger_issue.get("labels"))
    current_labels = _labels(current.get("labels"))
    if trigger_labels is None or current_labels is None:
        return None, "labels.malformed"
    locks = current_labels.intersection(LOCK_LABELS)
    expected_locks = {"processing"} if processing == "required" else set()
    if locks != expected_locks or "needs-decision" not in current_labels:
        return None, "card.locked"
    stable_current = current_labels - {"processing"}
    if (
        label not in trigger_labels
        or label in current_labels
        or trigger_labels - stable_current != {label}
        or stable_current - trigger_labels
        or len(
            {
                name
                for name in trigger_labels | current_labels
                if name.startswith("decision:")
            }
        )
        != 1
    ):
        return None, "label.delta"
    events = _flatten_pages(event_pages)
    relevant = _relevant_label_events(events, label) if events is not None else None
    trigger_time = _event_time(trigger_issue.get("updated_at"))
    if relevant is None or trigger_time is None:
        return None, "timeline.unavailable"
    candidates = [
        row
        for row in relevant
        if row["event"] == "labeled"
        and row["created_at"] == trigger_time
        and row["actor"] == sender
    ]
    if len(candidates) != 1:
        return None, "timeline.ambiguous"
    labeled = candidates[0]
    tail = [
        row
        for row in relevant
        if (row["created_at"], row["id"])
        >= (labeled["created_at"], labeled["id"])
    ]
    if (
        len(tail) != 2
        or tail[0] != labeled
        or tail[1]["event"] != "unlabeled"
        or tail[1]["actor"] not in TRUSTED_AUTOMATION
    ):
        return None, "timeline.not_projection_erasure"
    unsigned = {
        "version": VERSION,
        "labeled_event_id": labeled["id"],
        "erased_event_id": tail[1]["id"],
        "erased_at": tail[1]["created_at"],
        "repo_slug": repo_slug,
        "card_issue": issue,
        "card_node_id": current_id,
        "label": label,
        "target_repo": current_identity["repo"],
        "target_number": current_identity["number"],
        "head_sha": current_identity["head_sha"],
        "observation_id": current_identity["observation_id"],
        "context_id": current_identity["context_id"],
        "body_sha256": _body_digest(current_body),
    }
    return {**unsigned, "event_key": _record_key(unsigned)}, "admission.ok"


def _gh_json(*args):
    result = subprocess.run(
        ("gh", *args),
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _read_event(path):
    info = os.lstat(path)
    if not os.path.isfile(path) or os.path.islink(path) or info.st_size > 2_000_000:
        raise RecoveryError("event payload was unavailable")
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RecoveryError("event payload was malformed")
    return value


def _read_world(repo_slug, issue):
    current = _gh_json("api", "repos/%s/issues/%s" % (repo_slug, issue))
    events = _read_complete_history(repo_slug, issue, "events")
    comments = _read_complete_history(repo_slug, issue, "comments")
    return current, events, comments


def _read_complete_history(repo_slug, issue, resource):
    pages = []
    for page_number in range(1, MAX_HISTORY_PAGES + 1):
        page = _gh_json(
            "api",
            "repos/%s/issues/%s/%s?per_page=%s&page=%s"
            % (
                repo_slug,
                issue,
                resource,
                HISTORY_PAGE_SIZE,
                page_number,
            ),
        )
        if not isinstance(page, list) or len(page) > HISTORY_PAGE_SIZE:
            raise RecoveryError("%s history was unavailable" % resource)
        pages.append(page)
        if len(page) < HISTORY_PAGE_SIZE:
            return pages
    raise RecoveryError("%s history exceeded the recovery bound" % resource)


def _trusted_claims(comment_pages, event_key):
    matches = []
    for comment in _flatten_pages(comment_pages) or []:
        body = comment.get("body") if isinstance(comment, dict) else None
        if not isinstance(body, str) or PREFIX not in body:
            continue
        if _login(comment.get("user")) not in TRUSTED_AUTOMATION:
            continue
        record = parse_marker(body)
        if record is None:
            raise RecoveryError("decision-label recovery claim was malformed")
        if body != claim_body(record):
            raise RecoveryError("decision-label recovery claim body was malformed")
        if record["event_key"] == event_key:
            matches.append(comment)
    if len(matches) > 1:
        raise RecoveryError("decision-label recovery claim was duplicated")
    return matches


def claim_body(record):
    return (
        "Recovered decision-label event admitted for one pinned revision."
        "\n\n%s" % marker(record)
    )


def _write_claim(repo_slug, issue, record):
    body = claim_body(record)
    created = _gh_json(
        "api",
        "--method",
        "POST",
        "repos/%s/issues/%s/comments" % (repo_slug, issue),
        "-f",
        "body=%s" % body,
    )
    comment_id = created.get("id") if isinstance(created, dict) else None
    if (
        isinstance(comment_id, bool)
        or not isinstance(comment_id, int)
        or comment_id < 1
        or created.get("body") != body
        or _login(created.get("user")) not in TRUSTED_AUTOMATION
    ):
        raise RecoveryError("decision-label recovery claim write failed")
    direct = _gh_json(
        "api",
        "repos/%s/issues/comments/%s" % (repo_slug, comment_id),
    )
    if (
        not isinstance(direct, dict)
        or direct.get("body") != body
        or _login(direct.get("user")) not in TRUSTED_AUTOMATION
    ):
        raise RecoveryError("decision-label recovery claim did not verify")
    return comment_id


def claim(args):
    _output("required", "false")
    _output("admitted", "false")
    event_payload = _read_event(args.event_file)
    label = ((event_payload.get("label") or {}).get("name"))
    if (
        event_payload.get("action") != "labeled"
        or not isinstance(label, str)
        or not label.startswith("decision:")
    ):
        return 0
    current, events, comments = _read_world(args.repo_slug, args.issue)
    current_labels = _labels((current or {}).get("labels"))
    if current_labels is not None and label in current_labels:
        return 0
    _output("required", "true")
    record, reason = admission_record(
        event_payload,
        current,
        events,
        repo_slug=args.repo_slug,
        issue=args.issue,
        sender=args.sender,
        authorized=args.authorized == "true",
    )
    _output("reason", reason)
    if record is None:
        return 0
    if _trusted_claims(comments, record["event_key"]):
        _output("reason", "claim.replay")
        return 0
    if _event_time(current.get("updated_at")) != record["erased_at"]:
        _output("reason", "projection.not_current")
        return 0
    comment_id = _write_claim(args.repo_slug, args.issue, record)
    current, events, comments = _read_world(args.repo_slug, args.issue)
    repeated, reason = admission_record(
        event_payload,
        current,
        events,
        repo_slug=args.repo_slug,
        issue=args.issue,
        sender=args.sender,
        authorized=args.authorized == "true",
    )
    claims = _trusted_claims(comments, record["event_key"])
    if repeated != record or len(claims) != 1 or claims[0].get("id") != comment_id:
        _output("reason", reason if repeated is None else "claim.verification")
        return 0
    _output("admitted", "true")
    _output("event_key", record["event_key"])
    _output("comment_id", comment_id)
    _output("label", record["label"])
    _output("reason", "admission.ok")
    return 0


def revalidate(args):
    _output("allowed", "false")
    event_payload = _read_event(args.event_file)
    current, events, comments = _read_world(args.repo_slug, args.issue)
    claims = _trusted_claims(comments, args.event_key)
    if len(claims) != 1:
        return 0
    claimed_record = parse_marker(claims[0]["body"])
    repeated, _ = admission_record(
        event_payload,
        current,
        events,
        repo_slug=args.repo_slug,
        issue=args.issue,
        sender=args.sender,
        authorized=args.authorized == "true",
        processing=args.processing,
    )
    if repeated == claimed_record:
        _output("allowed", "true")
    return 0


def parser():
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command", required=True)
    for name in ("claim", "revalidate"):
        command = sub.add_parser(name)
        command.add_argument("--event-file", required=True)
        command.add_argument("--repo-slug", required=True)
        command.add_argument("--issue", required=True, type=int)
        command.add_argument("--sender", required=True)
        command.add_argument("--authorized", required=True)
        if name == "revalidate":
            command.add_argument("--event-key", required=True)
            command.add_argument(
                "--processing",
                choices=("forbidden", "required"),
                required=True,
            )
    return root


def main():
    args = parser().parse_args()
    try:
        code = claim(args) if args.command == "claim" else revalidate(args)
    except (
        json.JSONDecodeError,
        OSError,
        RecoveryError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        print("decision-label recovery error: %s" % str(error)[:200], file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
