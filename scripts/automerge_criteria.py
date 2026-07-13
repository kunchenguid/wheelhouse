#!/usr/bin/env python3
"""Shared schema for auto-merge eligibility criteria shown on decision cards.

The authoritative evaluator in auto_merge.py produces rows in this schema.
render_card.py only normalizes and presents those rows. Card-side criterion data
is advisory, non-material state: auto_merge.py always re-evaluates live facts and
never trusts a displayed or persisted row to authorize a merge.
"""

CRITERIA_VERSION = 1
STATUS_MET = "met"
STATUS_UNMET = "unmet"
STATUS_UNAVAILABLE = "unavailable"
STATUSES = frozenset({STATUS_MET, STATUS_UNMET, STATUS_UNAVAILABLE})

# Stable IDs and labels are a card UI contract. Keep this order aligned with the
# existing G0-G7 policy order in auto_merge.py.
CRITERIA_SPECS = (
    ("scope_candidate", "Scope - merge-ready PR review"),
    ("scan_complete", "Safety - complete healthy scan"),
    ("g0_repo_enabled", "G0 - repository auto-merge enabled"),
    ("g0_vision_present", "G0 - default-branch VISION.md present"),
    ("g1_card_identity", "G1 - trusted unique decision card"),
    ("g1_card_published", "G1 - card published after triage"),
    ("g1_card_claim", "G1 - exclusive card claim"),
    ("g2_files_complete", "G2 - complete immutable file list"),
    ("g2_exclusions_clear", "G2 - workflow and security exclusions clear"),
    ("g3_author_identity", "G3 - non-maintainer human contributor"),
    ("g3_prior_merge", "G3 - prior merged contribution in this repo"),
    ("g4_checks_green", "G4 - configured checks green"),
    ("g4_mergeable", "G4 - PR mergeable"),
    ("g4_clean", "G4 - merge state clean"),
    ("g5_file_limit", "G5 - changed-file limit"),
    ("g5_line_limit", "G5 - changed-line limit"),
    ("g6_triage_available", "G6 - automatic triage available"),
    ("g6_triage_success", "G6 - successful triage for current head"),
    ("g6_merge_recommendation", "G6 - top-level recommendation is merge"),
    ("g6_behavior_class", "G6 - eligible behavior class"),
    ("g6_vision_alignment", "G6 - behavior aligns with VISION.md"),
    ("g6_default_behavior", "G6 - existing/default behavior unchanged"),
    ("g6_verdict_merge", "G6 - behavior verdict recommends merge"),
    ("g6_class_c_mode", "G6 - class C is opt-in and default-off"),
    ("g6_vision_revision", "G6 - verdict uses current VISION.md revision"),
    ("g6_base_revision", "G6 - verdict uses current base revision"),
    ("safety_target_open", "Safety - target PR is open"),
    ("safety_escape_hatch", "Safety - no per-PR auto-merge opt-out"),
    ("safety_head_current", "Safety - head SHA unchanged"),
    ("g7_immediate_recheck", "G7 - immediate live recheck and manual merge gate"),
)

CRITERIA_IDS = tuple(spec[0] for spec in CRITERIA_SPECS)
CRITERIA_LABELS = dict(CRITERIA_SPECS)


def unavailable_criteria(reason="criterion evidence was not produced"):
    text = str(reason or "criterion evidence was not produced").strip()
    return [
        {
            "id": criterion_id,
            "label": label,
            "status": STATUS_UNAVAILABLE,
            "evidence": text,
        }
        for criterion_id, label in CRITERIA_SPECS
    ]


def normalize_criteria(rows, missing_reason="criterion evidence was not produced"):
    """Return one strict, ordered row for every stable criterion.

    Unknown, duplicate, malformed, or missing rows never become met. They
    degrade to explicit unavailable rows so old cards and partial handoffs fail
    closed in the UI.
    """
    by_id = {}
    duplicates = set()
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            criterion_id = str(row.get("id") or "").strip()
            if criterion_id not in CRITERIA_LABELS:
                continue
            if criterion_id in by_id:
                duplicates.add(criterion_id)
                continue
            status = str(row.get("status") or "").strip().lower()
            if status not in STATUSES:
                status = STATUS_UNAVAILABLE
            evidence = str(row.get("evidence") or "").strip()
            by_id[criterion_id] = {
                "id": criterion_id,
                "label": CRITERIA_LABELS[criterion_id],
                "status": status,
                "evidence": evidence or str(missing_reason),
            }
    normalized = []
    for criterion_id, label in CRITERIA_SPECS:
        if criterion_id in duplicates:
            row = None
            reason = "duplicate criterion evidence was rejected"
        else:
            row = by_id.get(criterion_id)
            reason = missing_reason
        normalized.append(
            row
            or {
                "id": criterion_id,
                "label": label,
                "status": STATUS_UNAVAILABLE,
                "evidence": str(reason or "criterion evidence was not produced"),
            }
        )
    return normalized
