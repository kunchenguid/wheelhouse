#!/usr/bin/env python3
"""Bounded, operator-only replay for exact-revision auto-triage failures.

The script is intentionally absent from every scheduled path. A run is
admitted only from an owner-started ``workflow_dispatch`` with a valid wave
slug. Candidate listings contribute issue numbers only: every card and target
used for eligibility is re-read by exact number.
"""

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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import reconcile  # noqa: E402
import render_card  # noqa: E402
import wheelhouse_core as core  # noqa: E402
from scripts import agent_claim  # noqa: E402

REPLAY_FIELD = "triage_replay"
REPLAY_VERSION = 1
REPLAY_LIMIT_DEFAULT = 25
REPLAY_LIMIT_MAX = 25
REPLAY_WAVE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")
PR_REVISION_RE = re.compile(r"^[0-9A-Fa-f]{7,64}$")
ISSUE_REVISION_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
TRIAGE_NON_SUCCESS_FIELDS = (
    "triaged_sha",
    "triage_status",
    "triage_error",
    "triage_repair_status",
    "triage_repair_reason",
    "triage_repair_candidate",
)
MAX_RUN_NUMBER = 9_007_199_254_740_991


def _triage_action(kind):
    search = os.environ.get("WHEELHOUSE_AUTO_TRIAGE_HAS_READONLY_TOKEN", "")
    if search not in {"true", "false"}:
        raise ValueError("replay requires the trusted READONLY_TOKEN presence flag")
    noun = "issue" if kind == "issue-triage" else "pr"
    mode = "search" if search == "true" else "local"
    return "triage.%s.%s" % (noun, mode)


def _card_repo_slug(owner):
    slug = os.environ.get("GITHUB_REPOSITORY", "")
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug)
        or slug.split("/", 1)[0] != owner
    ):
        raise ValueError("replay requires the trusted current repository slug")
    return slug


def _label_names(labels):
    names = set()
    for label in labels or []:
        name = label if isinstance(label, str) else (label or {}).get("name")
        if not isinstance(name, str) or not name:
            return None
        names.add(name)
    return names


def _valid_timestamp(value):
    if not isinstance(value, str) or not ISSUE_REVISION_RE.fullmatch(value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_marker(marker, revision):
    if not isinstance(marker, dict) or set(marker) != {
        "version",
        "wave",
        "revision",
        "cleared",
        "at",
        "run_number",
    }:
        return False
    run_number = marker.get("run_number")
    return bool(
        not isinstance(marker.get("version"), bool)
        and marker.get("version") == REPLAY_VERSION
        and isinstance(marker.get("wave"), str)
        and REPLAY_WAVE_RE.fullmatch(marker["wave"])
        and marker.get("revision") == revision
        and marker.get("cleared") in {"error", "absent"}
        and _valid_timestamp(marker.get("at"))
        and not isinstance(run_number, bool)
        and isinstance(run_number, int)
        and 1 <= run_number <= MAX_RUN_NUMBER
    )


def _source_json(owner, repo, number, kind):
    token = os.environ.get("FLEET_TOKEN", "")
    if not token:
        raise RuntimeError("FLEET_TOKEN is unavailable")
    noun = "pulls" if kind == "pr-review" else "issues"
    endpoint = "repos/%s/%s/%s/%s" % (owner, repo, noun, number)
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    result = subprocess.run(
        ("gh", "api", endpoint),
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError("source by-number read failed")
    value = json.loads(result.stdout or "null")
    if not isinstance(value, dict):
        raise RuntimeError("source by-number read was malformed")
    return value


def _effective_policy(config, repo, kind):
    repo_cfg = (config.get("repos") or {}).get(repo)
    if not isinstance(repo_cfg, dict):
        return None
    cap_map = config.get("triage_attempt_caps") or {}
    cap = (
        cap_map[repo]
        if repo in cap_map
        else core._triage_attempt_cap(
            repo_cfg, config.get("triage_attempt_cap_per_revision", 1)
        )
    )
    return {
        "auto_triage": core._auto_triage_enabled(
            repo_cfg, config.get("auto_triage", True)
        ),
        "auto_triage_issues": core._auto_triage_issues_enabled(
            repo_cfg, config.get("auto_triage_issues", True)
        ),
        "triage_attempt_cap_per_revision": cap,
    }


def _maintainer_logins(config, owner):
    return {
        str(login).strip().casefold()
        for login in (owner, (config or {}).get("maintainer", ""))
        if str(login).strip()
    }


def inspect_candidate(number, config, owner, has_token):
    """Return an eligible replay plan or a fail-closed skip reason."""
    card = render_card.get_card(number)
    if not isinstance(card, dict):
        return None, "card-unreadable"
    if not render_card.issue_is_open(card):
        return None, "card-closed"
    author = card.get("author") or {}
    login = author.get("login", "") if isinstance(author, dict) else ""
    if not render_card._trusted_automation_login(login):
        return None, "card-author-untrusted"
    body = card.get("body", "")
    state = render_card._unique_state_block(body)
    if state is None:
        return None, "card-state-malformed"
    repo = state.get("repo")
    target_number = state.get("number")
    kind = state.get("kind")
    if (
        not isinstance(repo, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo)
        or isinstance(target_number, bool)
        or not isinstance(target_number, int)
        or target_number < 1
        or kind not in render_card.AUTO_TRIAGE_FLAG_BY_KIND
    ):
        return None, "card-identity-malformed"
    names = _label_names(card.get("labels"))
    if names is None:
        return None, "card-labels-malformed"
    expected = {
        "repo": {"repo:%s" % repo},
        "kind": {"kind:%s" % kind},
        "target": {render_card.marker_label({"repo": repo, "number": target_number})},
    }
    for prefix, wanted in expected.items():
        actual = {name for name in names if name.startswith(prefix + ":")}
        if actual != wanted:
            return None, "card-label-identity-mismatch"
    if not render_card.is_refreshable(card.get("labels")):
        return None, "card-not-refreshable"
    if "held" in state and state.get("held") is not True:
        return None, "card-state-malformed"
    policy = _effective_policy(config, repo, kind)
    if policy is None:
        return None, "repo-not-configured"
    try:
        source = _source_json(owner, repo, target_number, kind)
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError, RuntimeError):
        return None, "source-unreadable"
    source_author = source.get("user") if isinstance(source.get("user"), dict) else {}
    if core._author_excluded_from_queue(
        source_author, _maintainer_logins(config, owner)
    ):
        return None, "source-author-excluded"
    if str(source.get("state") or "").lower() != "open":
        return None, "source-closed"
    if kind == "issue-triage" and "pull_request" in source:
        return None, "source-kind-mismatch"
    if kind == "pr-review":
        head = source.get("head") or {}
        revision = head.get("sha", "") if isinstance(head, dict) else ""
        if not isinstance(revision, str) or not PR_REVISION_RE.fullmatch(revision):
            return None, "source-revision-malformed"
    else:
        revision = source.get("updated_at", "")
        if not _valid_timestamp(revision):
            return None, "source-revision-malformed"
    source_updated_at = source.get("updated_at", "")
    if not isinstance(source_updated_at, str) or not _valid_timestamp(
        source_updated_at
    ):
        source_updated_at = ""
    if render_card.state_revision(state, kind) != revision:
        return None, "source-revision-moved"
    triaged_sha = state.get("triaged_sha")
    if triaged_sha is not None and triaged_sha != revision:
        return None, "triage-cache-stale"
    action = _triage_action(kind)
    marker = state.get(REPLAY_FIELD) if REPLAY_FIELD in state else None
    if marker is not None and not _valid_marker(marker, revision):
        return None, "replay-marker-untrusted"
    status = state.get("triage_status")
    duplicate_reentry = False
    if marker is not None:
        if state.get("held") or triaged_sha != revision or status not in {
            "queued",
            "error",
        }:
            return None, "already-replayed"
        try:
            duplicate_reentry = agent_claim.triage_replay_duplicate_only_evidence(
                action=action,
                owner=owner,
                repo=repo,
                number=target_number,
                issue=number,
                revision=revision,
                repo_slug=_card_repo_slug(owner),
                replayed_at=marker["at"],
            )
        except Exception:
            return None, "duplicate-evidence-unreadable"
        if not duplicate_reentry:
            return None, "already-replayed"
        cleared = marker["cleared"]
    elif triaged_sha == revision and status == "error":
        cleared = "error"
    elif triaged_sha is None:
        if any(field in state for field in TRIAGE_NON_SUCCESS_FIELDS[1:]):
            return None, "absent-cache-state-malformed"
        cleared = "absent"
    elif state.get("held") and status == "queued":
        return None, "held-queued-owned-by-recovery"
    elif isinstance(status, str) and status in {"queued", "succeeded"}:
        return None, "triage-cache-not-terminal-error"
    else:
        return None, "triage-status-untrusted"
    item = {
        "repo": repo,
        "number": target_number,
        "kind": kind,
        "head_sha": revision if kind == "pr-review" else "",
        "updated_at": source_updated_at,
        "title": str(source.get("title") or "(no title)"),
        "url": str(source.get("html_url") or ""),
        "author": (
            (source.get("user") or {}).get("login", "")
            if isinstance(source.get("user"), dict)
            else ""
        ),
        "recommendation": "Needs your call.",
        **policy,
    }
    if not render_card.should_hold(item, has_token):
        return None, "auto-triage-disabled"
    cap = policy["triage_attempt_cap_per_revision"]
    attempt_count = render_card.triage_attempt_count(state, kind, revision, cap)
    if duplicate_reentry:
        if attempt_count < 1:
            return None, "duplicate-attempt-count-untrusted"
        attempt_count -= 1
    if attempt_count >= cap:
        return None, "attempt-cap-exhausted"
    return {
        "number": number,
        "card": card,
        "state": state,
        "item": item,
        "revision": revision,
        "cleared": cleared,
        "attempt_count": attempt_count,
        "action": action,
        "duplicate_reentry": duplicate_reentry,
    }, "eligible"


def _marker(wave, revision, cleared, run_number):
    return {
        "version": REPLAY_VERSION,
        "wave": wave,
        "revision": revision,
        "cleared": cleared,
        "at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "run_number": run_number,
    }


def _body_with_replay_marker(body, plan, wave, run_number):
    state = render_card._unique_state_block(body)
    if state != plan["state"]:
        return body
    new_state = dict(state)
    if plan["cleared"] == "error" or plan["duplicate_reentry"]:
        for field in TRIAGE_NON_SUCCESS_FIELDS:
            new_state.pop(field, None)
    new_state[render_card.TRIAGE_ATTEMPTS_FIELD] = {
        "version": render_card.TRIAGE_ATTEMPTS_VERSION,
        "kind": plan["item"]["kind"],
        "revision": plan["revision"],
        "count": plan["attempt_count"],
    }
    new_state[REPLAY_FIELD] = _marker(
        wave, plan["revision"], plan["cleared"], run_number
    )
    clean = (
        render_card.remove_triage_section(body)
        if plan["cleared"] == "error" or plan["duplicate_reentry"]
        else body
    )
    return render_card._replace_state_block(clean, new_state)


def _candidate_numbers(path):
    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle)
    if not isinstance(rows, list):
        raise ValueError("candidate file must contain an array")
    numbers = set()
    for row in rows:
        number = row.get("number") if isinstance(row, dict) else None
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            print("::warning::replay ignored a malformed candidate-list row")
            continue
        numbers.add(number)
    return sorted(numbers)


def _entry(wave, limit):
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    actor = os.environ.get("GITHUB_ACTOR", "")
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise ValueError("replay requires workflow_dispatch")
    if not owner or actor != owner:
        raise ValueError("replay requires the repository owner actor")
    if not REPLAY_WAVE_RE.fullmatch(wave or ""):
        raise ValueError("replay_wave must match ^[a-z0-9][a-z0-9-]{2,40}$")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= REPLAY_LIMIT_MAX
    ):
        raise ValueError(
            "replay_limit must be an integer from 1 through %s" % REPLAY_LIMIT_MAX
        )
    value = os.environ.get("GITHUB_RUN_NUMBER", "")
    if not value.isdigit() or not 1 <= int(value) <= MAX_RUN_NUMBER:
        raise ValueError("replay requires a trusted GitHub run number")
    return owner, int(value)


def run(cards_path, wave, limit, dry_run=False):
    owner, run_number = _entry(wave, limit)
    repo_slug = _card_repo_slug(owner)
    config = core.load_config()
    has_token = render_card.auto_triage_has_token()
    numbers = _candidate_numbers(cards_path)
    eligible = []
    skipped = {}
    for number in numbers:
        plan, reason = inspect_candidate(number, config, owner, has_token)
        if plan:
            eligible.append(plan)
            print(
                "replay candidate card #%s: clear=%s revision=%s"
                % (number, plan["cleared"], plan["revision"])
            )
        else:
            skipped[reason] = skipped.get(reason, 0) + 1
    if skipped:
        print(
            "replay skip summary: "
            + json.dumps(skipped, sort_keys=True, separators=(",", ":"))
        )
    ceiling = config.get("triage_daily_ceiling", 0)
    remaining = render_card.triage_budget_remaining(ceiling)
    wave_bound = min(limit, remaining)
    selected = eligible[:wave_bound]
    deferred = len(eligible) - len(selected)
    if deferred:
        print(
            "::notice::replay deferred %s candidates (limit=%s remaining-budget=%s)"
            % (deferred, limit, remaining)
        )
    if dry_run:
        for plan in selected:
            print(
                "DRY-RUN card #%s: write triage_replay v1 cleared=%s, "
                "queue through maybe_queue_auto_triage, then dispatch through the existing permit"
                % (plan["number"], plan["cleared"])
            )
        print(
            "replay dry-run summary: listed=%s eligible=%s planned=%s deferred=%s writes=0"
            % (len(numbers), len(eligible), len(selected), deferred)
        )
        return {
            "eligible": len(eligible),
            "planned": len(selected),
            "deferred": deferred,
            "written": 0,
        }
    written = 0
    queued = 0
    for initial in selected:
        plan, reason = inspect_candidate(initial["number"], config, owner, has_token)
        if not plan:
            print(
                "::notice::replay skipped card #%s after re-read: %s"
                % (initial["number"], reason)
            )
            continue
        live = render_card.get_card(plan["number"])
        if not reconcile._matches_snapshot(live, plan["card"]):
            print(
                "::warning::replay deferred card #%s: card-raced-before-queue"
                % plan["number"]
            )
            continue
        current = reconcile.current_card({"number": plan["number"]})
        try:
            superseded = agent_claim.supersede_triage_claim(
                action=plan["action"],
                owner=owner,
                repo=plan["item"]["repo"],
                number=plan["item"]["number"],
                issue=plan["number"],
                revision=plan["revision"],
                repo_slug=repo_slug,
            )
        except Exception as error:
            print(
                "::error::replay refused card #%s before queueing: "
                "claim tombstone failed (%s)"
                % (plan["number"], str(error)[:180])
            )
            continue
        if superseded["superseded"]:
            print(
                "replay superseded stale triage claim for card #%s"
                % plan["number"]
            )
        prepare_body = lambda body, plan=plan: _body_with_replay_marker(
            body, plan, wave, run_number
        )
        if reconcile.maybe_queue_auto_triage(
            plan["item"],
            current,
            has_token,
            owner=owner,
            prepare_body=prepare_body,
            publish_budget_deferral=False,
        ):
            queued += 1
            written += 1
        else:
            print(
                "::warning::replay queueing deferred without card unlock for card #%s"
                % plan["number"]
            )
    print(
        "replay summary: listed=%s eligible=%s marked=%s queued=%s deferred=%s"
        % (len(numbers), len(eligible), written, queued, deferred)
    )
    return {
        "eligible": len(eligible),
        "planned": len(selected),
        "deferred": deferred,
        "written": written,
        "queued": queued,
    }


def parser():
    root = argparse.ArgumentParser()
    root.add_argument(
        "cards", help="Open-card listing used only to discover issue numbers"
    )
    root.add_argument("--wave", required=True)
    root.add_argument("--limit", type=int, default=REPLAY_LIMIT_DEFAULT)
    root.add_argument(
        "--dry-run",
        action="store_true",
        help="List exact-number candidates and planned actions with zero writes",
    )
    return root


def main():
    args = parser().parse_args()
    try:
        run(args.cards, args.wave, args.limit, dry_run=args.dry_run)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print("::error::triage replay refused: %s" % str(error)[:240])
        raise SystemExit(1)


if __name__ == "__main__":
    main()
