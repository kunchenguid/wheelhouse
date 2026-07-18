#!/usr/bin/env python3
"""Bounded schema repair for natural-language decision results.

Run: python tests/test_nl_schema_repair.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import apply_decision as decision  # noqa: E402
from agent_runtime.claude_bridge import IMMUTABLE_MODEL, bridge  # noqa: E402
from agent_runtime.claude_handoff import hydrate, pack  # noqa: E402
from agent_runtime.contract import (  # noqa: E402
    canonical_json_bytes,
    file_sha256,
    load_json_regular,
    validate_contract,
)
from agent_runtime_testlib import make_task, run_fake  # noqa: E402
from test_agent_runtime_claude_bridge import (  # noqa: E402
    make_bundle,
    run_bridge,
    transcript,
)


FAILURES = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


MALFORMED_ESCAPE = (
    '{"mode":"answer","answer":"The budget is enforced by the '
    '\\`max_tokens\\` limit."}'
)
VALID_ANSWER = {
    "mode": "answer",
    "answer": "The budget is enforced by the `max_tokens` limit.",
}


def native_transcript(execution: Path, structured_output, result_text=""):
    execution.write_text(
        json.dumps(
            [
                {"type": "system", "subtype": "init", "model": IMMUTABLE_MODEL},
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": result_text,
                    "structured_output": structured_output,
                    "duration_ms": 100,
                    "num_turns": 1,
                },
            ]
        ),
        encoding="utf-8",
    )


def parse_outputs(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if "<<" in line:
            name, delimiter = line.split("<<", 1)
            index += 1
            body = []
            while index < len(lines) and lines[index] != delimiter:
                body.append(lines[index])
                index += 1
            values[name] = "\n".join(body)
        elif "=" in line:
            name, value = line.split("=", 1)
            values[name] = value
        index += 1
    return values


def malformed_claude_result(root: Path) -> Path:
    _, bundle = make_bundle(root / "primary", action="nl-decision.local")
    execution = root / "primary-execution.json"
    transcript(execution, IMMUTABLE_MODEL, "Wrote decision.json.")
    run_bridge(bundle, execution, "enforcement")
    malformed_file = root / "decision.json"
    malformed_file.write_text(MALFORMED_ESCAPE + "\n", encoding="utf-8")
    result_path = bundle / "malformed-result.json"
    result = bridge(
        str(bundle / "task.json"),
        str(bundle),
        str(execution),
        str(malformed_file),
        str(bundle / "enforcement-enforcement.json"),
        "a" * 64,
        str(result_path),
        str(bundle / "malformed-events.ndjson"),
    )
    validate_contract(result, "AgentResult")
    check(
        "bridge regression: malformed decision.json is schema-invalid",
        result["status"] == "failed"
        and result["error"]["code"] == "output.schema_invalid",
    )
    check(
        "bridge regression: exact malformed file survives as bounded repair data",
        result.get("delivered", {}).get("value") == MALFORMED_ESCAPE,
    )
    check(
        "bridge regression: harness terminal prose does not replace the candidate",
        result.get("delivered", {}).get("value") != "Wrote decision.json.",
    )
    return result_path


def test_pure_repair_contract():
    valid = json.dumps(VALID_ANSWER)
    check(
        "plan: valid primary result is untouched",
        decision.plan_nl_repair(valid)["repair_needed"] is False,
    )
    check(
        "plan: missing primary result never spends a repair turn",
        decision.plan_nl_repair("")["repair_needed"] is False,
    )
    plan = decision.plan_nl_repair(MALFORMED_ESCAPE)
    check("plan: delivered malformed escape gets one repair", plan["repair_needed"] is True)
    forced = decision.plan_nl_repair(json.dumps(VALID_ANSWER), force_repair=True)
    check(
        "plan: schema-valid portable carrier still repairs after native failure",
        forced["repair_needed"] is True
        and "native structured output" in forced["reason"],
    )
    check(
        "plan: malformed escape has precise structural reason",
        plan["reason"] == "result was not parseable as strict JSON",
    )
    check(
        "prompt: exact authoritative fields are named",
        all(field in plan["prompt"] for field in ("mode", "action", "free_text", "answer")),
    )
    check(
        "prompt: repair is no-tool and not a re-analysis",
        "NO tools" in plan["prompt"] and "NOT a re-analysis" in plan["prompt"],
    )
    huge = MALFORMED_ESCAPE + ("x" * 100000)
    check(
        "prompt: pathological candidate is byte-bounded",
        len(decision.build_nl_repair_prompt(huge).encode("utf-8")) < 30000,
    )

    repaired = decision.decide_nl_apply(MALFORMED_ESCAPE, valid)
    check(
        "apply: valid repair is selected only after strict re-validation",
        repaired["outcome"] == "repaired" and repaired["result"] == VALID_ANSWER,
    )
    failed = decision.decide_nl_apply(MALFORMED_ESCAPE, MALFORMED_ESCAPE)
    check(
        "apply: still-invalid repair fails with the repaired structural reason",
        failed == {
            "outcome": "repair-failed",
            "result": None,
            "reason": "result was not parseable as strict JSON",
        },
    )
    projection = decision.nl_failure_projection(failed["outcome"], failed["reason"])
    check(
        "projection: exhausted repair is precise and explicitly retryable",
        "schema-invalid" in projection
        and "single bounded repair attempt" in projection
        and "not parseable as strict JSON" in projection
        and "Retry" in projection,
    )
    duplicate = decision.decide_nl_apply(
        MALFORMED_ESCAPE, "", repair_claim_admitted=False
    )
    check(
        "apply: duplicate durable repair claim cannot spend another turn",
        duplicate["outcome"] == "repair-failed"
        and duplicate["reason"] == "schema repair claim was duplicate",
    )
    dishonest = decision.decide_nl_apply(
        json.dumps(VALID_ANSWER),
        "",
        primary_trusted=False,
    )
    check(
        "apply: schema-valid fallback carrier cannot masquerade as native success",
        dishonest["outcome"] == "repair-failed" and dishonest["result"] is None,
    )


def test_native_bridge_and_portable_fallback():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        _, native_bundle = make_bundle(
            root / "native", action="nl-decision.local"
        )
        native_execution = root / "native-execution.json"
        native_transcript(native_execution, VALID_ANSWER, "ignored terminal prose")
        native_result, _ = run_bridge(
            native_bundle, native_execution, "native"
        )
        check(
            "native: terminal structured_output is independently validated and accepted",
            native_result["status"] == "succeeded"
            and native_result["final"]["value"] == VALID_ANSWER
            and {row["name"] for row in native_result["final"]["validation"]}
            >= {"native-schema", "json-schema", "observed-provenance"},
        )
        check(
            "native: terminal prose and decision file are not the trusted success carrier",
            native_result["delivered"]["value"] != "ignored terminal prose",
        )

        _, absent_bundle = make_bundle(
            root / "absent", action="nl-decision.local"
        )
        absent_execution = root / "absent-execution.json"
        transcript(absent_execution, IMMUTABLE_MODEL, "Wrote decision.json.")
        portable = root / "portable-decision.json"
        portable.write_text(json.dumps(VALID_ANSWER) + "\n", encoding="utf-8")
        absent_result_path = absent_bundle / "absent-result.json"
        # run_bridge prepares the trusted enforcement proof. Re-run with the
        # portable file after borrowing that exact fixture proof.
        run_bridge(absent_bundle, absent_execution, "absent-proof")
        absent_result = bridge(
            str(absent_bundle / "task.json"),
            str(absent_bundle),
            str(absent_execution),
            str(portable),
            str(absent_bundle / "enforcement-absent-proof.json"),
            "a" * 64,
            str(absent_result_path),
            str(absent_bundle / "absent-events.ndjson"),
        )
        check(
            "fallback: missing native carrier fails closed with bounded repair data",
            absent_result["status"] == "failed"
            and absent_result["error"]["code"] == "output.schema_invalid"
            and absent_result["delivered"]["value"] == json.dumps(VALID_ANSWER),
        )

        repair_root = root / "absent-repair"
        run_fake(repair_root, "nl-decision.schema-repair", final=VALID_ANSWER)
        outputs = route_cli(
            absent_result_path,
            repair_root / "bundle" / "result.json",
            root / "absent-route.txt",
        )
        check(
            "fallback: native absence routes only after one trusted repair",
            outputs.get("result_valid") == "true"
            and outputs.get("repair_status") == "repaired"
            and outputs.get("answer") == VALID_ANSWER["answer"],
        )

        _, multiple_bundle = make_bundle(
            root / "multiple", action="nl-decision.local"
        )
        multiple_execution = root / "multiple-execution.json"
        native_transcript(multiple_execution, [VALID_ANSWER, VALID_ANSWER])
        multiple_result, _ = run_bridge(
            multiple_bundle, multiple_execution, "multiple"
        )
        check(
            "native: multiple structured values never record native success",
            multiple_result["status"] == "failed"
            and multiple_result["error"]["code"] == "output.schema_invalid"
            and "final" not in multiple_result,
        )
        invalid_repair_root = root / "multiple-invalid-repair"
        run_fake(
            invalid_repair_root,
            "nl-decision.schema-repair",
            final={"answer": "still missing mode"},
        )
        multiple_failed = route_cli(
            multiple_bundle / "result-multiple.json",
            invalid_repair_root / "bundle" / "result.json",
            root / "multiple-failure-route.txt",
        )
        check(
            "fallback: invalid native plus invalid repair keeps precise retry projection",
            multiple_failed.get("result_valid") == "false"
            and multiple_failed.get("failure_code") == "output.schema_invalid"
            and "missing required field mode"
            in multiple_failed.get("failure_reason", "")
            and "single bounded repair attempt"
            in multiple_failed.get("failure_message", "")
            and multiple_failed.get("retryable") == "true",
        )


def test_bound_schema_is_passed_to_action():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        task, bundle = make_bundle(
            root / "task", action="nl-decision.local"
        )
        handoff = root / "handoff"
        pack(
            str(bundle / "task.json"),
            str(bundle),
            str(handoff),
            "[]",
        )
        outputs = hydrate(str(handoff), str(root / "workspace"))
        schema = load_json_regular(
            bundle / task["spec"]["output"]["schemaArtifact"],
            max_bytes=65536,
        )
        check(
            "schema: hydrated action argument is the bound canonical nl-decision-v1 bytes",
            outputs["nativeSchema"].encode("utf-8")
            == canonical_json_bytes(schema)
            == (bundle / task["spec"]["output"]["schemaArtifact"]).read_bytes()
            and task["spec"]["output"]["schemaSha256"]
            == file_sha256(bundle / task["spec"]["output"]["schemaArtifact"]),
        )
        mechanisms = next(
            row["constraints"]["mechanismAnyOf"]
            for row in task["spec"]["capabilities"]["required"]
            if row["name"] == "output.structured"
        )
        repair, _ = make_bundle(
            root / "repair-task", action="nl-decision.schema-repair"
        )
        repair_mechanisms = next(
            row["constraints"]["mechanismAnyOf"]
            for row in repair["spec"]["capabilities"]["required"]
            if row["name"] == "output.structured"
        )
        check(
            "capability: primary negotiates native while repair stays portable",
            mechanisms == ["native-schema"]
            and repair_mechanisms == ["trusted-post-action-bridge"],
        )

    model = yaml.safe_load(
        (ROOT / ".github/workflows/claude-model.yml").read_text()
    )
    steps = model["jobs"]["model"]["steps"]
    for step_id in ("nl_local", "nl_search"):
        args = step_by_id(steps, step_id)["with"]["claude_args"]
        check(
            "workflow: %s passes only the verified hydrated schema" % step_id,
            args.count("--json-schema") == 1
            and "${{ steps.hydrate.outputs.nativeSchema }}" in args,
        )
    check(
        "workflow: portable repair does not claim native enforcement",
        "--json-schema" not in step_by_id(steps, "nl_repair")["with"]["claude_args"],
    )


def route_cli(primary: Path, repair: Path, output: Path) -> dict[str, str]:
    state = {
        "repo": "firstmate",
        "number": 423,
        "kind": "pr-review",
        "head_sha": "abc1234",
    }
    body = "<!-- wheelhouse-state: %s -->" % json.dumps(state, separators=(",", ":"))
    environment = os.environ.copy()
    environment.update(
        GITHUB_OUTPUT=str(output),
        NL_EXECUTION_FILE=str(primary),
        NL_REPAIR_EXECUTION_FILE=str(repair),
        ISSUE_BODY=body,
        KIND="pr-review",
        GITHUB_REPOSITORY_OWNER="owner",
    )
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "apply_decision.py"), "nl-route"],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    return parse_outputs(output)


def test_end_to_end_route_and_failure():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        primary = malformed_claude_result(root)
        repair_root = root / "valid-repair"
        valid_repair = run_fake(
            repair_root,
            "nl-decision.schema-repair",
            final=VALID_ANSWER,
        )
        valid_repair_path = repair_root / "bundle" / "result.json"
        check("runtime: valid repair AgentResult succeeds", valid_repair["status"] == "succeeded")
        outputs = route_cli(primary, valid_repair_path, root / "success-output.txt")
        check(
            "E2E: malformed primary plus valid repair routes a real answer",
            outputs.get("result_valid") == "true"
            and outputs.get("repair_status") == "repaired"
            and outputs.get("mode") == "answer"
            and outputs.get("answer") == VALID_ANSWER["answer"]
            and not outputs.get("decision"),
        )

        invalid_root = root / "invalid-repair"
        invalid_repair = run_fake(
            invalid_root,
            "nl-decision.schema-repair",
            final={"answer": "still missing mode"},
        )
        invalid_path = invalid_root / "bundle" / "result.json"
        check(
            "runtime: still-invalid repair remains schema-invalid",
            invalid_repair["error"]["code"] == "output.schema_invalid",
        )
        failed = route_cli(primary, invalid_path, root / "failure-output.txt")
        check(
            "E2E: invalid repair cannot post or route anything",
            failed.get("result_valid") == "false"
            and failed.get("mode") == ""
            and failed.get("decision") == ""
            and failed.get("answer") == "",
        )
        check(
            "E2E: invalid repair projects a precise retryable schema reason",
            failed.get("failure_code") == "output.schema_invalid"
            and failed.get("retryable") == "true"
            and "missing required field mode" in failed.get("failure_reason", "")
            and "schema-invalid" in failed.get("failure_message", ""),
        )


def step_by_id(steps, step_id):
    return next((step for step in steps if step.get("id") == step_id), None)


def test_structural_single_attempt_and_token_isolation():
    handler = yaml.safe_load((ROOT / ".github/workflows/decision-handler.yml").read_text())
    model = yaml.safe_load((ROOT / ".github/workflows/claude-model.yml").read_text())
    handle_steps = handler["jobs"]["handle"]["steps"]
    repair_prepare = handler["jobs"]["nl-repair-prepare"]
    consume = handler["jobs"]["nl-claude-consume"]
    model_steps = model["jobs"]["model"]["steps"]

    codex_runs = [
        step
        for step in handle_steps
        if step.get("id") == "nl-agent-runtime-repair"
    ]
    claude_calls = [
        step
        for step in repair_prepare["steps"]
        if step.get("id") == "nl-claude-repair-model"
    ]
    repair_claims = [
        step
        for step in handle_steps + repair_prepare["steps"]
        if step.get("id") == "nl-repair-claim"
    ]
    check(
        "workflow: each runtime branch contains exactly one repair execution",
        len(codex_runs) == 1 and len(claude_calls) == 1,
    )
    check(
        "workflow: both runtime branches durably claim the repair action",
        len(repair_claims) == 2
        and all(
            "--action nl-decision.schema-repair" in step.get("run", "")
            and "--event-id" in step.get("run", "")
            for step in repair_claims
        ),
    )
    repair_model_jobs = [name for name in handler["jobs"] if name == "nl-repair-model"]
    check("workflow: Claude repair has exactly one child model job", len(repair_model_jobs) == 1)
    check(
        "workflow: repair model has only the one preparation dependency",
        handler["jobs"]["nl-repair-model"].get("needs") == "nl-repair-prepare",
    )

    repair_step = step_by_id(model_steps, "nl_repair")
    repair_text = json.dumps(repair_step, sort_keys=True)
    check("model: dedicated NL repair step exists", repair_step is not None)
    check("model: repair is exactly one turn", "--max-turns 1" in repair_step["with"]["claude_args"])
    check("model: repair requests an empty allowlist", '--allowedTools ""' in repair_step["with"]["claude_args"])
    check(
        "model: repair fail-closes every file, exec, network, and subagent tool",
        all(name in repair_step["with"]["settings"] for name in ("Bash", "Read", "Write", "Glob", "Grep", "WebFetch", "WebSearch", "Task")),
    )
    check(
        "model: repair step receives no FLEET_TOKEN or READONLY_TOKEN",
        "FLEET_TOKEN" not in repair_text and "READONLY_TOKEN" not in repair_text,
    )

    route = step_by_id(consume["steps"], "route")
    failure = step_by_id(consume["steps"], "nl-failure-consumer")
    reply = step_by_id(consume["steps"], "nl-reply-consumer")
    check(
        "workflow: route consumes both trusted AgentResults and re-validates before reply",
        "NL_EXECUTION_FILE" in route["env"]
        and "NL_REPAIR_EXECUTION_FILE" in route["env"]
        and "NL_REPAIR_CLAIM_ADMITTED" in route["env"]
        and "steps.route.outputs.mode == 'answer'" in str(reply.get("if", "")),
    )
    check(
        "workflow: schema failure uses the precise trusted projection",
        "result_valid" in str(failure.get("if", ""))
        and "FAILURE_MESSAGE" in failure["env"],
    )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        primary, primary_bundle, _ = make_task(root / "primary", "nl-decision.local")
        repair, repair_bundle, _ = make_task(root / "repair", "nl-decision.schema-repair")
        check(
            "runtime: repair uses the identical nl-decision-v1 schema",
            primary["spec"]["output"]["schemaSha256"]
            == repair["spec"]["output"]["schemaSha256"]
            and primary["spec"]["output"]["schemaId"]
            == repair["spec"]["output"]["schemaId"]
            == "wheelhouse/nl-decision/v1",
        )
        check(
            "runtime: repair task is one-turn, zero-tool, and input-free",
            repair["spec"]["limits"]["maxTurns"] == 1
            and repair["spec"]["limits"]["maxToolCalls"] == 0
            and repair["spec"]["tools"]["tools"] == []
            and repair["spec"]["inputs"] == [],
        )
        check(
            "runtime: primary declares one repair layer and repair cannot recurse",
            primary["spec"]["retry"]["repairTask"] == "nl-decision.schema-repair/v1"
            and repair["spec"]["retry"]["repairTask"] is None,
        )
        check(
            "runtime: repair bundle does not contain the target input",
            not any(row.get("id") == "target" for row in repair["spec"]["inputs"])
            and repair_bundle.is_dir()
            and primary_bundle.is_dir(),
        )


def main():
    test_pure_repair_contract()
    test_native_bridge_and_portable_fallback()
    test_bound_schema_is_passed_to_action()
    test_end_to_end_route_and_failure()
    test_structural_single_attempt_and_token_isolation()
    if FAILURES:
        raise SystemExit("%d NL schema-repair checks failed" % len(FAILURES))
    print("\nall NL schema-repair tests passed")


if __name__ == "__main__":
    main()
