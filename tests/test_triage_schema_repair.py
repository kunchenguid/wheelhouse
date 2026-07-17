#!/usr/bin/env python3
"""
Cards #551/#547 regression: bounded automatic recovery for the MODEL-SCHEMA-MISS
auto-triage failure class.

A schema-miss is a DELIVERED execution result (the model ran and produced an
answer) that reaches parse/normalize and yields None - distinct from the #556
delivered-then-dropped cap defect and from E2BIG / auth / rate-limit / infra
failures, which leave NO extractable result. On a schema-miss, exactly ONE
bounded, tokenless, no-tool Claude turn is given the preserved candidate plus the
required schema and asked to REPAIR its structure (no diff re-read, no fresh
analysis). The repaired output is validated again; if still invalid the card
records the visible triage-unavailable error now carrying the structural
validation reason. Structurally there is at most one repair attempt per admitted
triage dispatch.

These tests are OFFLINE: pure helpers, mocked card I/O, and static YAML
inspection - the live LLM turn is only exercised end-to-end in CI.

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


def target_file_with(d, text='<target-content>\n# add bounded stop conditions to crewmate briefs\n</target-content>'):
    p = os.path.join(d, "target.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


# --------------------------------------------------------------------------- #
# 1. triage_schema_reason: structural, precise, LEAK-FREE
# --------------------------------------------------------------------------- #
def test_schema_reason_is_structural_and_leak_free():
    check("reason: a valid result yields no reason", rc.triage_schema_reason(json.dumps(VALID)) == "")

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
    check("shape: reports known present fields", "summary" in shape and "product_implications" in shape)
    check("shape: counts unknown keys without naming them", "unknown_keys=1" in shape)
    check("shape: unparseable text is labeled", rc.redacted_candidate_shape("not json at all") == "unparseable-json")

    # A candidate MISSING a required field lists it under missing=[...].
    missing_shape = rc.redacted_candidate_shape(json.dumps({k: v for k, v in VALID.items() if k != "summary"}))
    check("shape: a missing required field is listed", "summary" in missing_shape.split("missing=[")[1])
    # A complete-but-mistyped candidate still yields a content-free shape.
    complete = dict(VALID)
    complete["evidence"] = 123
    shp = rc.redacted_candidate_shape(json.dumps(complete))
    check("shape: present list uses only allowlisted names", all(k in rc._KNOWN_TRIAGE_KEYS for k in shp.split("present=[")[1].split("]")[0].split(",") if k))


# --------------------------------------------------------------------------- #
# 2. build_repair_prompt: self-contained, schema-complete, bounded
# --------------------------------------------------------------------------- #
def test_build_repair_prompt():
    for kind, enum, absent in (
        ("pr-review", "merge | request-changes", None),
        ("issue-triage", "close | decline | hold", "merge | request-changes"),
    ):
        p = rc.build_repair_prompt(json.dumps(VALID), kind)
        for field in ("summary", "product_implications", "recommended_action", "recommended_reason", "evidence"):
            check("prompt(%s): names required field %s" % (kind, field), field in p)
        check("prompt(%s): kind-specific action enum" % kind, enum in p)
        if absent:
            check("prompt(%s): omits the other kind's action enum" % kind, absent not in p)
        check("prompt(%s): embeds the candidate" % kind, VALID["summary"] in p and "<candidate>" in p)
        check("prompt(%s): forbids reading files / re-analysis" % kind, "NO tools" in p and "re-analysis" in p.lower())
        check("prompt(%s): requires verbatim evidence" % kind, "VERBATIM" in p)
        check(
            "prompt(%s): requires evidence as one string" % kind,
            "single JSON string, not an array" in p,
        )
        check("prompt(%s): output-only-JSON instruction" % kind, "ONLY a single compact JSON object" in p)

    # Schema lockstep: every field the repair prompt promises must be a field
    # the validator actually requires, so repair can never target a stale schema.
    prompt = rc.build_repair_prompt("{}", "pr-review")
    for field in rc.TRIAGE_FIELDS:
        check("lockstep: validator field %s is in the repair schema" % field, field in prompt)
    check("lockstep: evidence field is in the repair schema", rc.EVIDENCE_FIELD in prompt)

    # A pathological (huge) candidate is byte-bounded so the repair prompt can
    # never re-introduce the E2BIG class the pass-by-reference redesign fixed.
    huge = json.dumps({"summary": "x" * 200000, "junk": "y" * 200000})
    pbig = rc.build_repair_prompt(huge, "pr-review")
    check("prompt: oversized candidate is truncated", "[candidate truncated]" in pbig)
    check(
        "prompt: bounded well under MAX_ARG_STRLEN even for a huge candidate",
        len(pbig.encode("utf-8")) < 60000,
    )


# --------------------------------------------------------------------------- #
# 3. plan_triage_repair: TRIGGER discipline (clause 1)
# --------------------------------------------------------------------------- #
def test_plan_trigger_discipline():
    valid = rc.plan_triage_repair(json.dumps(VALID), "pr-review")
    check("plan: a valid delivered result needs NO repair", valid["repair_needed"] is False)
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
    check("plan: schema-miss carries a structural reason", "recommended_action" in p["reason"])

    # EXCLUDED: no delivered result at all (E2BIG / missing / auth / rate-limit /
    # infra all leave nothing extractable) -> NEVER repair.
    for label, text in (
        ("empty string", ""),
        ("whitespace only", "   \n  "),
    ):
        pl = rc.plan_triage_repair(text, "pr-review")
        check("plan(EXCLUDED %s): missing result never triggers repair" % label, pl["repair_needed"] is False)
        check("plan(EXCLUDED %s): no repair prompt" % label, pl["prompt"] == "")


# --------------------------------------------------------------------------- #
# 4. decide_triage_apply: routing incl. the excluded classes
# --------------------------------------------------------------------------- #
def test_decide_routing():
    with tempfile.TemporaryDirectory() as d:
        tf = target_file_with(d)
        invalid = json.dumps({k: v for k, v in VALID.items() if k != "recommended_action"})
        valid = json.dumps(VALID)

        # success-on-repair (clause 6): invalid original + valid repaired.
        dec = rc.decide_triage_apply(invalid, valid, tf)
        check("route: schema-miss + valid repair -> repaired", dec["outcome"] == "repaired")
        check("route: repaired carries the raw repaired dict", isinstance(dec["triage"], dict))
        check("route: repaired reason is the ORIGINAL structural failure", "recommended_action" in dec["reason"])

        # repair-failure cap (clause 6): invalid original + still-invalid repair.
        dec2 = rc.decide_triage_apply(invalid, invalid, tf)
        check("route: schema-miss + still-invalid repair -> repair-failed", dec2["outcome"] == "repair-failed")
        check(
            "route: repair-failed reports the repaired schema stage",
            "recommended_action" in dec2["reason"],
        )

        # schema-miss with NO repair supplied (repair step errored / no output).
        dec3 = rc.decide_triage_apply(invalid, "", tf)
        check("route: schema-miss + no repair output -> repair-failed", dec3["outcome"] == "repair-failed")
        check(
            "route: missing repair output reports the actual stage",
            dec3["reason"] == "schema repair produced no result",
        )
        duplicate = rc.decide_triage_apply(
            invalid, "", tf, repair_claim_admitted=False
        )
        check(
            "route: duplicate repair claim reports the actual stage",
            duplicate["reason"] == "schema repair claim was duplicate",
        )

        # original already valid -> success, repair never consulted.
        dec4 = rc.decide_triage_apply(valid, valid, tf)
        check("route: valid original -> success (repair ignored)", dec4["outcome"] == "success")

        # EXCLUDED: no delivered result -> no-result, never repair-* .
        dec5 = rc.decide_triage_apply("", "", tf)
        check("route(EXCLUDED): empty result -> no-result", dec5["outcome"] == "no-result")

        # EXCLUDED: parse-valid but fabricated evidence not anchored to target ->
        # anchor-fail, NOT repair (a repair turn can't conjure real quotes).
        fabricated = dict(VALID)
        fabricated["evidence"] = 'target.txt: "a quote that does not appear in the fetched target at all"'
        dec6 = rc.decide_triage_apply(json.dumps(fabricated), "", tf)
        check("route(EXCLUDED): anchor-fail is not routed to repair", dec6["outcome"] == "anchor-fail")

        # The repaired output must STILL pass the evidence anchor guard: a repair
        # that dropped/fabricated evidence is rejected as repair-failed.
        repaired_fabricated = json.dumps(fabricated)
        dec7 = rc.decide_triage_apply(invalid, repaired_fabricated, tf)
        check("route: a repair with non-anchoring evidence -> repair-failed", dec7["outcome"] == "repair-failed")
        check(
            "route: repaired anchor failure reports the actual stage",
            dec7["reason"]
            == "repaired field 'evidence' did not anchor to the fetched target",
        )

        array_valid = dict(
            VALID,
            evidence=[
                "target.txt: 'add bounded stop conditions to crewmate briefs'",
                "target-src/brief.py: unrelated source quote",
            ],
        )
        array_decision = rc.decide_triage_apply(
            json.dumps(array_valid), "", tf
        )
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
        "repo": "firstmate", "number": 469, "kind": "pr-review", "head_sha": "8b7547c1",
        "title": "Add stops", "author": "stoneymarrow", "bucket": "review-needed",
        "comp": "pass", "tests": "green", "url": "u",
        "summary": "compliance=pass tests=green", "recommendation": "Look closer.",
        "priority": "med", "options": ["merge", "investigate"],
    }
    return it, rc.body_with_triage_queued(rc.render(it)["body"], it)


def test_repair_telemetry_and_non_materiality():
    it, queued = _queued_body()

    repaired = rc.body_with_triage_result(
        queued, "8b7547c1", triage=VALID,
        repair_status="repaired", repair_reason="field 'summary' is empty",
    )
    st = core.parse_state_block(repaired)
    check("telemetry: repaired records triage_status succeeded", st.get("triage_status") == "succeeded")
    check("telemetry: repaired records repair_status", st.get("triage_repair_status") == "repaired")
    check("telemetry: repaired records the structural reason", st.get("triage_repair_reason") == "field 'summary' is empty")
    check("telemetry: repaired renders the real triage section", "### Triage" in repaired and VALID["summary"] in repaired)

    check("telemetry: repaired records the redacted candidate shape",
          rc.body_with_triage_result(queued, "8b7547c1", triage=VALID,
                                     repair_status="repaired", repair_reason="r",
                                     repair_candidate="present=[summary] missing=[]").count("triage_repair_candidate") == 1)

    failed = rc.body_with_triage_result(
        queued, "8b7547c1",
        error="%s (%s)" % (rc.TRIAGE_UNAVAILABLE, "field 'evidence' is missing or empty"),
        repair_status="repair-failed", repair_reason="field 'evidence' is missing or empty",
        repair_candidate="present=[summary,product_implications] missing=[evidence]",
    )
    st2 = core.parse_state_block(failed)
    check("telemetry: repair-failed records triage_status error", st2.get("triage_status") == "error")
    check("telemetry: repair-failed records repair_status", st2.get("triage_repair_status") == "repair-failed")
    check("telemetry: repair-failed records the redacted candidate", "evidence" in (st2.get("triage_repair_candidate") or ""))
    check("telemetry: repair-failed visible error carries the reason", rc.TRIAGE_UNAVAILABLE in failed and "evidence" in failed)

    # A normal (non-repair) write clears any stale repair telemetry.
    normal = rc.body_with_triage_result(repaired, "8b7547c1", triage=VALID)
    st3 = core.parse_state_block(normal)
    check("telemetry: a non-repair write clears repair_status", st3.get("triage_repair_status") is None)
    check("telemetry: a non-repair write clears repair_reason", st3.get("triage_repair_reason") is None)

    # NON-MATERIAL: the repair fields never enter material comparison.
    check(
        "material: repair fields are not MATERIAL_FIELDS",
        all(f not in rc.MATERIAL_FIELDS for f in ("triage_repair_status", "triage_repair_reason")),
    )
    normal_success = core.parse_state_block(rc.body_with_triage_result(queued, "8b7547c1", triage=VALID))
    repaired_success = core.parse_state_block(repaired)
    diff = {
        k for k in set(normal_success) | set(repaired_success)
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
        all(state.get(key) == old_state.get(key) for key in (
            "triage_repair_status",
            "triage_repair_reason",
            "triage_repair_candidate",
        )),
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
    check("leak: the derived reason omits the candidate's content", SECRET not in reason)
    check("leak: the redacted candidate shape omits the candidate's content", SECRET not in shape)

    failed = rc.body_with_triage_result(
        queued, "8b7547c1",
        error="%s (%s)" % (rc.TRIAGE_UNAVAILABLE, reason),
        repair_status="repair-failed", repair_reason=reason, repair_candidate=shape,
    )
    check("leak: the persisted card body never carries the raw content", SECRET not in failed)
    st = core.parse_state_block(failed)
    check("leak: the persisted repair telemetry never carries the raw content", SECRET not in json.dumps(st))


# --------------------------------------------------------------------------- #
# 7. CLI: triage-repair-prep emits $GITHUB_OUTPUT (offline)
# --------------------------------------------------------------------------- #
def test_cli_repair_prep():
    with tempfile.TemporaryDirectory() as d:
        invalid = write_exec(os.path.join(d, "inv.json"), json.dumps({k: v for k, v in VALID.items() if k != "summary"}))
        valid = write_exec(os.path.join(d, "val.json"), json.dumps(VALID))
        missing = os.path.join(d, "empty.json")
        with open(missing, "w") as f:
            json.dump([{"type": "result", "is_error": True, "result": ""}], f)
        gho = os.path.join(d, "gho.txt")

        def run(exec_file, kind):
            open(gho, "w").close()
            proc = subprocess.run(
                [sys.executable, os.path.join(ROOT, "scripts", "render_card.py"),
                 "triage-repair-prep", "--execution-file", exec_file, "--kind", kind],
                env={**os.environ, "GITHUB_OUTPUT": gho}, capture_output=True, text=True,
            )
            return proc, open(gho).read()

        _, out = run(invalid, "pr-review")
        check("cli: schema-miss sets repair_needed=true", "repair_needed=true" in out)
        check("cli: schema-miss emits a repair_prompt heredoc", "repair_prompt<<" in out)
        # The heredoc must be well-formed (matching random delimiter) and embed
        # the candidate - but MUST NOT inline target.txt (pass-by-reference).
        import re
        m = re.search(r"repair_prompt<<(\S+)\n(.*?)\n\1\n", out, re.S)
        check("cli: repair_prompt heredoc is well-formed", bool(m))
        check("cli: repair prompt embeds the candidate", bool(m) and VALID["product_implications"] in m.group(2))

        _, out2 = run(valid, "pr-review")
        check("cli: a valid result sets repair_needed=false", "repair_needed=false" in out2)
        check("cli: a valid result emits no repair prompt", "repair_prompt<<" not in out2)

        _, out3 = run(missing, "issue-triage")
        check("cli(EXCLUDED): missing result sets repair_needed=false", "repair_needed=false" in out3)
        check("cli(EXCLUDED): missing result emits no repair prompt", "repair_prompt<<" not in out3)


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
        "number": int(n), "body": card_body,
        "labels": [{"name": "needs-decision"}], "state": "OPEN",
    }
    rc._edit_issue_body = lambda number, body, remove_labels=None: captured.update(
        {"body": body, "remove": remove_labels}
    )
    try:
        with tempfile.NamedTemporaryFile() as output:
            os.environ["GITHUB_OUTPUT"] = output.name
            sys.argv = [
                "render_card.py", "triage-apply",
                "--issue", str(issue), "--revision", revision,
                "--execution-file", exec_file,
                "--repair-execution-file", repair_file,
                "--repair-claim-admitted", repair_claim_admitted,
                "--target-file", target_file,
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
        "number": int(n), "body": card_body,
        "labels": [{"name": "needs-decision"}], "state": "OPEN",
    }
    rc._edit_issue_body = lambda number, body, remove_labels=None: captured.update(
        {"body": body, "remove": remove_labels}
    )
    try:
        with tempfile.NamedTemporaryFile() as output:
            os.environ["GITHUB_OUTPUT"] = output.name
            sys.argv = [
                "render_card.py", "triage-fail",
                "--issue", str(issue), "--revision", revision,
                "--message", "bounded failure",
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
        invalid = write_exec(os.path.join(d, "inv.json"), json.dumps({k: v for k, v in VALID.items() if k != "recommended_action"}))
        valid = write_exec(os.path.join(d, "val.json"), json.dumps(VALID))
        still_invalid = write_exec(os.path.join(d, "inv2.json"), json.dumps({"summary": "only this"}))

        # success-on-repair: invalid original + valid repair -> real triage card.
        cap = _run_triage_apply(469, "8b7547c1", queued, invalid, valid, tf)
        st = core.parse_state_block(cap.get("body", "")) or {}
        check("e2e: repaired card gets a real triage section", "### Triage" in cap.get("body", ""))
        check("e2e: repaired card records success", st.get("triage_status") == "succeeded")
        check("e2e: repaired card records repair telemetry", st.get("triage_repair_status") == "repaired")
        check("e2e: repaired card shows the model's summary", VALID["summary"] in cap.get("body", ""))
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
        check("e2e: repair-failure records repair-failed telemetry", st2.get("triage_repair_status") == "repair-failed")
        check("e2e: repair-failure records the redacted candidate shape", bool(st2.get("triage_repair_candidate")))
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
        check("e2e(EXCLUDED): missing result records plain error", st3.get("triage_status") == "error")
        check("e2e(EXCLUDED): missing result has NO repair telemetry", st3.get("triage_repair_status") is None)
        check(
            "e2e(EXCLUDED): missing result keeps the plain unavailable text",
            st3.get("triage_error") == rc.TRIAGE_UNAVAILABLE,
        )

        # A normal valid original still works unchanged, no repair telemetry.
        cap4 = _run_triage_apply(469, "8b7547c1", queued, valid, "", tf)
        st4 = core.parse_state_block(cap4.get("body", "")) or {}
        check("e2e: a valid original succeeds with no repair telemetry",
              st4.get("triage_status") == "succeeded" and st4.get("triage_repair_status") is None)

        skipped = _run_triage_apply(469, "newer-revision", queued, valid, "", tf)
        check(
            "e2e: stale card reports explicit rejected output",
            skipped.get("outputs")
            == "applied=false\ntriage_status=succeeded\n"
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
            failed_stale.get("outputs")
            == "applied=false\ntriage_status=error\n"
            and "body" not in failed_stale,
        )


# --------------------------------------------------------------------------- #
# 9. triage.yml static wiring + token/posture isolation (clause 7)
# --------------------------------------------------------------------------- #
def test_triage_yml_repair_wiring():
    triage_source = read(".github", "workflows", "triage.yml")
    doc = yaml.safe_load(triage_source)
    steps = doc["jobs"]["triage"]["steps"]
    model_steps = yaml.safe_load(read(".github", "workflows", "claude-model.yml"))["jobs"]["model"]["steps"]
    ids = [s.get("id") for s in steps]
    check(
        "yaml: every primary evidence schema asks for one JSON string",
        triage_source.count("a single JSON string, not an array") == 3,
    )

    def idx(pred):
        for i, s in enumerate(steps):
            if pred(s):
                return i
        return None

    tr_i = idx(lambda s: s.get("id") == "triage-result")
    prep_i = idx(lambda s: s.get("id") == "repair-prep")
    rep_i = idx(lambda s: s.get("id") == "claude-repair-model")
    res_i = idx(lambda s: s.get("id") == "repair-result")
    fresh_i = idx(lambda s: s.get("id") == "post-model-freshness")
    upd_i = idx(lambda s: s.get("name") == "Update the decision card")

    check("yaml: repair-prep step exists", "repair-prep" in ids)
    check("yaml: Claude repair model boundary exists", "claude-repair-model" in ids)
    check("yaml: repair-result step exists", "repair-result" in ids)
    check(
        "yaml: repair runs AFTER triage-result and BEFORE the card update",
        None not in (tr_i, prep_i, rep_i, res_i, fresh_i, upd_i)
        and tr_i < prep_i < rep_i < res_i < fresh_i < upd_i,
    )

    prep = steps[prep_i]
    prun = str(prep.get("run", ""))
    check("yaml: repair-prep reads the delivered result path", prep.get("env", {}).get("ORIG_RESULT") == "${{ steps.triage-result.outputs.path }}")
    check("yaml: repair-prep runs the trusted render_card.py triage-repair-prep", "triage-repair-prep" in prun and 'TRUSTED_SRC/scripts/render_card.py' in prun.replace('"', "").replace("$", ""))
    check(
        "yaml: repair-prep is pass-by-reference (never inlines target.txt)",
        "cat target.txt" not in prun and "gh pr diff" not in prun,
    )

    rep = next(s for s in model_steps if s.get("id") == "triage_repair")
    repw = rep.get("with", {})
    dumped = yaml.safe_dump(rep)
    boundary = steps[rep_i]
    check("yaml: Claude repair boundary follows its immutable task", "steps.claude-repair-task.outcome == 'success'" in str(boundary.get("if", "")))
    check("yaml: claude_repair is fail-open (continue-on-error)", rep.get("continue-on-error") is True)
    check("yaml: claude_repair uses the pinned action", str(rep.get("uses", "")).endswith("fad22eb3fa582b7357fc0ea48af6645851b884fd"))
    check("yaml: Claude repair prompt is hydrated from its AgentTask", repw.get("prompt") == "${{ steps.hydrate.outputs.prompt }}")
    check("yaml: claude_repair is exactly one turn", "--max-turns 1" in str(repw.get("claude_args", "")))
    check("yaml: claude_repair requests an empty allowlist", '--allowedTools ""' in str(repw.get("claude_args", "")))
    settings = str(repw.get("settings", ""))
    check(
        "yaml: claude_repair fail-closed deny of exec/file/network tools",
        '"deny"' in settings and all(t in settings for t in ("Bash", "Read", "Write", "WebFetch", "Grep", "Glob")),
    )
    check("yaml: claude_repair is tokenless (no FLEET_TOKEN)", "FLEET_TOKEN" not in dumped)
    check("yaml: claude_repair is tokenless (no READONLY_TOKEN)", "READONLY_TOKEN" not in dumped)
    check("yaml: claude_repair allowed_bots stays narrow", repw.get("allowed_bots") == "github-actions[bot]")
    check("yaml: claude_repair uses immutable model", "--model claude-sonnet-4-6" in str(repw.get("claude_args", "")))

    res = steps[res_i]
    rrun = str(res.get("run", ""))
    check("yaml: repair-result extracts via the trusted render_card.py", "extract-result" in rrun)
    check("yaml: repair-result reads normalized AgentResult", "steps.claude-repair-model.outputs.result" in str(res.get("env", {}).get("RUNTIME_RESULT")))

    upd = steps[upd_i]
    update_run = str(upd.get("run", ""))
    check(
        "yaml: card update wires the repaired result file into triage-apply",
        upd.get("env", {}).get("REPAIR_EXECUTION_FILE") == "${{ steps.repair-result.outputs.path }}"
        and upd.get("env", {}).get("REPAIR_CLAIM_ADMITTED")
        == "${{ steps.repair-claim.outputs.admitted }}"
        and "--repair-execution-file" in update_run
        and "--repair-claim-admitted" in update_run,
    )
    check("yaml: scrubbed consumers preserve the output channel", update_run.count('GITHUB_OUTPUT="$GITHUB_OUTPUT"') == 2)
    fresh = steps[fresh_i]
    check(
        "yaml: final freshness re-reads the exact target revision fail closed",
        fresh.get("env", {}).get("EXPECTED_REVISION") == "${{ steps.resolve.outputs.revision }}"
        and "issues/$NUMBER" in str(fresh.get("run", ""))
        and "pulls/$NUMBER" in str(fresh.get("run", ""))
        and "target.stale" in str(fresh.get("run", "")),
    )
    check(
        "yaml: card projection rejects post-model freshness loss",
        "steps.post-model-freshness.outputs.fresh == 'false'" in str(upd.get("env", {}).get("HEAD_OK", "")),
    )
    primary_finalize = next(s for s in steps if s.get("name") == "Finalize primary triage claim and stage evidence")
    repair_finalize = next(s for s in steps if s.get("name") == "Finalize schema-repair claim and stage evidence")
    recovery_i = idx(lambda s: s.get("id") == "card-recovery")
    primary_finalize_i = idx(lambda s: s.get("name") == "Finalize primary triage claim and stage evidence")
    repair_finalize_i = idx(lambda s: s.get("name") == "Finalize schema-repair claim and stage evidence")
    primary_run = str(primary_finalize.get("run", ""))
    repair_run = str(repair_finalize.get("run", ""))
    check(
        "yaml: admitted schema repair emits event-bound terminal evidence",
        repair_finalize.get("env", {}).get("REPAIR_EVENT_KEY") == "${{ steps.repair-claim.outputs.event_key }}"
        and "steps.repair-claim.outputs.admitted == 'true'" in str(repair_finalize.get("if", ""))
        and "--action triage.schema-repair" in repair_run
        and '--event-key "$REPAIR_EVENT_KEY"' in repair_run,
    )
    check(
        "yaml: each durable claim is patched before its terminal stage",
        primary_run.index("gh api --method PATCH") < primary_run.index("agent_runtime.py stage")
        and repair_run.index("gh api --method PATCH") < repair_run.index("agent_runtime.py stage")
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
        "yaml: terminal evidence follows fail-open recovery",
        None not in (recovery_i, primary_finalize_i, repair_finalize_i)
        and recovery_i < primary_finalize_i
        and recovery_i < repair_finalize_i,
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
    if _failures:
        print("\n%d check(s) failed:" % len(_failures))
        for name in _failures:
            print("  - " + name)
        sys.exit(1)
    print("\nall schema-repair tests passed")


if __name__ == "__main__":
    main()
