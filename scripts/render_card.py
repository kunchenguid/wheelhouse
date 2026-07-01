#!/usr/bin/env python3
"""
Wheelhouse - decision-card renderer + card operations.

`render(item)` turns one classified item into a decision card: a human-readable
body with quick-decision checkboxes and a hidden machine-readable state block.
`upsert_card`/`close_card` create/refresh/consume cards in THIS repo (via the
ambient GH_TOKEN, which the workflow sets to the default GITHUB_TOKEN so that
card-side activity never re-triggers the handler).

CLI:
  render_card.py upsert --item-file item.json    create-or-refresh a card (dedup by marker)
  render_card.py render --item-file item.json --out-dir DIR    debug: write title/body/labels
  render_card.py queue-triage --item-file item.json    mark triage queued and dispatch triage.yml when eligible
  render_card.py triage-apply --issue N --head-sha SHA --execution-file FILE    update the card from Claude output
  render_card.py triage-fail --issue N --head-sha SHA --message TEXT    write the auto-triage unavailable section
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wheelhouse_core import parse_state_block  # noqa: E402

# Quick-decision (checkbox) option keys per kind. Comment / decline carry text,
# so they are slash-command-only (see apply_decision.py), not checkboxes.
#
# `investigate` is the odd one out: it is NON-CONSUMING. Ticking it triggers a
# code-grounded deep review (deep-review.yml) and leaves the card open for the
# owner's real decision; the handler clears the box so it can be re-triggered
# after new commits (see apply_decision.py / decision-handler.yml). It is offered
# on the kinds where deeper analysis helps (pr-review, issue-triage) but NOT on
# ci-approval, which is a fast security gate, not a merit review.
CHECKBOX_OPTIONS = {
    "pr-review": ["merge", "close", "investigate", "hold"],
    "ci-approval": ["approve-ci", "close", "hold"],
    "issue-triage": ["close", "investigate", "hold"],
}

OPTION_LABELS = {
    "merge": "Merge it",
    "approve-ci": "Approve the CI run (security-gated)",
    "close": "Close / decline",
    "investigate": "Investigate - deep code-grounded review (leaves this card open)",
    "hold": "Hold - I'll handle this manually",
}

SLASH_HINT = {
    "pr-review": "`/merge`, `/close`, `/decline <reason>`, `/hold`, `/comment <text>`",
    "ci-approval": "`/approve-ci`, `/close`, `/decline <reason>`, `/hold`, `/comment <text>`",
    "issue-triage": "`/close`, `/decline <reason>`, `/hold`, `/comment <text>`",
}

KIND_LABEL = {
    "pr-review": "PR review",
    "ci-approval": "CI approval",
    "issue-triage": "Issue triage",
}


# --------------------------------------------------------------------------- #
# Card-refresh semantics (an open card must reflect CURRENT target state)
# --------------------------------------------------------------------------- #
# Wheelhouse-managed label namespaces. On refresh `upsert_card` REPLACES these
# (removing ones that no longer apply); `needs-decision` and any human-added
# label are left untouched.
MANAGED_LABEL_PREFIXES = ("repo:", "kind:", "priority:", "target:")

# A card carrying any of these is past the pure pending state: the owner has a
# decision in flight (`processing`), the card is consumed (`resolved`), or it is
# held (`blocked`). Re-rendering the body resets its checkboxes, which would
# clobber an in-progress decision or race the decision-handler - so a refresh
# SKIPS a card with any of these. Only a pure `needs-decision` card is refreshed.
NON_REFRESHABLE_LABELS = frozenset({"processing", "resolved", "blocked"})

# The fields whose change makes a card materially stale and worth re-rendering.
# Title / summary / recommendation re-render naturally; they are NOT triggers.
MATERIAL_FIELDS = ("head_sha", "comp", "tests", "kind", "priority", "options")

# The version of the body `render()` currently produces. A card's stored
# `render_version` behind this value is stale and gets exactly one re-render
# (see `render_stale`) - the same missing-field-reads-as-behind backfill shape
# already used for legacy material fields and for `triaged_sha`. A card
# written before this field existed has none, which reads as version 0
# (behind), so every pre-existing card refreshes exactly once and then
# no-ops. Bump this whenever a future display-only change (copy, formatting,
# the author line, etc.) should propagate to existing open cards. This is
# NOT a material field: never add it to MATERIAL_FIELDS / material_signature
# / _state_material, and it must never affect classify/decision-parsing/
# merge-close-approve/fork-CI-safety/author-filtering/conflict-routing/triage.
CARD_RENDER_VERSION = 1

TRIAGE_FIELDS = ("summary", "product_implications", "recommended_next_step")
TRIAGE_START = "<!-- wheelhouse-triage:start -->"
TRIAGE_END = "<!-- wheelhouse-triage:end -->"
TRIAGE_UNAVAILABLE = "Auto triage unavailable for this PR version."

_STATE_BLOCK_RE = re.compile(
    r"<!--\s*(?:wheelhouse|triage)-state:\s*(\{.*?\})\s*-->",
    re.S,
)
_TRIAGE_SECTION_RE = re.compile(
    r"\n?<!--\s*wheelhouse-triage:start\s*-->.*?"
    r"<!--\s*wheelhouse-triage:end\s*-->\n?",
    re.S,
)

# Sentinel for a material field absent from an old card's state block. It can
# never equal a real value, so a card written before these fields were carried
# is detected as "changed" exactly once and refreshes itself safely (backfilling
# the fields), then no-ops thereafter.
_UNKNOWN = "\x00unknown"


def marker_label(item):
    return "target:%s-%s" % (item["repo"], item["number"])


def card_labels(item):
    return [
        "needs-decision",
        "repo:%s" % item["repo"],
        "kind:%s" % item["kind"],
        "priority:%s" % item.get("priority", "low"),
        marker_label(item),
    ]


def card_options(item):
    kind = item.get("kind", "pr-review")
    return item.get("options") or CHECKBOX_OPTIONS.get(kind, ["close", "hold"])


def normalized_options(options):
    if options is None:
        return []
    if isinstance(options, str):
        options = [options]
    return sorted({str(o) for o in options})


def material_signature(item):
    """The material comparison signature, with the same defaults as the card
    body/labels. Options compare as a normalized set so order-only changes do
    not make a card stale."""
    kind = item.get("kind", "pr-review")
    return {
        "head_sha": item.get("head_sha", "") or "",
        "comp": item.get("comp", "n/a"),
        "tests": item.get("tests", "n/a"),
        "kind": kind,
        "priority": item.get("priority", "low"),
        "options": normalized_options(card_options(item)),
    }


def _state_material(state):
    """The material fields from a parsed state block. A field missing from an old
    card (pre-refresh-feature) reads as `_UNKNOWN` so it never matches a real
    value - that card refreshes once and backfills the fields."""
    s = state or {}
    material = {}
    for field in MATERIAL_FIELDS:
        if field not in s:
            material[field] = _UNKNOWN
        elif field == "options":
            material[field] = normalized_options(s.get(field))
        else:
            material[field] = s.get(field)
    return material


def material_changed(item, state):
    """True if any material field differs between the freshly scanned item and
    the card's stored state. A legacy card lacking the new fields counts as
    changed (one safe refresh). `state` is a parsed state block or None."""
    return material_signature(item) != _state_material(state)


def render_stale(state):
    """True when the card's stored `render_version` is behind the current
    `CARD_RENDER_VERSION` - a non-material, one-time re-render trigger for
    display-only fixes (e.g. dropping the author @mention) that have no
    material-field trigger. A missing `render_version` (a card written before
    this field existed) reads as version 0, so it is stale exactly once. Pure
    and side-effect free, like `material_changed`."""
    raw_version = (state or {}).get("render_version", 0)
    if isinstance(raw_version, bool):
        stored_version = 0
    else:
        try:
            stored_version = int(raw_version)
        except (TypeError, ValueError):
            stored_version = 0
    return stored_version < CARD_RENDER_VERSION


def triage_fresh(item, state):
    """True when the card has already attempted auto-triage for this PR head.

    `triaged_sha` is a cost-control cache, not a material refresh field. It is
    written before the workflow dispatch so a failed or timed-out workflow does
    not get re-run every hourly scan for the same head SHA.
    """
    head_sha = item.get("head_sha", "") or ""
    return bool(head_sha and (state or {}).get("triaged_sha") == head_sha)


def triage_queued_for_head(state, head_sha):
    return bool(
        head_sha
        and (state or {}).get("triaged_sha") == head_sha
        and (state or {}).get("triage_status") == "queued"
    )


def should_auto_triage(item, state, labels, has_token=True):
    """Whether this card should queue the lightweight automatic PR triage."""
    if not has_token:
        return False
    if item.get("kind", "pr-review") != "pr-review":
        return False
    if item.get("auto_triage", True) is False:
        return False
    if not is_refreshable(labels):
        return False
    if not item.get("head_sha"):
        return False
    return not triage_fresh(item, state)


def _label_names(labels):
    """Normalize a `gh ... --json labels` list (objects) or a plain string list
    into a set of label names."""
    return {
        label if isinstance(label, str) else label.get("name", "")
        for label in (labels or [])
    }


def is_refreshable(labels):
    """A card is refreshable only in the pure `needs-decision` state."""
    names = _label_names(labels)
    return "needs-decision" in names and names.isdisjoint(NON_REFRESHABLE_LABELS)


def plan_label_update(desired, current):
    """Plan a true label replace of the wheelhouse-managed namespaces. Returns
    (to_add, to_remove): managed labels that no longer apply are removed;
    `needs-decision` and any non-managed (human-added) label are never removed."""
    current_names = _label_names(current)
    desired_set = set(desired)
    managed_now = {n for n in current_names if n.startswith(MANAGED_LABEL_PREFIXES)}
    to_add = [label for label in desired if label not in current_names]
    to_remove = sorted(managed_now - desired_set)
    return to_add, to_remove


def _clean_triage_text(value, limit=700, default="n/a"):
    text = str(value or "").strip()
    text = text.replace("\r", "\n")
    text = re.sub(r"\s+", " ", text)
    # Cards are private to the owner; never notify contributors from model text.
    text = text.replace("@", "")
    text = text.replace("<!--", "").replace("-->", "")
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text or default


def normalize_triage(data):
    if not isinstance(data, dict):
        return None
    triage = {}
    for field in TRIAGE_FIELDS:
        value = data.get(field)
        if not isinstance(value, str):
            return None
        cleaned = _clean_triage_text(value, default="")
        if not cleaned:
            return None
        triage[field] = cleaned
    rec = triage["recommended_next_step"]
    allowed = ("merge", "look closer", "discuss", "decline")
    if not rec.lower().startswith(allowed):
        triage["recommended_next_step"] = "look closer - " + rec
    return triage


def triage_section(triage=None, error=None):
    lines = [TRIAGE_START, "### Triage", ""]
    if triage:
        lines.append("- **Summary:** %s" % triage["summary"])
        lines.append("- **Product implications:** %s" % triage["product_implications"])
        lines.append(
            "- **Recommended next step:** %s" % triage["recommended_next_step"]
        )
    else:
        note = _clean_triage_text(error or TRIAGE_UNAVAILABLE, limit=220)
        lines.append("_%s_" % note)
    lines.append(TRIAGE_END)
    return "\n".join(lines)


def remove_triage_section(body):
    return _TRIAGE_SECTION_RE.sub("\n", body or "").strip() + "\n"


def _existing_triage_section(body):
    match = _TRIAGE_SECTION_RE.search(body or "")
    return match.group(0).strip() if match else ""


def _insert_triage_section(body, section):
    without = remove_triage_section(body).rstrip()
    marker = "\n### Recommended action"
    idx = without.find(marker)
    if idx >= 0:
        return without[:idx].rstrip() + "\n\n" + section + "\n" + without[idx:]
    state_idx = without.rfind("<!-- wheelhouse-state:")
    if state_idx >= 0:
        return (
            without[:state_idx].rstrip()
            + "\n\n"
            + section
            + "\n\n"
            + without[state_idx:]
        )
    return without + "\n\n" + section


def _replace_state_block(body, state):
    marker = "<!-- wheelhouse-state: %s -->" % json.dumps(
        state or {},
        separators=(",", ":"),
    )
    if _STATE_BLOCK_RE.search(body or ""):
        return _STATE_BLOCK_RE.sub(marker, body, count=1)
    return (body or "").rstrip() + "\n\n" + marker


def _preserve_same_head_triage(body, existing_body, item, old_state):
    head_sha = item.get("head_sha", "") or ""
    if not head_sha or (old_state or {}).get("head_sha") != head_sha:
        return body
    old_kind = (old_state or {}).get("kind")
    new_kind = item.get("kind", "pr-review")
    if old_kind != "pr-review" or new_kind != "pr-review":
        return body

    section = _existing_triage_section(existing_body)
    if section:
        body = _insert_triage_section(body, section)

    state = parse_state_block(body)
    if not state:
        return body
    changed = False
    for key in ("triaged_sha", "triage_status", "triage_error"):
        if key in (old_state or {}):
            state[key] = old_state[key]
            changed = True
    return _replace_state_block(body, state) if changed else body


def _state_with_triage(state, head_sha, status, error=None):
    new_state = dict(state or {})
    new_state["triaged_sha"] = head_sha
    new_state["triage_status"] = status
    if error:
        new_state["triage_error"] = _clean_triage_text(error, limit=220)
    else:
        new_state.pop("triage_error", None)
    return new_state


def body_with_triage_queued(body, item):
    state = parse_state_block(body)
    head_sha = item.get("head_sha", "") or ""
    if (
        not state
        or state.get("kind") != "pr-review"
        or state.get("head_sha") != head_sha
    ):
        return body
    clean = remove_triage_section(body)
    return _replace_state_block(clean, _state_with_triage(state, head_sha, "queued"))


def body_with_triage_result(body, head_sha, triage=None, error=None):
    state = parse_state_block(body)
    if (
        not state
        or state.get("kind") != "pr-review"
        or state.get("head_sha") != head_sha
    ):
        return body
    normalized = normalize_triage(triage)
    status = "succeeded" if normalized else "error"
    section = triage_section(normalized, error or TRIAGE_UNAVAILABLE)
    updated = _insert_triage_section(body, section)
    new_state = _state_with_triage(
        state, head_sha, status, None if normalized else error
    )
    return _replace_state_block(updated, new_state)


def render(item):
    """item -> {title, body, labels, marker}. Tolerates missing optional fields."""
    kind = item.get("kind", "pr-review")
    repo = item["repo"]
    number = int(item["number"])
    title = (item.get("title") or "").strip() or "(no title)"
    options = card_options(item)
    triage = normalize_triage(item.get("triage")) if kind == "pr-review" else None

    # The stored material set lets a refresh cheaply and deterministically decide
    # "did this materially change?".
    state = {
        "repo": repo,
        "number": number,
        "kind": kind,
        "head_sha": item.get("head_sha", "") or "",
        "options": options,
    }
    state.update({k: v for k, v in material_signature(item).items() if k != "options"})
    state["render_version"] = CARD_RENDER_VERSION
    if triage:
        state["triaged_sha"] = item.get("triaged_sha") or state["head_sha"]
        state["triage_status"] = "succeeded"

    short = title if len(title) <= 70 else title[:67] + "..."
    issue_title = "[%s#%d] %s" % (repo, number, short)

    lines = []
    lines.append(
        "## Decision needed - [%s#%d](%s)" % (repo, number, item.get("url", ""))
    )
    lines.append("")
    # Keep the author visible without a GitHub @mention; cards are the owner's
    # private queue and must not notify target contributors.
    meta = "**%s** by %s" % (KIND_LABEL.get(kind, kind), item.get("author", "?"))
    if item.get("bucket"):
        meta += " &middot; `%s`" % item["bucket"]
    lines.append(meta)
    lines.append("")
    lines.append("> %s" % title)
    lines.append("")
    lines.append("### Situation")
    lines.append("- Compliance: `%s`" % item.get("comp", "n/a"))
    lines.append("- Tests: `%s`" % item.get("tests", "n/a"))
    if item.get("summary"):
        lines.append("- Notes: %s" % item["summary"])
    lines.append("")
    # A security warning (e.g. a pull_request_target posture on a ci-approval
    # card) is surfaced as a prominent callout so the maintainer decides with
    # eyes open. Display-only - not part of the material refresh signature.
    if item.get("warning"):
        lines.append("> [!WARNING]")
        lines.append("> %s" % item["warning"])
        lines.append("")
    if triage:
        lines.append(triage_section(triage))
        lines.append("")
    lines.append("### Recommended action")
    lines.append(item.get("recommendation", "Needs your call."))
    lines.append("")
    lines.append("### Your decision")
    lines.append(
        "Tick **one** box for a quick call, or reply with a slash-command "
        "(%s):" % SLASH_HINT.get(kind, "`/close`, `/hold`")
    )
    lines.append("")
    for key in options:
        label = OPTION_LABELS.get(key, key)
        lines.append("- [ ] %s <!-- opt:%s -->" % (label, key))
    lines.append("")
    lines.append(
        "<sub>Only the repository owner can drive this decision - everyone "
        "else's edits and comments are ignored.</sub>"
    )
    lines.append("")
    lines.append(
        "<!-- wheelhouse-state: %s -->" % json.dumps(state, separators=(",", ":"))
    )
    body = "\n".join(lines)

    return {
        "title": issue_title,
        "body": body,
        "labels": card_labels(item),
        "marker": marker_label(item),
    }


# --------------------------------------------------------------------------- #
# gh card operations (ambient GH_TOKEN = default GITHUB_TOKEN)
# --------------------------------------------------------------------------- #
def _gh(args, check=True):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("gh %s failed: %s" % (" ".join(args), r.stderr.strip()))
    return r


def ensure_labels(labels):
    """Idempotently create the labels (gh issue create/edit needs them to exist)."""
    for label in labels:
        color = "ededed"
        if label == "needs-decision":
            color = "1d76db"
        elif label.startswith("priority:high"):
            color = "d93f0b"
        elif label.startswith("priority:"):
            color = "fbca04"
        elif label.startswith("kind:"):
            color = "5319e7"
        elif label.startswith("repo:"):
            color = "0e8a16"
        _gh(["label", "create", label, "--force", "--color", color], check=False)


def find_card(marker):
    """Find the open card for this target. Returns {number, body, labels} (the
    full row, so the caller can diff state + labels without a second fetch), or
    None if no open card exists."""
    r = _gh(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            marker,
            "--json",
            "number,body,labels",
            "--limit",
            "5",
        ]
    )
    arr = json.loads(r.stdout or "[]")
    return arr[0] if arr else None


def get_card(number):
    r = _gh(
        ["issue", "view", str(number), "--json", "number,body,labels,state"],
        check=False,
    )
    if r.returncode != 0:
        return None
    return json.loads(r.stdout or "{}") or None


def issue_is_open(issue):
    return str((issue or {}).get("state", "OPEN")).upper() == "OPEN"


def _write_body(body):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
        return f.name


def _edit_issue_body(number, body):
    body_path = _write_body(body)
    try:
        _gh(["issue", "edit", str(number), "--body-file", body_path])
    finally:
        os.unlink(body_path)


def mark_triage_queued(number, item, body):
    """Cache an auto-triage attempt for this head before dispatching the LLM.

    This is intentionally a hidden state update only. It bounds spend even if
    the asynchronous workflow fails before it can write a visible result.
    """
    new_body = body_with_triage_queued(body, item)
    if new_body == body:
        return False
    _edit_issue_body(number, new_body)
    return True


def dispatch_triage_workflow(number, item):
    _gh(
        [
            "workflow",
            "run",
            "triage.yml",
            "-f",
            "issue=%s" % number,
            "-f",
            "repo=%s" % item["repo"],
            "-f",
            "number=%s" % item["number"],
            "-f",
            "head_sha=%s" % (item.get("head_sha") or ""),
        ]
    )


def update_card_triage(number, head_sha, triage=None, error=None):
    card = get_card(number)
    if not card or not issue_is_open(card) or not is_refreshable(card.get("labels")):
        return False
    body = card.get("body", "")
    new_body = body_with_triage_result(body, head_sha, triage=triage, error=error)
    if new_body == body:
        return False
    _edit_issue_body(number, new_body)
    return True


def _create_card(card):
    body_path = _write_body(card["body"])
    try:
        args = ["issue", "create", "--title", card["title"], "--body-file", body_path]
        for label in card["labels"]:
            args += ["--label", label]
        r = _gh(args)
        url = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        print("created card %s for %s" % (url or "?", card["marker"]))
        return url
    finally:
        os.unlink(body_path)


def _refresh_card(number, card, existing, item, old_state):
    """Re-render an existing card's body in place and REPLACE its managed labels.
    If the target's head moved, drop a short comment so the owner sees a
    re-review is warranted rather than being silently swapped underneath."""
    to_add, to_remove = plan_label_update(card["labels"], existing.get("labels"))
    card = dict(card)
    card["body"] = _preserve_same_head_triage(
        card["body"],
        existing.get("body", ""),
        item,
        old_state,
    )
    body_path = _write_body(card["body"])
    try:
        args = ["issue", "edit", str(number), "--body-file", body_path]
        for label in to_add:
            args += ["--add-label", label]
        for label in to_remove:
            args += ["--remove-label", label]
        _gh(args)
    finally:
        os.unlink(body_path)

    old_sha = (old_state or {}).get("head_sha", "") or ""
    new_sha = item.get("head_sha", "") or ""
    if old_sha and new_sha and old_sha != new_sha:
        _gh(
            [
                "issue",
                "comment",
                str(number),
                "--body",
                "Target updated: head moved from `%s` to `%s`. Re-rendered this card "
                "with current state - a fresh review is warranted."
                % (old_sha[:8], new_sha[:8]),
            ],
            check=False,
        )
    churn = (
        " (+%d/-%d labels)" % (len(to_add), len(to_remove))
        if (to_add or to_remove)
        else ""
    )
    print("refreshed card #%s for %s%s" % (number, card["marker"], churn))
    return number


def upsert_card(item, existing=None):
    """Create a new card, or refresh the existing one for this target in place.

    Refresh rules (see AGENTS.md "Card refresh"):
      * Only a pure `needs-decision` card is refreshed; a card already
        `processing`/`resolved`/`blocked` is left untouched (never rewrite a
        decision in flight - re-rendering the body would reset its checkboxes).
      * A refresh runs when a MATERIAL field changed OR the card's stored
        `render_version` is behind `CARD_RENDER_VERSION` (a one-time, self-
        terminating re-render for display-only fixes); a card that is neither
        is a full no-op (no body edit, no label churn, no comment).
      * On refresh the wheelhouse-managed labels (`repo:`/`kind:`/`priority:`/
        `target:`) are REPLACED so stale ones are removed, and a head-SHA change
        also drops a short "target updated" comment.

    Returns the issue number (or the created card's URL for a brand-new card)."""
    card = render(item)
    ensure_labels(card["labels"])
    known_number = (existing or {}).get("number")
    if known_number:
        existing = get_card(known_number)
        if not existing or not issue_is_open(existing):
            print(
                "skip card #%s for %s: card no longer open"
                % (known_number, card["marker"])
            )
            return known_number
    else:
        existing = find_card(card["marker"])
    if not existing:
        return _create_card(card)

    number = existing["number"]
    if not is_refreshable(existing.get("labels")):
        print(
            "skip card #%s for %s: decision in flight (not pure needs-decision)"
            % (number, card["marker"])
        )
        return number
    old_state = parse_state_block(existing.get("body", ""))
    if not material_changed(item, old_state) and not render_stale(old_state):
        print("skip card #%s for %s: no material change" % (number, card["marker"]))
        return number
    return _refresh_card(number, card, existing, item, old_state)


def close_card(number, message, label="resolved"):
    ensure_labels([label])
    _gh(["issue", "comment", str(number), "--body", message], check=False)
    _gh(
        [
            "issue",
            "edit",
            str(number),
            "--add-label",
            label,
            "--remove-label",
            "needs-decision",
        ],
        check=False,
    )
    _gh(["issue", "close", str(number)], check=False)


def _text_from_content(content):
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if (
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ):
            text = item["text"].strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def extract_claude_result(path):
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            events = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(events, list):
        return ""

    for event in reversed(events):
        if (
            isinstance(event, dict)
            and event.get("type") == "result"
            and not event.get("is_error")
            and isinstance(event.get("result"), str)
            and event["result"].strip()
        ):
            return event["result"].strip()

    for event in reversed(events):
        if isinstance(event, dict) and event.get("type") == "assistant":
            message = event.get("message")
            if isinstance(message, dict):
                text = _text_from_content(message.get("content"))
                if text:
                    return text
    return ""


def parse_triage_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except (TypeError, ValueError):
            return None
    triage = normalize_triage(data)
    if not triage:
        return None
    return triage


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def load_item(path):
    with open(path) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("upsert")
    up.add_argument("--item-file", required=True)

    rd = sub.add_parser("render")
    rd.add_argument("--item-file", required=True)
    rd.add_argument("--out-dir", required=True)

    ta = sub.add_parser("triage-apply")
    ta.add_argument("--issue", required=True)
    ta.add_argument("--head-sha", required=True)
    ta.add_argument("--execution-file", required=True)

    tf = sub.add_parser("triage-fail")
    tf.add_argument("--issue", required=True)
    tf.add_argument("--head-sha", required=True)
    tf.add_argument("--message", default=TRIAGE_UNAVAILABLE)

    qt = sub.add_parser("queue-triage")
    qt.add_argument("--item-file", required=True)

    args = ap.parse_args()

    if args.cmd == "upsert":
        item = load_item(args.item_file)
        upsert_card(item)
    elif args.cmd == "render":
        item = load_item(args.item_file)
        card = render(item)
        os.makedirs(args.out_dir, exist_ok=True)
        with open(os.path.join(args.out_dir, "title"), "w") as f:
            f.write(card["title"])
        with open(os.path.join(args.out_dir, "body.md"), "w") as f:
            f.write(card["body"])
        with open(os.path.join(args.out_dir, "labels"), "w") as f:
            f.write("\n".join(card["labels"]))
        with open(os.path.join(args.out_dir, "marker"), "w") as f:
            f.write(card["marker"])
        print(card["title"])
    elif args.cmd == "triage-apply":
        result_text = extract_claude_result(args.execution_file)
        triage = parse_triage_json(result_text)
        if triage:
            if update_card_triage(args.issue, args.head_sha, triage=triage):
                print("updated auto triage on card #%s" % args.issue)
            else:
                print("auto triage result skipped for card #%s" % args.issue)
        else:
            print("::warning::auto triage produced no valid structured result")
            update_card_triage(args.issue, args.head_sha, error=TRIAGE_UNAVAILABLE)
    elif args.cmd == "triage-fail":
        print("::warning::auto triage failed: %s" % _clean_triage_text(args.message))
        update_card_triage(args.issue, args.head_sha, error=args.message)
    elif args.cmd == "queue-triage":
        try:
            item = load_item(args.item_file)
            card = find_card(marker_label(item))
            if not card:
                print("auto triage skipped: no open card for %s" % marker_label(item))
                return
            current = get_card(card["number"])
            if not current or not issue_is_open(current):
                print("auto triage skipped: card no longer open")
                return
            state = parse_state_block(current.get("body", ""))
            if not should_auto_triage(
                item, state, current.get("labels"), has_token=True
            ):
                print("auto triage skipped for card #%s" % current["number"])
                return
            if mark_triage_queued(current["number"], item, current.get("body", "")):
                dispatch_triage_workflow(current["number"], item)
                print("queued auto triage for card #%s" % current["number"])
        except Exception as e:
            item = locals().get("item") or {}
            print(
                "::warning::failed to queue auto triage for %s#%s: %s"
                % (item.get("repo", "?"), item.get("number", "?"), str(e)[:160])
            )


if __name__ == "__main__":
    main()
