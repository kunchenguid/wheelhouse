#!/usr/bin/env python3
"""Trusted scheduled-scan epoch ledger for soft-close hysteresis."""

import json
import os
import re

SCHEMA = "wheelhouse.scheduled-observation-epoch/v1"
LABEL = "wheelhouse:scheduled-observation-epoch"
TITLE = "Wheelhouse scheduled observation epoch (automated)"
MARKER = "wheelhouse-scheduled-observation-epoch"
_MAX_EPOCH = 9_007_199_254_740_991
_RE = re.compile(r"<!--\s*%s:\s*(\{.*?\})\s*-->" % MARKER, re.S)


def render(epoch, run_id):
    record = {"schema": SCHEMA, "epoch": epoch, "run_id": run_id}
    return "Scheduled observation epoch ledger.\n\n<!-- %s: %s -->" % (
        MARKER,
        json.dumps(record, sort_keys=True, separators=(",", ":")),
    )


def parse(body):
    matches = list(_RE.finditer(body or ""))
    if len(matches) != 1:
        return None

    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate epoch key")
            out[key] = value
        return out

    try:
        value = json.loads(matches[0].group(1), object_pairs_hook=unique)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {"schema", "epoch", "run_id"}:
        return None
    epoch = value.get("epoch")
    run_id = value.get("run_id")
    if (
        value.get("schema") != SCHEMA
        or isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch < 0
        or epoch > _MAX_EPOCH
        or not isinstance(run_id, str)
        or not re.fullmatch(r"[1-9][0-9]{0,30}", run_id)
        or body != render(epoch, run_id)
    ):
        return None
    return value


def _labels(issue):
    return {
        label if isinstance(label, str) else (label or {}).get("name", "")
        for label in (issue or {}).get("labels") or []
    }


def _trusted(issue):
    import render_card

    author = (issue or {}).get("user") or (issue or {}).get("author") or {}
    login = author.get("login", "") if isinstance(author, dict) else ""
    return bool(
        isinstance(issue, dict)
        and issue.get("title") == TITLE
        and str(issue.get("state") or "").upper() == "CLOSED"
        and _labels(issue) == {LABEL}
        and login in {
            render_card.CARD_AUTOMATION_AUTHOR,
            render_card.GET_CARD_AUTOMATION_AUTHOR,
        }
        and parse(issue.get("body", "")) is not None
    )


def _list():
    import render_card

    result = render_card._gh(
        [
            "api", "--paginate", "--slurp",
            "repos/{owner}/{repo}/issues?state=all&labels=%s&per_page=100" % LABEL,
        ]
    )
    pages = json.loads(result.stdout or "null")
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise RuntimeError("scheduled epoch ledger listing is incomplete")
    return [issue for page in pages for issue in page if "pull_request" not in issue]


def _get(number):
    import render_card

    result = render_card._gh(["api", "repos/{owner}/{repo}/issues/%s" % int(number)])
    value = json.loads(result.stdout or "null")
    return value if isinstance(value, dict) else None


def _patch(number, body):
    import render_card

    result = render_card._gh(
        [
            "api", "--method", "PATCH", "repos/{owner}/{repo}/issues/%s" % int(number),
            "-f", "body=" + body, "-f", "state=closed", "-f", "title=" + TITLE,
            "-f", "labels[]=" + LABEL,
        ]
    )
    return json.loads(result.stdout or "null")


def advance():
    """Advance once per scheduled workflow run; manual runs return zero."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return 0
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not re.fullmatch(r"[1-9][0-9]{0,30}", run_id):
        return 0
    import render_card

    rows = _list()
    if len(rows) > 1:
        raise RuntimeError("multiple scheduled epoch ledgers exist")
    if not rows:
        render_card.ensure_labels([LABEL])
        body = render(1, run_id)
        result = render_card._gh(
            [
                "api", "--method", "POST", "repos/{owner}/{repo}/issues",
                "-f", "title=" + TITLE, "-f", "body=" + body,
                "-f", "labels[]=" + LABEL,
            ]
        )
        created = json.loads(result.stdout or "null")
        number = created.get("number") if isinstance(created, dict) else None
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise RuntimeError("scheduled epoch ledger creation was invalid")
        _patch(number, body)
        verified = _get(number)
        if not _trusted(verified) or parse(verified["body"]) != {"schema": SCHEMA, "epoch": 1, "run_id": run_id}:
            raise RuntimeError("scheduled epoch ledger creation did not verify")
        return 1
    if not _trusted(rows[0]):
        raise RuntimeError("scheduled epoch ledger is untrusted")
    number = rows[0].get("number")
    current = _get(number)
    if not _trusted(current):
        raise RuntimeError("scheduled epoch ledger reread is untrusted")
    record = parse(current["body"])
    if record["run_id"] == run_id:
        return record["epoch"]
    if record["epoch"] >= _MAX_EPOCH:
        raise RuntimeError("scheduled epoch ledger is exhausted")
    expected = render(record["epoch"] + 1, run_id)
    _patch(number, expected)
    verified = _get(number)
    if not _trusted(verified) or verified.get("body") != expected:
        raise RuntimeError("scheduled epoch ledger update did not verify")
    return record["epoch"] + 1
