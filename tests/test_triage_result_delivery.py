#!/usr/bin/env python3
"""
Card #556 regression: a valid Claude auto-triage verdict must NEVER be
discarded because the execution transcript is bulky.

This is the delivered-then-dropped defect. Under the pass-by-reference triage
design (#544) the agentic transcript legitimately grows into the hundreds of
KB (every file the model Reads is recorded), so triage.yml's old copy step
dropped the whole execution file when it exceeded 262144 bytes - throwing away
a successful, already-produced structured result. The fix is STRUCTURAL, not a
bigger magic cap: `render_card.extract_result_to_file` extracts the compact
final result BEFORE any transcript-size enforcement, so transcript bulk can
never gate result delivery. A size cap still bounds only the retained debug
transcript copy.

Run: python tests/test_triage_result_delivery.py
"""

import json
import os
import subprocess
import sys
import tempfile

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import render_card as rc  # noqa: E402
import wheelhouse_core as core  # noqa: E402

# The exact cap that used to drop the result (triage.yml, cap origin #110).
DROP_CAP = 262144

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


# A valid, schema-complete structured triage verdict - the compact final
# result the pipeline actually consumes.
VALID_VERDICT = {
    "summary": "Adds bounded stop conditions to crewmate briefs.",
    "product_implications": "Internal maintenance change; no product discussion needed.",
    "evidence": 'target.txt: "add bounded stop conditions to crewmate briefs"',
    "recommended_action": "comment",
    "recommended_reason": "Scope is small and well contained; leave a note for #12.",
}


def big_transcript_with_valid_result(target_bytes):
    """A Claude execution transcript (events list) padded PAST target_bytes with
    bulky assistant tool-use turns (simulating file Reads), ending in a valid
    successful `result` event carrying the structured verdict as JSON."""
    events = [{"type": "system", "subtype": "init", "session_id": "s"}]
    # Pad with bulky recorded Read output, just like a pass-by-reference run.
    blob = "x" * 4096
    while True:
        events.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Reading target-src ... " + blob}
                    ]
                },
            }
        )
        events.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "file contents: " + blob,
                        }
                    ]
                },
            }
        )
        if len(json.dumps(events)) > target_bytes:
            break
    # The real, valid answer the model produced (this is what must survive).
    events.append(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 8,
            "total_cost_usd": 1.04,
            "result": json.dumps(VALID_VERDICT),
        }
    )
    return events


def test_extract_result_survives_oversize_transcript():
    events = big_transcript_with_valid_result(DROP_CAP * 2)
    with tempfile.TemporaryDirectory() as d:
        exec_file = os.path.join(d, "claude-execution-output.json")
        with open(exec_file, "w", encoding="utf-8") as f:
            json.dump(events, f)
        out_file = os.path.join(d, "execution.json")

        transcript_size = os.path.getsize(exec_file)
        # Reproduce the exact failing condition: the transcript is over the cap
        # that used to discard it.
        check(
            "repro: transcript exceeds the old 262144 drop cap",
            transcript_size > DROP_CAP,
        )

        ok = rc.extract_result_to_file(exec_file, out_file)
        check("extract: returns True for a bulky transcript with a valid result", ok)
        check("extract: compact result file was written", os.path.exists(out_file))

        # The delivered file is tiny and bounded even though the input was huge.
        check(
            "extract: delivered result file is small (bounded), not the transcript",
            os.path.getsize(out_file) < DROP_CAP,
        )

        # The compact file flows through triage-apply's exact pipeline unchanged:
        # triage-apply does extract_claude_result -> parse_triage_json (returns
        # the RAW verdict dict) -> update_card_triage(triage=<raw dict>).
        result_text = rc.extract_claude_result(out_file)
        parsed = rc.parse_triage_json(result_text)
        check("deliver: extract_claude_result recovers the verdict text", bool(result_text))
        check("deliver: parse_triage_json accepts the delivered verdict", parsed is not None)
        check(
            "deliver: normalize_triage confirms a valid structured verdict",
            rc.normalize_triage(parsed) is not None if parsed else False,
        )
        normalized = rc.normalize_triage(parsed) if parsed else None
        if normalized:
            check(
                "deliver: the verdict content is preserved end-to-end",
                normalized.get("summary") == VALID_VERDICT["summary"]
                and VALID_VERDICT["recommended_action"]
                in normalized.get("recommended_next_step", ""),
            )

        # The verdict renders onto the card body (the surface the owner sees).
        it = {
            "repo": "firstmate",
            "number": 423,
            "kind": "pr-review",
            "head_sha": "6aeb38e6",
            "title": "Add bounded stop conditions",
            "author": "stoneymarrow",
            "bucket": "review-needed",
            "comp": "pass",
            "tests": "green",
            "url": "https://github.com/o/firstmate/pull/423",
            "summary": "compliance=pass tests=green",
            "recommendation": "Look closer.",
            "priority": "med",
            "options": ["merge", "investigate"],
        }
        body = rc.render(it)["body"]
        queued = rc.body_with_triage_queued(body, it)
        # Mirror triage-apply exactly: hand the RAW parsed verdict dict through.
        updated = rc.body_with_triage_result(queued, it["head_sha"], triage=parsed)
        state = core.parse_state_block(updated)
        check(
            "deliver: a bulky-transcript verdict reaches the visible card section",
            "### Triage" in updated,
        )
        check(
            "deliver: bulky-transcript verdict records success, not error",
            state.get("triage_status") == "succeeded",
        )


def test_extract_result_reports_no_result_when_absent():
    # A transcript with only an is_error result (no usable answer) must NOT be
    # delivered - the CLI signals failure so the workflow leaves result_path
    # empty and the normal fail-open path records the miss.
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": ""}]}},
        {"type": "result", "subtype": "error", "is_error": True, "result": ""},
    ]
    with tempfile.TemporaryDirectory() as d:
        exec_file = os.path.join(d, "e.json")
        with open(exec_file, "w", encoding="utf-8") as f:
            json.dump(events, f)
        out_file = os.path.join(d, "out.json")
        check(
            "no-result: extract_result_to_file returns False when there is no answer",
            rc.extract_result_to_file(exec_file, out_file) is False,
        )


def test_extract_result_cli_roundtrip():
    events = big_transcript_with_valid_result(DROP_CAP + 50000)
    with tempfile.TemporaryDirectory() as d:
        exec_file = os.path.join(d, "e.json")
        with open(exec_file, "w", encoding="utf-8") as f:
            json.dump(events, f)
        out_file = os.path.join(d, "out.json")
        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(ROOT, "scripts", "render_card.py"),
                "extract-result",
                "--execution-file",
                exec_file,
                "--out",
                out_file,
            ],
            capture_output=True,
            text=True,
        )
        check("cli: extract-result exits 0 on a valid oversize transcript", proc.returncode == 0)
        check("cli: extract-result wrote the compact file", os.path.exists(out_file))
        if os.path.exists(out_file):
            check(
                "cli: the compact file parses back to the verdict",
                rc.parse_triage_json(rc.extract_claude_result(out_file)) is not None,
            )

        empty = os.path.join(d, "empty.json")
        with open(empty, "w", encoding="utf-8") as f:
            json.dump([{"type": "result", "is_error": True, "result": ""}], f)
        missing_out = os.path.join(d, "missing.json")
        proc2 = subprocess.run(
            [
                sys.executable,
                os.path.join(ROOT, "scripts", "render_card.py"),
                "extract-result",
                "--execution-file",
                empty,
                "--out",
                missing_out,
            ],
            capture_output=True,
            text=True,
        )
        check("cli: extract-result exits non-zero when no result exists", proc2.returncode != 0)


def test_triage_yml_extracts_before_size_gate():
    doc = yaml.safe_load(read(".github", "workflows", "triage.yml"))
    steps = doc["jobs"]["triage"]["steps"]
    step = next((s for s in steps if s.get("id") == "triage-result"), None)
    check("yaml: triage-result step exists", step is not None)
    if step:
        run = str(step.get("run", ""))
        env = step.get("env", {})
        extract_idx = run.find("extract-result")
        size_idx = run.find("262144")
        check(
            "yaml: triage-result calls render_card.py extract-result",
            "render_card.py" in run and extract_idx != -1,
        )
        check(
            "yaml: extraction runs BEFORE any transcript-size gate",
            extract_idx != -1 and size_idx != -1 and extract_idx < size_idx,
        )
        check(
            "yaml: result_path is set from extraction success, not the size cap",
            'result_path="$result_file"' in run,
        )
        check(
            "yaml: the old 'execution file is too large' drop is gone",
            "auto triage execution file is too large" not in run,
        )
        check(
            "yaml: the size cap now only bounds the retained transcript copy",
            'cp "$EXECUTION_FILE" "$transcript_file"' in run,
        )
        check(
            "yaml: extraction uses the trusted Python interpreter",
            env.get("TRUSTED_PYTHON") == "${{ steps.trusted-src.outputs.python }}",
        )


def test_deep_review_reads_execution_file_without_a_size_drop():
    # Audit: deep-review.yml already extracts its verdict directly from the
    # execution file via json.load with NO size cap, so the delivered-drop
    # mechanism is NOT present there. Pin that so a future change does not
    # reintroduce a copy-cap that could discard a deep-review verdict.
    text = read(".github", "workflows", "deep-review.yml")
    check(
        "audit: deep-review reads the execution file directly",
        "def extract_verdict(path):" in text and "json.load(f)" in text,
    )
    check(
        "audit: deep-review has no 262144-style execution-file drop",
        "262144" not in text and "execution file is too large" not in text,
    )


def main():
    test_extract_result_survives_oversize_transcript()
    test_extract_result_reports_no_result_when_absent()
    test_extract_result_cli_roundtrip()
    test_triage_yml_extracts_before_size_gate()
    test_deep_review_reads_execution_file_without_a_size_drop()
    if _failures:
        print("\n%d check(s) failed:" % len(_failures))
        for name in _failures:
            print("  - " + name)
        sys.exit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
