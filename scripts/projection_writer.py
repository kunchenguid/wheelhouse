#!/usr/bin/env python3
"""The authoritative verified PR-review card projection writer.

This module owns complete pending-card title/body/managed-label updates. Action
locks and target actions remain separate. Every commit rereads by card number,
defers on owner/handler races, performs one REST issue update, and verifies the
complete result by number.
"""

import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import card_projection  # noqa: E402

WRITER_NAME = "pr-review-projection-writer/v2"
MANAGED_PREFIXES = ("repo:", "kind:", "priority:", "target:")
MANAGED_EXACT = frozenset(
    {"needs-decision", "pending-triage", "wheelhouse:manual-merge-required", "wheelhouse:confirming-target-state"}
)
_RUN_STATS = {
    "planned": 0,
    "noop": 0,
    "deferred": 0,
    "committed": 0,
    "verification_failed": 0,
    "committed_by_cause": {},
}


def run_stats():
    return json.loads(_canonical(_RUN_STATS))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _labels(value):
    return sorted(
        {
            label if isinstance(label, str) else (label or {}).get("name", "")
            for label in (value or [])
            if (label if isinstance(label, str) else (label or {}).get("name", ""))
        }
    )


def _comments(value):
    comments = (value or {}).get("comments")
    if isinstance(comments, list):
        return {
            "count": len(comments),
            "digest": hashlib.sha256(_canonical(comments).encode("utf-8")).hexdigest(),
        }
    if isinstance(comments, bool):
        return {"count": -1, "digest": ""}
    try:
        return {"count": int(comments), "digest": ""}
    except (TypeError, ValueError):
        return {"count": -1, "digest": ""}


def _author(card):
    author = (card or {}).get("author") or (card or {}).get("user") or {}
    return author.get("login", "") if isinstance(author, dict) else str(author or "")


def card_snapshot(card):
    import render_card

    if not isinstance(card, dict):
        return None
    number = card.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        return None
    state = render_card._unique_state_block(card.get("body", ""))
    if state is None:
        return None
    comments = _comments(card)
    updated_at = render_card.card_updated_at(card)
    snapshot = {
        "number": number,
        "title": card.get("title", ""),
        "body": card.get("body", ""),
        "labels": _labels(card.get("labels")),
        "updated_at": updated_at,
        "comments": comments,
        "author": _author(card),
        "open": render_card.issue_is_open(card),
        "target": {
            "repo": state.get("repo"),
            "number": state.get("number"),
            "kind": state.get("kind"),
            "head_sha": state.get("head_sha", ""),
        },
    }
    if (
        not snapshot["title"]
        or not snapshot["body"]
        or not updated_at
        or comments["count"] < 0
        or snapshot["author"] not in {
            render_card.CARD_AUTOMATION_AUTHOR,
            render_card.GET_CARD_AUTOMATION_AUTHOR,
        }
    ):
        return None
    return snapshot


def _expected_matches(current, expected):
    if current is None or expected is None:
        return False
    if current["number"] != expected.get("number"):
        return False
    for key in ("title", "body", "labels", "updated_at", "author", "open", "target"):
        if key in expected and current.get(key) != expected.get(key):
            return False
    expected_comments = expected.get("comments")
    if isinstance(expected_comments, dict):
        if current["comments"]["count"] != expected_comments.get("count"):
            return False
        digest = expected_comments.get("digest")
        if digest and current["comments"].get("digest") != digest:
            return False
    return True


def _final_labels(current, managed):
    unmanaged = {
        label
        for label in current
        if not label.startswith(MANAGED_PREFIXES) and label not in MANAGED_EXACT
    }
    return sorted(unmanaged | set(managed))


def _event(event, **fields):
    if event in _RUN_STATS:
        _RUN_STATS[event] += 1
    if event == "committed":
        cause = str(fields.get("cause") or "unknown")
        causes = _RUN_STATS["committed_by_cause"]
        causes[cause] = causes.get(cause, 0) + 1
    payload = {"event": event, "writer": WRITER_NAME}
    payload.update(fields)
    print("wheelhouse projection-event " + _canonical(payload))


def _patch_issue(number, title, body, labels):
    import render_card

    payload = {"title": title, "body": body, "labels": labels}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(payload, handle, separators=(",", ":"))
        path = handle.name
    try:
        result = render_card._gh(
            [
                "api",
                "--method",
                "PATCH",
                "repos/{owner}/{repo}/issues/%s" % int(number),
                "--input",
                path,
            ],
            check=False,
        )
    finally:
        os.unlink(path)
    if result.returncode != 0:
        raise RuntimeError("complete issue projection update failed")


def commit_projection(number, expected, projection):
    """Commit one complete projection or defer without mutation.

    Returns ``committed``, ``noop``, or ``deferred``. Verification failures
    raise so the scheduled retry remains loud.
    """
    import render_card

    projection = card_projection.normalize_card_projection(projection)
    if projection is None:
        raise ValueError("card projection is malformed")
    _event(
        "planned",
        card=int(number),
        cause=projection["cause"],
        projection=projection["projection_id"][:24],
        changed=projection["changed_sections"],
        queue_effect=projection["queue_effect"],
    )
    current_card = render_card.get_card(number)
    current = card_snapshot(current_card)
    if not _expected_matches(current, expected):
        _event(
            "deferred",
            card=int(number),
            cause=projection["cause"],
            reason="owner_or_handler_race",
            queue_effect="none",
        )
        return "deferred"
    if not current["open"] or current["target"].get("kind") != "pr-review":
        _event(
            "deferred",
            card=int(number),
            cause=projection["cause"],
            reason="card_not_open_pr_review",
            queue_effect="none",
        )
        return "deferred"
    if projection["cause"] == "noop":
        _event(
            "noop",
            card=int(number),
            cause="noop",
            projection=projection["projection_id"][:24],
            queue_effect="none",
        )
        return "noop"
    final_labels = _final_labels(current["labels"], projection["managed_labels"])
    _patch_issue(
        number,
        projection["title"],
        projection["body"],
        final_labels,
    )
    verified_card = render_card.get_card(number)
    verified = card_snapshot(verified_card)
    if (
        verified is None
        or verified["title"] != projection["title"]
        or verified["body"] != projection["body"]
        or verified["labels"] != final_labels
        or verified["comments"]["count"] != current["comments"]["count"]
        or verified["author"] != current["author"]
        or not verified["open"]
    ):
        _event(
            "verification_failed",
            card=int(number),
            cause=projection["cause"],
            projection=projection["projection_id"][:24],
            queue_effect="promote",
        )
        raise RuntimeError("complete card projection did not verify")
    committed_fields = {
        "card": int(number),
        "cause": projection["cause"],
        "projection": projection["projection_id"][:24],
        "observation": projection["observation_id"][:24],
        "context": projection["context_id"][:24],
        "changed": projection["changed_sections"],
        "old_updated_at": current["updated_at"],
        "new_updated_at": verified["updated_at"],
        "queue_effect": projection["queue_effect"],
        "verified": True,
    }
    _event("committed", **committed_fields)
    print(
        "wheelhouse card-write "
        + _canonical({"writer": WRITER_NAME, **committed_fields})
    )
    return "committed"


def commit_preplanned(number, card, *, title, body, managed_labels, cause, observation_id="", context_id=""):
    expected = card_snapshot(card)
    if expected is None:
        _event(
            "deferred", card=int(number), cause=cause,
            reason="prewrite_snapshot_untrusted", queue_effect="none"
        )
        return "deferred"
    projection = card_projection.projection_from_values(
        title=title,
        body=body,
        labels=managed_labels,
        cause=cause,
        observation_id=observation_id,
        context_id=context_id,
        prior=card,
    )
    return commit_projection(number, expected, projection)


def observe_item(path):
    """Replace a PR dispatch hint with one exact reducer-owned observation."""
    import decision_context
    import target_observation
    import wheelhouse_core as core

    with open(path, encoding="utf-8") as handle:
        item = json.load(handle)
    if not isinstance(item, dict) or item.get("kind") != "pr-review":
        return
    cfg = core.load_config()
    repo_cfg = (cfg.get("repos") or {}).get(item.get("repo"))
    owner = core.get_owner()
    if not isinstance(repo_cfg, dict):
        observation = target_observation.incomplete_observation(
            owner,
            item.get("repo"),
            item.get("number"),
            expected_head_sha=str(item.get("head_sha") or ""),
            error="target repository policy is unavailable",
        )
    else:
        observation = core.observe_exact_pr(
            owner,
            repo_cfg,
            item.get("number"),
            expected_head_sha=str(item.get("head_sha") or ""),
        )
    facts = observation["facts"]
    item.update(
        {
            "head_sha": observation["revision"]["head_sha"],
            "base_sha": observation["revision"]["base_sha"],
            "title": facts.get("title") or item.get("title") or "(no title)",
            "author": facts.get("author") or item.get("author") or "?",
            "updated_at": facts.get("updated_at") or item.get("updated_at", ""),
            "bucket": facts.get("bucket") or "ci-state-unknown",
            "comp": facts.get("comp") or "unknown",
            "tests": facts.get("tests") or "unknown",
            "target_observation": observation,
            "projection_ref": target_observation.make_projection_ref(
                observation,
                "current" if observation["completeness"]["complete"] else "unknown",
                facts.get("bucket") or "ci-state-unknown",
            ),
            "decision_context": decision_context.unavailable_context(
                observation, "event-ingest.repository-snapshot-unavailable"
            ),
        }
    )
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(item, handle, indent=2, sort_keys=True)
        handle.write("\n")
    _event(
        "observation_input",
        target="%s#%s" % (item.get("repo"), item.get("number")),
        observation=observation["observation_id"][:24],
        complete=observation["completeness"]["complete"],
        source=observation["source"],
    )


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "observe-item":
        observe_item(sys.argv[2])
        return
    raise SystemExit("usage: projection_writer.py observe-item ITEM.json")


if __name__ == "__main__":
    main()
