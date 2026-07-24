#!/usr/bin/env python3
"""Contract, hashing, schema, artifact, and output-delivery tests."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.contract import ContractError, canonical_sha256, load_schema, validate_contract, validate_schema
from agent_runtime.events import EventError, EventWriter, read_events
from agent_runtime.supervisor import RuntimeFailure, _verify_artifacts
from agent_runtime.task_builder import ACTION_SCHEMAS, build_task
from agent_runtime_testlib import WHEELHOUSE_REVISION, codex_selection, make_task


def _git_commit_repo(repository: Path) -> str:
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "wheelhouse-test@example.com"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Wheelhouse Test"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().lower()
    # task_builder requires detached HEAD so git.detached remains a truthful const.
    subprocess.run(["git", "checkout", "--detach", sha], cwd=repository, check=True, capture_output=True)
    return sha

FAILURES = []


def check(name, condition):
    if condition:
        print("ok  ", name)
    else:
        print("FAIL", name)
        FAILURES.append(name)


def rejects(document, text=""):
    try:
        validate_contract(document)
    except ContractError as error:
        return text in str(error)
    return False


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        task, bundle, _ = make_task(root, "triage.issue.local")
        validate_contract(task, "AgentTask")
        check("task: valid v1 request", True)
        check("task: canonical hash is stable", canonical_sha256(task) == canonical_sha256(json.loads(json.dumps(task))))

        changed = copy.deepcopy(task)
        changed["unknown"] = True
        check("task: unknown top-level fields rejected", rejects(changed, "unknown field"))
        changed = copy.deepcopy(task)
        del changed["metadata"]["target"]
        check("task: missing required fields rejected", rejects(changed, "missing required"))
        changed = copy.deepcopy(task)
        changed["apiVersion"] = "wheelhouse.agent-runtime/v2"
        check("task: wrong major rejected", rejects(changed, "unsupported"))
        changed = copy.deepcopy(task)
        changed["spec"]["inputs"][0]["logicalPath"] = "../../host-home"
        check("task: traversal logical paths rejected", rejects(changed, "invalid format"))
        changed = copy.deepcopy(task)
        changed["spec"]["selection"]["fallback"]["mode"] = "automatic"
        check("task: fallback substitution rejected by schema", rejects(changed, "contract constant"))
        changed = copy.deepcopy(task)
        changed["spec"]["selection"]["candidates"][0]["allowModelAlias"] = "false"
        check("task: alias flag has a strict type", rejects(changed, "wrong type"))
        changed = copy.deepcopy(task)
        changed["spec"]["limits"]["maxFinalBytes"] = 999999
        check("task: output byte limit bounded", rejects(changed, "maximum"))
        changed = copy.deepcopy(task)
        changed["spec"]["limits"]["maxToolCalls"] = None
        changed["spec"]["limits"]["enforcement"]["maxToolCalls"] = "unavailable"
        validate_contract(changed, "AgentTask")
        check("task: mixed generic limit enforcement validates", True)
        changed["spec"]["limits"]["enforcement"]["maxToolCalls"] = "adapter-enforced"
        check("task: unavailable limit evidence must match null value", rejects(changed, "enforcement availability"))
        check("task: no host source path is serialized", str(root) not in json.dumps(task))
        check("task: every artifact reference is content-addressed", all("artifacts/sha256/" in row["artifact"] for row in task["spec"]["inputs"]))

        repository = root / "repository"
        repository.mkdir()
        (repository / "safe.py").write_text("safe = True\n", encoding="utf-8")
        repo_commit = _git_commit_repo(repository)
        prompt = root / "repo-prompt.txt"
        prompt.write_text("Inspect the bounded repository.\n", encoding="utf-8")
        target = root / "repo-target.txt"
        target.write_text("fixture evidence anchor text for runtime tests\n", encoding="utf-8")
        repo_bundle = root / "repo-bundle"
        repo_task = build_task(
            action="deep-review.local",
            selection=codex_selection(),
            prompt_path=str(prompt),
            bundle_dir=str(repo_bundle),
            output_path=str(repo_bundle / "task.json"),
            owner="owner",
            repo="repo",
            number=1,
            target_kind="pr-review",
            revision=repo_commit,
            wheelhouse_revision=WHEELHOUSE_REVISION,
            event_key="a" * 64,
            target_file=str(target),
            repository_dir=str(repository),
            repository_commit=repo_commit,
        )
        _verify_artifacts(repo_task, repo_bundle)
        check("artifacts: directory tree digest verifies", True)
        repo_input = next(row for row in repo_task["spec"]["inputs"] if row["id"] == "repository")
        provenance_input = next(row for row in repo_task["spec"]["inputs"] if row["id"] == "repository-provenance")
        check(
            "task: repository provenance is separately content-addressed",
            repo_input["git"]["symlinkProvenanceArtifact"] == provenance_input["artifact"]
            and repo_input["git"]["symlinkProvenanceSha256"] == provenance_input["sha256"],
        )
        changed = copy.deepcopy(repo_task)
        next(row for row in changed["spec"]["inputs"] if row["id"] == "repository")["git"]["symlinkProvenanceSha256"] = "0" * 64
        check(
            "task: repository provenance binding mismatch is rejected",
            rejects(changed, "provenance binding"),
        )
        stored_file = repo_bundle / repo_input["artifact"] / "safe.py"
        stored_file.chmod(0o600)
        stored_file.write_text("evil = True\n", encoding="utf-8")
        try:
            _verify_artifacts(repo_task, repo_bundle)
        except RuntimeFailure:
            check("artifacts: same-size directory mutation rejected", True)
        else:
            check("artifacts: same-size directory mutation rejected", False)

        for name in ("triage-issue-v1.schema.json", "triage-pr-v1.schema.json", "deep-review-text-v1.schema.json", "nl-decision-v1.schema.json"):
            schema = json.load(open(ACTION_SCHEMAS / name, encoding="utf-8"))
            check("action schema %s rejects unknown output" % name, schema.get("additionalProperties") is False)

        triage_schema = json.load(open(ACTION_SCHEMAS / "triage-issue-v1.schema.json", encoding="utf-8"))
        valid = {
            "summary": "s",
            "product_implications": "p",
            "recommended_action": "hold",
            "recommended_reason": "r",
            "evidence": "target.txt: quote",
        }
        validate_schema(valid, triage_schema)
        check("action schema: valid triage accepted", True)
        invalid = dict(valid, recommended_action="merge")
        try:
            validate_schema(invalid, triage_schema)
        except ContractError:
            check("action schema: issue action allowlist enforced", True)
        else:
            check("action schema: issue action allowlist enforced", False)

        pr_schema = json.load(
            open(ACTION_SCHEMAS / "triage-pr-v1.schema.json", encoding="utf-8")
        )
        restoration = {
            "corrected_defect": "Daemon restart lost an open monitored run.",
            "corrected_defect_evidence": {
                "source": "target.txt",
                "quote": "Daemon restart lost an open monitored run.",
            },
            "intended_behavior_restored": (
                "An open monitored run remains recoverable."
            ),
            "intended_behavior_restored_evidence": {
                "source": "target-src/lib/recovery.py",
                "quote": "An open monitored run remains recoverable.",
            },
        }
        pr_valid = dict(valid, recommended_action="merge")
        pr_valid["recommendation_basis"] = {
            "kind": "other",
            "observation_id": "sha256:" + "0" * 64,
            "context_id": "sha256:" + "1" * 64,
            "check_names": [],
        }
        pr_valid["automerge"] = {
            "behavior_class": "B",
            "behavior_assertions": [],
            "class_b_restoration": restoration,
            "changes_existing_or_default_behavior": False,
            "optin_default_off": False,
        }
        validate_schema(pr_valid, pr_schema)
        check("action schema: bounded class B restoration evidence accepted", True)
        missing_basis = copy.deepcopy(pr_valid)
        missing_basis.pop("recommendation_basis")
        try:
            validate_schema(missing_basis, pr_schema)
        except ContractError:
            check("action schema: PR recommendation basis required", True)
        else:
            check("action schema: PR recommendation basis required", False)
        for label, bad_restoration in (
            (
                "short",
                dict(restoration, corrected_defect="too short"),
            ),
            (
                "oversized",
                dict(restoration, intended_behavior_restored="x" * 501),
            ),
            (
                "unknown-field",
                dict(restoration, unsupported="value"),
            ),
        ):
            malformed = copy.deepcopy(pr_valid)
            malformed["automerge"]["class_b_restoration"] = bad_restoration
            try:
                validate_schema(malformed, pr_schema)
            except ContractError:
                check(
                    "action schema: class B restoration %s rejected" % label,
                    True,
                )
            else:
                check(
                    "action schema: class B restoration %s rejected" % label,
                    False,
                )

        task_schema = load_schema("AgentTask")
        check("schema: draft 2020-12 pinned", task_schema["$schema"].endswith("2020-12/schema"))
        check("schema: exact API version pinned", task_schema["properties"]["apiVersion"]["const"] == "wheelhouse.agent-runtime/v1alpha1")
        result_selection = load_schema("AgentResult")["properties"]["selection"]
        check("schema: observed provider provenance required", "actualProvider" in result_selection["required"])

        event_path = root / "events.ndjson"
        with EventWriter(str(event_path), task["metadata"]["executionId"], 65536) as writer:
            writer.emit("execution.accepted", {})
            writer.emit("execution.completed", {"status": "succeeded"})
        check("events: monotonic stream accepted", len(read_events(str(event_path))) == 2)
        optional_path = root / "optional-events.ndjson"
        with EventWriter(str(optional_path), task["metadata"]["executionId"], 65536) as writer:
            writer.emit("adapter.future.compaction.started", {"opaque": True})
        check("events: namespaced future adapter event retained", read_events(str(optional_path))[0]["type"] == "adapter.future.compaction.started")
        rows = event_path.read_text(encoding="utf-8").splitlines()
        broken = json.loads(rows[1])
        broken["seq"] = 7
        (root / "broken-events.ndjson").write_text(rows[0] + "\n" + json.dumps(broken) + "\n", encoding="utf-8")
        try:
            read_events(str(root / "broken-events.ndjson"))
        except EventError:
            check("events: out-of-order sequence rejected", True)
        else:
            check("events: out-of-order sequence rejected", False)
        try:
            with EventWriter(str(root / "tiny-events.ndjson"), task["metadata"]["executionId"], 100) as writer:
                writer.emit("message.delta", {"payload": "x" * 500})
        except EventError:
            check("events: oversized stream rejected", True)
        else:
            check("events: oversized stream rejected", False)
        (root / "invalid-utf8.ndjson").write_bytes(b"\xff\n")
        try:
            read_events(str(root / "invalid-utf8.ndjson"))
        except EventError:
            check("events: invalid UTF-8 rejected", True)
        else:
            check("events: invalid UTF-8 rejected", False)
        duplicate = root / "duplicate-terminal.ndjson"
        with EventWriter(str(duplicate), task["metadata"]["executionId"], 65536) as writer:
            writer.emit("execution.completed", {"status": "failed"})
            writer.emit("execution.completed", {"status": "failed"})
        try:
            read_events(str(duplicate))
        except EventError:
            check("events: duplicate terminal rejected", True)
        else:
            check("events: duplicate terminal rejected", False)

    if FAILURES:
        raise SystemExit("%d agent runtime contract checks failed" % len(FAILURES))
    print("\nall agent runtime contract tests passed")


if __name__ == "__main__":
    main()
