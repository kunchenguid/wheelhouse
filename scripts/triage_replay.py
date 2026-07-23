#!/usr/bin/env python3
"""Bounded, operator-only replay for exact-revision auto-triage failures.

The script is intentionally absent from every scheduled path. A run is
admitted only from an owner-started ``workflow_dispatch`` with a valid wave
slug. Candidate listings contribute issue numbers only: every card and target
used for eligibility is re-read by exact number.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
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
from agent_runtime import output_validation  # noqa: E402
from scripts import agent_claim  # noqa: E402

REPLAY_FIELD = "triage_replay"
REPLAY_VERSION = 1
ATTEMPT_RESET_REPLAY_VERSION = 2
INCIDENT_PERMIT_REPLAY_VERSION = 3
REPLAY_LIMIT_DEFAULT = 25
REPLAY_LIMIT_MAX = 25
EXACT_SELECTOR_VERSION = 1
EXACT_SELECTOR_PREFIX = "v%s:" % EXACT_SELECTOR_VERSION
EXACT_SELECTOR_LABEL = "exact-selector/v%s" % EXACT_SELECTOR_VERSION
EXACT_SELECTOR_MAX_BYTES = 512
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
MAX_CARD_NUMBER = MAX_RUN_NUMBER

# One immutable, self-expiring incident permit. The fixed wave plus exact-card
# selector is the capability request; there is deliberately no reusable reset
# input. Runtime admission re-proves the anchor behavior and every trusted
# source-review binding captured from run 29985490774 before it lets the normal
# queue checkpoint reserve budget or dispatch.
CARD_1585_INCIDENT_WAVE = "card-1585-anchor-fix-r3-final"
CARD_1585_INCIDENT_PERMIT = {
    "version": 1,
    "id": "card-1585-anchor-fix-r3",
    "wave": CARD_1585_INCIDENT_WAVE,
    "selector": (1585,),
    "card": 1585,
    "kind": "pr-review",
    "source_binding": {
        "source_identity_sha256": (
            "a16e805c2cdeaa2293e9129c0b7461baf11820c1befabefb607e1d40cb082294"
        ),
        "number": 547,
        "target_head_sha": "0f29152c44b808064f9a2a2621c9bde6456f6262",
        "base_sha": "3d4691aedba97d9f877c073e3e652a8fde69d574",
        "target_facts_sha256": (
            "c8308310c07e85d840ea41785f78786a04d181bcf25c1b2ae6dbe4db278f6ea9"
        ),
        "source_updated_at": "2026-07-23T04:56:49Z",
        "source_snapshot_sha256": (
            "a0dd38be93e516c4bd3c376993d2dc3eee89f6e90638f63c55017aac808661a6"
        ),
        "vision_sha": "08077197b28d5f6b5b74b405d4617f066f620e33",
        "vision_content_sha256": (
            "be04f798e4e616390c87a7fd21db7a3f656a4a7077b897c6a8aeb5cb49721b43"
        ),
    },
    "event_key": "7e7dfd540ff5e6babd7951b72d9bd3169df8fedee071eda13c1c5001f7c371c9",
    "required_fix_commit": "c45c3c8a8378fb29a12d421f743b7f8d2c8df7a4",
    "prior_claim": {
        "id": 5055236986,
        "created_at": "2026-07-23T06:35:10Z",
        "updated_at": "2026-07-23T06:38:23Z",
        "status": "consumer.committed",
    },
    "prior_result": {
        "id": 5052243850,
        "created_at": "2026-07-22T22:25:37Z",
        "updated_at": "2026-07-22T22:25:37Z",
        "status": "error",
        "code": "consumer.committed",
    },
    "prior_marker": {
        "version": REPLAY_VERSION,
        "wave": "missing-re-recovery-r2-exact",
        "revision": "0f29152c44b808064f9a2a2621c9bde6456f6262",
        "cleared": "error",
        "at": "2026-07-23T06:34:50Z",
        "run_number": 318,
    },
}
INCIDENT_REPLAY_PERMITS = {
    CARD_1585_INCIDENT_WAVE: CARD_1585_INCIDENT_PERMIT,
}

# Incident-scoped reset capabilities for captain-approved evidence-array
# recoveries. Each entry binds the card to the exact source revision and prior
# replay marker observed before its reset. A capability cannot follow a moved
# target, admit another card, or be reused after its v2 marker is written.
ATTEMPT_RESET_WAVE = "evidence-empty-e7-final"


def _attempt_reset_prior_marker(revision, wave, run_number, at):
    return {
        "version": REPLAY_VERSION,
        "wave": wave,
        "revision": revision,
        "cleared": "error",
        "at": at,
        "run_number": run_number,
    }


ATTEMPT_RESET_COHORT = {
    1367: _attempt_reset_prior_marker(
        "bf88f51742cd401e8c8f207fb4c5abd847afb386",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:07:39Z",
    ),
    1368: _attempt_reset_prior_marker(
        "2026-07-14T22:18:31Z",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:07:47Z",
    ),
    1374: _attempt_reset_prior_marker(
        "2c241fb3dd6dcb2a4399c920e873977760274226",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:08:02Z",
    ),
    1378: _attempt_reset_prior_marker(
        "81084b38f3f05c9837f008778d043af4e856d055",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:08:11Z",
    ),
    1386: _attempt_reset_prior_marker(
        "2026-07-15T14:07:59Z",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:08:35Z",
    ),
    1388: _attempt_reset_prior_marker(
        "2026-07-15T09:50:55Z",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:08:50Z",
    ),
    1389: _attempt_reset_prior_marker(
        "2026-07-15T09:39:31Z",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:08:57Z",
    ),
    1390: _attempt_reset_prior_marker(
        "2026-07-15T00:16:28Z",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:09:04Z",
    ),
    1391: _attempt_reset_prior_marker(
        "2026-07-14T12:33:32Z",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:09:12Z",
    ),
    1393: _attempt_reset_prior_marker(
        "2026-07-15T11:36:12Z",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:09:27Z",
    ),
    1395: _attempt_reset_prior_marker(
        "2026-07-14T12:16:32Z",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:09:42Z",
    ),
    1396: _attempt_reset_prior_marker(
        "2026-07-14T04:16:19Z",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:09:50Z",
    ),
    1397: _attempt_reset_prior_marker(
        "83ca8584c5e0387d44e9521d1616183bb3e31faa",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:09:58Z",
    ),
    1398: _attempt_reset_prior_marker(
        "19057b7f4626f235544ecafdbdf8a1e6ffd7a642",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:10:06Z",
    ),
    1399: _attempt_reset_prior_marker(
        "2026-07-16T00:25:10Z",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:10:14Z",
    ),
    1400: _attempt_reset_prior_marker(
        "2026-07-15T23:49:21Z",
        "cohort-rerun-2",
        238,
        "2026-07-17T20:23:41Z",
    ),
    1414: _attempt_reset_prior_marker(
        "2026-07-16T15:33:54Z",
        "cohort-rerun-2",
        238,
        "2026-07-17T20:24:27Z",
    ),
    1415: _attempt_reset_prior_marker(
        "2026-07-16T16:17:20Z",
        "cohort-rerun-2",
        238,
        "2026-07-17T20:24:36Z",
    ),
    1420: _attempt_reset_prior_marker(
        "2026-07-16T19:40:06Z",
        "cohort-rerun-2",
        238,
        "2026-07-17T20:24:45Z",
    ),
}

ARRAY_RECOVERY_ATTEMPT_RESET_WAVE = "array-recovery-g1-final"
ARRAY_RECOVERY_ATTEMPT_RESET_COHORT = {
    154: _attempt_reset_prior_marker(
        "2026-07-13T19:27:04Z",
        "canary-1",
        224,
        "2026-07-17T01:55:44Z",
    ),
    481: _attempt_reset_prior_marker(
        "2026-07-15T03:11:28Z",
        "recovery-wave-1",
        225,
        "2026-07-17T02:10:40Z",
    ),
    572: _attempt_reset_prior_marker(
        "1ccebb15df4966e1b08da1fa4825346d51cb3ac0",
        "cohort-rerun-1",
        237,
        "2026-07-17T20:07:24Z",
    ),
    907: _attempt_reset_prior_marker(
        "2026-07-14T02:54:04Z",
        "recovery-wave-1",
        225,
        "2026-07-17T02:11:00Z",
    ),
    951: _attempt_reset_prior_marker(
        "2026-07-14T21:24:04Z",
        "recovery-wave-1",
        225,
        "2026-07-17T02:11:06Z",
    ),
    1266: _attempt_reset_prior_marker(
        "2026-07-14T10:18:18Z",
        "recovery-wave-1",
        225,
        "2026-07-17T02:11:19Z",
    ),
    1275: _attempt_reset_prior_marker(
        "2026-07-15T02:35:46Z",
        "recovery-wave-1",
        225,
        "2026-07-17T02:11:25Z",
    ),
    1428: _attempt_reset_prior_marker(
        "76502958188953a3efb639ef5eb0bd0da47566b1",
        "cohort-rerun-2",
        238,
        "2026-07-17T20:24:53Z",
    ),
    1430: _attempt_reset_prior_marker(
        "5fb0cc655d02f25eba1cb0c0b37a0f2893cd9d6a",
        "cohort-rerun-2",
        238,
        "2026-07-17T20:25:03Z",
    ),
    1435: _attempt_reset_prior_marker(
        "57808325def6aa1ae2cd5d43d2e7d6d82b2127ad",
        "cohort-rerun-2",
        238,
        "2026-07-17T20:25:12Z",
    ),
    1436: _attempt_reset_prior_marker(
        "2026-07-17T13:07:28Z",
        "cohort-rerun-2",
        238,
        "2026-07-17T20:25:20Z",
    ),
    1437: _attempt_reset_prior_marker(
        "376b3cbf12cdd26a8c9b3f19a00f5370b800baed",
        "cohort-rerun-2",
        238,
        "2026-07-17T20:25:29Z",
    ),
    1441: _attempt_reset_prior_marker(
        "16f11d8cd07036c2ee1e54c863d274a5dbcc1a78",
        "cohort-rerun-2",
        238,
        "2026-07-17T20:25:46Z",
    ),
    1442: _attempt_reset_prior_marker(
        "53504c275b87880147857184cd3418a333711368",
        "cohort-rerun-2",
        238,
        "2026-07-17T20:25:55Z",
    ),
    1443: _attempt_reset_prior_marker(
        "2026-07-17T16:35:47Z",
        "cohort-rerun-2",
        238,
        "2026-07-17T20:26:04Z",
    ),
}

ATTEMPT_RESET_COHORTS = {
    ATTEMPT_RESET_WAVE: ATTEMPT_RESET_COHORT,
    ARRAY_RECOVERY_ATTEMPT_RESET_WAVE: ARRAY_RECOVERY_ATTEMPT_RESET_COHORT,
}


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
    if not isinstance(marker, dict):
        return False
    version = marker.get("version")
    base_keys = {
        "version",
        "wave",
        "revision",
        "cleared",
        "at",
        "run_number",
    }
    if version == REPLAY_VERSION:
        if set(marker) != base_keys:
            return False
    elif version == ATTEMPT_RESET_REPLAY_VERSION:
        if set(marker) != base_keys | {"attempt_reset"}:
            return False
        if marker.get("attempt_reset") is not True:
            return False
    elif version == INCIDENT_PERMIT_REPLAY_VERSION:
        if set(marker) != base_keys | {"incident_permit"}:
            return False
        if (
            not isinstance(marker.get("incident_permit"), str)
            or not REPLAY_WAVE_RE.fullmatch(marker["incident_permit"])
        ):
            return False
    else:
        return False
    run_number = marker.get("run_number")
    return bool(
        not isinstance(version, bool)
        and isinstance(marker.get("wave"), str)
        and REPLAY_WAVE_RE.fullmatch(marker["wave"])
        and marker.get("revision") == revision
        and marker.get("cleared") in {"error", "absent"}
        and _valid_timestamp(marker.get("at"))
        and not isinstance(run_number, bool)
        and isinstance(run_number, int)
        and 1 <= run_number <= MAX_RUN_NUMBER
    )


def _exact_card_scope(value):
    """Parse the versioned exact-card selector into canonical card order."""
    if value == "":
        return ()
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > EXACT_SELECTOR_MAX_BYTES
    ):
        raise ValueError(
            "exact card selector must be at most %s bytes" % EXACT_SELECTOR_MAX_BYTES
        )
    match = re.fullmatch(
        re.escape(EXACT_SELECTOR_PREFIX)
        + r"([1-9][0-9]*(?:,[1-9][0-9]*)*)",
        value,
    )
    if not match:
        raise ValueError(
            "exact card selector must match %sN[,N...] with positive decimal integers"
            % EXACT_SELECTOR_PREFIX
        )
    numbers = [int(part) for part in match.group(1).split(",")]
    if len(numbers) > REPLAY_LIMIT_MAX:
        raise ValueError(
            "exact card selector may contain at most %s cards" % REPLAY_LIMIT_MAX
        )
    if len(numbers) != len(set(numbers)):
        raise ValueError("exact card selector must not contain duplicates")
    if any(number > MAX_CARD_NUMBER for number in numbers):
        raise ValueError("exact card selector contains an oversized card number")
    return tuple(sorted(numbers))


def _canonical_exact_selector(numbers):
    return EXACT_SELECTOR_PREFIX + ",".join(str(number) for number in numbers)


def _incident_permit_scope(wave, exact_scope):
    """Admit only the code-defined wave with its one immutable selector."""
    permit = INCIDENT_REPLAY_PERMITS.get(wave)
    if permit is None:
        return None
    if exact_scope != permit["selector"]:
        raise ValueError(
            "incident permit requires exact selector %s"
            % _canonical_exact_selector(permit["selector"])
        )
    return permit


def _incident_anchor_fix_present(permit):
    """Prove the landed escaped-delimiter fix by behavior, even in a shallow clone."""
    if permit.get("required_fix_commit") != CARD_1585_INCIDENT_PERMIT[
        "required_fix_commit"
    ]:
        return False
    target = "the repository's effective git identity remains intact"
    evidence = r"target.txt: 'the repository\'s effective git identity remains intact'"
    return output_validation.evidence_anchor_ok(evidence, target)


def _incident_prior_evidence_reason(owner, permit):
    """Bind the permit to the exact pre-incident claim and result records."""
    event_key = permit.get("event_key")
    issue = permit.get("card")
    try:
        marker = agent_claim.event_claim_marker(event_key)
        claims = agent_claim.list_claims(_card_repo_slug(owner), issue, marker)
        records = agent_claim.list_triage_records(
            _card_repo_slug(owner), issue, event_key
        )
    except Exception:
        return "incident-prior-evidence-unreadable"
    if len(claims) != 1 or len(records) != 1:
        return "incident-prior-evidence-mismatch"
    claim = claims[0]
    expected_claim = permit.get("prior_claim")
    observed_claim = {
        "id": claim.get("id"),
        "created_at": claim.get("created_at"),
        "updated_at": claim.get("updated_at"),
        "status": (
            "consumer.committed"
            if claim.get("body")
            == "Agent triage event finished with consumer.committed. %s" % marker
            else ""
        ),
    }
    record = records[0]
    record_value = record.get("record") if isinstance(record, dict) else None
    observed_result = {
        "id": record.get("id") if isinstance(record, dict) else None,
        "created_at": record.get("created_at") if isinstance(record, dict) else None,
        "updated_at": record.get("updated_at") if isinstance(record, dict) else None,
        "status": (
            record_value.get("status") if isinstance(record_value, dict) else None
        ),
        "code": record_value.get("code") if isinstance(record_value, dict) else None,
    }
    return (
        ""
        if observed_claim == expected_claim
        and observed_result == permit.get("prior_result")
        else "incident-prior-evidence-mismatch"
    )


def _attempt_reset_scope(wave, value):
    """Validate the exact operator-supplied one-time incident cohort."""
    text = str(value or "").strip()
    if not text:
        return {}
    cohort = ATTEMPT_RESET_COHORTS.get(wave)
    if cohort is None:
        raise ValueError("attempt reset requires the sanctioned replay wave")
    parts = [part.strip() for part in text.split(",")]
    if any(not re.fullmatch(r"[1-9][0-9]*", part) for part in parts):
        raise ValueError("attempt reset cards must be comma-separated issue numbers")
    numbers = [int(part) for part in parts]
    if len(numbers) != len(set(numbers)):
        raise ValueError("attempt reset cards must not contain duplicates")
    if set(numbers) != set(cohort):
        raise ValueError("attempt reset cards must exactly match the sanctioned cohort")
    return {number: dict(cohort[number]) for number in sorted(numbers)}


def _attempt_reset_count(state, kind, revision, cap):
    """Return the reset count only for an exact trusted exhausted record."""
    if cap != 2:
        return None
    record = state.get(render_card.TRIAGE_ATTEMPTS_FIELD)
    expected = {
        "version": render_card.TRIAGE_ATTEMPTS_VERSION,
        "kind": kind,
        "revision": revision,
        "count": cap,
    }
    if (
        record != expected
        or isinstance(record.get("version"), bool)
        or not isinstance(record.get("version"), int)
        or isinstance(record.get("count"), bool)
        or not isinstance(record.get("count"), int)
    ):
        return None
    return cap - 1


def _attempt_reset_marker_applied(marker, wave, revision):
    return bool(
        _valid_marker(marker, revision)
        and marker.get("version") == ATTEMPT_RESET_REPLAY_VERSION
        and marker.get("wave") == wave
        and marker.get("cleared") == "error"
        and marker.get("attempt_reset") is True
    )


def _fleet_json(endpoint):
    token = os.environ.get("FLEET_TOKEN", "")
    if not token:
        raise RuntimeError("FLEET_TOKEN is unavailable")
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
        raise RuntimeError("fleet source read failed")
    value = json.loads(result.stdout or "null")
    if not isinstance(value, dict):
        raise RuntimeError("fleet source read was malformed")
    return value


def _source_json(owner, repo, number, kind):
    noun = "pulls" if kind == "pr-review" else "issues"
    return _fleet_json("repos/%s/%s/%s/%s" % (owner, repo, noun, number))


def _incident_source_binding_reason(
    owner, repo, number, kind, permit, before
):
    """Rebuild the approved base/VISION/target-facts binding from live reads."""
    expected = permit.get("source_binding")
    if not isinstance(expected, dict):
        return "incident-source-binding-malformed"
    head_sha = expected.get("target_head_sha")
    base_sha = expected.get("base_sha")
    identity = "%s/%s#%s/%s" % (owner, repo, number, kind)
    if (
        hashlib.sha256(identity.encode("utf-8")).hexdigest()
        != expected.get("source_identity_sha256")
        or permit.get("card") != 1585
        or kind != "pr-review"
        or number != expected.get("number")
        or not isinstance(repo, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo)
        or isinstance(number, bool)
        or not isinstance(number, int)
        or not isinstance(head_sha, str)
        or not isinstance(base_sha, str)
    ):
        return "incident-source-identity-mismatch"
    try:
        comparison = _fleet_json(
            "repos/%s/%s/compare/%s...%s" % (owner, repo, base_sha, head_sha)
        )
        after = _source_json(owner, repo, number, "pr-review")
        vision = _fleet_json("repos/%s/%s/contents/VISION.md" % (owner, repo))
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError, RuntimeError):
        return "incident-source-unreadable"
    facts = render_card.build_triage_target_facts(
        before,
        comparison,
        after,
        owner=owner,
        repo=repo,
        number=number,
        head_sha=head_sha,
        base_sha=base_sha,
    )
    facts_bytes = render_card.serialize_triage_target_facts(facts)

    def source_snapshot(value):
        if not isinstance(value, dict):
            return None
        fields = {
            "title": value.get("title"),
            "body": value.get("body"),
            "updated_at": value.get("updated_at"),
        }
        if not all(isinstance(item, str) for item in fields.values()):
            return None
        payload = (
            json.dumps(
                fields,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    before_snapshot = source_snapshot(before)
    after_snapshot = source_snapshot(after)
    try:
        encoded = vision.get("content")
        if (
            vision.get("name") != "VISION.md"
            or vision.get("path") != "VISION.md"
            or vision.get("type") != "file"
            or not isinstance(encoded, str)
            or isinstance(vision.get("size"), bool)
            or not isinstance(vision.get("size"), int)
        ):
            raise ValueError("VISION response identity mismatch")
        vision_bytes = base64.b64decode("".join(encoded.split()), validate=True)
        if not vision_bytes or len(vision_bytes) != vision["size"]:
            raise ValueError("VISION response size mismatch")
    except (TypeError, ValueError):
        return "incident-vision-unreadable"
    if facts_bytes is None:
        return "incident-target-facts-unavailable"
    observed = {
        "source_identity_sha256": hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest(),
        "number": number,
        "target_head_sha": head_sha,
        "base_sha": base_sha,
        "target_facts_sha256": hashlib.sha256(facts_bytes).hexdigest(),
        "source_updated_at": before.get("updated_at"),
        "source_snapshot_sha256": before_snapshot,
        "vision_sha": vision.get("sha"),
        "vision_content_sha256": hashlib.sha256(vision_bytes).hexdigest(),
    }
    return (
        ""
        if before_snapshot is not None
        and before_snapshot == after_snapshot
        and observed == expected
        else "incident-source-binding-mismatch"
    )


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


def inspect_candidate(
    number,
    config,
    owner,
    has_token,
    attempt_reset=None,
    attempt_reset_wave="",
    incident_permit=None,
):
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
    if incident_permit is not None:
        binding_reason = _incident_source_binding_reason(
            owner, repo, target_number, kind, incident_permit, source
        )
        if binding_reason:
            return None, binding_reason
    triaged_sha = state.get("triaged_sha")
    if triaged_sha is not None and triaged_sha != revision:
        return None, "triage-cache-stale"
    action = _triage_action(kind)
    if incident_permit is not None:
        identity = agent_claim.normalized_event_identity(
            action=action,
            owner=owner,
            repo=repo,
            number=target_number,
            card_issue=number,
            revision=revision,
        )
        if (
            agent_claim.event_key_sha256(identity)
            != incident_permit.get("event_key")
        ):
            return None, "incident-event-key-mismatch"
    marker = state.get(REPLAY_FIELD) if REPLAY_FIELD in state else None
    if marker is not None and not _valid_marker(marker, revision):
        return None, "replay-marker-untrusted"
    if (
        attempt_reset is None
        and marker is not None
        and marker.get("version") == ATTEMPT_RESET_REPLAY_VERSION
    ):
        return None, "replay-marker-untrusted"
    if (
        incident_permit is None
        and marker is not None
        and marker.get("version") == INCIDENT_PERMIT_REPLAY_VERSION
    ):
        return None, "replay-marker-untrusted"
    status = state.get("triage_status")
    reset_already_applied = bool(
        attempt_reset is not None
        and _attempt_reset_marker_applied(
            marker, wave=attempt_reset_wave, revision=revision
        )
    )
    duplicate_reentry = False
    if incident_permit is not None:
        if number != incident_permit.get("card"):
            return None, "incident-card-mismatch"
        expected_binding = incident_permit["source_binding"]
        if (
            kind != incident_permit.get("kind")
            or target_number != expected_binding.get("number")
            or revision != expected_binding.get("target_head_sha")
        ):
            return None, "incident-source-identity-mismatch"
        if (
            marker is not None
            and marker.get("version") == INCIDENT_PERMIT_REPLAY_VERSION
            and marker.get("incident_permit") == incident_permit.get("id")
        ):
            return None, "incident-permit-consumed"
        if marker != incident_permit.get("prior_marker"):
            return None, "incident-prior-marker-mismatch"
        if state.get("held") or triaged_sha != revision or status != "error":
            return None, "incident-state-mismatch"
        evidence_reason = _incident_prior_evidence_reason(owner, incident_permit)
        if evidence_reason:
            return None, evidence_reason
        cleared = "error"
    elif attempt_reset is not None:
        expected_marker = attempt_reset
        expected_revision = expected_marker["revision"]
        if revision != expected_revision:
            return None, "attempt-reset-revision-mismatch"
        if not reset_already_applied and marker != expected_marker:
            return None, "attempt-reset-prior-marker-mismatch"
        expected_statuses = {"queued", "succeeded", "error"}
        if reset_already_applied and (
            state.get("held")
            or triaged_sha != revision
            or status not in expected_statuses
        ):
            return None, "attempt-reset-resume-state-mismatch"
        if not reset_already_applied and (
            state.get("held") or triaged_sha != revision or status != "error"
        ):
            return None, "attempt-reset-state-mismatch"
        cleared = "error"
    elif marker is not None:
        if (
            state.get("held")
            or triaged_sha != revision
            or status
            not in {
                "queued",
                "error",
            }
        ):
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
    cap = policy["triage_attempt_cap_per_revision"]
    if incident_permit is not None:
        attempt_count = _attempt_reset_count(state, kind, revision, cap)
        if attempt_count is None:
            return None, "incident-attempt-count-mismatch"
    elif attempt_reset is not None:
        attempt_count = _attempt_reset_count(state, kind, revision, cap)
        if attempt_count is None:
            return None, "attempt-reset-count-mismatch"
        if reset_already_applied:
            return {
                "number": number,
                "card": card,
                "state": state,
                "item": item,
                "revision": revision,
                "cleared": cleared,
                "attempt_count": attempt_count,
                "action": action,
                "duplicate_reentry": False,
                "attempt_reset": True,
                "attempt_reset_applied": True,
            }, "eligible"
    if not render_card.should_hold(item, has_token):
        return None, "auto-triage-disabled"
    if attempt_reset is None and incident_permit is None:
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
        "attempt_reset": attempt_reset is not None,
        "attempt_reset_applied": False,
        "incident_permit": (
            incident_permit.get("id") if incident_permit is not None else ""
        ),
    }, "eligible"


def _card_snapshot_identity(card):
    if not isinstance(card, dict):
        return None
    author = card.get("author") if isinstance(card.get("author"), dict) else {}
    return {
        "number": card.get("number"),
        "title": card.get("title", ""),
        "body": card.get("body", ""),
        "labels": sorted(_label_names(card.get("labels")) or []),
        "state": card.get("state", ""),
        "updated_at": render_card.card_updated_at(card),
        "comments": reconcile._comment_count(card.get("comments")),
        "author": author.get("login", ""),
    }


def _plans_match(initial, reread):
    return bool(
        initial
        and reread
        and initial.get("number") == reread.get("number")
        and initial.get("revision") == reread.get("revision")
        and initial.get("cleared") == reread.get("cleared")
        and initial.get("attempt_count") == reread.get("attempt_count")
        and initial.get("action") == reread.get("action")
        and initial.get("incident_permit") == reread.get("incident_permit")
        and initial.get("item") == reread.get("item")
        and initial.get("state") == reread.get("state")
        and _card_snapshot_identity(initial.get("card"))
        == _card_snapshot_identity(reread.get("card"))
    )


def _preflight_attempt_reset(
    selected, attempt_reset_scope, wave, config, owner, has_token
):
    if not attempt_reset_scope:
        return selected
    if len(selected) != len(attempt_reset_scope):
        raise ValueError("attempt reset requires the complete eligible cohort")
    reread_plans = []
    for initial in selected:
        number = initial["number"]
        plan, reason = inspect_candidate(
            number,
            config,
            owner,
            has_token,
            attempt_reset=attempt_reset_scope.get(number),
            attempt_reset_wave=wave,
        )
        if not plan or not _plans_match(initial, plan):
            print(
                "::warning::attempt reset preflight refused card #%s: %s"
                % (number, reason if not plan else "card-raced-before-reset")
            )
            raise ValueError(
                "attempt reset refused because a sanctioned card changed before mutation"
            )
        reread_plans.append(plan)
    return reread_plans


def _preflight_exact_scope(
    selected, exact_scope, config, owner, has_token, incident_permit=None
):
    """Atomically revalidate every requested card before any exact-scope write."""
    if not exact_scope:
        return selected
    if tuple(plan["number"] for plan in selected) != exact_scope:
        raise ValueError("exact card selector did not produce the requested cohort")
    reread_plans = []
    refused = []
    for initial in selected:
        number = initial["number"]
        plan, reason = inspect_candidate(
            number,
            config,
            owner,
            has_token,
            incident_permit=incident_permit,
        )
        if not plan or not _plans_match(initial, plan):
            refusal = reason if not plan else "card-raced-before-replay"
            print(
                "::error::replay %s refused card #%s: %s"
                % (EXACT_SELECTOR_LABEL, number, refusal)
            )
            refused.append(number)
        else:
            reread_plans.append(plan)
    if refused:
        raise ValueError(
            "exact card selector refused because a requested card changed before mutation"
        )
    return reread_plans


def _print_exact_plans(exact_scope, plans):
    if not exact_scope:
        return
    print(
        "replay %s canonical=%s count=%s"
        % (
            EXACT_SELECTOR_LABEL,
            _canonical_exact_selector(exact_scope),
            len(exact_scope),
        )
    )
    for plan in plans:
        print(
            "replay %s admitted card #%s: revision=%s clear=%s"
            % (
                EXACT_SELECTOR_LABEL,
                plan["number"],
                plan["revision"],
                plan["cleared"],
            )
        )


def _marker(
    wave,
    revision,
    cleared,
    run_number,
    attempt_reset=False,
    incident_permit="",
):
    if attempt_reset and incident_permit:
        raise ValueError("replay marker capabilities are mutually exclusive")
    if incident_permit:
        version = INCIDENT_PERMIT_REPLAY_VERSION
    elif attempt_reset:
        version = ATTEMPT_RESET_REPLAY_VERSION
    else:
        version = REPLAY_VERSION
    marker = {
        "version": version,
        "wave": wave,
        "revision": revision,
        "cleared": cleared,
        "at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "run_number": run_number,
    }
    if attempt_reset:
        marker["attempt_reset"] = True
    if incident_permit:
        marker["incident_permit"] = incident_permit
    return marker


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
        wave,
        plan["revision"],
        plan["cleared"],
        run_number,
        attempt_reset=plan.get("attempt_reset", False),
        incident_permit=plan.get("incident_permit", ""),
    )
    clean = (
        render_card.remove_triage_section(body)
        if plan["cleared"] == "error" or plan["duplicate_reentry"]
        else body
    )
    return render_card._replace_state_block(clean, new_state)


def _body_with_incident_consumption_marker(body, plan, wave, run_number):
    state = render_card._unique_state_block(body)
    if state != plan["state"]:
        return body
    new_state = dict(state)
    new_state[REPLAY_FIELD] = _marker(
        wave,
        plan["revision"],
        plan["cleared"],
        run_number,
        incident_permit=plan["incident_permit"],
    )
    return render_card._replace_state_block(body, new_state)


def _body_with_incident_queue_transition(body, plan, consumed_state):
    state = render_card._unique_state_block(body)
    if state != consumed_state:
        return body
    marker = state.get(REPLAY_FIELD)
    if (
        not _valid_marker(marker, plan["revision"])
        or marker.get("version") != INCIDENT_PERMIT_REPLAY_VERSION
        or marker.get("incident_permit") != plan["incident_permit"]
    ):
        return body
    new_state = dict(state)
    for field in TRIAGE_NON_SUCCESS_FIELDS:
        new_state.pop(field, None)
    new_state[render_card.TRIAGE_ATTEMPTS_FIELD] = {
        "version": render_card.TRIAGE_ATTEMPTS_VERSION,
        "kind": plan["item"]["kind"],
        "revision": plan["revision"],
        "count": plan["attempt_count"],
    }
    clean = render_card.remove_triage_section(body)
    return render_card._replace_state_block(clean, new_state)


def _consume_incident_permit(plan, wave, run_number, owner):
    before = render_card.get_card(plan["number"])
    body = plan["card"].get("body", "")
    if (
        _card_snapshot_identity(before) != _card_snapshot_identity(plan["card"])
        or not render_card._queue_card_snapshot_matches(
            before,
            plan["number"],
            plan["item"],
            body,
        )
    ):
        return None
    marked_body = _body_with_incident_consumption_marker(
        body, plan, wave, run_number
    )
    if marked_body == body:
        return None
    marked_body = render_card._atomic_automerge_card_body(
        marked_body,
        before,
        owner=owner,
    )
    render_card._edit_issue_body(plan["number"], marked_body)
    verified = render_card.get_card(plan["number"])
    if not render_card._queue_card_snapshot_matches(
        verified,
        plan["number"],
        plan["item"],
        marked_body,
    ):
        return None
    state = render_card.parse_state_block(marked_body)
    marker = state.get(REPLAY_FIELD) if isinstance(state, dict) else None
    if (
        not _valid_marker(marker, plan["revision"])
        or marker.get("version") != INCIDENT_PERMIT_REPLAY_VERSION
        or marker.get("incident_permit") != plan["incident_permit"]
        or marker.get("wave") != wave
    ):
        return None
    return {
        "number": verified["number"],
        "title": verified.get("title", ""),
        "body": marked_body,
        "state": state,
        "labels": verified.get("labels", []),
        "updated_at": render_card.card_updated_at(verified),
        "comments": reconcile._comment_count(verified.get("comments")),
    }


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


def run(
    cards_path,
    wave,
    limit,
    dry_run=False,
    attempts_reset_cards="",
    exact_cards="",
):
    owner, run_number = _entry(wave, limit)
    exact_scope = _exact_card_scope(exact_cards)
    attempt_reset_scope = _attempt_reset_scope(wave, attempts_reset_cards)
    incident_permit = _incident_permit_scope(wave, exact_scope)
    if exact_scope and attempt_reset_scope:
        raise ValueError("exact card selector cannot be combined with attempt reset")
    if incident_permit is not None and not _incident_anchor_fix_present(
        incident_permit
    ):
        raise ValueError("incident permit requires the landed card-1585 anchor fix")
    if exact_scope and limit != len(exact_scope):
        raise ValueError("replay_limit must equal the exact card selector count")
    if attempt_reset_scope and limit != len(attempt_reset_scope):
        raise ValueError("attempt reset limit must equal the sanctioned cohort size")
    repo_slug = _card_repo_slug(owner)
    config = core.load_config()
    has_token = render_card.auto_triage_has_token()
    if exact_scope:
        numbers = list(exact_scope)
    elif attempt_reset_scope:
        numbers = sorted(attempt_reset_scope)
    else:
        numbers = _candidate_numbers(cards_path)
    eligible = []
    skipped = {}
    refused = []
    for number in numbers:
        plan, reason = inspect_candidate(
            number,
            config,
            owner,
            has_token,
            attempt_reset=attempt_reset_scope.get(number),
            attempt_reset_wave=wave,
            incident_permit=incident_permit,
        )
        if plan:
            eligible.append(plan)
            print(
                "replay candidate card #%s: clear=%s revision=%s"
                % (number, plan["cleared"], plan["revision"])
            )
        else:
            skipped[reason] = skipped.get(reason, 0) + 1
            if exact_scope:
                refused.append((number, reason))
                print(
                    "::error::replay %s refused card #%s: %s"
                    % (EXACT_SELECTOR_LABEL, number, reason)
                )
    if skipped:
        print(
            "replay skip summary: "
            + json.dumps(skipped, sort_keys=True, separators=(",", ":"))
        )
        if exact_scope:
            raise ValueError(
                "exact card selector refused because %s requested card(s) failed validation"
                % len(refused)
            )
        if attempt_reset_scope:
            raise ValueError(
                "attempt reset refused because a sanctioned card failed validation"
            )
    if (
        attempt_reset_scope
        and eligible
        and all(plan.get("attempt_reset_applied") for plan in eligible)
    ):
        raise ValueError("attempt reset has already consumed the sanctioned cohort")
    ceiling = config.get("triage_daily_ceiling", 0)
    remaining = render_card.triage_budget_remaining(ceiling)
    pending_attempt_resets = sum(
        not plan.get("attempt_reset_applied") for plan in eligible
    )
    if attempt_reset_scope and remaining < pending_attempt_resets:
        raise ValueError("attempt reset requires budget for the pending cohort")
    if exact_scope and remaining < len(exact_scope):
        print(
            "::error::replay %s refused cards %s: "
            "insufficient-budget remaining=%s required=%s"
            % (
                EXACT_SELECTOR_LABEL,
                ",".join("#%s" % number for number in exact_scope),
                remaining,
                len(exact_scope),
            )
        )
        raise ValueError("exact card selector requires budget for the complete cohort")
    if exact_scope:
        wave_bound = len(exact_scope)
    elif attempt_reset_scope:
        wave_bound = len(attempt_reset_scope)
    else:
        wave_bound = min(limit, remaining)
    selected = eligible[:wave_bound]
    deferred = len(eligible) - len(selected)
    if deferred:
        print(
            "::notice::replay deferred %s candidates (limit=%s remaining-budget=%s)"
            % (deferred, limit, remaining)
        )
    selected = _preflight_exact_scope(
        selected,
        exact_scope,
        config,
        owner,
        has_token,
        incident_permit=incident_permit,
    )
    _print_exact_plans(exact_scope, selected)
    if dry_run:
        for plan in selected:
            if exact_scope:
                marker_version = (
                    INCIDENT_PERMIT_REPLAY_VERSION
                    if incident_permit is not None
                    else REPLAY_VERSION
                )
                capability = (
                    " incident_permit=%s" % incident_permit["id"]
                    if incident_permit is not None
                    else ""
                )
                print(
                    "DRY-RUN card #%s revision=%s: write triage_replay v%s%s cleared=%s, "
                    "queue through maybe_queue_auto_triage, then dispatch through the existing permit"
                    % (
                        plan["number"],
                        plan["revision"],
                        marker_version,
                        capability,
                        plan["cleared"],
                    )
                )
            else:
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
    selected = _preflight_attempt_reset(
        selected, attempt_reset_scope, wave, config, owner, has_token
    )
    written = 0
    queued = 0
    for initial in selected:
        if initial.get("attempt_reset_applied"):
            continue
        plan, reason = inspect_candidate(
            initial["number"],
            config,
            owner,
            has_token,
            attempt_reset=attempt_reset_scope.get(initial["number"]),
            attempt_reset_wave=wave,
            incident_permit=incident_permit,
        )
        if not plan or (exact_scope and not _plans_match(initial, plan)):
            refusal = reason if not plan else "card-raced-before-replay"
            print(
                "::notice::replay skipped card #%s after re-read: %s"
                % (initial["number"], refusal)
            )
            if attempt_reset_scope:
                raise ValueError(
                    "attempt reset paused because a sanctioned card changed during mutation"
                )
            if exact_scope:
                raise ValueError(
                    "exact replay paused because a requested card changed during mutation"
                )
            continue
        live = render_card.get_card(plan["number"])
        if _card_snapshot_identity(live) != _card_snapshot_identity(plan["card"]):
            print(
                "::warning::replay deferred card #%s: card-raced-before-queue"
                % plan["number"]
            )
            if attempt_reset_scope:
                raise ValueError(
                    "attempt reset paused because a sanctioned card changed during mutation"
                )
            if exact_scope:
                raise ValueError(
                    "exact replay paused because a requested card changed during mutation"
                )
            continue
        current = reconcile.current_card({"number": plan["number"]})
        if current is None:
            if exact_scope:
                raise ValueError(
                    "exact replay paused because a requested card changed during mutation"
                )
            continue
        if incident_permit is not None:
            try:
                current = _consume_incident_permit(plan, wave, run_number, owner)
            except Exception as error:
                print(
                    "::error::replay refused card #%s before claim mutation: "
                    "incident marker write failed (%s)"
                    % (plan["number"], str(error)[:180])
                )
                raise ValueError(
                    "incident replay paused because permit consumption failed"
                )
            if current is None:
                raise ValueError(
                    "incident replay paused because permit consumption did not verify"
                )
            written += 1
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
                "claim tombstone failed (%s)" % (plan["number"], str(error)[:180])
            )
            if attempt_reset_scope:
                raise ValueError(
                    "attempt reset paused because the cohort could not be claimed"
                )
            if exact_scope:
                raise ValueError(
                    "exact replay paused because the cohort could not be claimed"
                )
            continue
        if incident_permit is not None and (
            superseded.get("event_key") != incident_permit["event_key"]
            or superseded.get("superseded") is not True
        ):
            raise ValueError(
                "incident permit requires the exact prior claim to be superseded"
            )
        if superseded["superseded"]:
            print("replay superseded stale triage claim for card #%s" % plan["number"])

        if incident_permit is not None:
            consumed_state = current["state"]

            def prepare_body(body, plan=plan, consumed_state=consumed_state):
                return _body_with_incident_queue_transition(
                    body, plan, consumed_state
                )

        else:

            def prepare_body(body, plan=plan):
                return _body_with_replay_marker(body, plan, wave, run_number)

        if reconcile.maybe_queue_auto_triage(
            plan["item"],
            current,
            has_token,
            owner=owner,
            prepare_body=prepare_body,
            publish_budget_deferral=False,
        ):
            queued += 1
            if incident_permit is None:
                written += 1
        else:
            print(
                "::warning::replay queueing deferred without card unlock for card #%s"
                % plan["number"]
            )
            if attempt_reset_scope:
                raise ValueError(
                    "attempt reset paused because a sanctioned card could not be queued"
                )
            if exact_scope:
                raise ValueError(
                    "exact replay paused because a requested card could not be queued"
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
    root.add_argument(
        "--exact-cards",
        default="",
        help="Versioned exact selector v1:N[,N...] (max 25; no whitespace, "
        "ranges, duplicates, or leading zeroes). Card order is canonicalized "
        "ascending and count must equal --limit.",
    )
    root.add_argument(
        "--attempts-reset-cards",
        default="",
        help="Incident-scoped comma-separated card numbers. Non-empty is "
        "accepted only for the exact sanctioned cohort and wave.",
    )
    return root


def main():
    args = parser().parse_args()
    try:
        run(
            args.cards,
            args.wave,
            args.limit,
            dry_run=args.dry_run,
            attempts_reset_cards=args.attempts_reset_cards,
            exact_cards=args.exact_cards,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print("::error::triage replay refused: %s" % str(error)[:240])
        raise SystemExit(1)


if __name__ == "__main__":
    main()
