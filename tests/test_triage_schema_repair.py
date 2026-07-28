#!/usr/bin/env python3
"""
The context-equivalent single correction turn plus the evidence-quote UTF-8
byte policy (cards #551/#547 lineage; card #1693 byte-policy class).

Any DELIVERED triage candidate that fails the complete bound action schema,
the evidence-quote byte policy, or trusted evidence anchoring - including
candidates the looser advisory parser can consume - is eligible for exactly
ONE correction turn: the ORIGINAL AgentTask rebuilt from its verified handoff
(same action, model, tools, search, network boundaries, and immutable inputs)
with the rejected candidate and every trusted validation error appended to the
original prompt. Missing results and infrastructure failures (auth / quota /
rate-limit / transport / sandbox / timeout / provider) never enter correction.
The corrected result is revalidated through the same trusted guards before any
authority; a failed correction leaves an advisory-consumable original
explicitly advisory-only, and anything less records the visible
triage-unavailable error carrying the structural reason. The legacy no-tool
repair helpers remain only for the disabled codex inline evidence branch.

These tests are OFFLINE: pure helpers, mocked card I/O, the real bridge over
synthetic transcripts, and static YAML inspection - the live LLM turn is only
exercised end-to-end in CI.

Run: python tests/test_triage_schema_repair.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import render_card as rc  # noqa: E402
import wheelhouse_core as core  # noqa: E402

# Schema-repair tests isolate model-result normalization from cross-repo gate
# reads. Atomic evaluator/write behavior is covered in test_automerge_card_ui.py.
rc._evaluate_automerge_card_projection = lambda *args, **kwargs: (
    rc.criteria_schema.unavailable_criteria("offline schema-repair fixture")
)

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


VALID = {
    "summary": "Adds bounded stop conditions to crewmate briefs.",
    "product_implications": "Internal maintenance change; no product discussion needed.",
    "recommended_action": "comment",
    "recommended_reason": "Scope is small and well contained; leave a note.",
    "evidence": 'target.txt: "add bounded stop conditions to crewmate briefs"',
}


def exec_events(result_text):
    """A minimal Claude execution transcript ending in a successful result."""
    return [
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": result_text,
        }
    ]


def write_exec(path, result_text):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(exec_events(result_text), f)
    return path


def target_file_with(
    d,
    text="<target-content>\n# add bounded stop conditions to crewmate briefs\n</target-content>",
):
    p = os.path.join(d, "target.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


# --------------------------------------------------------------------------- #
# 1. triage_schema_reason: structural, precise, LEAK-FREE
# --------------------------------------------------------------------------- #
def test_schema_reason_is_structural_and_leak_free():
    check(
        "reason: a valid result yields no reason",
        rc.triage_schema_reason(json.dumps(VALID)) == "",
    )

    # Each schema-miss variant yields a short structural reason that names the
    # FIELD and the DEFECT, never a field VALUE.
    missing_summary = {k: v for k, v in VALID.items() if k != "summary"}
    r = rc.triage_schema_reason(json.dumps(missing_summary))
    check("reason: missing summary named", "summary" in r and r != "")

    SECRET = "SUPER-SECRET-TARGET-CONTENT-9F3A"
    list_field = dict(VALID)
    list_field["product_implications"] = [SECRET]  # wrong type carrying "content"
    r2 = rc.triage_schema_reason(json.dumps(list_field))
    check("reason: wrong-typed field named", "product_implications" in r2)
    check(
        "reason(NO LEAK): a mistyped field's value never appears in the reason",
        SECRET not in r2,
    )

    blank_ev = dict(VALID)
    blank_ev["evidence"] = "   "
    r3 = rc.triage_schema_reason(json.dumps(blank_ev))
    check("reason: blank evidence named", "evidence" in r3)

    # A missing recommended_action with no legacy recommended_next_step fallback.
    no_action = {k: v for k, v in VALID.items() if k != "recommended_action"}
    r4 = rc.triage_schema_reason(json.dumps(no_action))
    check("reason: missing recommended_action explained", "recommended_action" in r4)

    check(
        "reason: no JSON object at all",
        rc.triage_schema_reason("here is my prose answer, no json") != "",
    )
    check("reason: empty text explained", rc.triage_schema_reason("") != "")

    # The evidence VALUE (verbatim target quotes) is never surfaced in any
    # reason - even when the evidence field itself is what's wrong.
    leaky = dict(VALID)
    leaky["evidence"] = 123  # not a string
    r5 = rc.triage_schema_reason(json.dumps(leaky))
    check("reason: non-string evidence named", "evidence" in r5)


def test_redacted_candidate_shape_is_content_free():
    SECRET = "RAW-TARGET-DIFF-abc123"
    # Content stuffed into VALUES and into an UNEXPECTED KEY must never surface.
    candidate = {
        "summary": SECRET,
        "product_implications": SECRET,
        "evidence": SECRET,
        SECRET: "sneaky key name",  # a model-chosen key carrying content
    }
    shape = rc.redacted_candidate_shape(json.dumps(candidate))
    check("shape: never echoes a value", SECRET not in shape)
    check(
        "shape: reports known present fields",
        "summary" in shape and "product_implications" in shape,
    )
    check("shape: counts unknown keys without naming them", "unknown_keys=1" in shape)
    check(
        "shape: unparseable text is labeled",
        rc.redacted_candidate_shape("not json at all") == "unparseable-json",
    )

    # A candidate MISSING a required field lists it under missing=[...].
    missing_shape = rc.redacted_candidate_shape(
        json.dumps({k: v for k, v in VALID.items() if k != "summary"})
    )
    check(
        "shape: a missing required field is listed",
        "summary" in missing_shape.split("missing=[")[1],
    )
    # A complete-but-mistyped candidate still yields a content-free shape.
    complete = dict(VALID)
    complete["evidence"] = 123
    shp = rc.redacted_candidate_shape(json.dumps(complete))
    check(
        "shape: present list uses only allowlisted names",
        all(
            k in rc._KNOWN_TRIAGE_KEYS
            for k in shp.split("present=[")[1].split("]")[0].split(",")
            if k
        ),
    )


# --------------------------------------------------------------------------- #
# 2. build_repair_prompt: self-contained, schema-complete, bounded
# --------------------------------------------------------------------------- #
def test_build_repair_prompt():
    for kind, enum, absent in (
        ("pr-review", "merge | request-changes", None),
        ("issue-triage", "close | decline | hold", "merge | request-changes"),
    ):
        p = rc.build_repair_prompt(json.dumps(VALID), kind)
        for field in (
            "summary",
            "product_implications",
            "recommended_action",
            "recommended_reason",
            "evidence",
        ):
            check("prompt(%s): names required field %s" % (kind, field), field in p)
        check("prompt(%s): kind-specific action enum" % kind, enum in p)
        if absent:
            check(
                "prompt(%s): omits the other kind's action enum" % kind, absent not in p
            )
        check(
            "prompt(%s): embeds the candidate" % kind,
            VALID["summary"] in p and "<candidate>" in p,
        )
        check(
            "prompt(%s): forbids reading files / re-analysis" % kind,
            "NO tools" in p and "re-analysis" in p.lower(),
        )
        check("prompt(%s): requires verbatim evidence" % kind, "VERBATIM" in p)
        check(
            "prompt(%s): requires evidence as one string" % kind,
            "single JSON string, not an array" in p,
        )
        check(
            "prompt(%s): output-only-JSON instruction" % kind,
            "ONLY a single compact JSON object" in p,
        )

    # Schema lockstep: every field the repair prompt promises must be a field
    # the validator actually requires, so repair can never target a stale schema.
    prompt = rc.build_repair_prompt("{}", "pr-review")
    for field in rc.TRIAGE_FIELDS:
        check(
            "lockstep: validator field %s is in the repair schema" % field,
            field in prompt,
        )
    check(
        "lockstep: evidence field is in the repair schema", rc.EVIDENCE_FIELD in prompt
    )

    # A pathological (huge) candidate is byte-bounded so the repair prompt can
    # never re-introduce the E2BIG class the pass-by-reference redesign fixed.
    huge = json.dumps({"summary": "x" * 200000, "junk": "y" * 200000})
    pbig = rc.build_repair_prompt(huge, "pr-review")
    check(
        "prompt: oversized candidate is truncated",
        "[candidate truncated: retained" in pbig,
    )
    check(
        "prompt: bounded well under MAX_ARG_STRLEN even for a huge candidate",
        len(pbig.encode("utf-8")) < 60000,
    )


# --------------------------------------------------------------------------- #
# 3. plan_triage_repair: TRIGGER discipline (clause 1)
# --------------------------------------------------------------------------- #
def test_plan_trigger_discipline():
    valid = rc.plan_triage_repair(json.dumps(VALID), "pr-review")
    check(
        "plan: a valid delivered result needs NO repair",
        valid["repair_needed"] is False,
    )
    check("plan: valid result carries no prompt", valid["prompt"] == "")

    array_evidence = dict(
        VALID,
        evidence=[
            'target.txt: "add bounded stop conditions to crewmate briefs"',
            'target-src/brief.py: "bounded stop conditions"',
        ],
    )
    array_plan = rc.plan_triage_repair(json.dumps(array_evidence), "pr-review")
    check(
        "plan: array evidence is accepted by primary validation without repair",
        array_plan["repair_needed"] is False,
    )

    # Schema-miss = delivered but invalid -> repair with a prompt.
    invalid = {k: v for k, v in VALID.items() if k != "recommended_action"}
    p = rc.plan_triage_repair(json.dumps(invalid), "pr-review")
    check("plan: a delivered schema-miss needs repair", p["repair_needed"] is True)
    check("plan: schema-miss carries a repair prompt", bool(p["prompt"]))
    check(
        "plan: schema-miss carries a structural reason",
        "recommended_action" in p["reason"],
    )

    # EXCLUDED: no delivered result at all (E2BIG / missing / auth / rate-limit /
    # infra all leave nothing extractable) -> NEVER repair.
    for label, text in (
        ("empty string", ""),
        ("whitespace only", "   \n  "),
    ):
        pl = rc.plan_triage_repair(text, "pr-review")
        check(
            "plan(EXCLUDED %s): missing result never triggers repair" % label,
            pl["repair_needed"] is False,
        )
        check("plan(EXCLUDED %s): no repair prompt" % label, pl["prompt"] == "")


# --------------------------------------------------------------------------- #
# 4. decide_triage_apply: routing incl. the excluded classes
# --------------------------------------------------------------------------- #
def test_decide_routing():
    with tempfile.TemporaryDirectory() as d:
        tf = target_file_with(d)
        invalid = json.dumps(
            {k: v for k, v in VALID.items() if k != "recommended_action"}
        )
        valid = json.dumps(VALID)

        # success-on-repair (clause 6): invalid original + valid repaired.
        dec = rc.decide_triage_apply(invalid, valid, tf)
        check(
            "route: schema-miss + valid repair -> repaired",
            dec["outcome"] == "repaired",
        )
        check(
            "route: repaired carries the raw repaired dict",
            isinstance(dec["triage"], dict),
        )
        check(
            "route: repaired reason is the ORIGINAL structural failure",
            "recommended_action" in dec["reason"],
        )
        bound_original = dict(VALID)
        bound_original.pop("recommended_action")
        bound_original["recommendation_basis"] = {
            "kind": "other",
            "observation_id": "sha256:" + "0" * 64,
            "context_id": "sha256:" + "1" * 64,
            "check_names": [],
        }
        bound_repair = rc.decide_triage_apply(
            json.dumps(bound_original), valid, tf
        )
        # The context-equivalent correction is a COMPLETE replacement validated
        # on its own: trusted code never merges fields (like a basis) from the
        # rejected candidate into the corrected result.
        check(
            "route: corrected result is complete - no basis restore from the rejected candidate",
            bound_repair["outcome"] == "repaired"
            and "recommendation_basis" not in bound_repair["triage"],
        )

        # repair-failure cap (clause 6): invalid original + still-invalid repair.
        dec2 = rc.decide_triage_apply(invalid, invalid, tf)
        check(
            "route: schema-miss + still-invalid repair -> repair-failed",
            dec2["outcome"] == "repair-failed",
        )
        check(
            "route: repair-failed reports the repaired schema stage",
            "recommended_action" in dec2["reason"],
        )

        # schema-miss with NO correction supplied and no claim outcome known:
        # honest telemetry says a correction was never attempted, so the visible
        # reason is the ORIGINAL structural failure.
        dec3 = rc.decide_triage_apply(invalid, "", tf)
        check(
            "route: schema-miss + no repair output -> repair-failed",
            dec3["outcome"] == "repair-failed",
        )
        check(
            "route: unattempted correction reports the original structural stage",
            "recommended_action" in dec3["reason"]
            and dec3["correction_attempted"] is False,
        )
        claimed = rc.decide_triage_apply(invalid, "", tf, repair_claim_admitted=True)
        check(
            "route: claimed-but-missing correction reports the actual stage",
            claimed["outcome"] == "repair-failed"
            and claimed["reason"] == "correction produced no result"
            and claimed["correction_attempted"] is True,
        )
        duplicate = rc.decide_triage_apply(invalid, "", tf, repair_claim_admitted=False)
        check(
            "route: duplicate repair claim reports the actual stage",
            duplicate["reason"] == "schema repair claim was duplicate",
        )

        # original already valid -> success, repair never consulted.
        dec4 = rc.decide_triage_apply(valid, valid, tf)
        check(
            "route: valid original -> success (repair ignored)",
            dec4["outcome"] == "success",
        )

        # EXCLUDED: no delivered result -> no-result, never repair-* .
        dec5 = rc.decide_triage_apply("", "", tf)
        check(
            "route(EXCLUDED): empty result -> no-result", dec5["outcome"] == "no-result"
        )

        # Fabricated evidence (parse-valid, unanchored) is now part of the
        # correction-eligible class: the correction turn has the same evidence
        # access as the primary and CAN produce genuinely anchored quotes.
        # With no correction result supplied, the consume side lands on the
        # visible failure carrying the anchor reason - never silent success and
        # never the advisory path (advisory consumption requires anchoring).
        fabricated = dict(VALID)
        fabricated["evidence"] = (
            'target.txt: "a quote that does not appear in the fetched target at all"'
        )
        dec6 = rc.decide_triage_apply(json.dumps(fabricated), "", tf)
        check(
            "route: unanchored primary is correction-class, not advisory",
            dec6["outcome"] == "repair-failed"
            and dec6["reason"]
            == "evidence quotes did not match the fetched target",
        )

        # The corrected output must STILL pass the evidence anchor guard: a
        # correction that dropped/fabricated evidence is rejected.
        repaired_fabricated = json.dumps(fabricated)
        dec7 = rc.decide_triage_apply(invalid, repaired_fabricated, tf)
        check(
            "route: a correction with non-anchoring evidence -> repair-failed",
            dec7["outcome"] == "repair-failed",
        )
        check(
            "route: corrected anchor failure reports the actual stage",
            dec7["reason"]
            == "corrected field 'evidence' did not anchor to the fetched target",
        )

        # A trusted-bridge validation failure on the primary (e.g. the bound
        # schema or byte policy) makes an advisory-consumable candidate the
        # correction class even though it parses and anchors locally: a valid
        # correction wins with full authority, and a failed/absent correction
        # leaves the original explicitly advisory-only.
        dec8 = rc.decide_triage_apply(
            valid, valid, tf, primary_error_code="output.schema_invalid"
        )
        check(
            "route: bridge-invalid primary + valid correction -> repaired",
            dec8["outcome"] == "repaired"
            and dec8["reason"] == "primary validation failed (output.schema_invalid)",
        )
        provenance_calls = []
        saved_provenance = rc.enforce_triage_source_provenance
        rc.enforce_triage_source_provenance = (
            lambda data, provenance_file, *_args, **expected: (
                provenance_calls.append((provenance_file, expected["event_key"]))
                or data
            )
        )
        try:
            provenance_decision = rc.decide_triage_apply(
                valid,
                valid,
                tf,
                primary_error_code="output.schema_invalid",
                source_provenance_file="primary-provenance.json",
                repair_source_provenance_file="repair-provenance.json",
                source_provenance_expected={"event_key": "primary-event"},
                repair_source_provenance_expected={"event_key": "repair-event"},
            )
        finally:
            rc.enforce_triage_source_provenance = saved_provenance
        check(
            "route: corrected result uses its own provenance and event binding",
            provenance_decision["outcome"] == "repaired"
            and provenance_calls == [("repair-provenance.json", "repair-event")],
        )
        dec9 = rc.decide_triage_apply(
            valid, "", tf, primary_error_code="output.schema_invalid"
        )
        check(
            "route: bridge-invalid advisory-consumable primary without correction -> advisory",
            dec9["outcome"] == "advisory"
            and isinstance(dec9["triage"], dict)
            and dec9["correction_attempted"] is False,
        )
        dec10 = rc.decide_triage_apply(
            valid,
            valid,
            tf,
            primary_error_code="output.schema_invalid",
            repair_error_code="output.schema_invalid",
        )
        check(
            "route: bridge-invalid correction can never take the authority path",
            dec10["outcome"] == "advisory"
            and dec10["failed_reason"]
            == "corrected result failed trusted validation (output.schema_invalid)"
            and dec10["correction_attempted"] is True,
        )
        oversized = dict(VALID)
        oversized["evidence"] = [
            "target.txt: 'add bounded stop conditions to crewmate briefs'",
            "x" * 2049,
        ]
        dec11 = rc.decide_triage_apply(json.dumps(oversized), "", tf)
        check(
            "route: local byte-policy violation is correction-class, not success",
            dec11["outcome"] in {"advisory", "repair-failed"}
            and "2048 UTF-8 bytes" in (dec11.get("reason") or ""),
        )

        array_valid = dict(
            VALID,
            evidence=[
                "target.txt: 'add bounded stop conditions to crewmate briefs'",
                "target-src/brief.py: unrelated source quote",
            ],
        )
        array_decision = rc.decide_triage_apply(json.dumps(array_valid), "", tf)
        check(
            "route: array evidence anchors on the primary path",
            array_decision["outcome"] == "success",
        )


# --------------------------------------------------------------------------- #
# 5. Telemetry persistence + non-materiality (clause 5)
# --------------------------------------------------------------------------- #
def _queued_body():
    os.environ["GITHUB_REPOSITORY_OWNER"] = "kunchenguid"
    it = {
        "repo": "firstmate",
        "number": 469,
        "kind": "issue-triage",
        "head_sha": "",
        "updated_at": "8b7547c1",
        "title": "Add stops",
        "author": "stoneymarrow",
        "bucket": "issue-triage",
        "comp": "n/a",
        "tests": "n/a",
        "url": "https://github.com/kunchenguid/firstmate/issues/469",
        "summary": "compliance=pass tests=green",
        "recommendation": "Look closer.",
        "priority": "med",
        "options": ["merge", "investigate"],
    }
    return it, rc.body_with_triage_queued(rc.render(it)["body"], it)


def test_repair_telemetry_and_non_materiality():
    it, queued = _queued_body()

    repaired = rc.body_with_triage_result(
        queued,
        "8b7547c1",
        triage=VALID,
        repair_status="repaired",
        repair_reason="field 'summary' is empty",
    )
    st = core.parse_state_block(repaired)
    check(
        "telemetry: repaired records triage_status succeeded",
        st.get("triage_status") == "succeeded",
    )
    check(
        "telemetry: repaired records repair_status",
        st.get("triage_repair_status") == "repaired",
    )
    check(
        "telemetry: repaired records the structural reason",
        st.get("triage_repair_reason") == "field 'summary' is empty",
    )
    check(
        "telemetry: repaired renders the real triage section",
        "### Triage" in repaired and VALID["summary"] in repaired,
    )

    check(
        "telemetry: repaired records the redacted candidate shape",
        rc.body_with_triage_result(
            queued,
            "8b7547c1",
            triage=VALID,
            repair_status="repaired",
            repair_reason="r",
            repair_candidate="present=[summary] missing=[]",
        ).count("triage_repair_candidate")
        == 1,
    )

    failed = rc.body_with_triage_result(
        queued,
        "8b7547c1",
        error="%s (%s)"
        % (rc.TRIAGE_UNAVAILABLE, "field 'evidence' is missing or empty"),
        repair_status="repair-failed",
        repair_reason="field 'evidence' is missing or empty",
        repair_candidate="present=[summary,product_implications] missing=[evidence]",
    )
    st2 = core.parse_state_block(failed)
    check(
        "telemetry: repair-failed records triage_status error",
        st2.get("triage_status") == "error",
    )
    check(
        "telemetry: repair-failed records repair_status",
        st2.get("triage_repair_status") == "repair-failed",
    )
    check(
        "telemetry: repair-failed records the redacted candidate",
        "evidence" in (st2.get("triage_repair_candidate") or ""),
    )
    check(
        "telemetry: repair-failed visible error carries the reason",
        rc.TRIAGE_UNAVAILABLE in failed and "evidence" in failed,
    )

    # A normal (non-repair) write clears any stale repair telemetry.
    normal = rc.body_with_triage_result(repaired, "8b7547c1", triage=VALID)
    st3 = core.parse_state_block(normal)
    check(
        "telemetry: a non-repair write clears repair_status",
        st3.get("triage_repair_status") is None,
    )
    check(
        "telemetry: a non-repair write clears repair_reason",
        st3.get("triage_repair_reason") is None,
    )

    # NON-MATERIAL: the repair fields never enter material comparison.
    check(
        "material: repair fields are not MATERIAL_FIELDS",
        all(
            f not in rc.MATERIAL_FIELDS
            for f in ("triage_repair_status", "triage_repair_reason")
        ),
    )
    normal_success = core.parse_state_block(
        rc.body_with_triage_result(queued, "8b7547c1", triage=VALID)
    )
    repaired_success = core.parse_state_block(repaired)
    diff = {
        k
        for k in set(normal_success) | set(repaired_success)
        if normal_success.get(k) != repaired_success.get(k)
    }
    check(
        "material: repaired vs normal success differ ONLY by repair telemetry",
        diff == {"triage_repair_status", "triage_repair_reason"},
    )


def test_same_revision_refresh_preserves_repair_telemetry():
    it, queued = _queued_body()
    repaired = rc.body_with_triage_result(
        queued,
        it["head_sha"],
        triage=VALID,
        repair_status="repaired",
        repair_reason="field 'summary' is empty",
        repair_candidate="present=[summary] missing=[]",
    )
    old_state = core.parse_state_block(repaired)
    refreshed = rc._preserve_same_revision_triage(
        rc.render(it)["body"], repaired, it, old_state, owner="kunchenguid"
    )
    state = core.parse_state_block(refreshed)
    check(
        "telemetry: same-revision refresh preserves all repair fields",
        all(
            state.get(key) == old_state.get(key)
            for key in (
                "triage_repair_status",
                "triage_repair_reason",
                "triage_repair_candidate",
            )
        ),
    )


# --------------------------------------------------------------------------- #
# 6. No leakage of target/comment content into PERSISTED diagnostics (clause 6)
# --------------------------------------------------------------------------- #
def test_no_leak_in_persisted_diagnostics():
    it, queued = _queued_body()
    SECRET = "PROPRIETARY-DIFF-LINE-DO-NOT-PERSIST-7Q2"
    # A candidate whose invalid field carries "raw target content".
    candidate = dict(VALID)
    candidate["product_implications"] = [SECRET]  # wrong type -> schema-miss
    reason = rc.triage_schema_reason(json.dumps(candidate))
    shape = rc.redacted_candidate_shape(json.dumps(candidate))
    check(
        "leak: the derived reason omits the candidate's content", SECRET not in reason
    )
    check(
        "leak: the redacted candidate shape omits the candidate's content",
        SECRET not in shape,
    )

    failed = rc.body_with_triage_result(
        queued,
        "8b7547c1",
        error="%s (%s)" % (rc.TRIAGE_UNAVAILABLE, reason),
        repair_status="repair-failed",
        repair_reason=reason,
        repair_candidate=shape,
    )
    check(
        "leak: the persisted card body never carries the raw content",
        SECRET not in failed,
    )
    st = core.parse_state_block(failed)
    check(
        "leak: the persisted repair telemetry never carries the raw content",
        SECRET not in json.dumps(st),
    )


# --------------------------------------------------------------------------- #
# 7. CLI: triage-repair-prep emits $GITHUB_OUTPUT (offline)
# --------------------------------------------------------------------------- #
def test_cli_repair_prep():
    with tempfile.TemporaryDirectory() as d:
        invalid = write_exec(
            os.path.join(d, "inv.json"),
            json.dumps({k: v for k, v in VALID.items() if k != "summary"}),
        )
        valid = write_exec(os.path.join(d, "val.json"), json.dumps(VALID))
        missing = os.path.join(d, "empty.json")
        with open(missing, "w") as f:
            json.dump([{"type": "result", "is_error": True, "result": ""}], f)
        gho = os.path.join(d, "gho.txt")

        def run(exec_file, kind):
            open(gho, "w").close()
            proc = subprocess.run(
                [
                    sys.executable,
                    os.path.join(ROOT, "scripts", "render_card.py"),
                    "triage-repair-prep",
                    "--execution-file",
                    exec_file,
                    "--kind",
                    kind,
                ],
                env={**os.environ, "GITHUB_OUTPUT": gho},
                capture_output=True,
                text=True,
            )
            return proc, open(gho).read()

        _, out = run(invalid, "pr-review")
        check("cli: schema-miss sets repair_needed=true", "repair_needed=true" in out)
        check(
            "cli: schema-miss emits a repair_prompt heredoc", "repair_prompt<<" in out
        )
        # The heredoc must be well-formed (matching random delimiter) and embed
        # the candidate - but MUST NOT inline target.txt (pass-by-reference).
        import re

        m = re.search(r"repair_prompt<<(\S+)\n(.*?)\n\1\n", out, re.S)
        check("cli: repair_prompt heredoc is well-formed", bool(m))
        check(
            "cli: repair prompt embeds the candidate",
            bool(m) and VALID["product_implications"] in m.group(2),
        )

        _, out2 = run(valid, "pr-review")
        check(
            "cli: a valid result sets repair_needed=false",
            "repair_needed=false" in out2,
        )
        check(
            "cli: a valid result emits no repair prompt", "repair_prompt<<" not in out2
        )

        _, out3 = run(missing, "issue-triage")
        check(
            "cli(EXCLUDED): missing result sets repair_needed=false",
            "repair_needed=false" in out3,
        )
        check(
            "cli(EXCLUDED): missing result emits no repair prompt",
            "repair_prompt<<" not in out3,
        )


# --------------------------------------------------------------------------- #
# 8. CLI: triage-apply --repair-execution-file end-to-end (mocked card I/O)
# --------------------------------------------------------------------------- #
def _run_triage_apply(
    issue,
    revision,
    card_body,
    exec_file,
    repair_file,
    target_file,
    repair_claim_admitted="",
):
    """Drive the real triage-apply CLI branch with card reads/writes mocked."""
    captured = {}
    orig_get, orig_edit, orig_argv = rc.get_card, rc._edit_issue_body, sys.argv
    original_output = os.environ.get("GITHUB_OUTPUT")
    rc.get_card = lambda n: {
        "number": int(n),
        "body": card_body,
        "labels": [{"name": "needs-decision"}],
        "state": "OPEN",
    }
    rc._edit_issue_body = lambda number, body, remove_labels=None: captured.update(
        {"body": body, "remove": remove_labels}
    )
    try:
        with tempfile.NamedTemporaryFile() as output:
            os.environ["GITHUB_OUTPUT"] = output.name
            sys.argv = [
                "render_card.py",
                "triage-apply",
                "--issue",
                str(issue),
                "--revision",
                revision,
                "--execution-file",
                exec_file,
                "--repair-execution-file",
                repair_file,
                "--repair-claim-admitted",
                repair_claim_admitted,
                "--target-file",
                target_file,
            ]
            rc.main()
            captured["outputs"] = Path(output.name).read_text(encoding="utf-8")
    finally:
        rc.get_card, rc._edit_issue_body, sys.argv = orig_get, orig_edit, orig_argv
        if original_output is None:
            os.environ.pop("GITHUB_OUTPUT", None)
        else:
            os.environ["GITHUB_OUTPUT"] = original_output
    return captured


def _run_triage_fail(issue, revision, card_body):
    captured = {}
    orig_get, orig_edit, orig_argv = rc.get_card, rc._edit_issue_body, sys.argv
    original_output = os.environ.get("GITHUB_OUTPUT")
    rc.get_card = lambda n: {
        "number": int(n),
        "body": card_body,
        "labels": [{"name": "needs-decision"}],
        "state": "OPEN",
    }
    rc._edit_issue_body = lambda number, body, remove_labels=None: captured.update(
        {"body": body, "remove": remove_labels}
    )
    try:
        with tempfile.NamedTemporaryFile() as output:
            os.environ["GITHUB_OUTPUT"] = output.name
            sys.argv = [
                "render_card.py",
                "triage-fail",
                "--issue",
                str(issue),
                "--revision",
                revision,
                "--message",
                "bounded failure",
            ]
            rc.main()
            captured["outputs"] = Path(output.name).read_text(encoding="utf-8")
    finally:
        rc.get_card, rc._edit_issue_body, sys.argv = orig_get, orig_edit, orig_argv
        if original_output is None:
            os.environ.pop("GITHUB_OUTPUT", None)
        else:
            os.environ["GITHUB_OUTPUT"] = original_output
    return captured


def test_cli_triage_apply_repair_end_to_end():
    _, queued = _queued_body()
    with tempfile.TemporaryDirectory() as d:
        tf = target_file_with(d)
        invalid = write_exec(
            os.path.join(d, "inv.json"),
            json.dumps({k: v for k, v in VALID.items() if k != "recommended_action"}),
        )
        valid = write_exec(os.path.join(d, "val.json"), json.dumps(VALID))
        still_invalid = write_exec(
            os.path.join(d, "inv2.json"), json.dumps({"summary": "only this"})
        )

        # success-on-repair: invalid original + valid repair -> real triage card.
        cap = _run_triage_apply(469, "8b7547c1", queued, invalid, valid, tf)
        st = core.parse_state_block(cap.get("body", "")) or {}
        check(
            "e2e: repaired card gets a real triage section",
            "### Triage" in cap.get("body", ""),
        )
        check(
            "e2e: repaired card records success", st.get("triage_status") == "succeeded"
        )
        check(
            "e2e: repaired card records repair telemetry",
            st.get("triage_repair_status") == "repaired",
        )
        check(
            "e2e: repaired card shows the model's summary",
            VALID["summary"] in cap.get("body", ""),
        )
        check(
            "e2e: repaired card reports explicit applied output",
            cap.get("outputs") == "applied=true\ntriage_status=succeeded\n",
        )

        # repair-failure cap: invalid original + still-invalid repair -> visible
        # error carrying the reason, exactly one repair attempt (the CLI consults
        # exactly one repair file - there is no retry loop).
        cap2 = _run_triage_apply(469, "8b7547c1", queued, invalid, still_invalid, tf)
        st2 = core.parse_state_block(cap2.get("body", "")) or {}
        check("e2e: repair-failure records error", st2.get("triage_status") == "error")
        check(
            "e2e: repair-failure records repair-failed telemetry",
            st2.get("triage_repair_status") == "repair-failed",
        )
        check(
            "e2e: repair-failure records the redacted candidate shape",
            bool(st2.get("triage_repair_candidate")),
        )
        check(
            "e2e: repair-failure error carries the repaired structural reason",
            "product_implications" in (st2.get("triage_error") or ""),
        )

        # EXCLUDED classes take the unchanged path with NO repair telemetry.
        empty_exec = os.path.join(d, "noresult.json")
        with open(empty_exec, "w") as f:
            json.dump([{"type": "result", "is_error": True, "result": ""}], f)
        cap3 = _run_triage_apply(469, "8b7547c1", queued, empty_exec, "", tf)
        st3 = core.parse_state_block(cap3.get("body", "")) or {}
        check(
            "e2e(EXCLUDED): missing result records plain error",
            st3.get("triage_status") == "error",
        )
        check(
            "e2e(EXCLUDED): missing result has NO repair telemetry",
            st3.get("triage_repair_status") is None,
        )
        check(
            "e2e(EXCLUDED): missing result keeps the plain unavailable text",
            st3.get("triage_error") == rc.TRIAGE_UNAVAILABLE,
        )

        # A normal valid original still works unchanged, no repair telemetry.
        cap4 = _run_triage_apply(469, "8b7547c1", queued, valid, "", tf)
        st4 = core.parse_state_block(cap4.get("body", "")) or {}
        check(
            "e2e: a valid original succeeds with no repair telemetry",
            st4.get("triage_status") == "succeeded"
            and st4.get("triage_repair_status") is None,
        )

        skipped = _run_triage_apply(469, "newer-revision", queued, valid, "", tf)
        check(
            "e2e: stale card reports explicit rejected output",
            skipped.get("outputs") == "applied=false\ntriage_status=succeeded\n"
            and "body" not in skipped,
        )
        failed = _run_triage_fail(469, "8b7547c1", queued)
        check(
            "e2e: triage-fail reports explicit applied output",
            failed.get("outputs") == "applied=true\ntriage_status=error\n"
            and "body" in failed,
        )
        failed_stale = _run_triage_fail(469, "newer-revision", queued)
        check(
            "e2e: stale triage-fail reports explicit rejected output",
            failed_stale.get("outputs") == "applied=false\ntriage_status=error\n"
            and "body" not in failed_stale,
        )


# --------------------------------------------------------------------------- #
# 9. triage.yml static wiring + token/posture isolation (clause 7)
# --------------------------------------------------------------------------- #
def test_triage_yml_repair_wiring():
    triage_source = read(".github", "workflows", "triage.yml")
    doc = yaml.safe_load(triage_source)
    prepare_job = doc["jobs"]["triage-repair-prepare"]
    prepare_steps = prepare_job["steps"]
    consume_job = doc["jobs"]["triage-claude-consume"]
    consume_steps = consume_job["steps"]
    repair_job = doc["jobs"]["triage-repair-model"]
    model_steps = yaml.safe_load(read(".github", "workflows", "claude-model.yml"))[
        "jobs"
    ]["model"]["steps"]
    check(
        "yaml: every primary evidence schema asks for one JSON string",
        triage_source.count('"evidence": "one string:') == 2
        and triage_source.count('"evidence": "one JSON string') == 1,
    )

    def idx(steps, pred):
        for i, s in enumerate(steps):
            if pred(s):
                return i
        return None

    handoff_i = idx(
        prepare_steps,
        lambda s: s.get("name") == "Download the primary model handoff",
    )
    tr_i = idx(prepare_steps, lambda s: s.get("id") == "primary-result")
    prep_i = idx(prepare_steps, lambda s: s.get("id") == "repair-prep")
    task_i = idx(prepare_steps, lambda s: s.get("id") == "claude-repair-task")
    rep_i = idx(prepare_steps, lambda s: s.get("id") == "claude-repair-model")
    received_i = idx(consume_steps, lambda s: s.get("id") == "repair-result-received")
    compact_i = idx(consume_steps, lambda s: s.get("id") == "compact-results")
    fresh_i = idx(consume_steps, lambda s: s.get("id") == "post-model-freshness")
    upd_i = idx(consume_steps, lambda s: s.get("name") == "Update the decision card")
    target_download_i = idx(
        consume_steps, lambda s: s.get("name") == "Download exact target evidence"
    )

    check("yaml: repair-prep step exists", prep_i is not None)
    check("yaml: Claude repair model boundary exists", rep_i is not None)
    check("yaml: repair-result receiver exists", received_i is not None)
    check(
        "yaml: correction eligibility follows the verified handoff and result receipt",
        None not in (handoff_i, tr_i, prep_i, task_i, rep_i)
        and handoff_i < tr_i < prep_i < task_i < rep_i,
    )
    check(
        "yaml: repair model and projection follow the caller-bound job graph",
        prepare_job.get("needs") == ["triage", "triage-model"]
        and repair_job.get("needs") == "triage-repair-prepare"
        and repair_job.get("uses") == "./.github/workflows/claude-model.yml"
        and all(
            name in consume_job.get("needs", [])
            for name in ("triage-model", "triage-repair-prepare", "triage-repair-model")
        )
        and None not in (received_i, compact_i, fresh_i, upd_i)
        and received_i < compact_i < fresh_i < upd_i,
    )
    check(
        "yaml: missing target evidence cannot block stale projection",
        target_download_i is not None
        and consume_steps[target_download_i].get("if")
        == "${{ needs.triage.outputs.target_artifact != '' }}",
    )

    prep = prepare_steps[prep_i]
    prun = str(prep.get("run", ""))
    check(
        "yaml: repair-prep reads the bind-verified result and handoff",
        prep.get("env", {}).get("RESULT")
        == "${{ steps.primary-result.outputs.result }}"
        and prep.get("env", {}).get("HANDOFF_SHA256")
        == "${{ needs.triage.outputs.handoff_sha256 }}",
    )
    check(
        "yaml: repair-prep runs the trusted correction-eligibility owner",
        "correction-eligible" in prun
        and "scripts/agent_runtime.py" in prun
        and "--expected-revision" in prun
        and "--expected-source-sha" in prun
        and "--handoff-sha256" in prun,
    )
    check(
        "yaml: repair-prep is pass-by-reference (never inlines target.txt)",
        "cat target.txt" not in prun and "gh pr diff" not in prun,
    )
    task_step = prepare_steps[task_i]
    task_run = str(task_step.get("run", ""))
    check(
        "yaml: correction task rebuilds the ORIGINAL task from its handoff",
        "build-correction-task" in task_run
        and "--handoff" in task_run
        and "--result-dir" in task_run
        and "--event-key" in task_run
        and "--action" in task_run
        and task_step.get("env", {}).get("ACTION")
        == "${{ needs.triage.outputs.event_action }}"
        and task_step.get("env", {}).get("EVENT_KEY")
        == "${{ steps.repair-claim.outputs.event_key }}",
    )
    check(
        "yaml: correction task build is claim-gated",
        "steps.repair-claim.outputs.admitted == 'true'"
        in str(task_step.get("if", "")),
    )
    check(
        "yaml: correction model call carries the original search scope",
        prepare_steps[rep_i].get("with", {}).get("allowed-repos")
        == "${{ steps.claude-repair-task.outputs.allowed_repos || '[]' }}",
    )
    claim_i = idx(prepare_steps, lambda s: s.get("id") == "repair-claim")
    claim_run = str(prepare_steps[claim_i].get("run", ""))
    check(
        "yaml: the correction keeps the exact triage.schema-repair claim identity",
        claim_i is not None
        and prep_i < claim_i < task_i
        and "--action triage.schema-repair" in claim_run,
    )

    rep = next(s for s in model_steps if s.get("id") == "triage_repair")
    repw = rep.get("with", {})
    dumped = yaml.safe_dump(rep)
    boundary = prepare_steps[rep_i]
    check(
        "yaml: Claude repair boundary follows its immutable task",
        "steps.claude-repair-task.outcome == 'success'" in str(boundary.get("if", "")),
    )
    check(
        "yaml: repair reusable job binds the caller commit",
        repair_job.get("with", {}).get("expected_commit_sha") == "${{ github.sha }}",
    )
    check(
        "yaml: claude_repair is fail-open (continue-on-error)",
        rep.get("continue-on-error") is True,
    )
    check(
        "yaml: claude_repair uses the pinned action",
        str(rep.get("uses", "")).endswith(
            "af0559ee4f514d1ef21826982bed13f7edc3c35e"
        ),
    )
    check(
        "yaml: Claude repair prompt is hydrated from its AgentTask",
        repw.get("prompt") == "${{ steps.hydrate.outputs.prompt }}",
    )
    check(
        "yaml: claude_repair is exactly one turn",
        "--max-turns 1" in str(repw.get("claude_args", "")),
    )
    check(
        "yaml: claude_repair requests an empty allowlist",
        '--allowedTools ""' in str(repw.get("claude_args", "")),
    )
    settings = str(repw.get("settings", ""))
    check(
        "yaml: claude_repair fail-closed deny of exec/file/network tools",
        '"deny"' in settings
        and all(
            t in settings for t in ("Bash", "Read", "Write", "WebFetch", "Grep", "Glob")
        ),
    )
    check(
        "yaml: claude_repair is tokenless (no FLEET_TOKEN)",
        "FLEET_TOKEN" not in dumped,
    )
    check(
        "yaml: claude_repair is tokenless (no READONLY_TOKEN)",
        "READONLY_TOKEN" not in dumped,
    )
    check(
        "yaml: claude_repair allowed_bots stays narrow",
        repw.get("allowed_bots") == "github-actions[bot]",
    )
    check(
        "yaml: claude_repair uses immutable model",
        "--model claude-sonnet-4-6" in str(repw.get("claude_args", "")),
    )

    received = consume_steps[received_i]
    compact = consume_steps[compact_i]
    check(
        "yaml: repair result crosses only the verified normalized artifact boundary",
        received.get("uses") == "./.github/actions/claude-model-result"
        and received.get("with", {}).get("artifact")
        == "${{ needs.triage-repair-model.outputs.result_artifact }}"
        and compact.get("env", {}).get("REPAIR")
        == "${{ steps.repair-result-received.outputs.result }}"
        and "extract-result" in str(compact.get("run", "")),
    )

    upd = consume_steps[upd_i]
    update_run = str(upd.get("run", ""))
    check(
        "yaml: card update wires the repaired result file into triage-apply",
        upd.get("env", {}).get("REPAIR_EXECUTION_FILE")
        == "${{ steps.compact-results.outputs.repair }}"
        and upd.get("env", {}).get("REPAIR_CLAIM_ADMITTED")
        == "${{ needs.triage-repair-prepare.outputs.repair_admitted }}"
        and "--repair-execution-file" in update_run
        and "--repair-claim-admitted" in update_run,
    )
    check(
        "yaml: repair source drift projects to bounded triage retry",
        upd.get("env", {}).get("REPAIR_ERROR_CODE")
        == "${{ steps.repair-result-received.outputs.error-code }}"
        and '[ "$REPAIR_ERROR_CODE" = "source.revision_mismatch" ]' in update_run
        and "Wheelhouse updated while this request waited; please retry." in update_run,
    )
    check(
        "yaml: corrected consumption carries its own public-clone provenance binding",
        upd.get("env", {}).get("REPAIR_SOURCE_PROVENANCE_FILE")
        == "${{ steps.repair-result-received.outputs.public-clone-provenance }}"
        and upd.get("env", {}).get("REPAIR_SOURCE_REVIEW_EVENT_KEY")
        == "${{ needs.triage-repair-prepare.outputs.repair_event_key }}"
        and "--repair-source-provenance-file" in update_run
        and "--repair-source-review-event-key" in update_run,
    )
    check(
        "yaml: scrubbed consumers preserve the output channel",
        'GITHUB_OUTPUT="$GITHUB_OUTPUT"' in update_run,
    )
    fresh = consume_steps[fresh_i]
    check(
        "yaml: final freshness re-reads the exact target revision fail closed",
        fresh.get("env", {}).get("EXPECTED_REVISION")
        == "${{ needs.triage.outputs.revision }}"
        and "issues/$NUMBER" in str(fresh.get("run", ""))
        and "pulls/$NUMBER" in str(fresh.get("run", ""))
        and "target.stale" in str(fresh.get("run", "")),
    )
    check(
        "yaml: card projection rejects post-model freshness loss",
        "steps.post-model-freshness.outputs.fresh == 'false'"
        in str(upd.get("env", {}).get("HEAD_OK", "")),
    )
    primary_finalize = next(
        s
        for s in consume_steps
        if s.get("name") == "Finalize primary triage claim and stage evidence"
    )
    repair_finalize = next(
        s
        for s in consume_steps
        if s.get("name") == "Finalize schema-repair claim and stage evidence"
    )
    recovery_i = idx(consume_steps, lambda s: s.get("id") == "card-recovery")
    primary_finalize_i = idx(
        consume_steps,
        lambda s: s.get("name") == "Finalize primary triage claim and stage evidence",
    )
    repair_finalize_i = idx(
        consume_steps,
        lambda s: s.get("name") == "Finalize schema-repair claim and stage evidence",
    )
    primary_run = str(primary_finalize.get("run", ""))
    repair_run = str(repair_finalize.get("run", ""))
    record = next(
        s
        for s in consume_steps
        if s.get("name") == "Record the bounded triage attempt result"
    )
    record_run = str(record.get("run", ""))
    check(
        "yaml: admitted schema repair emits event-bound terminal evidence",
        "needs.triage-repair-prepare.outputs.repair_admitted == 'true'"
        in str(repair_finalize.get("if", ""))
        and "--action triage.schema-repair" in repair_run
        and "needs.triage-repair-prepare.outputs.repair_event_key" in repair_run,
    )
    check(
        "yaml: each durable claim is patched before its terminal stage",
        primary_run.index("gh api --method PATCH")
        < primary_run.index("agent_runtime.py stage")
        and repair_run.index("gh api --method PATCH")
        < repair_run.index("agent_runtime.py stage")
        and "always()" in str(repair_finalize.get("if", "")),
    )
    check(
        "yaml: committed evidence requires explicit applied output",
        "steps.card-consumer.outputs.applied" in primary_run
        and "steps.card-consumer.outputs.applied" in repair_run
        and "steps.card-recovery.outputs.applied" in primary_run
        and "steps.card-recovery.outputs.applied" in repair_run
        and primary_run.count('= "true"') >= 1
        and repair_run.count('= "true"') >= 1,
    )
    check(
        "yaml: repair source drift is the recorded triage model code",
        (
            (record.get("env") or {}).get("MODEL_ERROR_CODE")
            == "${{ steps.repair-result-received.outputs.error-code == 'source.revision_mismatch' && steps.repair-result-received.outputs.error-code || steps.primary-result.outputs.error-code }}"
        )
        and 'code="${MODEL_ERROR_CODE:-}"' in record_run,
    )
    check(
        "yaml: committed evidence preserves source revision mismatch",
        all(
            '[ -z "$code" ]; then code="consumer.committed"' in run
            and 'elif [ "$code" != "source.revision_mismatch" ]; then'
            in run
            and 'elif [ -z "$code" ]; then' in run
            and 'code="consumer.rejected"' in run
            for run in (record_run, primary_run)
        ),
    )
    check(
        "yaml: terminal evidence follows fail-open recovery",
        None not in (recovery_i, primary_finalize_i, repair_finalize_i)
        and recovery_i < primary_finalize_i
        and recovery_i < repair_finalize_i,
    )


# --------------------------------------------------------------------------- #
# 10. Evidence-quote UTF-8 byte policy (card #1693 class)
# --------------------------------------------------------------------------- #
# The exact 253-byte production quote from card #1693 (firstmate#1024, revision
# 91a74e4e), recovered verbatim from the durable assessment record
# result_id sha256:033aa02111c846e09b74af857ba8f1f03790cdc12226a8174fa0105baa1a8a40.
# Under the retired 240-character schema bound it made the whole candidate
# `output.schema_invalid` while the advisory parser consumed it, so the card
# stayed advisory-only with no correction. It MUST be valid now.
CARD_1693_QUOTE = (
    "fm_crew_merge_block_rules() {\n  printf '%s' '[\"Bash(gh pr merge:*)\","
    "\"Bash(gh-axi pr merge:*)\",\"Bash(gh api *pulls/*/merge*)\","
    "\"Bash(gh api *repos/*/merges*)\",\"Bash(gh api graphql*mergePullRequest*)\","
    "\"Bash(tk-feature land:*)\",\"Bash(tk-feature-land:*)\"]'\n}"
)


def _pr_candidate_with_quote(quote):
    return {
        "summary": "Restores the crew merge guard rules.",
        "product_implications": "Routine fix; no owner discussion needed.",
        "recommended_action": "merge",
        "recommended_reason": "Narrow corrective fix.",
        "evidence": 'target.txt: "add bounded stop conditions to crewmate briefs"',
        "recommendation_basis": {
            "kind": "other",
            "observation_id": "sha256:" + "1" * 64,
            "context_id": "sha256:" + "2" * 64,
        },
        "automerge": {
            "behavior_class": "A",
            "behavior_assertions": [
                {
                    "claim": "Merge guard rules stay enforced",
                    "subject": "existing_mode",
                    "effect": "unchanged",
                    "evidence": {"source": "target.txt", "quote": quote},
                }
            ],
            "changes_existing_or_default_behavior": False,
            "optin_default_off": False,
        },
    }


def test_evidence_quote_byte_policy():
    from agent_runtime.contract import ContractError, load_json_regular, validate_schema
    from agent_runtime.output_validation import (
        EVIDENCE_QUOTE_MAX_UTF8_BYTES,
        evidence_quote_utf8_byte_violations,
    )
    from agent_runtime.task_builder import _bound_output_schema

    check(
        "bytes: the trusted ceiling is the captain-fixed 2048 inclusive",
        EVIDENCE_QUOTE_MAX_UTF8_BYTES == 2048,
    )
    schema = load_json_regular(
        os.path.join(
            ROOT, "agent_runtime", "schemas", "actions", "triage-pr-v1.schema.json"
        ),
        max_bytes=65536,
    )
    bound = _bound_output_schema(schema, "triage.pr.search", "pr", True, False)

    def trusted_valid(candidate):
        try:
            validate_schema(candidate, bound)
        except ContractError:
            return False
        return not evidence_quote_utf8_byte_violations(candidate)

    quote_1693 = CARD_1693_QUOTE
    check(
        "bytes: the card #1693 fixture is byte-exact (253 UTF-8 bytes)",
        len(quote_1693.encode("utf-8")) == 253,
    )
    check(
        "bytes: the exact 253-byte card #1693 production quote is VALID",
        trusted_valid(_pr_candidate_with_quote(quote_1693)),
    )
    check(
        "bytes: the retired 240-character bound is gone from every quote surface",
        '"maxLength": 240' not in read(
            "agent_runtime", "schemas", "actions", "triage-pr-v1.schema.json"
        ),
    )
    # ASCII boundaries: char count == byte count.
    for n, expect in ((1024, True), (1025, True), (2048, True), (2049, False)):
        check(
            "bytes: %d ASCII-byte quote is %s" % (n, "valid" if expect else "invalid"),
            trusted_valid(_pr_candidate_with_quote("x" * n)) is expect,
        )
    # Multibyte divergence: 1025 two-byte chars = 2050 bytes. The character
    # (secondary) bound passes; ONLY the explicit byte count catches it.
    multibyte_over = _pr_candidate_with_quote("é" * 1025)
    schema_passes = True
    try:
        validate_schema(multibyte_over, bound)
    except ContractError:
        schema_passes = False
    violations = evidence_quote_utf8_byte_violations(multibyte_over)
    check(
        "bytes: 1025 multibyte chars (2050 bytes) pass the char bound but fail the byte count",
        schema_passes
        and len(violations) == 1
        and "2048 UTF-8 bytes (2050)" in violations[0]
        and "behavior_assertions[0]" in violations[0],
    )
    check(
        "bytes: 1024 multibyte chars (2048 bytes) are valid at the inclusive ceiling",
        trusted_valid(_pr_candidate_with_quote("é" * 1024)),
    )
    # Secondary defense: over 2048 characters is necessarily over 2048 bytes,
    # so the schema character bound can never reject a byte-valid quote.
    over_chars = _pr_candidate_with_quote("é" * 2049)
    schema_rejects = False
    try:
        validate_schema(over_chars, bound)
    except ContractError:
        schema_rejects = True
    check("bytes: the 2048-char schema bound remains as secondary defense", schema_rejects)
    # Violations are structural (path + counts), never quote content.
    check(
        "bytes: violation text never echoes quote content",
        all("xxxx" not in v and "é" not in v for v in
            evidence_quote_utf8_byte_violations(_pr_candidate_with_quote("x" * 4000))),
    )
    # Every quote surface is covered: top-level evidence (list and string
    # segments), vision criteria, class-B restoration.
    surfaces = {
        "evidence": {"evidence": ["ok quote", "y" * 3000]},
        "vision": {
            "vision_evidence": {
                "applicable_criteria": [{"id": "c1", "quote": "y" * 3000}]
            }
        },
        "class_b": {
            "automerge": {
                "class_b_restoration": {
                    "corrected_defect_evidence": {"quote": "y" * 3000}
                }
            }
        },
    }
    for name, shape in surfaces.items():
        check(
            "bytes: %s quote surface is byte-checked" % name,
            len(evidence_quote_utf8_byte_violations(shape)) == 1,
        )
    prefixed_boundary = {"evidence": 'target.txt: "%s"' % ("x" * 2048)}
    split_oversized = {
        "evidence": 'target.txt: "%s"'
        % (("x" * 1024) + " | \n" + ("y" * 1024))
    }
    check(
        "bytes: source prefixes are excluded from the quote byte count",
        evidence_quote_utf8_byte_violations(prefixed_boundary) == [],
    )
    split_violations = evidence_quote_utf8_byte_violations(split_oversized)
    check(
        "bytes: separators inside one quoted value cannot evade the ceiling",
        len(split_violations) == 1
        and "2052" in split_violations[0]
        and "$.evidence quote 0" in split_violations[0],
    )
    multibyte_prefixed = {"evidence": 'target.txt: "%s"' % ("é" * 1024)}
    check(
        "bytes: prefixed multibyte quotes honor the inclusive byte boundary",
        evidence_quote_utf8_byte_violations(multibyte_prefixed) == [],
    )
    # The prompt tells the model the 1024-byte rule in every branch: one
    # unconditional line plus each branch's evidence field description.
    triage_source = read(".github", "workflows", "triage.yml")
    check(
        "bytes: every prompt branch states the 1024-UTF-8-byte quote rule",
        "Every evidence quote must be at most 1024 UTF-8 bytes" in triage_source
        and triage_source.count("each at most 1024 UTF-8 bytes") == 3
        and "<=120 chars" not in triage_source,
    )


# --------------------------------------------------------------------------- #
# 11. Context-equivalent correction task: bindings, parity, no recursion
# --------------------------------------------------------------------------- #
def _correction_fixture(root, candidate, action="triage.pr.search"):
    """Build a real primary task+handoff+bridge result for correction tests."""
    from agent_runtime.claude_bridge import IMMUTABLE_MODEL, bridge
    from agent_runtime.claude_handoff import pack
    from agent_runtime.config import resolve_selection
    from agent_runtime.contract import canonical_sha256, file_sha256
    from agent_runtime.task_builder import (
        build_task,
        claude_declared_outputs,
        claude_declared_tools,
    )

    source_sha = "30271b6907e568419cdc48694a11b0c2f699b433"
    root = Path(root)
    prompt = root / "prompt.txt"
    prompt.write_text(
        "Do READ-ONLY advisory triage. Read target.txt first.\n", encoding="utf-8"
    )
    target = root / "target.txt"
    target.write_text(
        "<target-content>\n# add bounded stop conditions to crewmate briefs\n"
        "</target-content>\n",
        encoding="utf-8",
    )
    bundle = root / "bundle"
    task = build_task(
        action=action,
        selection=resolve_selection(action, "repo"),
        prompt_path=str(prompt),
        bundle_dir=str(bundle),
        output_path=str(bundle / "task.json"),
        owner="owner",
        repo="repo",
        number=7,
        target_kind="pr-review",
        revision="abcdef1",
        wheelhouse_revision=source_sha,
        event_key="a" * 64,
        target_file=str(target),
        allow_automerge_behavior=True,
    )
    handoff = root / "handoff"
    meta = pack(str(bundle / "task.json"), str(bundle), str(handoff), '["owner/repo"]')
    execution = root / "execution.json"
    execution.write_text(
        json.dumps(
            [
                {"type": "system", "subtype": "init", "model": IMMUTABLE_MODEL},
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": json.dumps(candidate),
                    "duration_ms": 2500,
                    "num_turns": 2,
                },
            ]
        ),
        encoding="utf-8",
    )
    enforced = {
        name: task["spec"]["limits"][name]
        for name, quality in task["spec"]["limits"]["enforcement"].items()
        if quality == "externally-enforced"
    }
    enforcement = root / "enforcement.json"
    enforcement.write_text(
        json.dumps(
            {
                "version": 1,
                "boundary": "separate-read-only-github-job",
                "jobPermissions": {
                    "actions": "read",
                    "contents": "read",
                    "issues": "none",
                },
                "writeCapableGithubTokenAvailable": False,
                "fleetTokenAvailable": False,
                "readonlyTokenBoundary": "in-process"
                if action.endswith(".search")
                else "absent",
                "spendStarted": True,
                "isolationLevel": "github-readonly-artifact-bridge-v1",
                "artifactHydration": "content-addressed-bounded-verified",
                "targetInputsReadOnly": True,
                "preActionInputObservationSha256": "b" * 64,
                "postActionInputObservationSha256": "b" * 64,
                "declaredOutputPaths": claude_declared_outputs(action),
                "workspaceRepository": "local-no-remote",
                "declaredTools": claude_declared_tools(action),
                "action": action,
                "actionSourceCommit": "af0559ee4f514d1ef21826982bed13f7edc3c35e",
                "actionMetadataQuality": "pinned-action-reference",
                "actionMetadataSha256": None,
                "taskSha256": canonical_sha256(task),
                "handoffManifestSha256": meta["manifestSha256"],
                "transcriptSha256": file_sha256(execution),
                "childExecutionTimeoutMs": task["spec"]["limits"][
                    "childExecutionTimeoutMs"
                ],
                "controller": {
                    "parentRunId": "1",
                    "parentRunAttempt": "1",
                    "modelRunId": "2",
                    "hardDeadlineMs": None,
                    "dispatchDeadlineMs": task["spec"]["limits"]["dispatchDeadlineMs"],
                    "childExecutionTimeoutMs": task["spec"]["limits"][
                        "childExecutionTimeoutMs"
                    ],
                    "enforcedLimits": enforced,
                    "conclusion": "success",
                    "terminationReason": "completed",
                    "dispatchRef": "main",
                    "expectedCommitSha": source_sha,
                    "observedCommitSha": source_sha,
                    "correlationId": "a" * 32,
                },
            }
        ),
        encoding="utf-8",
    )
    result_dir = root / "received"
    result_dir.mkdir()
    result = bridge(
        str(bundle / "task.json"),
        str(bundle),
        str(execution),
        "",
        str(enforcement),
        meta["manifestSha256"],
        str(result_dir / "result.json"),
        str(result_dir / "events.ndjson"),
    )
    (result_dir / "task.json").write_bytes((bundle / "task.json").read_bytes())
    return task, handoff, meta["manifestSha256"], result_dir, result, source_sha


def test_context_equivalent_correction_task():
    from agent_runtime.config import resolve_selection
    from agent_runtime.contract import canonical_sha256
    from agent_runtime.task_builder import (
        build_correction_task,
        correction_eligibility,
    )

    with tempfile.TemporaryDirectory() as d:
        # A complete-schema-invalid candidate that the advisory parser can
        # consume: valid except one over-ceiling (2049-byte) quote.
        candidate = _pr_candidate_with_quote("x" * 2049)
        task, handoff, handoff_sha, result_dir, result, source_sha = (
            _correction_fixture(d, candidate)
        )
        check(
            "correction: the fixture primary fails trusted validation but stays advisory-normalizable",
            result["status"] == "failed"
            and result["error"]["code"] == "output.schema_invalid"
            and "delivered" in result
            and rc.normalize_triage(candidate) is not None,
        )
        ok, reason, errors = correction_eligibility(
            str(handoff), str(result_dir), "abcdef1", source_sha, handoff_sha
        )
        check(
            "correction: an advisory-normalizable bound-schema failure enters exactly one correction",
            ok and any("2048 UTF-8 bytes" in e for e in errors),
        )
        # Stale or mismatched bindings refuse rather than correcting changed
        # evidence.
        refusals = [
            correction_eligibility(
                str(handoff), str(result_dir), "fffffff", source_sha, handoff_sha
            ),
            correction_eligibility(
                str(handoff), str(result_dir), "abcdef1", "c" * 40, handoff_sha
            ),
            correction_eligibility(
                str(handoff), str(result_dir), "abcdef1", source_sha, "d" * 64
            ),
        ]
        check(
            "correction: stale revision / source SHA / handoff identity all refuse",
            all(not row[0] for row in refusals),
        )
        cbundle = Path(d) / "correction-bundle"
        ctask, allowed = build_correction_task(
            handoff_dir=str(handoff),
            result_dir=str(result_dir),
            bundle_dir=str(cbundle),
            output_path=str(cbundle / "task.json"),
            event_key="b" * 64,
            expected_revision="abcdef1",
            expected_source_sha=source_sha,
            expected_handoff_sha256=handoff_sha,
            selection=resolve_selection("triage.pr.search", "repo"),
        )
        check(
            "correction: model/tool/search/network/evidence/limit parity with the original task",
            ctask["metadata"]["action"] == task["metadata"]["action"]
            and all(
                ctask["spec"][key] == task["spec"][key]
                for key in (
                    "selection",
                    "capabilities",
                    "tools",
                    "isolation",
                    "limits",
                    "inputs",
                    "output",
                    "session",
                    "retention",
                )
            ),
        )
        check(
            "correction: the original search scope rides along",
            allowed == ["owner/repo"],
        )
        check(
            "correction: exact task/result bindings are recorded",
            ctask["metadata"]["correction"]["originalTaskSha256"]
            == canonical_sha256(task)
            and ctask["metadata"]["correction"]["rejectedValueSha256"]
            == result["delivered"]["valueSha256"]
            and ctask["metadata"]["target"] == task["metadata"]["target"]
            and ctask["metadata"]["idempotencyKey"] == "b" * 64,
        )
        cprompt = (cbundle / ctask["spec"]["prompt"]["userArtifact"]).read_text(
            encoding="utf-8"
        )
        check(
            "correction: prompt is the exact original plus rejected candidate and every trusted error",
            cprompt.startswith("Do READ-ONLY advisory triage.")
            and "<rejected-candidate>" in cprompt
            and "Restores the crew merge guard rules." in cprompt
            and "2048 UTF-8 bytes" in cprompt
            and "COMPLETE replacement" in cprompt,
        )
        from agent_runtime.task_builder import correction_prompt

        all_errors = ["$.field[%d] failed" % index for index in range(48)]
        complete_error_prompt = correction_prompt("original", "candidate", all_errors)
        check(
            "correction: every trusted validation error reaches the correction turn",
            all(error in complete_error_prompt for error in all_errors)
            and "further errors omitted" not in complete_error_prompt,
        )
        check(
            "correction: no recursion, no fallback, no second correction",
            ctask["spec"]["retry"]
            == {"sameCandidateMaxAttempts": 1, "retryable": [], "repairTask": None}
            and ctask["spec"]["selection"]["fallback"] == {"mode": "none"},
        )
        # A correction task can never be corrected again.
        from agent_runtime.claude_handoff import pack as pack_handoff

        chandoff = Path(d) / "correction-handoff"
        cmeta = pack_handoff(
            str(cbundle / "task.json"), str(cbundle), str(chandoff), "[]"
        )
        cresult_dir = Path(d) / "correction-received"
        cresult_dir.mkdir()
        (cresult_dir / "result.json").write_bytes(
            (result_dir / "result.json").read_bytes()
        )
        (cresult_dir / "task.json").write_bytes((cbundle / "task.json").read_bytes())
        ok2, reason2, _ = correction_eligibility(
            str(chandoff), str(cresult_dir), "abcdef1", source_sha,
            cmeta["manifestSha256"],
        )
        check(
            "correction: correcting a correction is refused",
            not ok2
            and ("forbidden" in reason2 or "does not match" in reason2),
        )
        # The correction task grants no mutation capability beyond the original
        # read-only surface: same read-only github permissions constraint, no
        # acting token, and read-only declared tools.
        perms = next(
            row["constraints"]
            for row in ctask["spec"]["capabilities"]["required"]
            if row["name"] == "github.permissions"
        )
        creds = next(
            row["constraints"]
            for row in ctask["spec"]["capabilities"]["required"]
            if row["name"] == "credentials.isolated"
        )
        check(
            "correction: read-only privilege parity (no acting token, no card/target writes)",
            perms == {"actions": "read", "contents": "read", "issues": "none", "actingToken": False}
            and creds["fleetToken"] == "absent",
        )


def test_correction_excludes_missing_and_infrastructure_failures():
    from agent_runtime.task_builder import correction_eligibility

    with tempfile.TemporaryDirectory() as d:
        # A VALID primary is never correction-eligible.
        candidate = _pr_candidate_with_quote(CARD_1693_QUOTE)
        task, handoff, handoff_sha, result_dir, result, source_sha = (
            _correction_fixture(d, candidate)
        )
        check(
            "correction(EXCLUDED): the 253-byte card #1693 quote now passes the primary outright",
            result["status"] == "succeeded" and "final" in result,
        )
        ok, reason, _ = correction_eligibility(
            str(handoff), str(result_dir), "abcdef1", source_sha, handoff_sha
        )
        check(
            "correction(EXCLUDED): a trusted-valid primary never enters correction",
            not ok and "passed trusted validation" in reason,
        )
        # Missing and infrastructure failures never enter correction: rewrite
        # the result as failure classes with and without a delivered candidate.
        from agent_runtime.contract import canonical_json_bytes, canonical_sha256
        from agent_runtime.supervisor import _error as make_error

        base = json.loads((result_dir / "result.json").read_text(encoding="utf-8"))
        for code, drop_delivered in (
            ("output.missing", True),
            ("lifecycle.timeout", False),
            ("auth.invalid", False),
            ("provider.quota_exhausted", False),
            ("transport.connection", False),
            ("sandbox.violation", False),
        ):
            mutated = json.loads(json.dumps(base))
            mutated.pop("final", None)
            mutated["status"] = "failed"
            mutated["error"] = make_error(
                code, "synthetic excluded-class failure", spend_started=True
            )
            if drop_delivered:
                mutated.pop("delivered", None)
            elif "delivered" not in mutated:
                mutated["delivered"] = {
                    "value": candidate,
                    "valueSha256": canonical_sha256(candidate),
                    "bytes": len(canonical_json_bytes(candidate)),
                }
            (result_dir / "result.json").write_text(
                json.dumps(mutated), encoding="utf-8"
            )
            ok2, reason2, _ = correction_eligibility(
                str(handoff), str(result_dir), "abcdef1", source_sha, handoff_sha
            )
            check(
                "correction(EXCLUDED): %s never enters correction" % code,
                not ok2,
            )


# --------------------------------------------------------------------------- #
# 12. Authority semantics: corrected keeps authority, failed correction is
#     explicitly advisory-only
# --------------------------------------------------------------------------- #
def test_correction_authority_semantics():
    _, body = _queued_body()
    valid = dict(VALID)
    with tempfile.TemporaryDirectory() as d:
        tf = target_file_with(d)
        # Failed correction: the advisory-consumable original is applied with
        # NO authority - no recommendation, no Accept, and advisory consumption.
        advisory = rc.body_with_triage_result(
            body,
            "8b7547c1",
            triage=valid,
            owner="kunchenguid",
            primary_error_code="output.schema_invalid",
            authority_allowed=False,
            consumption="advisory",
            repair_status="repair-failed",
            repair_reason="corrected result failed trusted validation (output.schema_invalid)",
        )
        state = rc.parse_state_block(advisory)
        check(
            "authority: failed correction stays advisory and non-authoritative",
            state.get("triage_status") == "succeeded"
            and state.get("triage_primary_status") == "failed"
            and state.get("triage_consumption") == "advisory"
            and "triage_recommendation" not in state
            and "automerge_verdict" not in state
            and not rc.accept_recommendation_available(state),
        )
        check(
            "authority: advisory-only telemetry records the failed correction",
            state.get("triage_repair_status") == "repair-failed",
        )
        # A fully revalidated correction keeps existing authority semantics:
        # the recommendation persists exactly as a valid primary's would.
        corrected = rc.body_with_triage_result(
            body,
            "8b7547c1",
            triage=valid,
            owner="kunchenguid",
            primary_error_code="output.schema_invalid",
            consumption="corrected",
            repair_status="repaired",
            repair_reason="primary validation failed (output.schema_invalid)",
        )
        cstate = rc.parse_state_block(corrected)
        primary = rc.body_with_triage_result(
            body, "8b7547c1", triage=valid, owner="kunchenguid"
        )
        pstate = rc.parse_state_block(primary)
        check(
            "authority: a valid corrected result keeps existing authority semantics",
            cstate.get("triage_status") == "succeeded"
            and cstate.get("triage_consumption") == "corrected"
            and cstate.get("triage_primary_status") == "failed"
            and cstate.get("triage_recommendation")
            == pstate.get("triage_recommendation")
            and cstate.get("triage_repair_status") == "repaired",
        )
        check(
            "authority: corrected card copy names corrected authority, not advisory-only consumption",
            "single correction passed complete trusted validation" in corrected
            and "Recommendation authority comes from that corrected result"
            in corrected
            and "consumed for advisory triage" not in corrected,
        )
        check(
            "authority: a valid primary keeps existing authority semantics",
            pstate.get("triage_status") == "succeeded"
            and pstate.get("triage_consumption") == "primary"
            and pstate.get("triage_recommendation") is not None,
        )


def main():
    test_schema_reason_is_structural_and_leak_free()
    test_redacted_candidate_shape_is_content_free()
    test_build_repair_prompt()
    test_plan_trigger_discipline()
    test_decide_routing()
    test_repair_telemetry_and_non_materiality()
    test_same_revision_refresh_preserves_repair_telemetry()
    test_no_leak_in_persisted_diagnostics()
    test_cli_repair_prep()
    test_cli_triage_apply_repair_end_to_end()
    test_triage_yml_repair_wiring()
    test_evidence_quote_byte_policy()
    test_context_equivalent_correction_task()
    test_correction_excludes_missing_and_infrastructure_failures()
    test_correction_authority_semantics()
    if _failures:
        print("\n%d check(s) failed:" % len(_failures))
        for name in _failures:
            print("  - " + name)
        sys.exit(1)
    print("\nall schema-repair tests passed")


if __name__ == "__main__":
    main()
