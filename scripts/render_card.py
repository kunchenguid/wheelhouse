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
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wheelhouse_core import parse_state_block  # noqa: E402

# Quick-decision (checkbox) option keys per kind. Comment / decline carry text,
# so they are slash-command-only (see apply_decision.py), not checkboxes.
CHECKBOX_OPTIONS = {
    "pr-review": ["merge", "close", "hold"],
    "ci-approval": ["approve-ci", "close", "hold"],
    "issue-triage": ["close", "hold"],
}

OPTION_LABELS = {
    "merge": "Merge it",
    "approve-ci": "Approve the CI run (security-gated)",
    "close": "Close / decline",
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


def _label_names(labels):
    """Normalize a `gh ... --json labels` list (objects) or a plain string list
    into a set of label names."""
    return {label if isinstance(label, str) else label.get("name", "")
            for label in (labels or [])}


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


def render(item):
    """item -> {title, body, labels, marker}. Tolerates missing optional fields."""
    kind = item.get("kind", "pr-review")
    repo = item["repo"]
    number = int(item["number"])
    title = (item.get("title") or "").strip() or "(no title)"
    options = card_options(item)

    # The stored material set lets a refresh cheaply and deterministically decide
    # "did this materially change?".
    state = {
        "repo": repo,
        "number": number,
        "kind": kind,
        "head_sha": item.get("head_sha", "") or "",
        "options": options,
    }
    state.update({k: v for k, v in material_signature(item).items()
                  if k != "options"})

    short = title if len(title) <= 70 else title[:67] + "..."
    issue_title = "[%s#%d] %s" % (repo, number, short)

    lines = []
    lines.append("## Decision needed - [%s#%d](%s)" % (repo, number, item.get("url", "")))
    lines.append("")
    meta = "**%s** by @%s" % (KIND_LABEL.get(kind, kind), item.get("author", "?"))
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
    lines.append("### Recommended action")
    lines.append(item.get("recommendation", "Needs your call."))
    lines.append("")
    lines.append("### Your decision")
    lines.append("Tick **one** box for a quick call, or reply with a slash-command "
                 "(%s):" % SLASH_HINT.get(kind, "`/close`, `/hold`"))
    lines.append("")
    for key in options:
        label = OPTION_LABELS.get(key, key)
        lines.append("- [ ] %s <!-- opt:%s -->" % (label, key))
    lines.append("")
    lines.append("<sub>Only the repository owner can drive this decision - everyone "
                 "else's edits and comments are ignored.</sub>")
    lines.append("")
    lines.append("<!-- wheelhouse-state: %s -->" % json.dumps(state, separators=(",", ":")))
    body = "\n".join(lines)

    return {"title": issue_title, "body": body, "labels": card_labels(item),
            "marker": marker_label(item)}


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
    r = _gh(["issue", "list", "--state", "open", "--label", marker,
             "--json", "number,body,labels", "--limit", "5"])
    arr = json.loads(r.stdout or "[]")
    return arr[0] if arr else None


def get_card(number):
    r = _gh(["issue", "view", str(number), "--json", "number,body,labels,state"],
            check=False)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout or "{}") or None


def issue_is_open(issue):
    return str((issue or {}).get("state", "OPEN")).upper() == "OPEN"


def _write_body(body):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
        return f.name


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
        _gh(["issue", "comment", str(number), "--body",
             "Target updated: head moved from `%s` to `%s`. Re-rendered this card "
             "with current state - a fresh review is warranted."
             % (old_sha[:8], new_sha[:8])], check=False)
    churn = " (+%d/-%d labels)" % (len(to_add), len(to_remove)) if (to_add or to_remove) else ""
    print("refreshed card #%s for %s%s" % (number, card["marker"], churn))
    return number


def upsert_card(item, existing=None):
    """Create a new card, or refresh the existing one for this target in place.

    Refresh rules (see AGENTS.md "Card refresh"):
      * Only a pure `needs-decision` card is refreshed; a card already
        `processing`/`resolved`/`blocked` is left untouched (never rewrite a
        decision in flight - re-rendering the body would reset its checkboxes).
      * A refresh runs only when a MATERIAL field changed; an unchanged card is a
        full no-op (no body edit, no label churn, no comment).
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
            print("skip card #%s for %s: card no longer open"
                  % (known_number, card["marker"]))
            return known_number
    else:
        existing = find_card(card["marker"])
    if not existing:
        return _create_card(card)

    number = existing["number"]
    if not is_refreshable(existing.get("labels")):
        print("skip card #%s for %s: decision in flight (not pure needs-decision)"
              % (number, card["marker"]))
        return number
    old_state = parse_state_block(existing.get("body", ""))
    if not material_changed(item, old_state):
        print("skip card #%s for %s: no material change" % (number, card["marker"]))
        return number
    return _refresh_card(number, card, existing, item, old_state)


def close_card(number, message, label="resolved"):
    ensure_labels([label])
    _gh(["issue", "comment", str(number), "--body", message], check=False)
    _gh(["issue", "edit", str(number), "--add-label", label,
         "--remove-label", "needs-decision"], check=False)
    _gh(["issue", "close", str(number)], check=False)


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

    args = ap.parse_args()
    item = load_item(args.item_file)

    if args.cmd == "upsert":
        upsert_card(item)
    elif args.cmd == "render":
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


if __name__ == "__main__":
    main()
