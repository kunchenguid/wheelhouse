#!/usr/bin/env python3
"""Offline Claude Action bridge contract and provenance tests."""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.admission import event_key_sha256, normalized_event_identity, stage_from_task
from agent_runtime.claude_bridge import ACTION_COMMIT, ACTION_VERSION, CLAUDE_CODE_VERSION, IMMUTABLE_MODEL, bridge, write_controller_failure_result, write_revision_mismatch_result
from agent_runtime.config import resolve_selection
from agent_runtime.consumer import result_text
from agent_runtime.contract import ContractError, canonical_sha256, file_sha256, result_projection_sha256, validate_contract
from agent_runtime.task_builder import build_task, claude_declared_outputs, claude_declared_tools

sys.path.insert(0, str(Path("scripts").resolve()))
import render_card  # noqa: E402

FAILURES = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def cancellation(conclusion="cancelled", request_status="accepted", return_code=0):
    return {
        "requestStatus": request_status,
        "requestReturnCode": return_code,
        "terminalStatus": "completed" if conclusion else "",
        "terminalConclusion": conclusion,
        "cancellationConfirmed": conclusion == "cancelled",
    }


def make_bundle(
    root: Path,
    action: str = "deep-review.local",
    include_vision: bool = False,
    allow_automerge_behavior: bool = False,
    target_text: str = "fixture target\n",
    event_key: str = "a" * 64,
    execution_id: str = "",
    owner: str = "owner",
    repo: str = "repo",
    number: int = 7,
    revision: str = "abcdef1",
):
    root.mkdir(parents=True)
    prompt = root / "prompt.txt"
    target = root / "target.txt"
    prompt.write_text("Return the bounded result.\n", encoding="utf-8")
    target.write_text(target_text, encoding="utf-8")
    vision = root / "vision.md"
    if include_vision:
        vision.write_text("Trusted project vision.\n", encoding="utf-8")
    bundle = root / "bundle"
    task = build_task(
        action=action,
        selection=resolve_selection(action, "repo"),
        prompt_path=str(prompt),
        bundle_dir=str(bundle),
        output_path=str(bundle / "task.json"),
        owner=owner,
        repo=repo,
        number=number,
        target_kind="pr-review",
        revision=revision,
        wheelhouse_revision="30271b6907e568419cdc48694a11b0c2f699b433",
        event_key=event_key,
        target_file=str(target),
        vision_file=str(vision) if include_vision else "",
        allow_automerge_behavior=allow_automerge_behavior,
    )
    if execution_id:
        task["metadata"]["executionId"] = execution_id
        (bundle / "task.json").write_text(json.dumps(task), encoding="utf-8")
    return task, bundle


def transcript(path: Path, model: str, text: str, duration_ms: int = 2500):
    path.write_text(
        json.dumps(
            [
                {"type": "system", "subtype": "init", "model": model},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
                {"type": "result", "subtype": "success", "is_error": False, "result": text, "duration_ms": duration_ms, "num_turns": 2},
            ]
        ),
        encoding="utf-8",
    )


def run_bridge(bundle: Path, execution: Path, suffix: str, conclusion: str = "success", termination_reason: str = "completed", readonly_token_boundary: str | None = None):
    result = bundle / ("result-%s.json" % suffix)
    events = bundle / ("events-%s.ndjson" % suffix)
    task = json.loads((bundle / "task.json").read_text(encoding="utf-8"))
    enforcement = bundle / ("enforcement-%s.json" % suffix)
    handoff_sha256 = "a" * 64
    inputs_verified = termination_reason == "completed"
    observation = "b" * 64
    enforced = {name: task["spec"]["limits"][name] for name, quality in task["spec"]["limits"]["enforcement"].items() if quality == "externally-enforced"}
    if readonly_token_boundary is None:
        readonly_token_boundary = "in-process" if task["metadata"]["action"].endswith(".search") else "absent"
    enforcement.write_text(json.dumps({"version": 1, "boundary": "separate-read-only-github-job", "jobPermissions": {"actions": "read", "contents": "read", "issues": "none"}, "writeCapableGithubTokenAvailable": False, "fleetTokenAvailable": False, "readonlyTokenBoundary": readonly_token_boundary, "spendStarted": True, "isolationLevel": "github-readonly-artifact-bridge-v1", "artifactHydration": "content-addressed-bounded-verified", "targetInputsReadOnly": inputs_verified, "preActionInputObservationSha256": observation, "postActionInputObservationSha256": observation if inputs_verified else None, "declaredOutputPaths": claude_declared_outputs(task["metadata"]["action"]), "workspaceRepository": "local-no-remote", "declaredTools": claude_declared_tools(task["metadata"]["action"]), "action": task["metadata"]["action"], "actionSourceCommit": ACTION_COMMIT, "actionMetadataQuality": "pinned-action-reference", "actionMetadataSha256": None, "taskSha256": canonical_sha256(task), "handoffManifestSha256": handoff_sha256, "transcriptSha256": file_sha256(execution) if execution.is_file() else None, "childExecutionTimeoutMs": task["spec"]["limits"]["childExecutionTimeoutMs"], "controller": {"parentRunId": "1", "parentRunAttempt": "1", "modelRunId": "2", "hardDeadlineMs": None, "dispatchDeadlineMs": task["spec"]["limits"]["dispatchDeadlineMs"], "childExecutionTimeoutMs": task["spec"]["limits"]["childExecutionTimeoutMs"], "enforcedLimits": enforced, "conclusion": conclusion, "terminationReason": termination_reason, "dispatchRef": "main", "expectedCommitSha": task["metadata"]["wheelhouseRevision"], "observedCommitSha": task["metadata"]["wheelhouseRevision"], "correlationId": "a" * 32}}), encoding="utf-8")
    value = bridge(str(bundle / "task.json"), str(bundle), str(execution), "", str(enforcement), handoff_sha256, str(result), str(events))
    validate_contract(value, "AgentResult")
    return value, events


def main():
    lock = json.loads(Path("agent_runtime/runtime.lock.json").read_text(encoding="utf-8"))["claudeProduction"]
    check("bridge: action and harness pins match the runtime lock", lock["actionCommit"] == ACTION_COMMIT and lock["actionRelease"] == "v" + ACTION_VERSION and lock["claudeCodeVersion"] == CLAUDE_CODE_VERSION and lock["model"] == IMMUTABLE_MODEL and lock["allowModelAlias"] is False)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        def triage_value(
            include_vision: bool = False, include_automerge: bool = True
        ):
            value = {
                "summary": "A narrow internal change.",
                "product_implications": "Routine maintenance.",
                "recommended_action": "merge",
                "recommended_reason": "Checks are green.",
                "evidence": 'target.txt: "fixture target"',
                "automerge": {
                    "behavior_class": "A",
                    "behavior_assertions": [],
                    "changes_existing_or_default_behavior": False,
                    "optin_default_off": False,
                },
            }
            if not include_automerge:
                value.pop("automerge")
            elif include_vision:
                value["automerge"].update(
                    aligns_with_vision=True,
                    recommend_merge=True,
                    external_source_required=False,
                )
                value["vision_evidence"] = {
                    "target_owner": "owner",
                    "target_repo": "repo",
                    "target_number": 7,
                    "target_facts_sha256": "e" * 64,
                    "vision_sha": "b" * 40,
                    "vision_content_sha256": "c" * 64,
                    "base_sha": "d" * 40,
                    "target_head_sha": "abcdef1",
                    "applicable_criteria": [
                        {
                            "id": "local",
                            "quote": "Trusted project vision.",
                            "external_source_required": False,
                        }
                    ],
                }
            return value

        _, incomplete_bundle = make_bundle(
            root / "triage-incomplete", action="triage.pr.local"
        )
        incomplete_execution = root / "triage-incomplete.json"
        transcript(incomplete_execution, IMMUTABLE_MODEL, json.dumps(triage_value()))
        incomplete_result, _ = run_bridge(
            incomplete_bundle, incomplete_execution, "triage-incomplete"
        )
        check(
            "bridge: incomplete-diff triage rejects behavior facts",
            incomplete_result["status"] == "failed"
            and incomplete_result["error"]["code"] == "output.schema_invalid",
        )
        _, incomplete_plain_bundle = make_bundle(
            root / "triage-incomplete-plain", action="triage.pr.local"
        )
        incomplete_plain_execution = root / "triage-incomplete-plain.json"
        transcript(
            incomplete_plain_execution,
            IMMUTABLE_MODEL,
            json.dumps(triage_value(include_automerge=False)),
        )
        incomplete_plain_result, _ = run_bridge(
            incomplete_plain_bundle,
            incomplete_plain_execution,
            "triage-incomplete-plain",
        )
        check(
            "bridge: incomplete-diff triage accepts no behavior object",
            incomplete_plain_result["status"] == "succeeded",
        )
        _, no_vision_bundle = make_bundle(
            root / "triage-no-vision",
            action="triage.pr.local",
            allow_automerge_behavior=True,
        )
        no_vision_execution = root / "triage-no-vision.json"
        transcript(no_vision_execution, IMMUTABLE_MODEL, json.dumps(triage_value()))
        no_vision_result, _ = run_bridge(
            no_vision_bundle, no_vision_execution, "triage-no-vision"
        )
        check(
            "bridge: complete no-VISION triage accepts independent facts",
            no_vision_result["status"] == "succeeded",
        )
        _, no_vision_extra_bundle = make_bundle(
            root / "triage-no-vision-extra",
            action="triage.pr.local",
            allow_automerge_behavior=True,
        )
        no_vision_extra_execution = root / "triage-no-vision-extra.json"
        transcript(
            no_vision_extra_execution,
            IMMUTABLE_MODEL,
            json.dumps(triage_value(include_vision=True)),
        )
        no_vision_extra_result, _ = run_bridge(
            no_vision_extra_bundle,
            no_vision_extra_execution,
            "triage-no-vision-extra",
        )
        check(
            "bridge: complete no-VISION triage rejects vision-bound fields",
            no_vision_extra_result["status"] == "failed"
            and no_vision_extra_result["error"]["code"] == "output.schema_invalid",
        )
        _, vision_bundle = make_bundle(
            root / "triage-vision",
            action="triage.pr.local",
            include_vision=True,
            allow_automerge_behavior=True,
        )
        vision_missing_execution = root / "triage-vision-missing.json"
        transcript(vision_missing_execution, IMMUTABLE_MODEL, json.dumps(triage_value()))
        vision_missing_result, _ = run_bridge(
            vision_bundle, vision_missing_execution, "triage-vision-missing"
        )
        check(
            "bridge: trusted VISION triage requires vision-bound fields",
            vision_missing_result["status"] == "failed"
            and vision_missing_result["error"]["code"] == "output.schema_invalid",
        )
        vision_execution = root / "triage-vision.json"
        transcript(vision_execution, IMMUTABLE_MODEL, json.dumps(triage_value(include_vision=True)))
        vision_result, _ = run_bridge(vision_bundle, vision_execution, "triage-vision")
        check(
            "bridge: trusted VISION triage accepts a complete verdict",
            vision_result["status"] == "succeeded",
        )

        production = json.loads(
            Path("tests/fixtures/provider-telemetry-six.json").read_text(
                encoding="utf-8"
            )
        )
        check(
            "production cohort: exact replay runs, cards, and executions are fixed",
            {
                (case["run_id"], case["card"], case["execution_id"])
                for case in production
            }
            == {
                ("29985469441", 1483, "6dedd49f-1009-46b2-bda7-5dc5d6467114"),
                ("29985480267", 1584, "889d5768-59c0-431f-9963-1645eb3be218"),
                ("29985490774", 1585, "c81c01f3-f707-4e7a-a1a4-b56f9cd7ef77"),
                ("29985502800", 1586, "442ff57c-635a-4aee-9cb6-0c5db7b722aa"),
                ("29985514969", 1594, "08ed5f8a-e1fd-4230-9566-441056581487"),
                ("29985527456", 1598, "663ba663-c5f0-4f78-87fb-e10d6bdcaf67"),
            },
        )
        normalized_successes = 0
        consumer_successes = 0
        for case in production:
            case_root = root / ("production-%s" % case["run_id"])
            binding = case.get("source_binding") or {
                "owner": "owner",
                "repo": "repo",
                "number": 7,
                "target_head_sha": "abcdef1",
                "base_sha": "d" * 40,
                "vision_sha": "b" * 40,
            }
            if case.get("target_fixture"):
                target_fixture = Path(case["target_fixture"])
                target_fixture_bytes = target_fixture.read_bytes()
                if case.get("target_fixture_encoding") == "base64":
                    target_fixture_bytes = base64.b64decode(
                        b"".join(target_fixture_bytes.split()), validate=True
                    )
                target_text = target_fixture_bytes.decode("utf-8")
            else:
                target_text = case["target_excerpt"] + "\n"
            event_key = event_key_sha256(
                normalized_event_identity(
                    action="triage.pr.search",
                    owner=binding["owner"],
                    repo=binding["repo"],
                    number=binding["number"],
                    card_issue=case["card"],
                    revision=binding["target_head_sha"],
                )
            )
            case_task, case_bundle = make_bundle(
                case_root,
                action="triage.pr.search",
                include_vision=True,
                allow_automerge_behavior=True,
                target_text=target_text,
                event_key=event_key,
                execution_id=case["execution_id"],
                owner=binding["owner"],
                repo=binding["repo"],
                number=binding["number"],
                revision=binding["target_head_sha"],
            )
            case_execution = root / ("production-%s.json" % case["run_id"])
            transcript(
                case_execution,
                IMMUTABLE_MODEL,
                case["raw_output"],
            )
            case_result, case_events = run_bridge(
                case_bundle,
                case_execution,
                "production-%s" % case["run_id"],
            )
            compact = result_text(
                str(case_bundle / ("result-production-%s.json" % case["run_id"])),
                require_success=False,
            )
            repair = render_card.plan_triage_repair(compact, "pr-review")
            consumer = render_card.decide_triage_apply(
                compact,
                "",
                str(case_root / "target.txt"),
            )
            item = {
                "repo": binding["repo"],
                "number": binding["number"],
                "kind": "pr-review",
                "head_sha": binding["target_head_sha"],
                "title": "Production replay %s" % case["run_id"],
                "author": "fixture",
                "bucket": "review-needed",
                "comp": "pass",
                "tests": "green",
                "url": "https://example.invalid/%s/pull/%s"
                % (binding["repo"], binding["number"]),
                "summary": "production replay",
                "recommendation": "Review the normalized result.",
                "priority": "med",
                "options": ["merge", "investigate"],
            }
            rendered = render_card.render(item)
            queued_body = render_card.body_with_triage_queued(
                rendered["body"], item
            )
            card = {
                "number": case["card"],
                "state": "OPEN",
                "body": queued_body,
                "labels": rendered["labels"],
            }
            card_writes = []
            card_error = (
                "evidence quotes did not match the fetched target"
                if consumer["outcome"] == "anchor-fail"
                else None
            )
            with (
                patch.object(render_card, "get_card", return_value=card),
                patch.object(
                    render_card,
                    "_edit_issue_body",
                    side_effect=lambda number, body, **kwargs: card_writes.append(
                        (number, body, kwargs)
                    ),
                ),
                patch.object(
                    render_card,
                    "_evaluate_automerge_card_projection",
                    return_value=render_card.criteria_schema.unavailable_criteria(
                        "offline production replay"
                    ),
                ),
            ):
                applied = render_card.update_card_triage(
                    case["card"],
                    binding["target_head_sha"],
                    triage=consumer["triage"],
                    error=card_error,
                    owner=binding["owner"],
                    vision_sha=binding["vision_sha"],
                    base_sha=binding["base_sha"],
                    automerge_behavior_available=True,
                    require_queued=True,
                )
            committed_body = card_writes[0][1] if applied and len(card_writes) == 1 else ""
            committed_state = render_card.parse_state_block(committed_body)
            stage = stage_from_task(
                case_task,
                stage="consumer-committed",
                status="ok",
                code=case["expected_stage_code"],
            )
            events_rows = [
                json.loads(line)
                for line in case_events.read_text(encoding="utf-8").splitlines()
            ]
            validation_events = [
                row for row in events_rows if row["type"] == "validation.completed"
            ]
            terminal_events = [
                row for row in events_rows if row["type"] == "execution.completed"
            ]
            check(
                "production %s: raw provider terminal remains successful"
                % case["run_id"],
                json.loads(case_execution.read_text(encoding="utf-8"))[-1][
                    "subtype"
                ]
                == "success",
            )
            check(
                "production %s: normalized status and code are truthful"
                % case["run_id"],
                case_result["status"] == case["expected_normalized_status"]
                and (case_result.get("error") or {}).get("code")
                == case["expected_normalized_code"],
            )
            check(
                "production %s: valid compact output is never repair eligible"
                % case["run_id"],
                bool(compact) and repair["repair_needed"] is False,
            )
            check(
                "production %s: deterministic consumer outcome stays distinct"
                % case["run_id"],
                consumer["outcome"] == case["expected_consumer_outcome"],
            )
            check(
                "production %s: one correlated validation and terminal event"
                % case["run_id"],
                len(validation_events) == 1
                and len(terminal_events) == 1
                and all(
                    row["executionId"] == case_result["executionId"]
                    for row in validation_events + terminal_events
                )
                and validation_events[0]["data"]["errorCode"]
                == case["expected_normalized_code"]
                and terminal_events[0]["data"]["status"]
                == case["expected_normalized_status"],
            )
            check(
                "production %s: task binds exact replay execution and event"
                % case["run_id"],
                case_task["metadata"]["executionId"] == case["execution_id"]
                and case_result["executionId"] == case["execution_id"]
                and case_task["metadata"]["idempotencyKey"] == event_key,
            )
            if case["card"] == 1585:
                candidate, candidate_reason = render_card._extract_json_object(
                    case["raw_output"]
                )
                facts_path = Path(case["target_facts_fixture"])
                vision_path = Path(case["vision_fixture"])
                target_input = next(
                    row for row in case_task["spec"]["inputs"] if row["id"] == "target"
                )
                check(
                    "production 29985490774: frozen target content and source facts are exact",
                    target_input["sha256"] == case["target_sha256"]
                    and target_input["bytes"] == case["target_bytes"]
                    and file_sha256(facts_path)
                    == binding["target_facts_sha256"]
                    and file_sha256(vision_path)
                    == binding["vision_content_sha256"],
                )
                check(
                    "production 29985490774: compact candidate carries the frozen head/base/VISION binding",
                    candidate_reason == ""
                    and candidate["vision_evidence"]
                    == {
                        "target_owner": binding["owner"],
                        "target_repo": binding["repo"],
                        "target_number": binding["number"],
                        "target_facts_sha256": binding["target_facts_sha256"],
                        "vision_sha": binding["vision_sha"],
                        "vision_content_sha256": binding["vision_content_sha256"],
                        "base_sha": binding["base_sha"],
                        "target_head_sha": binding["target_head_sha"],
                        "applicable_criteria": [],
                    },
                )
                exact_facts = render_card._trusted_triage_target_facts(
                    str(facts_path),
                    owner=binding["owner"],
                    repo=binding["repo"],
                    number=binding["number"],
                    revision=binding["target_head_sha"],
                    base_sha=binding["base_sha"],
                    target_facts_sha256=binding["target_facts_sha256"],
                )
                wrong_head_facts = render_card._trusted_triage_target_facts(
                    str(facts_path),
                    owner=binding["owner"],
                    repo=binding["repo"],
                    number=binding["number"],
                    revision="f" * 40,
                    base_sha=binding["base_sha"],
                    target_facts_sha256=binding["target_facts_sha256"],
                )
                check(
                    "production 29985490774: target facts admit only the frozen head and allowed paths",
                    exact_facts
                    == (
                        [
                            "internal/cli/helpers_test.go",
                            "internal/daemon/manager.go",
                            "internal/daemon/manager_test.go",
                            "internal/git/git.go",
                            "internal/git/git_test.go",
                        ],
                        binding["target_facts_sha256"],
                    )
                    and wrong_head_facts is None,
                )
            check(
                "production %s: offline card update commits expected status"
                % case["run_id"],
                applied
                and committed_state.get("triage_status")
                == case["expected_card_status"],
            )
            check(
                "production %s: projection stage binds task and stays distinct"
                % case["run_id"],
                stage["code"] == case["expected_stage_code"]
                and stage["stage"] == "consumer-committed"
                and stage["executionId"] == case["execution_id"]
                and stage["eventKeySha256"] == event_key
                and stage["sourceSha"]
                == case_task["metadata"]["wheelhouseRevision"],
            )
            if case_result["status"] == "succeeded":
                normalized_successes += 1
            if consumer["outcome"] == "success":
                consumer_successes += 1
                check(
                    "production %s: successful consumer retains behavior verdict"
                    % case["run_id"],
                    bool(
                        render_card.normalize_triage(consumer["triage"]).get(
                            "automerge_verdict"
                        )
                    ),
                )
                check(
                    "production %s: card persists behavior verdict"
                    % case["run_id"],
                    bool(committed_state.get("automerge_verdict")),
                )
            else:
                check(
                    "production %s: rejected evidence persists no verdict or schema success"
                    % case["run_id"],
                    committed_state.get("automerge_verdict") is None
                    and committed_state.get("triage_status") == "error"
                    and case_result["error"]["code"] == "output.evidence_invalid",
                )
        check(
            "production cohort: all six normalized and consumer successes",
            normalized_successes == 6 and consumer_successes == 6,
        )

        _, unsupported_bundle = make_bundle(
            root / "unsupported-evidence",
            action="triage.pr.local",
        )
        unsupported_execution = root / "unsupported-evidence.json"
        unsupported_value = triage_value(include_automerge=False)
        unsupported_value["evidence"] = (
            'target-src/internal/git/git.go: "source-only quote outside target scope"'
        )
        transcript(
            unsupported_execution,
            IMMUTABLE_MODEL,
            json.dumps(unsupported_value),
        )
        unsupported_result, _ = run_bridge(
            unsupported_bundle,
            unsupported_execution,
            "unsupported-evidence",
        )
        unsupported_text = result_text(
            str(unsupported_bundle / "result-unsupported-evidence.json"),
            require_success=False,
        )
        unsupported_repair = render_card.plan_triage_repair(
            unsupported_text, "pr-review"
        )
        unsupported_consumer = render_card.decide_triage_apply(
            unsupported_text,
            "",
            str(root / "unsupported-evidence" / "target.txt"),
        )
        check(
            "evidence boundary: structurally valid unsupported quote is not a schema failure",
            unsupported_result["status"] == "failed"
            and unsupported_result["error"]["code"] == "output.evidence_invalid"
            and unsupported_repair["repair_needed"] is False
            and unsupported_consumer["outcome"] == "anchor-fail",
        )

        _, invalid_bundle = make_bundle(
            root / "genuine-schema-invalid",
            action="triage.pr.local",
        )
        invalid_execution = root / "genuine-schema-invalid.json"
        transcript(
            invalid_execution,
            IMMUTABLE_MODEL,
            json.dumps({"summary": "Missing every other required field."}),
        )
        invalid_result, invalid_events = run_bridge(
            invalid_bundle,
            invalid_execution,
            "genuine-schema-invalid",
        )
        invalid_text = result_text(
            str(invalid_bundle / "result-genuine-schema-invalid.json"),
            require_success=False,
        )
        invalid_plan = render_card.plan_triage_repair(invalid_text, "pr-review")
        valid_repair = {
            "summary": "A corrected bounded result.",
            "product_implications": "Routine maintenance.",
            "recommended_action": "hold",
            "recommended_reason": "Owner review remains appropriate.",
            "evidence": 'target.txt: "fixture target"',
        }
        repaired = render_card.decide_triage_apply(
            invalid_text,
            json.dumps(valid_repair),
            str(root / "genuine-schema-invalid" / "target.txt"),
        )
        repair_failed = render_card.decide_triage_apply(
            invalid_text,
            json.dumps({"summary": "Still incomplete."}),
            str(root / "genuine-schema-invalid" / "target.txt"),
        )
        invalid_event_rows = [
            json.loads(line)
            for line in invalid_events.read_text(encoding="utf-8").splitlines()
        ]
        check(
            "bridge: genuine schema-invalid output remains explicitly invalid",
            invalid_result["status"] == "failed"
            and invalid_result["error"]["code"] == "output.schema_invalid"
            and invalid_plan["repair_needed"] is True,
        )
        check(
            "bridge: genuine schema-invalid output has one terminal failure",
            sum(row["type"] == "validation.completed" for row in invalid_event_rows)
            == 1
            and sum(row["type"] == "execution.completed" for row in invalid_event_rows)
            == 1,
        )
        check(
            "consumer: the one bounded repair can succeed or fail closed",
            repaired["outcome"] == "repaired"
            and repair_failed["outcome"] == "repair-failed",
        )

        task, bundle = make_bundle(root / "success")
        execution = root / "success.json"
        transcript(execution, IMMUTABLE_MODEL, "HOLD\n\n- Reviewed the bounded target.")
        result, events = run_bridge(bundle, execution, "success")
        check("bridge: immutable Claude task validates", task["spec"]["selection"]["candidates"][0]["allowModelAlias"] is False)
        check("bridge: honest artifact isolation is recorded", task["spec"]["isolation"]["profile"] == "claude-artifact-bridge-v1" and task["spec"]["isolation"]["modelNetwork"]["mode"] == "runner-default" and result["proof"]["isolationLevel"] == "github-readonly-artifact-bridge-v1")
        check("bridge: unsupported worker guarantees are absent", task["spec"]["isolation"]["dropCapabilities"] is False and task["spec"]["isolation"]["noNewPrivileges"] is False and task["spec"]["isolation"]["denyHostHome"] is False)
        check("bridge: unenforceable provider limits are explicit unavailable", all(task["spec"]["limits"][name] is None for name in ("softDeadlineMs", "cancelGraceMs", "maxTurns", "maxToolCalls", "maxProviderRequests", "maxInputTokens", "maxOutputTokens")))
        check("bridge: end-to-end hard deadline is honestly unavailable", task["spec"]["limits"]["hardDeadlineMs"] is None and task["spec"]["limits"]["enforcement"]["hardDeadlineMs"] == "unavailable")
        check("bridge: result preserves generic limit enforcement", result["proof"]["limitEnforcement"] == task["spec"]["limits"]["enforcement"] and result["proof"]["limitEnforcementSha256"] == canonical_sha256(task["spec"]["limits"]["enforcement"]))
        task["spec"]["limits"]["maxToolCalls"] = 1
        task["spec"]["limits"]["enforcement"]["maxToolCalls"] = "adapter-enforced"
        validate_contract(task, "AgentTask")
        check("bridge: mixed limit enforcement is provider neutral", True)
        task["spec"]["limits"]["enforcement"]["maxToolCalls"] = "unavailable"
        try:
            validate_contract(task, "AgentTask")
        except ContractError:
            check("bridge: mismatched limit evidence is rejected", True)
        else:
            check("bridge: mismatched limit evidence is rejected", False)
        task["spec"]["limits"]["maxToolCalls"] = None
        task["spec"]["limits"]["enforcement"]["maxToolCalls"] = "unavailable"
        check("bridge: harness executable provenance remains unavailable", result["selection"]["harnessVersion"] is None and result["selection"]["harnessDigest"] is None and result["selection"]["harnessProvenanceQuality"] == "pinned-action-reference" and result["selection"]["harnessSourceCommit"] == ACTION_COMMIT)
        check("bridge: observed model accepted", result["status"] == "succeeded" and result["selection"]["actualModel"] == IMMUTABLE_MODEL)
        check("bridge: provider and effort are not falsely labeled observed", result["selection"]["provider"] == "anthropic" and result["selection"]["actualProvider"] == "" and result["selection"]["actualEffort"] == "")
        check("bridge: usage remains unavailable when action omits tokens", result["usage"]["inputTokens"] is None and result["usage"]["providerRequests"] is None)
        check("bridge: timing comes from the terminal action event", result["usage"]["durationMs"] == 2500 and result["startedAt"] < result["completedAt"])
        check("bridge: normalized events contain no delivered text", "Reviewed the bounded target" not in events.read_text(encoding="utf-8"))
        terminal_event = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
        check("bridge: terminal hash uses stable non-cyclic projection", terminal_event["data"]["projection"] == "agent-result-without-artifacts/v1" and terminal_event["data"]["resultSha256"] == result_projection_sha256(result))

        search_task, search_bundle = make_bundle(
            root / "search", action="deep-review.search"
        )
        search_execution = root / "search.json"
        transcript(search_execution, IMMUTABLE_MODEL, "HOLD\n\n- Reviewed the bounded target.")
        search_result, _ = run_bridge(search_bundle, search_execution, "search")
        check(
            "bridge: reconciled in-process search boundary is accepted",
            search_result["status"] == "succeeded"
            and search_task["spec"]["isolation"]["toolNetwork"]["mode"]
            == "runner-default"
            and next(
                capability
                for capability in search_task["spec"]["capabilities"]["required"]
                if capability["name"] == "credentials.isolated"
            )["constraints"]["readonlyToken"]
            == "in-process",
        )
        stale_search_result, _ = run_bridge(
            search_bundle,
            search_execution,
            "search-broker-only",
            readonly_token_boundary="broker-only",
        )
        check(
            "bridge: stale broker-only search boundary fails closed",
            stale_search_result["status"] == "failed"
            and stale_search_result["error"]["code"] == "sandbox.violation",
        )

        mismatch_result_path = bundle / "revision-mismatch-result.json"
        mismatch_events_path = bundle / "revision-mismatch-events.ndjson"
        revision_mismatch = write_revision_mismatch_result(
            str(bundle / "task.json"),
            str(bundle),
            task["metadata"]["wheelhouseRevision"],
            "c" * 40,
            "42",
            "main",
            "d" * 32,
            cancellation(),
            str(mismatch_result_path),
            str(mismatch_events_path),
        )
        validate_contract(revision_mismatch, "AgentResult")
        check("bridge: source revision mismatch is precise and retryable", revision_mismatch["status"] == "failed" and revision_mismatch["error"]["code"] == "source.revision_mismatch" and revision_mismatch["error"]["retryable"] is True and revision_mismatch["error"]["fallbackEligible"] is False and revision_mismatch["error"]["spendStarted"] is True)
        check("bridge: revision mismatch records only parent run evidence", revision_mismatch["proof"]["revisionBinding"]["expectedCommitSha"] == task["metadata"]["wheelhouseRevision"] and revision_mismatch["proof"]["revisionBinding"]["observedCommitSha"] == "c" * 40 and revision_mismatch["proof"]["revisionBinding"]["cancellationConfirmed"] is True and revision_mismatch["proof"]["revisionBinding"]["cancellationError"] is None and revision_mismatch["selection"]["actualModel"] == "" and revision_mismatch["usage"]["providerRequests"] is None and revision_mismatch["usage"]["toolCalls"] is None)
        check("bridge: revision mismatch events remain content free", "fixture target" not in mismatch_events_path.read_text(encoding="utf-8"))

        unconfirmed_mismatch = write_revision_mismatch_result(
            str(bundle / "task.json"),
            str(bundle),
            task["metadata"]["wheelhouseRevision"],
            "c" * 40,
            "43",
            "main",
            "f" * 32,
            cancellation("success", "failed", 1),
            str(bundle / "unconfirmed-mismatch-result.json"),
            str(bundle / "unconfirmed-mismatch-events.ndjson"),
        )
        check("bridge: unconfirmed mismatch cancellation remains conservative", unconfirmed_mismatch["error"]["spendStarted"] is True and unconfirmed_mismatch["proof"]["revisionBinding"]["cancellationConfirmed"] is False and unconfirmed_mismatch["proof"]["revisionBinding"]["cancellationError"] == "lifecycle.cancel_unconfirmed")

        controller_failure = write_controller_failure_result(
            str(bundle / "task.json"),
            str(bundle),
            "42",
            "main",
            "e" * 32,
            cancellation(),
            str(bundle / "controller-failure-result.json"),
            str(bundle / "controller-failure-events.ndjson"),
        )
        validate_contract(controller_failure, "AgentResult")
        check("bridge: malformed run metadata has stable conservative failure", controller_failure["error"]["code"] == "harness.protocol" and controller_failure["error"]["spendStarted"] is True and controller_failure["usage"]["providerRequests"] is None)

        _, timeout_bundle = make_bundle(root / "timeout")
        timeout, _ = run_bridge(timeout_bundle, root / "missing-timeout.json", "timeout", "timed_out", "child-timeout")
        check("bridge: pre-invocation checkpoint preserves conservative spend", timeout["error"]["code"] == "lifecycle.timeout" and timeout["error"]["spendStarted"] is True)

        _, cancelled_bundle = make_bundle(root / "cancelled")
        cancelled, _ = run_bridge(cancelled_bundle, root / "missing-cancelled.json", "cancelled", "cancelled", "parent-sigterm")
        check("bridge: parent cancellation has normalized cancelled status", cancelled["status"] == "cancelled" and cancelled["error"]["code"] == "lifecycle.cancelled")

        _, overclaim_bundle = make_bundle(root / "overclaim")
        overclaim_task_path = overclaim_bundle / "task.json"
        overclaim_task = json.loads(overclaim_task_path.read_text(encoding="utf-8"))
        overclaim_task["spec"]["isolation"]["dropCapabilities"] = True
        overclaim_task_path.write_text(json.dumps(overclaim_task), encoding="utf-8")
        overclaim_execution = root / "overclaim.json"
        transcript(overclaim_execution, IMMUTABLE_MODEL, "HOLD\n\n- Reviewed the bounded target.")
        overclaim, _ = run_bridge(overclaim_bundle, overclaim_execution, "overclaim")
        check("bridge: Claude worker overclaims fail closed", overclaim["status"] == "failed" and overclaim["error"]["code"] == "sandbox.violation")

        _, partial_bundle = make_bundle(root / "partial")
        partial_execution = root / "partial.json"
        partial_execution.write_text(json.dumps([{"type": "system", "subtype": "init", "model": IMMUTABLE_MODEL}, {"type": "assistant", "message": {"content": [{"type": "text", "text": "HOLD"}]}}]), encoding="utf-8")
        partial, _ = run_bridge(partial_bundle, partial_execution, "partial")
        check("bridge: partial assistant output fails closed", partial["status"] == "failed" and partial["error"]["code"] == "harness.protocol" and "delivered" not in partial and "final" not in partial)

        _, duplicate_bundle = make_bundle(root / "duplicate")
        duplicate_execution = root / "duplicate.json"
        duplicate_execution.write_text(json.dumps([{"type": "system", "subtype": "init", "model": IMMUTABLE_MODEL}, {"type": "result", "subtype": "success", "is_error": False, "result": "HOLD", "duration_ms": 10}, {"type": "result", "subtype": "success", "is_error": False, "result": "HOLD", "duration_ms": 10}]), encoding="utf-8")
        duplicate, _ = run_bridge(duplicate_bundle, duplicate_execution, "duplicate")
        check("bridge: duplicate terminal results fail closed", duplicate["status"] == "failed" and duplicate["error"]["code"] == "harness.protocol" and "final" not in duplicate)

        _, mismatch_bundle = make_bundle(root / "mismatch")
        mismatch_execution = root / "mismatch.json"
        transcript(mismatch_execution, "claude-substituted-model", "HOLD")
        mismatch, _ = run_bridge(mismatch_bundle, mismatch_execution, "mismatch")
        check("bridge: observed model substitution fails closed", mismatch["status"] == "failed" and mismatch["error"]["code"] == "model.mismatch" and "final" not in mismatch)

        _, unobserved_bundle = make_bundle(root / "unobserved")
        unobserved_execution = root / "unobserved.json"
        unobserved_execution.write_text(json.dumps([{"type": "result", "is_error": False, "result": "HOLD"}]), encoding="utf-8")
        unobserved, _ = run_bridge(unobserved_bundle, unobserved_execution, "unobserved")
        check("bridge: missing observed model fails closed", unobserved["status"] == "failed" and unobserved["error"]["code"] == "model.mismatch" and not unobserved["selection"]["actualModel"])

        _, malformed_bundle = make_bundle(root / "malformed")
        malformed_execution = root / "malformed.json"
        malformed_execution.write_text("{malformed", encoding="utf-8")
        malformed, _ = run_bridge(malformed_bundle, malformed_execution, "malformed")
        check("bridge: malformed spent execution emits stable failure", malformed["status"] == "failed" and malformed["error"]["code"] == "harness.protocol" and malformed["error"]["spendStarted"] is True)

        _, empty_bundle = make_bundle(root / "empty")
        empty_execution = root / "empty.json"
        empty_execution.write_text("[]", encoding="utf-8")
        empty, empty_events = run_bridge(empty_bundle, empty_execution, "empty")
        check("bridge: empty transcript preserves spend agreement", empty["error"]["spendStarted"] is True and '"spendStarted":true' in empty_events.read_text(encoding="utf-8"))

        # Pre-hydration / pre-checkpoint / missing output fault injection: one bounded
        # no-spend stage outcome, selection fields only, no fabricated provider/model.
        _, pre_bundle = make_bundle(root / "pre-hydration")
        missing_execution = root / "missing-execution.json"
        stage = pre_bundle / "stage-enforcement.json"
        stage.write_text(
            json.dumps(
                {
                    "version": 1,
                    "stage": "pre-hydration",
                    "stageStatus": "failed",
                    "spendStarted": False,
                    "transcriptSha256": None,
                    "postActionInputObservationSha256": None,
                    "targetInputsReadOnly": False,
                    "handoffManifestSha256": "a" * 64,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        pre = bridge(
            str(pre_bundle / "task.json"),
            str(pre_bundle),
            str(missing_execution),
            "",
            str(stage),
            "a" * 64,
            str(pre_bundle / "pre-result.json"),
            str(pre_bundle / "pre-events.ndjson"),
        )
        validate_contract(pre, "AgentResult")
        check(
            "bridge: pre-hydration stage failure is no-spend sandbox.violation",
            pre["status"] == "failed"
            and pre["error"]["code"] == "sandbox.violation"
            and pre["error"]["spendStarted"] is False
            and pre["selection"]["actualModel"] == ""
            and pre["selection"]["actualProvider"] == ""
            and pre["selection"]["fallbackUsed"] is False
            and pre["usage"]["providerRequests"] is None
            and pre["usage"]["inputTokens"] is None,
        )
        check(
            "bridge: pre-hydration events stay content-free",
            "fixture target" not in (pre_bundle / "pre-events.ndjson").read_text(encoding="utf-8")
            and "Return the bounded result" not in (pre_bundle / "pre-events.ndjson").read_text(encoding="utf-8"),
        )
        check(
            "bridge: selected provider/model remain selection-only when execution never starts",
            pre["selection"]["provider"] == "anthropic"
            and pre["selection"]["requestedModel"] == IMMUTABLE_MODEL
            and not pre["selection"]["actualModel"]
            and not pre["selection"]["actualProvider"],
        )

        missing_proof = bridge(
            str(pre_bundle / "task.json"),
            str(pre_bundle),
            "",
            "",
            str(pre_bundle / "missing-enforcement.json"),
            "a" * 64,
            str(pre_bundle / "missing-result.json"),
            str(pre_bundle / "missing-events.ndjson"),
        )
        check(
            "bridge: missing checkpoint/output yields one bounded no-spend failure",
            missing_proof["status"] == "failed"
            and missing_proof["error"]["code"] == "sandbox.violation"
            and missing_proof["error"]["spendStarted"] is False
            and missing_proof["selection"]["actualModel"] == "",
        )

        _, overflow_bundle = make_bundle(root / "overflow")
        overflow_execution = root / "overflow.json"
        transcript(overflow_execution, IMMUTABLE_MODEL, "HOLD", 10**100)
        overflow, _ = run_bridge(overflow_bundle, overflow_execution, "overflow")
        check("bridge: oversized duration emits stable protocol failure", overflow["status"] == "failed" and overflow["error"]["code"] == "harness.protocol")

        # Successful controller completion still requires equal non-null signed-input evidence.
        def bridge_with_observations(label: str, post_observation, target_inputs_read_only: bool, pre_observation: str = "b" * 64):
            task_obj, bundle_dir = make_bundle(root / label)
            execution_path = root / ("%s.json" % label)
            transcript(execution_path, IMMUTABLE_MODEL, "HOLD\n\n- Reviewed the bounded target.")
            result_path = bundle_dir / ("result-%s.json" % label)
            events_path = bundle_dir / ("events-%s.ndjson" % label)
            handoff_sha256 = "a" * 64
            enforced = {
                name: task_obj["spec"]["limits"][name]
                for name, quality in task_obj["spec"]["limits"]["enforcement"].items()
                if quality == "externally-enforced"
            }
            proof = {
                "version": 1,
                "boundary": "separate-read-only-github-job",
                "jobPermissions": {"actions": "read", "contents": "read", "issues": "none"},
                "writeCapableGithubTokenAvailable": False,
                "fleetTokenAvailable": False,
                "readonlyTokenBoundary": "absent",
                "spendStarted": True,
                "isolationLevel": "github-readonly-artifact-bridge-v1",
                "artifactHydration": "content-addressed-bounded-verified",
                "targetInputsReadOnly": target_inputs_read_only,
                "preActionInputObservationSha256": pre_observation,
                "postActionInputObservationSha256": post_observation,
                "declaredOutputPaths": claude_declared_outputs(task_obj["metadata"]["action"]),
                "workspaceRepository": "local-no-remote",
                "declaredTools": claude_declared_tools(task_obj["metadata"]["action"]),
                "action": task_obj["metadata"]["action"],
                "actionSourceCommit": ACTION_COMMIT,
                "actionMetadataQuality": "pinned-action-reference",
                "actionMetadataSha256": None,
                "taskSha256": canonical_sha256(task_obj),
                "handoffManifestSha256": handoff_sha256,
                "transcriptSha256": file_sha256(execution_path),
                "childExecutionTimeoutMs": task_obj["spec"]["limits"]["childExecutionTimeoutMs"],
                "controller": {
                    "parentRunId": "1",
                    "parentRunAttempt": "1",
                    "modelRunId": "2",
                    "hardDeadlineMs": None,
                    "dispatchDeadlineMs": task_obj["spec"]["limits"]["dispatchDeadlineMs"],
                    "childExecutionTimeoutMs": task_obj["spec"]["limits"]["childExecutionTimeoutMs"],
                    "enforcedLimits": enforced,
                    "conclusion": "success",
                    "terminationReason": "completed",
                    "dispatchRef": "main",
                    "expectedCommitSha": task_obj["metadata"]["wheelhouseRevision"],
                    "observedCommitSha": task_obj["metadata"]["wheelhouseRevision"],
                    "correlationId": "a" * 32,
                },
            }
            enforcement_path = bundle_dir / ("enforcement-%s.json" % label)
            enforcement_path.write_text(json.dumps(proof), encoding="utf-8")
            return bridge(
                str(bundle_dir / "task.json"),
                str(bundle_dir),
                str(execution_path),
                "",
                str(enforcement_path),
                handoff_sha256,
                str(result_path),
                str(events_path),
            )

        null_post = bridge_with_observations("null-post", None, False)
        check(
            "bridge: null post-action signed-input observation rejects successful controller",
            null_post["status"] == "failed" and null_post["error"]["code"] == "sandbox.violation",
        )
        unequal_post = bridge_with_observations("unequal-post", "c" * 64, True)
        check(
            "bridge: unequal post-action signed-input observation rejects successful controller",
            unequal_post["status"] == "failed" and unequal_post["error"]["code"] == "sandbox.violation",
        )
        equal_post = bridge_with_observations("equal-post", "b" * 64, True)
        check(
            "bridge: equal non-null signed-input observation allows trusted success",
            equal_post["status"] == "succeeded",
        )

    if FAILURES:
        raise SystemExit("%d Claude bridge checks failed" % len(FAILURES))
    print("\nall Claude bridge tests passed")


if __name__ == "__main__":
    main()
