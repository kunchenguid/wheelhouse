#!/usr/bin/env python3
"""
Wheelhouse - decision-card renderer + card operations.

`render(item)` turns one classified item into a decision card: a human-readable
body with quick-decision checkboxes (or a held auto-triage placeholder) and a
hidden machine-readable state block.
`upsert_card`/`close_card` create/refresh/consume cards in THIS repo (via the
ambient GH_TOKEN, which the workflow sets to the default GITHUB_TOKEN so that
card-side activity never re-triggers the handler).

When auto triage is enabled (`should_hold`), a brand-new pr-review/issue-
triage card is created HELD - `pending-triage` on top of `needs-decision`, a
placeholder body with no checkboxes - and published to its normal actionable
form by `update_card_triage` the moment its first auto-triage attempt
completes, success or failure alike. See "Held cards" above `HOLD_LABEL`.

CLI:
  render_card.py upsert --item-file item.json    create-or-refresh a card (dedup by marker)
  render_card.py render --item-file item.json --out-dir DIR    debug: write title/body/labels
  render_card.py queue-triage --item-file item.json [--issue N]    mark triage queued and dispatch triage.yml when eligible
  render_card.py triage-apply --issue N --revision REV --execution-file FILE    update the card from Claude output
  render_card.py triage-fail --issue N --revision REV --message TEXT    write the auto-triage unavailable section
  render_card.py triage-recover --issue N --kind KIND --revision REV    fail-open safety net: publish a held card still stuck "queued" for REV

REV is a PR's head SHA (pr-review) or an issue's `updatedAt` (issue-triage) -
whichever revision the auto-triage cache is keyed on for that card's kind.
When `upsert` runs under GitHub Actions it writes `issue=N` to `$GITHUB_OUTPUT`;
pass that number to `queue-triage --issue N` so a newly-created card is read
back by number instead of through the read-after-write-racy label listing.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wheelhouse_core import parse_state_block, qualify_issue_refs  # noqa: E402

# Quick-decision (checkbox) option keys per kind. Comment / decline /
# request-changes carry text, so they are slash-command-only (see
# apply_decision.py), not checkboxes.
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
    "pr-review": (
        "`/merge`, `/close`, `/decline <reason>`, `/hold`, `/comment <text>`, "
        "`/request-changes <text>`"
    ),
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
# decision in flight (`processing`), the card is consumed (`resolved`), or the
# owner parked it (`blocked`, via the `/hold` decision). Re-rendering the body
# resets its checkboxes, which would clobber an in-progress decision or race
# the decision-handler - so a refresh SKIPS a card with any of these. Only a
# pure `needs-decision` card is refreshed.
NON_REFRESHABLE_LABELS = frozenset({"processing", "resolved", "blocked"})

# A held card (see "Held cards" below) ALSO carries `needs-decision` and is
# therefore refreshable/triage-eligible like any other pure pending card -
# `HOLD_LABEL` is deliberately absent from `NON_REFRESHABLE_LABELS` because
# triage.yml's resolve step requires `needs-decision` to still be a pure,
# refreshable card in order to run at all (see `should_hold`/`update_card_triage`).
#
# --------------------------------------------------------------------------- #
# Held cards (visibility gated on the first auto-triage attempt completing)
# --------------------------------------------------------------------------- #
# When a brand-new pr-review/issue-triage card is eligible for auto triage
# (`should_hold`), it is created HELD: `needs-decision` stays (triage.yml needs
# it), `HOLD_LABEL` is added on top, and the body's "Your decision" section is
# a placeholder with no checkboxes (`_held_decision_lines` - no `<!-- opt:* -->`
# markers, so it is inert to the decision handler; see `cmd_parse`/
# `cmd_nl_eligible` in apply_decision.py, which also short-circuit on the
# state block's `held` flag as defense in depth). This is a deliberately
# DIFFERENT concept from the `/hold` decision action (which parks a card under
# the `blocked` label) - do not conflate the two.
#
# A held card is published - checkboxes appear, `HOLD_LABEL` is removed - the
# moment its own auto-triage ATTEMPT completes, via `update_card_triage`
# (called by both `triage-apply` on success and `triage-fail` on error/
# timeout - fail-open by construction, never gated on triage succeeding).
# Publishing is keyed to the card's own current revision
# (`state_revision`/`triage_revision`): if the card was refreshed to a newer
# revision while the attempt was in flight, that stale attempt's completion is
# a no-op (the fresh revision's own queued attempt - `should_auto_triage`
# always requeues on a revision change - will publish the card when it
# completes instead), exactly mirroring how a stale triage result is already
# dropped for a published card.
#
# `held` is carried as a non-material key in the state block (like
# `triaged_sha`/`triage_status`): it is never in `MATERIAL_FIELDS` and never
# affects classify/material_changed/decision-parsing/merge-close-approve/
# fork-CI-safety/author-filtering/conflict-routing. `HOLD_LABEL` is a display/
# filtering label kept in sync with it (added by `card_labels` whenever
# `render()` is called with `held=True`), never read back as the source of
# truth - `state["held"]` is. A refresh preserves held-ness only while the
# refreshed item still qualifies for auto triage; otherwise it renders the card
# actionable in the same refresh. `update_card_triage` publishes held cards when
# an auto-triage attempt completes.
HOLD_LABEL = "pending-triage"
SYNCED_EXACT_LABELS = frozenset({HOLD_LABEL})

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
# Bumped 1 -> 2 to retroactively re-qualify already-cached `### Triage`
# sections (bare `#N` -> `owner/repo#N`) via `_preserve_same_revision_triage`,
# mirroring how version 0 -> 1 propagated the author `@mention` drop.
CARD_RENDER_VERSION = 2

TRIAGE_FIELDS = ("summary", "product_implications", "recommended_next_step")
TRIAGE_START = "<!-- wheelhouse-triage:start -->"
TRIAGE_END = "<!-- wheelhouse-triage:end -->"
TRIAGE_UNAVAILABLE = "Auto triage unavailable for this version."

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


def card_labels(item, held=False):
    labels = [
        "needs-decision",
        "repo:%s" % item["repo"],
        "kind:%s" % item["kind"],
        "priority:%s" % item.get("priority", "low"),
        marker_label(item),
    ]
    if held:
        labels.append(HOLD_LABEL)
    return labels


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
    display-only or card-body repair fixes (e.g. dropping the author @mention
    or re-qualifying cached triage refs) that have no material-field trigger.
    A missing `render_version` (a card written before
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


def held_publish_needed(item, state, has_token):
    return bool((state or {}).get("held")) and not should_hold(item, has_token)


def refresh_needed(item, state, has_token=False):
    return (
        material_changed(item, state)
        or render_stale(state)
        or held_publish_needed(item, state, has_token)
    )


# Auto-triage caches against a per-kind revision: a PR's `head_sha`, or an
# issue's `updatedAt` (issues have no head SHA, and `updatedAt` advances on any
# edit or new comment). For PRs, `head_sha` is also a material refresh field; for
# issues, `updated_at` is deliberately non-material and gates only the triage side
# job. Each kind is gated by its OWN independent config flag so turning one off
# never affects the other.
AUTO_TRIAGE_FLAG_BY_KIND = {
    "pr-review": "auto_triage",
    "issue-triage": "auto_triage_issues",
}


def triage_revision(item):
    """The freshness key auto-triage caches against for this item's kind."""
    if item.get("kind") == "issue-triage":
        return item.get("updated_at", "") or ""
    return item.get("head_sha", "") or ""


def state_revision(state, kind):
    """The card's stored freshness key for `kind` (the counterpart of
    `triage_revision` read back off a parsed state block)."""
    if kind == "issue-triage":
        return (state or {}).get("updated_at", "") or ""
    return (state or {}).get("head_sha", "") or ""


def _parse_issue_revision(value):
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _issue_revision_is_older(revision, state):
    stored = state_revision(state, "issue-triage")
    if not revision or not stored:
        return False
    incoming = _parse_issue_revision(revision)
    current = _parse_issue_revision(stored)
    return bool(incoming and current and incoming < current)


def triage_fresh(item, state):
    """True when the card has already attempted auto-triage for this item's
    current revision (a PR's head SHA, or an issue's `updatedAt`).

    `triaged_sha` is a cost-control cache, not a material refresh field. It is
    written before the workflow dispatch so a failed or timed-out workflow does
    not get re-run every hourly scan for the same revision.
    """
    revision = triage_revision(item)
    return bool(revision and (state or {}).get("triaged_sha") == revision)


def triage_queued_for_head(state, revision):
    return bool(
        revision
        and (state or {}).get("triaged_sha") == revision
        and (state or {}).get("triage_status") == "queued"
    )


def should_hold(item, has_token):
    """Whether a BRAND-NEW card for this item should be created HELD - a
    placeholder body with no decision checkboxes, pending its first auto-
    triage attempt (see "Held cards" above).

    Gated on exactly the same enablement this item would need to have triage
    queued at all: the per-kind flag (`auto_triage`/`auto_triage_issues`) plus
    token presence, and a resolvable revision to cache against. A brand-new
    card has no state/labels yet, so this omits the `is_refreshable`/
    freshness checks `should_auto_triage` does for an EXISTING card."""
    if not has_token:
        return False
    kind = item.get("kind", "pr-review")
    flag = AUTO_TRIAGE_FLAG_BY_KIND.get(kind)
    if flag is None:
        return False
    if item.get(flag, True) is False:
        return False
    return bool(triage_revision(item))


def should_auto_triage(item, state, labels, has_token=True):
    """Whether this card should queue the lightweight automatic triage.

    pr-review cards are gated by `auto_triage`; issue-triage cards are gated
    by the INDEPENDENT `auto_triage_issues`. No other kind ever auto-triages."""
    if not should_hold(item, has_token):
        return False
    if not is_refreshable(labels):
        return False
    kind = item.get("kind", "pr-review")
    revision = triage_revision(item)
    if kind == "issue-triage" and _issue_revision_is_older(revision, state):
        return False
    return not triage_fresh(item, state)


def auto_triage_has_token():
    """Whether `CLAUDE_CODE_OAUTH_TOKEN` is configured, per the workflow-set
    `WHEELHOUSE_AUTO_TRIAGE_HAS_TOKEN` env var (secrets aren't readable from a
    script directly). Shared by `reconcile.py` and the `upsert`/`queue-triage`
    CLI commands so held-card gating and triage-queueing gating read the same
    signal."""
    return os.environ.get("WHEELHOUSE_AUTO_TRIAGE_HAS_TOKEN", "").lower() == "true"


def _label_names(labels):
    """Normalize a `gh ... --json labels` list (objects) or a plain string list
    into a set of label names."""
    return {
        label if isinstance(label, str) else label.get("name", "")
        for label in (labels or [])
    }


def is_refreshable(labels):
    """A card is refreshable only while it has `needs-decision` and no
    in-flight or terminal label. `pending-triage` is allowed because held cards
    must still refresh, auto-triage, and self-heal."""
    names = _label_names(labels)
    return "needs-decision" in names and names.isdisjoint(NON_REFRESHABLE_LABELS)


def plan_label_update(desired, current):
    """Plan a true label replace of Wheelhouse-owned labels."""
    current_names = _label_names(current)
    desired_set = set(desired)
    managed_now = {n for n in current_names if n.startswith(MANAGED_LABEL_PREFIXES)}
    synced_now = current_names.intersection(SYNCED_EXACT_LABELS)
    to_add = [label for label in desired if label not in current_names]
    to_remove = sorted((managed_now | synced_now) - desired_set)
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


def triage_section(triage=None, error=None, owner="", repo=""):
    """Render the visible `### Triage` block. `owner`+`repo` (the TARGET slug
    from deterministic card state, never from the model) qualify any bare
    `#N` cross-repo reference in the model's triage text so it does not
    autolink to this CARDS repo instead of the target."""
    lines = [TRIAGE_START, "### Triage", ""]
    if triage:
        lines.append(
            "- **Summary:** %s" % qualify_issue_refs(triage["summary"], owner, repo)
        )
        lines.append(
            "- **Product implications:** %s"
            % qualify_issue_refs(triage["product_implications"], owner, repo)
        )
        lines.append(
            "- **Recommended next step:** %s"
            % qualify_issue_refs(triage["recommended_next_step"], owner, repo)
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


def _preserve_same_revision_triage(body, existing_body, item, old_state, owner=""):
    """Lift the existing `### Triage` section onto a same-revision refresh
    without spending a new triage attempt.

    Before reinserting it, re-qualify any bare `#N` cross-repo ref it carries.
    `owner` is always `GITHUB_REPOSITORY_OWNER`; the target repo name comes from
    the card's deterministic `old_state["repo"]` (falling back to the item),
    never from the cached triage text itself - same trust rule as fresh triage
    rendering."""
    kind = item.get("kind", "pr-review")
    if kind not in AUTO_TRIAGE_FLAG_BY_KIND:
        return body
    if (old_state or {}).get("kind") != kind:
        return body
    revision = triage_revision(item)
    if not revision or state_revision(old_state, kind) != revision:
        return body

    section = _existing_triage_section(existing_body)
    if section:
        repo = (old_state or {}).get("repo") or item.get("repo", "")
        section = qualify_issue_refs(section, owner, repo)
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


def _state_with_triage(state, revision, status, error=None):
    new_state = dict(state or {})
    new_state["triaged_sha"] = revision
    new_state["triage_status"] = status
    if error:
        new_state["triage_error"] = _clean_triage_text(error, limit=220)
    else:
        new_state.pop("triage_error", None)
    return new_state


def body_with_triage_queued(body, item):
    state = parse_state_block(body)
    kind = item.get("kind", "pr-review")
    revision = triage_revision(item)
    if not state or kind not in AUTO_TRIAGE_FLAG_BY_KIND or state.get("kind") != kind:
        return body
    if not revision:
        return body
    if kind == "issue-triage":
        if _issue_revision_is_older(revision, state):
            return body
        state = dict(state)
        state["updated_at"] = revision
    elif state_revision(state, kind) != revision:
        return body
    clean = remove_triage_section(body)
    return _replace_state_block(clean, _state_with_triage(state, revision, "queued"))


def body_with_triage_result(body, revision, triage=None, error=None, owner=""):
    state = parse_state_block(body)
    kind = (state or {}).get("kind") if state else None
    if (
        not state
        or kind not in AUTO_TRIAGE_FLAG_BY_KIND
        or state_revision(state, kind) != revision
    ):
        return body
    normalized = normalize_triage(triage)
    status = "succeeded" if normalized else "error"
    section = triage_section(
        normalized, error or TRIAGE_UNAVAILABLE, owner=owner, repo=state.get("repo", "")
    )
    updated = _insert_triage_section(body, section)
    new_state = _state_with_triage(
        state, revision, status, None if normalized else error
    )
    return _replace_state_block(updated, new_state)


DECISION_START = "<!-- wheelhouse-decision:start -->"
DECISION_END = "<!-- wheelhouse-decision:end -->"
_DECISION_SECTION_RE = re.compile(
    r"<!--\s*wheelhouse-decision:start\s*-->.*?<!--\s*wheelhouse-decision:end\s*-->",
    re.S,
)


def _decision_lines(kind, options):
    lines = [
        "### Your decision",
        "",
        "Tick **one** box for a quick call, or reply with a slash-command "
        "(%s):" % SLASH_HINT.get(kind, "`/close`, `/hold`"),
        "",
    ]
    for key in options:
        label = OPTION_LABELS.get(key, key)
        lines.append("- [ ] %s <!-- opt:%s -->" % (label, key))
    lines.append("")
    lines.append(
        "<sub>Only the repository owner can drive this decision - everyone "
        "else's edits and comments are ignored.</sub>"
    )
    return lines


def _held_decision_lines():
    """The placeholder "Your decision" content for a held card: no checkboxes
    (no `<!-- opt:* -->` markers), so it is inert to the decision handler."""
    return [
        "### Your decision",
        "",
        "_Automatic triage is still running for this card. A decision to "
        "make will appear here once it finishes - triage succeeding or "
        "failing both unlock this card, so this is never a permanent wait._",
    ]


def _decision_section(kind, options, held):
    inner = _held_decision_lines() if held else _decision_lines(kind, options)
    return "\n".join([DECISION_START] + inner + [DECISION_END])


def _publish_decision_section(body, kind, options):
    """Replace a held card's placeholder "Your decision" block with the real
    checkboxes, in place. A no-op (returns `body` unchanged) if the markers
    are missing, e.g. a pre-feature card that was never held."""
    section = _decision_section(kind, options, held=False)
    new_body, count = _DECISION_SECTION_RE.subn(section.replace("\\", "\\\\"), body or "", count=1)
    return new_body if count else body


def render(item, held=False):
    """item -> {title, body, labels, marker}. Tolerates missing optional fields.

    `held=True` renders the placeholder "Held cards" form (see the module-
    level comment above `HOLD_LABEL`): the state block carries `held: true`
    and the "Your decision" section has no checkboxes."""
    kind = item.get("kind", "pr-review")
    repo = item["repo"]
    number = int(item["number"])
    title = (item.get("title") or "").strip() or "(no title)"
    options = card_options(item)
    triage = (
        normalize_triage(item.get("triage"))
        if kind in AUTO_TRIAGE_FLAG_BY_KIND
        else None
    )

    # The stored material set lets a refresh cheaply and deterministically decide
    # "did this materially change?". `updated_at` is non-material (never added to
    # MATERIAL_FIELDS) - it exists purely as the issue-triage auto-triage cache key,
    # mirroring how `head_sha` doubles as the pr-review cache key.
    state = {
        "repo": repo,
        "number": number,
        "kind": kind,
        "head_sha": item.get("head_sha", "") or "",
        "updated_at": item.get("updated_at", "") or "",
        "options": options,
    }
    state.update({k: v for k, v in material_signature(item).items() if k != "options"})
    state["render_version"] = CARD_RENDER_VERSION
    if held:
        state["held"] = True
    if triage:
        state["triaged_sha"] = item.get("triaged_sha") or triage_revision(item)
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
        owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
        lines.append(triage_section(triage, owner=owner, repo=repo))
        lines.append("")
    lines.append("### Recommended action")
    lines.append(item.get("recommendation", "Needs your call."))
    lines.append("")
    lines.append(_decision_section(kind, options, held))
    lines.append("")
    lines.append(
        "<!-- wheelhouse-state: %s -->" % json.dumps(state, separators=(",", ":"))
    )
    body = "\n".join(lines)

    return {
        "title": issue_title,
        "body": body,
        "labels": card_labels(item, held),
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
        elif label == HOLD_LABEL:
            color = "bfdadc"
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
    None if no open card exists.

    Do not use this to read back a card just created in the same pass; the
    underlying label-filtered issue listing is not read-after-write consistent.
    Use the issue number returned by `upsert_card` with `get_card` instead."""
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


def _edit_issue_body(number, body, remove_labels=None):
    body_path = _write_body(body)
    try:
        args = ["issue", "edit", str(number), "--body-file", body_path]
        for label in remove_labels or []:
            args += ["--remove-label", label]
        _gh(args)
    finally:
        os.unlink(body_path)


def mark_triage_queued(number, item, body):
    """Cache an auto-triage attempt for this revision before dispatching the LLM.

    This is intentionally a hidden state update only. It bounds spend even if
    the asynchronous workflow fails before it can write a visible result.
    """
    new_body = body_with_triage_queued(body, item)
    if new_body == body:
        return False
    _edit_issue_body(number, new_body)
    return True


def _body_without_queued_triage(body, revision):
    state = parse_state_block(body)
    if not state or not triage_queued_for_head(state, revision):
        return body
    new_state = dict(state)
    for key in ("triaged_sha", "triage_status", "triage_error"):
        new_state.pop(key, None)
    return _replace_state_block(remove_triage_section(body), new_state)


def clear_triage_queued(number, revision):
    card = get_card(number)
    if not card or not issue_is_open(card) or not is_refreshable(card.get("labels")):
        return False
    body = card.get("body", "")
    new_body = _body_without_queued_triage(body, revision)
    if new_body == body:
        return False
    _edit_issue_body(number, new_body)
    return True


def dispatch_triage_workflow(number, item):
    kind = item.get("kind", "pr-review")
    args = [
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
        "kind=%s" % kind,
    ]
    if kind == "issue-triage":
        args += ["-f", "revision=%s" % (item.get("updated_at") or "")]
    else:
        args += ["-f", "head_sha=%s" % (item.get("head_sha") or "")]
    _gh(args)


def publish_dispatch_failure(number, revision, message, owner=""):
    try:
        if update_card_triage(number, revision, error=message, owner=owner):
            return True
    except Exception as e:
        try:
            if clear_triage_queued(number, revision):
                raise RuntimeError(
                    "failed to publish dispatch-failure note; "
                    "cleared queued triage cache for retry: %s" % e
                ) from e
        except Exception as clear_error:
            if isinstance(clear_error, RuntimeError):
                raise
            raise RuntimeError(
                "failed to publish dispatch-failure note and failed to clear "
                "queued triage cache: %s; clear failed: %s" % (e, clear_error)
            ) from clear_error
        raise
    if clear_triage_queued(number, revision):
        raise RuntimeError(
            "dispatch-failure note was not applied; cleared queued triage cache "
            "for retry"
        )
    return False


def update_card_triage(number, revision, triage=None, error=None, owner=""):
    """Attach a completed auto-triage attempt's result to its card.

    If the card is still HELD, this ALSO publishes it in the same edit: the
    placeholder "Your decision" section is replaced with the real checkboxes
    and `HOLD_LABEL` is removed - the card becomes actionable. This runs
    identically whether `triage` succeeded or `error` is set (a `triage-fail`
    call): publishing is gated on the ATTEMPT completing, never on it
    succeeding, so a held card can never stay hidden because triage errored
    or timed out (see "Held cards" above).

    Publishing only happens when this attempt's revision still matches the
    card's own current revision. A mismatch means the card was refreshed to a
    newer revision while this attempt was in flight; that refresh either kept a
    held placeholder for the newer revision and queued a fresh attempt, or
    published the card because auto triage was no longer eligible. This stale
    attempt is therefore a no-op rather than publishing outdated content."""
    card = get_card(number)
    if not card or not issue_is_open(card) or not is_refreshable(card.get("labels")):
        return False
    body = card.get("body", "")
    state = parse_state_block(body)
    if not state:
        return False
    kind = state.get("kind")
    held = bool(state.get("held"))
    remove_labels = []
    if held:
        if state_revision(state, kind) != revision:
            return False
        options = state.get("options") or CHECKBOX_OPTIONS.get(kind, ["close", "hold"])
        body = _publish_decision_section(body, kind, options)
        state = dict(state)
        state.pop("held", None)
        body = _replace_state_block(body, state)
        remove_labels.append(HOLD_LABEL)

    new_body = body_with_triage_result(
        body, revision, triage=triage, error=error, owner=owner
    )
    if new_body == body and not held:
        return False
    _edit_issue_body(number, new_body, remove_labels=remove_labels)
    return True


def _create_card(card):
    """Create the card and return its issue number.

    `gh issue create` returns the created issue's URL, and a label-filtered
    `gh issue list` (see `find_card`) is not read-after-write consistent right
    after creation - so callers that need the fresh card back MUST use this
    number (e.g. via `get_card`), never `find_card`, to avoid a race where the
    listing doesn't see the just-created issue yet."""
    body_path = _write_body(card["body"])
    try:
        args = ["issue", "create", "--title", card["title"], "--body-file", body_path]
        for label in card["labels"]:
            args += ["--label", label]
        r = _gh(args)
        url = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        print("created card %s for %s" % (url or "?", card["marker"]))
        try:
            return int(url.rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            return None
    finally:
        os.unlink(body_path)


def _refresh_card(number, card, existing, item, old_state, preserve_triage=True):
    """Re-render an existing card's body in place and REPLACE its managed labels.
    If the target's head moved, drop a short comment so the owner sees a
    re-review is warranted rather than being silently swapped underneath."""
    to_add, to_remove = plan_label_update(card["labels"], existing.get("labels"))
    card = dict(card)
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    if preserve_triage:
        card["body"] = _preserve_same_revision_triage(
            card["body"],
            existing.get("body", ""),
            item,
            old_state,
            owner=owner,
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


def upsert_card(item, existing=None, has_token=False):
    """Create a new card, or refresh the existing one for this target in place.

    `has_token` gates whether a BRAND-NEW eligible card is created HELD (see
    "Held cards" above / `should_hold`) - pass the same
    `CLAUDE_CODE_OAUTH_TOKEN`-presence signal used to gate whether auto triage
    is queued at all (`auto_triage_has_token()`). On refresh, a currently-held
    card stays held only if the refreshed item still passes `should_hold`.

    Refresh rules (see AGENTS.md "Card refresh"):
      * Only a pure `needs-decision` card is refreshed; a card already
        `processing`/`resolved`/`blocked` is left untouched (never rewrite a
        decision in flight - re-rendering the body would reset its checkboxes).
      * A refresh runs when a MATERIAL field changed, the card's stored
        `render_version` is behind `CARD_RENDER_VERSION` (a one-time, self-
        terminating re-render for display-only fixes and card-body repairs like
        cached triage ref qualification), or a held card must be published
        because auto triage is no longer eligible; a card with none of those
        triggers is a full no-op (no body edit, no label churn, no comment).
      * On refresh the wheelhouse-managed labels (`repo:`/`kind:`/`priority:`/
        `target:`) are REPLACED so stale ones are removed, and a head-SHA change
        also drops a short "target updated" comment. A held card whose refreshed
        item no longer qualifies for auto triage is rendered actionable in that
        same refresh.

    Always returns an int issue number (new or existing), or None if a
    brand-new card's number could not be parsed from `gh issue create`'s
    output. Callers needing the fresh card back MUST read it by this number
    (e.g. `get_card`/`current_card`) - a label-filtered `find_card` listing is
    not read-after-write consistent immediately after creation."""
    marker = marker_label(item)
    known_number = (existing or {}).get("number")
    if known_number:
        existing = get_card(known_number)
        if not existing or not issue_is_open(existing):
            print(
                "skip card #%s for %s: card no longer open" % (known_number, marker)
            )
            return known_number
    else:
        existing = find_card(marker)

    if not existing:
        card = render(item, held=should_hold(item, has_token))
        ensure_labels(card["labels"])
        return _create_card(card)

    number = existing["number"]
    if not is_refreshable(existing.get("labels")):
        print(
            "skip card #%s for %s: decision in flight (not pure needs-decision)"
            % (number, marker)
        )
        return number
    old_state = parse_state_block(existing.get("body", ""))
    publish_held = held_publish_needed(item, old_state, has_token)
    if not refresh_needed(item, old_state, has_token):
        print("skip card #%s for %s: no material change" % (number, marker))
        return number
    held = bool((old_state or {}).get("held")) and not publish_held
    card = render(item, held=held)
    ensure_labels(card["labels"])
    return _refresh_card(
        number,
        card,
        existing,
        item,
        old_state,
        preserve_triage=not publish_held,
    )


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
    ta.add_argument("--revision", required=True)
    ta.add_argument("--execution-file", required=True)

    tf = sub.add_parser("triage-fail")
    tf.add_argument("--issue", required=True)
    tf.add_argument("--revision", required=True)
    tf.add_argument("--message", default=TRIAGE_UNAVAILABLE)

    tr = sub.add_parser("triage-recover")
    tr.add_argument("--issue", required=True)
    tr.add_argument("--kind", required=True)
    tr.add_argument("--revision", required=True)
    tr.add_argument(
        "--message",
        default="Auto triage did not finish (the workflow run did not reach "
        "its update step).",
    )

    qt = sub.add_parser("queue-triage")
    qt.add_argument("--item-file", required=True)
    qt.add_argument(
        "--issue",
        default="",
        help="Known card issue number (e.g. from `upsert`'s output). When "
        "given, read the card by number instead of the read-after-write-"
        "racy find_card label listing.",
    )

    args = ap.parse_args()

    if args.cmd == "upsert":
        item = load_item(args.item_file)
        number = upsert_card(item, has_token=auto_triage_has_token())
        gh_output = os.environ.get("GITHUB_OUTPUT")
        if gh_output and number:
            with open(gh_output, "a") as f:
                f.write("issue=%s\n" % number)
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
        owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
        result_text = extract_claude_result(args.execution_file)
        triage = parse_triage_json(result_text)
        if triage:
            if update_card_triage(
                args.issue, args.revision, triage=triage, owner=owner
            ):
                print("updated auto triage on card #%s" % args.issue)
            else:
                print("auto triage result skipped for card #%s" % args.issue)
        else:
            print("::warning::auto triage produced no valid structured result")
            update_card_triage(
                args.issue, args.revision, error=TRIAGE_UNAVAILABLE, owner=owner
            )
    elif args.cmd == "triage-fail":
        owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
        print("::warning::auto triage failed: %s" % _clean_triage_text(args.message))
        update_card_triage(args.issue, args.revision, error=args.message, owner=owner)
    elif args.cmd == "triage-recover":
        # Last-resort fail-open safety net, run `always()` at the end of
        # triage.yml using the RAW workflow_dispatch inputs (never a `resolve`
        # step output, which may be empty if `resolve` itself failed before
        # writing outputs - e.g. a transient `gh issue view` error). Ground-
        # truths against the CURRENT card state rather than trusting any
        # earlier step's outcome: a no-op unless the card is STILL held and
        # STILL "queued" for exactly this revision, which only happens if
        # nothing upstream (triage-apply/triage-fail) ever ran for it. See
        # "Held cards" above - without this, a `resolve`-step failure would
        # leave a held card hidden forever, since its `triaged_sha` cache
        # already blocks every future scan from requeuing that revision.
        owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
        card = get_card(args.issue)
        if not card or not issue_is_open(card):
            print("recover: card no longer open, nothing to recover")
        else:
            state = parse_state_block(card.get("body", ""))
            if not state or not state.get("held"):
                print("recover: card already published (or not a decision card)")
            elif (
                state_revision(state, args.kind) != args.revision
                or state.get("triage_status") != "queued"
            ):
                print(
                    "recover: card is not stuck on this exact queued attempt "
                    "(a newer attempt already superseded or published it)"
                )
            else:
                print(
                    "::warning::auto triage run did not reach its update step "
                    "for card #%s - recovering by publishing it" % args.issue
                )
                update_card_triage(
                    args.issue,
                    args.revision,
                    error=args.message,
                    owner=owner,
                )
    elif args.cmd == "queue-triage":
        try:
            item = load_item(args.item_file)
            number = None
            if args.issue:
                try:
                    number = int(args.issue)
                except ValueError:
                    number = None
            if number:
                # Known number (e.g. threaded from `upsert`'s output): read the
                # card back by number, which is read-after-write consistent
                # immediately after creation - unlike the label-filtered
                # find_card listing below.
                current = get_card(number)
            else:
                card = find_card(marker_label(item))
                if not card:
                    print(
                        "auto triage skipped: no open card for %s" % marker_label(item)
                    )
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
            if not mark_triage_queued(current["number"], item, current.get("body", "")):
                return
        except Exception as e:
            item = locals().get("item") or {}
            print(
                "::warning::failed to queue auto triage for %s#%s: %s"
                % (item.get("repo", "?"), item.get("number", "?"), str(e)[:160])
            )
            return
        try:
            dispatch_triage_workflow(current["number"], item)
        except Exception as e:
            # The queued-cache write above already landed, so a later scan
            # would never retry this revision. If the card is HELD, publish
            # it now with a note rather than leaving it held indefinitely -
            # fail-open (see "Held cards" above) must not depend on a
            # dispatch that never actually started.
            print(
                "::warning::failed to dispatch auto triage for card #%s (%s#%s): %s "
                "- publishing the card so it is not left held indefinitely"
                % (current["number"], item.get("repo"), item.get("number"), str(e)[:160])
            )
            owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
            publish_dispatch_failure(
                current["number"],
                triage_revision(item),
                "Auto triage could not be started: %s" % str(e)[:160],
                owner=owner,
            )
            return
        print("queued auto triage for card #%s" % current["number"])


if __name__ == "__main__":
    main()
