#!/usr/bin/env python3
"""Durable exact-revision triage result records and projection retry."""

import hashlib
import json
import re

SCHEMA = "wheelhouse.assessment-result/v1"
MARKER = "wheelhouse-assessment-result"
PREFIX = "<!-- %s: " % MARKER
MAX_BODY_BYTES = 60_000


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _result_id(payload):
    return "sha256:" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def make_record(state, revision, *, triage=None, error=""):
    payload = {
        "schema": SCHEMA,
        "target": {
            "repo": str((state or {}).get("repo") or ""),
            "number": (state or {}).get("number"),
            "kind": str((state or {}).get("kind") or ""),
            "revision": str(revision or ""),
        },
        "triage": triage if isinstance(triage, dict) else None,
        "error": str(error or "")[:220],
    }
    payload["result_id"] = _result_id(payload)
    if normalize_record(payload) is None:
        raise ValueError("durable assessment result is malformed")
    return payload


def normalize_record(value):
    if not isinstance(value, dict) or set(value) != {
        "schema", "result_id", "target", "triage", "error"
    }:
        return None
    target = value.get("target")
    if (
        value.get("schema") != SCHEMA
        or not isinstance(target, dict)
        or set(target) != {"repo", "number", "kind", "revision"}
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", str(target.get("repo") or ""))
        or isinstance(target.get("number"), bool)
        or not isinstance(target.get("number"), int)
        or target["number"] < 1
        or target.get("kind") not in {"pr-review", "issue-triage"}
        or not isinstance(target.get("revision"), str)
        or not target["revision"]
        or len(target["revision"]) > 128
        or (value.get("triage") is None) == (not value.get("error"))
        or (value.get("triage") is not None and not isinstance(value.get("triage"), dict))
        or not isinstance(value.get("error"), str)
        or len(value["error"]) > 220
    ):
        return None
    without = dict(value)
    claimed = without.pop("result_id", None)
    if claimed != _result_id(without):
        return None
    encoded = _canonical(value).encode("utf-8")
    return json.loads(encoded) if len(encoded) <= 40_000 else None


def body(record, projected=False):
    record = normalize_record(record)
    if record is None:
        raise ValueError("assessment result record is malformed")
    visible = (
        "Triage result projected."
        if projected
        else "Triage finished; card projection pending."
    )
    return "%s\n\n<!-- %s: %s -->" % (
        visible,
        MARKER,
        _canonical({"projected": bool(projected), "result": record}),
    )


def parse_body(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_BODY_BYTES:
        return None
    if value.count(PREFIX) != 1 or not value.endswith(" -->"):
        return None
    encoded = value.split(PREFIX, 1)[1][:-4].strip()
    try:
        envelope = json.loads(encoded)
    except (TypeError, ValueError):
        return None
    if not isinstance(envelope, dict) or set(envelope) != {"projected", "result"} or not isinstance(envelope.get("projected"), bool):
        return None
    record = normalize_record(envelope.get("result"))
    if record is None or value != body(record, envelope["projected"]):
        return None
    return {"projected": envelope["projected"], "result": record}


def _trusted_comment(comment):
    user = (comment or {}).get("user") or (comment or {}).get("author") or {}
    login = user.get("login", "") if isinstance(user, dict) else ""
    parsed = parse_body((comment or {}).get("body"))
    comment_id = (comment or {}).get("id")
    return parsed if (
        parsed
        and login == "github-actions[bot]"
        and isinstance(comment_id, int)
        and not isinstance(comment_id, bool)
        and comment_id > 0
    ) else None


def _comments(issue):
    import render_card

    result = render_card._gh(
        [
            "api", "--paginate", "--slurp",
            "repos/{owner}/{repo}/issues/%s/comments?per_page=100" % int(issue),
        ]
    )
    pages = json.loads(result.stdout or "null")
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise RuntimeError("assessment result comments are incomplete")
    return [comment for page in pages for comment in page]


def find(issue, result_id=None, revision=None, projected=None):
    if projected is not None and not isinstance(projected, bool):
        raise ValueError("projected assessment-result selector is invalid")
    matches = []
    for comment in _comments(issue):
        if PREFIX not in str((comment or {}).get("body") or ""):
            continue
        parsed = _trusted_comment(comment)
        if parsed is None:
            raise RuntimeError("assessment result marker is untrusted")
        record = parsed["result"]
        if result_id and record["result_id"] != result_id:
            continue
        if revision and record["target"]["revision"] != revision:
            continue
        if projected is not None and parsed["projected"] is not projected:
            continue
        matches.append({"id": comment["id"], **parsed})
    if len(matches) > 1:
        raise RuntimeError("duplicate durable assessment results")
    return matches[0] if matches else None


def persist(issue, record):
    import render_card

    record = normalize_record(record)
    if record is None:
        raise ValueError("assessment result record is malformed")
    existing = find(issue, result_id=record["result_id"])
    if existing:
        return existing
    result = render_card._gh(
        [
            "api", "--method", "POST", "repos/{owner}/{repo}/issues/%s/comments" % int(issue),
            "-f", "body=" + body(record, False),
        ]
    )
    comment = json.loads(result.stdout or "null")
    parsed = _trusted_comment(comment)
    if parsed is None or parsed["result"]["result_id"] != record["result_id"]:
        raise RuntimeError("assessment result write did not verify")
    direct = find(issue, result_id=record["result_id"])
    if direct is None:
        raise RuntimeError("assessment result is not authoritatively readable")
    return direct


def mark_projected(issue, result_id):
    import render_card

    existing = find(issue, result_id=result_id)
    if existing is None:
        raise RuntimeError("assessment result disappeared before projection")
    if existing["projected"]:
        return True
    result = render_card._gh(
        [
            "api", "--method", "PATCH",
            "repos/{owner}/{repo}/issues/comments/%s" % existing["id"],
            "-f", "body=" + body(existing["result"], True),
        ]
    )
    parsed = _trusted_comment(json.loads(result.stdout or "null"))
    return bool(parsed and parsed["projected"] and parsed["result"]["result_id"] == result_id)
