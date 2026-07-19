#!/usr/bin/env python3
"""Action-specific durable admission and stage evidence tests."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.admission import event_claim_marker, event_key_sha256, normalized_event_identity, stage_line, stage_record
from agent_runtime.contract import ContractError
from scripts import agent_claim
from agent_runtime_testlib import make_task

FAILURES = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def identity(action, revision="abcdef1", event_id=""):
    return normalized_event_identity(
        action=action,
        owner="owner",
        repo="repo",
        number=7,
        card_issue=42,
        revision=revision,
        event_id=event_id,
    )


def main():
    triage = event_key_sha256(identity("triage.pr.local"))
    check("identity: duplicate triage revision is stable", triage == event_key_sha256(identity("triage.pr.local")))
    check("identity: triage action is part of the key", triage != event_key_sha256(identity("triage.pr.search")))
    check("identity: triage revision is part of the key", triage != event_key_sha256(identity("triage.pr.local", "abcdef2")))

    repair = event_key_sha256(identity("triage.schema-repair"))
    check("identity: duplicate repair revision is stable", repair == event_key_sha256(identity("triage.schema-repair")))

    nl_one = event_key_sha256(identity("nl-decision.local", event_id="comment:100"))
    nl_two = event_key_sha256(identity("nl-decision.local", event_id="comment:101"))
    check("identity: duplicate NL comment is stable", nl_one == event_key_sha256(identity("nl-decision.local", event_id="comment:100")))
    check("identity: distinct same-revision NL comments remain distinct", nl_one != nl_two)
    nl_repair = event_key_sha256(
        identity("nl-decision.schema-repair", event_id="comment:100")
    )
    check(
        "identity: NL repair has a distinct same-comment action key",
        nl_repair != nl_one
        and nl_repair
        == event_key_sha256(
            identity("nl-decision.schema-repair", event_id="comment:100")
        ),
    )

    deep_one = event_key_sha256(identity("deep-review.search", event_id="investigate:200"))
    deep_two = event_key_sha256(identity("deep-review.search", event_id="manual:201"))
    check("identity: duplicate deep trigger is stable", deep_one == event_key_sha256(identity("deep-review.search", event_id="investigate:200")))
    check("identity: distinct same-revision deep triggers remain distinct", deep_one != deep_two)

    try:
        identity("nl-decision.local")
    except ContractError:
        check("identity: NL requires exact comment event identity", True)
    else:
        check("identity: NL requires exact comment event identity", False)
    try:
        identity("triage.issue.local", event_id="comment:1")
    except ContractError:
        check("identity: triage cannot be widened by event delivery id", True)
    else:
        check("identity: triage cannot be widened by event delivery id", False)

    for excluded_action in (
        "triage.schema-repair",
        "deep-review.local",
        "nl-decision.local",
    ):
        try:
            agent_claim.supersede_triage_claim(
                action=excluded_action,
                owner="owner",
                repo="repo",
                number=7,
                issue=42,
                revision="abcdef1",
                repo_slug="owner/cards",
            )
        except ContractError:
            check(
                "claim: replay supersede rejects %s scope" % excluded_action,
                True,
            )
        else:
            check(
                "claim: replay supersede rejects %s scope" % excluded_action,
                False,
            )

    record = stage_record(
        action="deep-review.local",
        source_sha="a" * 40,
        event_key=deep_one,
        stage="hydrated",
        status="ok",
        code="handoff.hydrated",
        execution_id="12345678-1234-1234-1234-123456789abc",
        deadline_ms=600_000,
    )
    encoded = stage_line(record)
    check("stages: record is machine parseable and content free", encoded.startswith("wheelhouse-agent-stage {") and "prompt" not in encoded and "comment" not in encoded and record["deadlineMs"] == 600_000)
    try:
        stage_record(action="deep-review.local", source_sha="a" * 40, event_key=deep_one, stage="hydrated", status="ok", code="handoff.hydrated", deadline_ms=0)
    except ContractError:
        check("stages: invalid blanket deadline rejected", True)
    else:
        check("stages: invalid blanket deadline rejected", False)

    with tempfile.TemporaryDirectory() as task_directory:
        task, _, _ = make_task(Path(task_directory), "nl-decision.local", event_key=nl_one)
        check("identity: AgentTask binds the normalized event key", task["metadata"]["idempotencyKey"] == nl_one)

    comments = []
    next_id = 1

    def fake_gh(*args):
        nonlocal next_id
        if "--paginate" in args:
            return [list(comments)]
        if "POST" in args:
            body_arg = next(value for value in args if value.startswith("body="))
            row = {"id": next_id, "body": body_arg[5:], "user": {"login": "github-actions[bot]"}}
            next_id += 1
            comments.append(row)
            return dict(row)
        comment_id = int(args[-1].rsplit("/", 1)[-1])
        return dict(next(row for row in comments if row["id"] == comment_id))

    saved = agent_claim.gh_json
    agent_claim.gh_json = fake_gh
    try:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "output"
            os.environ["GITHUB_OUTPUT"] = str(output_path)
            args = argparse.Namespace(action="nl-decision.local", owner="owner", repo="repo", number=7, issue=42, revision="abcdef1", event_id="comment:100", repo_slug="owner/cards")
            comments.append(
                {
                    "id": 999,
                    "body": event_claim_marker(nl_one),
                    "user": {"login": "untrusted-user"},
                }
            )
            first = agent_claim.claim(args)
            first_outputs = output_path.read_text(encoding="utf-8")
            primary_comment = comments[-1]
            output_path.write_text("", encoding="utf-8")
            second = agent_claim.claim(args)
            second_outputs = output_path.read_text(encoding="utf-8")
            check("claim: untrusted marker cannot block first admission", first == 0 and "admitted=true" in first_outputs and len(comments) == 2)
            check("claim: duplicate event exits before a second trusted claim", second == 0 and "admitted=false" in second_outputs and len(comments) == 2)

            output_path.write_text("", encoding="utf-8")
            args.action = "nl-decision.schema-repair"
            repair = agent_claim.claim(args)
            repair_outputs = output_path.read_text(encoding="utf-8")
            repair_comment = comments[-1]
            check(
                "claim: repair admission has distinct owner-facing text",
                repair == 0
                and "admitted=true" in repair_outputs
                and primary_comment["body"].startswith(
                    "Agent event admitted and is being processed."
                )
                and repair_comment["body"].startswith(
                    "Schema repair admitted and is being processed."
                ),
            )
            check(
                "claim: primary and repair hidden identities remain durable",
                event_claim_marker(nl_one) in primary_comment["body"]
                and event_claim_marker(nl_repair) in repair_comment["body"]
                and nl_one != nl_repair,
            )

            output_path.write_text("", encoding="utf-8")
            duplicate_repair = agent_claim.claim(args)
            check(
                "claim: repair admission remains idempotent",
                duplicate_repair == 0
                and "admitted=false" in output_path.read_text(encoding="utf-8")
                and comments[-1] is repair_comment,
            )

            output_path.write_text("", encoding="utf-8")
            args.action = "nl-decision.local"
            args.event_id = "comment:101"
            agent_claim.claim(args)
            check("claim: distinct same-revision comment gets a distinct claim", "admitted=true" in output_path.read_text(encoding="utf-8") and len(comments) == 4)

            workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/decision-handler.yml").read_text(encoding="utf-8")
            check(
                "claim: repair finalizer targets the repair claim and marker",
                'REPAIR_CLAIM_ID: ${{ needs.nl-repair-prepare.outputs.repair_claim_id }}'
                in workflow
                and 'REPAIR_MARKER: ${{ needs.nl-repair-prepare.outputs.repair_claim_marker }}'
                in workflow
                and "--action nl-decision.schema-repair" in workflow
                and 'issues/comments/$REPAIR_CLAIM_ID' in workflow,
            )
    finally:
        agent_claim.gh_json = saved
        os.environ.pop("GITHUB_OUTPUT", None)

    if FAILURES:
        raise SystemExit("%d Agent Runtime admission checks failed" % len(FAILURES))
    print("\nall Agent Runtime admission tests passed")


if __name__ == "__main__":
    main()
