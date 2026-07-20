#!/usr/bin/env python3
"""Real credential-free public broker and generic VISION acceptance tests.

The default mode exercises pure fail-closed policy and a real isolated broker
against public Internet sources. ``--production-e2e`` additionally creates a
local public-address HTTPS adversary and runs the exact Bubblewrap production
broker. No fetch path, receipt, or authority guard is mocked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT / "scripts"))

from agent_runtime.adapters.base import AdapterDescriptor, AdapterProbe  # noqa: E402
from agent_runtime.adapters.claude import ClaudeCliAdapter  # noqa: E402
from agent_runtime.brokers import PublicReadBrokerProcess  # noqa: E402
from agent_runtime.config import resolve_selection  # noqa: E402
from agent_runtime.contract import file_sha256  # noqa: E402
from agent_runtime.task_builder import build_task  # noqa: E402
from agent_runtime.tools import CanonicalTools  # noqa: E402
from agent_runtime.vision_policy import (  # noqa: E402
    VisionPolicyError,
    derive_evidence_plan,
    project_advisory_review,
)
import apply_decision  # noqa: E402
import auto_merge  # noqa: E402
import render_card  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "public_read"
EXECUTION_ID = "public-read-production-e2e"
TASK_SHA256 = hashlib.sha256(b"public-read-production-e2e").hexdigest()
PUBLIC_ADDRESS = "11.23.45.67"
PUBLIC_HOST = "public-evidence.test"


class Failure(AssertionError):
    pass


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        raise Failure(name)


def tool_client(socket_path):
    names = [
        "public.fetch",
        "public.search",
        "public.git_snapshot",
        "public.artifact",
    ]
    return CanonicalTools(
        str(ROOT),
        names,
        {name: 2 * 1024 * 1024 for name in names},
        public_socket=socket_path,
        execution_id=EXECUTION_ID,
        task_sha256=TASK_SHA256,
    )


def mcp_fetch(socket_path, url, accept_kind):
    """Call the same typed stdio MCP surface the production model receives."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        plan = root / "plan.json"
        plan.write_text(
            json.dumps(
                {
                    "executionId": EXECUTION_ID,
                    "taskSha256": TASK_SHA256,
                    "tools": {
                        "tools": [
                            {"name": "public.fetch", "maxResultBytes": 2 * 1024 * 1024}
                        ]
                    },
                    "limits": {"maxToolCalls": 2},
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        environment = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONPATH": str(ROOT),
            "HOME": str(root / "home"),
            "TMPDIR": str(root),
            "TZ": "UTC",
            "LC_ALL": "C.UTF-8",
            "WHEELHOUSE_WORK_ROOT": str(ROOT),
            "WHEELHOUSE_PUBLIC_SOCKET": socket_path,
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "agent_runtime.mcp_bridge", "--plan", str(plan)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=environment,
        )

        def request(value):
            process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
            process.stdin.flush()
            return json.loads(process.stdout.readline())

        try:
            request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"},
                }
            )
            inventory = request(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            )
            response = request(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "public_fetch",
                        "arguments": {"url": url, "accept_kind": accept_kind},
                    },
                }
            )
            text = response["result"]["content"][0]["text"]
            return json.loads(text), inventory["result"]["tools"]
        finally:
            process.terminate()
            process.wait(timeout=5)


def receipt(result):
    value = result.get("receipt")
    check("evidence: immutable receipt is returned", isinstance(value, dict))
    return value


def unavailable(result, reason_code):
    row = receipt(result)
    check(
        "ssrf: %s is rejected" % reason_code,
        row.get("status") == "unavailable" and row.get("reason_code") == reason_code,
    )


def vision_review(vision_path, result_rows, citations, receipt_dir):
    with tempfile.TemporaryDirectory() as directory:
        bundle = Path(directory)
        copied = bundle / "VISION.md"
        shutil.copyfile(vision_path, copied)
        plan = derive_evidence_plan(copied.read_text(encoding="utf-8"))
        task = {
            "metadata": {"executionId": EXECUTION_ID},
            "spec": {
                "inputs": [
                    {
                        "id": "vision",
                        "artifact": "VISION.md",
                        "sha256": file_sha256(copied),
                    }
                ]
            },
        }
        raw = {
            "result_kind": "AdvisoryReview",
            "plan_sha256": plan["plan_sha256"],
            "verdict": "positive",
            "summary": "The target-owned VISION requirements were reviewed.",
            "obligation_results": result_rows(plan),
            "citations": citations,
            "limitations": [],
            "requested_evidence": [],
        }
        return plan, project_advisory_review(
            raw,
            task=task,
            bundle=bundle,
            receipt_dir=receipt_dir,
            task_sha256=TASK_SHA256,
        )


def test_generic_vision(receipt_dir, manifest_result=None):
    axi_path = FIXTURES / "axi-vision-pr-106.md"
    unrelated_path = FIXTURES / "reproducible-data-vision.md"
    axi_plan = derive_evidence_plan(axi_path.read_text(encoding="utf-8"))
    unrelated_plan = derive_evidence_plan(unrelated_path.read_text(encoding="utf-8"))
    runtime_policy = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "agent_runtime/public_read.py",
            "agent_runtime/public_read_broker.py",
            "agent_runtime/mcp_bridge.py",
            "agent_runtime/vision_policy.py",
        )
    )
    check(
        "VISION: runtime contains no fixture-specific AXI or catalog policy",
        re.search(r"(?i)\b(?:axi|catalog)\b", runtime_policy) is None,
    )
    axi_operations = {row["operation"] for row in axi_plan["obligations"]}
    unrelated_operations = {row["operation"] for row in unrelated_plan["obligations"]}
    check(
        "VISION A: landed PR 106 source and released-package duties are derived",
        {"public.git_snapshot", "public.artifact", "exercise.run"}.issubset(
            axi_operations
        ),
    )
    check(
        "VISION B: unrelated manifest duty is derived without repository policy",
        unrelated_operations == {"public.fetch"}
        and all(
            row["operation"] == "policy.assess"
            or "stable key" in row["requirement"].casefold()
            or "manifest" in row["requirement"].casefold()
            for row in unrelated_plan["obligations"]
        ),
    )
    check(
        "VISION: every non-heading unit is mapped by the structural coverage record",
        axi_plan["coverage_audit"]["complete"] is True
        and unrelated_plan["coverage_audit"]["complete"] is True
        and set(unrelated_plan["coverage_audit"]["required_unit_ids"])
        == set(unrelated_plan["coverage_audit"]["mapped_unit_ids"]),
    )
    check(
        "VISION: different target policies produce different plans",
        axi_plan["plan_sha256"] != unrelated_plan["plan_sha256"]
        and axi_operations != unrelated_operations,
    )
    try:
        derive_evidence_plan("")
    except VisionPolicyError:
        missing_failed = True
    else:
        missing_failed = False
    check("VISION: missing policy fails closed", missing_failed)

    _, axi_review = vision_review(
        axi_path,
        lambda plan: [
            {
                "obligation_id": row["obligation_id"],
                "assessment": "pass",
                "rationale": "Required evidence is unavailable in this bounded run.",
                "citation_ids": [],
            }
            for row in plan["obligations"]
        ],
        [],
        receipt_dir,
    )
    check(
        "VISION A: unavailable required checks can never produce a positive verdict",
        axi_review["verdict"] == "inconclusive"
        and axi_review["auto_merge_eligible"] is False,
    )
    if manifest_result is None:
        return axi_review, None
    manifest_receipt = receipt(manifest_result)
    citation = {
        "evidence_id": manifest_receipt["evidence_id"],
        "location": manifest_receipt["final_url"],
        "claim": "The fetched rows are ordered by the declared stable key.",
    }
    _, unrelated_review = vision_review(
        unrelated_path,
        lambda plan: [
            {
                "obligation_id": row["obligation_id"],
                "assessment": "pass",
                "rationale": "The bounded manifest has a stable key and sorted rows.",
                "citation_ids": [manifest_receipt["evidence_id"]],
            }
            for row in plan["obligations"]
        ],
        [citation],
        receipt_dir,
    )
    check(
        "VISION B: the same generic projector applies its unrelated requirement",
        unrelated_review["verdict"] == "positive"
        and unrelated_review["policy_coverage_complete"] is True,
    )
    return axi_review, unrelated_review


def test_public_task_contract():
    triage_workflow = (ROOT / ".github/workflows/triage.yml").read_text(
        encoding="utf-8"
    )
    decision_workflow = (
        ROOT / ".github/workflows/decision-handler.yml"
    ).read_text(encoding="utf-8")
    check(
        "workflow: public broker lane is selected from generic plan operations",
        'row["operation"] != "policy.assess"' in triage_workflow
        and 'PUBLIC_REVIEW_REQUIRED="$(python - vision.md' in triage_workflow
        and 'steps.prepare.outputs.public_review_required' in triage_workflow
        and 'action="advisory-review.public"' in triage_workflow,
    )
    check(
        "workflow: action authority re-reads the exact current owner comment",
        decision_workflow.count(
            '"repos/$CARD_REPO/issues/comments/$TRIGGER_COMMENT_ID"'
        )
        == 2
        and decision_workflow.count('(.body // "") == $body') == 2
        and decision_workflow.count("EXPECTED_VISION_SHA") >= 4,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        prompt = root / "prompt.txt"
        target = root / "target.txt"
        prompt.write_text("Produce an authority-free AdvisoryReview.\n", encoding="utf-8")
        target.write_text("untrusted target data\n", encoding="utf-8")
        bundle = root / "bundle"
        task = build_task(
            action="advisory-review.public",
            selection=resolve_selection("advisory-review.public"),
            prompt_path=str(prompt),
            bundle_dir=str(bundle),
            output_path=str(bundle / "task.json"),
            owner="owner",
            repo="target",
            number=1,
            target_kind="pr-review",
            revision="a" * 40,
            wheelhouse_revision="b" * 40,
            event_key="c" * 64,
            target_file=str(target),
            vision_file=str(FIXTURES / "reproducible-data-vision.md"),
        )
        names = [row["name"] for row in task["spec"]["tools"]["tools"]]
        check(
            "task: production public advisory has typed tools and no acting schema",
            names
            == [
                "fs.read",
                "fs.grep",
                "fs.glob",
                "public.search",
                "public.fetch",
                "public.git_snapshot",
                "public.artifact",
            ]
            and task["spec"]["output"]["schemaId"]
            == "wheelhouse/advisory-review/v1"
            and task["spec"]["output"]["evidencePolicy"]
            == "public-evidence/v1",
        )
        plan_input = next(
            row for row in task["spec"]["inputs"] if row["id"] == "evidence-plan"
        )
        plan = json.loads((bundle / plan_input["artifact"]).read_text(encoding="utf-8"))
        check(
            "task: target VISION produces a content-bound exhaustive evidence plan",
            plan["coverage_complete"] is True
            and plan["vision_sha256"]
            == derive_evidence_plan(
                (FIXTURES / "reproducible-data-vision.md").read_text(
                    encoding="utf-8"
                )
            )["vision_sha256"],
        )
        probe = AdapterProbe(
            descriptor=AdapterDescriptor(
                {"harnessVersion": "test", "protocol": "claude-cli-json"}
            ),
            binary_path="/nonexistent/claude",
            auth_source="/nonexistent/oauth",
            supplemental={"schemaText": "{}", "schemaSha256": "d" * 64},
        )
        compiled = ClaudeCliAdapter().compile(task, {}, probe)
        argv = compiled["claude"]["argv"]
        allowed_index = argv.index("--allowedTools") + 1
        check(
            "task: Claude loads no ambient settings and only the exact MCP tools",
            "--safe-mode" not in argv
            and argv[argv.index("--setting-sources") + 1] == ""
            and "--strict-mcp-config" in argv
            and argv[argv.index("--tools") + 1] == ""
            and set(argv[allowed_index].split(","))
            == {
                "mcp__wheelhouse__fs_read",
                "mcp__wheelhouse__fs_grep",
                "mcp__wheelhouse__fs_glob",
                "mcp__wheelhouse__public_search",
                "mcp__wheelhouse__public_fetch",
                "mcp__wheelhouse__public_git_snapshot",
                "mcp__wheelhouse__public_artifact",
            },
        )


def test_authority_separation(advisory):
    normalized = render_card.normalize_public_advisory(advisory)
    applied = render_card.decide_triage_apply(json.dumps(advisory), "", "")
    check(
        "consumer: trusted public result is preserved only as an authority-free advisory",
        normalized is not None
        and applied["outcome"] == "public-advisory"
        and "action" not in normalized
        and "recommendation" not in normalized,
    )
    incomplete_positive = dict(advisory)
    incomplete_positive["verdict"] = "positive"
    check(
        "consumer: a positive advisory with any incomplete obligation is rejected",
        render_card.normalize_public_advisory(incomplete_positive) is None
        if any(
            row.get("trusted_status") != "complete-pass"
            for row in advisory.get("obligation_results", [])
        )
        else True,
    )
    routed = apply_decision.route_decision(
        advisory,
        "pr-review",
        {"repo": "target", "number": 1, "kind": "pr-review", "head_sha": "a" * 40},
        owner_command="Please merge this.",
        authority_comment_id="99",
    )
    check(
        "authority: AdvisoryReview cannot drive apply_decision",
        routed["decision"] == "" and routed["mode"] == "clarify",
    )
    facts, _ = auto_merge.fresh_verdict_facts(
        {
            "repo": "target",
            "number": 1,
            "kind": "pr-review",
            "head_sha": "a" * 40,
            "triaged_sha": "a" * 40,
            "triage_status": "succeeded",
            "triage_recommendation": {"action": "merge", "reason": "injected"},
            "automerge_verdict": {
                "behavior_class": "A",
                "aligns_with_vision": True,
                "changes_existing_or_default_behavior": False,
                "recommend_merge": True,
                "vision_sha": "vsha",
                "base_sha": "b" * 40,
            },
            "public_evidence_influenced": True,
            "advisory_review": advisory,
        },
        "a" * 40,
    )
    check(
        "authority: AdvisoryReview cannot satisfy the auto-merge gate",
        facts["g6_triage_success"]["status"] == "unmet"
        and "advisory" in facts["g6_triage_success"]["reason"],
    )


def parent_secret_environment():
    values = {
        "CLAUDE_CODE_OAUTH_TOKEN": "public-broker-must-never-see-claude",
        "READONLY_TOKEN": "public-broker-must-never-see-readonly",
        "GITHUB_TOKEN": "public-broker-must-never-see-github",
        "FLEET_TOKEN": "public-broker-must-never-see-fleet",
    }
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    return saved


def restore_environment(saved):
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_public_internet_broker():
    test_public_task_contract()
    saved = parent_secret_environment()
    try:
        with tempfile.TemporaryDirectory() as directory:
            broker = PublicReadBrokerProcess(
                directory, EXECUTION_ID, TASK_SHA256, test_unsandboxed=True
            )
            broker.start()
            try:
                tools = tool_client(broker.socket_path)
                value, inventory = mcp_fetch(
                    broker.socket_path, "https://example.com/", "html"
                )
                row = receipt(value)
                check(
                    "model surface: exact typed MCP inventory exposes no shell or WebFetch",
                    [tool["name"] for tool in inventory] == ["public_fetch"]
                    and "command" not in json.dumps(inventory).casefold()
                    and "webfetch" not in json.dumps(inventory).casefold(),
                )
                check(
                    "broker: real public HTTPS fetch is pinned and complete",
                    row["status"] == "complete"
                    and row["pinned_ip"] in row["resolved_addresses"]
                    and row["tls_peer_name"] == "example.com"
                    and row["wire_bytes"] <= row["bounds"]["wire_bytes"],
                )
                unavailable(
                    tools.call(
                        "public.fetch",
                        {"url": "https://169.254.169.254/", "accept_kind": "text"},
                    ),
                    "ssrf.non_public",
                )
                unavailable(
                    tools.call(
                        "public.fetch",
                        {"url": "https://224.0.0.1/", "accept_kind": "text"},
                    ),
                    "ssrf.non_public",
                )
                attestation = json.loads(
                    broker.attestation_path.read_text(encoding="utf-8")
                )
                check(
                    "credentials: real broker environment and filesystem are clean",
                    attestation["credential_reachable"] is False
                    and not attestation["forbidden_environment_names"]
                    and not attestation["reachable_forbidden_paths"]
                    and not attestation["home_entries"]
                    and len(attestation["process_tree"]) >= 1,
                )
                advisory, _ = test_generic_vision(broker.receipt_dir)
                test_authority_separation(advisory)
            finally:
                broker.close()
    finally:
        restore_environment(saved)


def append_hosts(line):
    subprocess.run(
        ["sudo", "--non-interactive", "tee", "-a", "/etc/hosts"],
        input=(line + "\n").encode("utf-8"),
        stdout=subprocess.DEVNULL,
        check=True,
    )


def production_setup(directory):
    root = Path(directory)
    certificate = root / "public-evidence.crt"
    key = root / "public-evidence.key"
    hosts_backup = root / "hosts.backup"
    shutil.copyfile("/etc/hosts", hosts_backup)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-subj",
            "/CN=" + PUBLIC_HOST,
            "-addext",
            "subjectAltName=DNS:" + PUBLIC_HOST,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["sudo", "--non-interactive", "ip", "address", "add", PUBLIC_ADDRESS + "/32", "dev", "lo"],
        check=True,
    )
    append_hosts(PUBLIC_ADDRESS + " " + PUBLIC_HOST)
    trust_path = "/usr/local/share/ca-certificates/wheelhouse-public-e2e.crt"
    subprocess.run(
        ["sudo", "--non-interactive", "cp", str(certificate), trust_path], check=True
    )
    subprocess.run(
        ["sudo", "--non-interactive", "update-ca-certificates"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    server = subprocess.Popen(
        [
            "sudo",
            "--non-interactive",
            "env",
            "-i",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "python3",
            str(FIXTURES / "adversarial_https.py"),
            "--address",
            PUBLIC_ADDRESS,
            "--certificate",
            str(certificate),
            "--key",
            str(key),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(0.5)
    if server.poll() is not None:
        raise Failure("local HTTPS adversary failed to start")
    return server, hosts_backup, trust_path


def production_cleanup(server, hosts_backup, trust_path):
    if server is not None:
        subprocess.run(
            [
                "sudo",
                "--non-interactive",
                "kill",
                "-TERM",
                "--",
                "-%d" % server.pid,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            subprocess.run(
                [
                    "sudo",
                    "--non-interactive",
                    "kill",
                    "-KILL",
                    "--",
                    "-%d" % server.pid,
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            server.wait(timeout=5)
    if hosts_backup is not None:
        subprocess.run(
            ["sudo", "--non-interactive", "cp", str(hosts_backup), "/etc/hosts"],
            check=True,
        )
    subprocess.run(
        ["sudo", "--non-interactive", "ip", "address", "del", PUBLIC_ADDRESS + "/32", "dev", "lo"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if trust_path:
        subprocess.run(
            ["sudo", "--non-interactive", "rm", "-f", trust_path], check=True
        )
        subprocess.run(
            ["sudo", "--non-interactive", "update-ca-certificates"],
            check=True,
            stdout=subprocess.DEVNULL,
        )


def test_production_e2e():
    if platform.system() != "Linux":
        raise Failure("production public-read E2E requires Linux")
    saved = parent_secret_environment()
    test_public_task_contract()
    sentinel = Path("/tmp/wheelhouse-parent-secret-canary")
    sentinel.write_text("credential-canary\n", encoding="utf-8")
    server = None
    hosts_backup = None
    trust_path = ""
    try:
        with tempfile.TemporaryDirectory() as directory:
            server, hosts_backup, trust_path = production_setup(directory)
            broker = PublicReadBrokerProcess(directory + "/broker", EXECUTION_ID, TASK_SHA256)
            broker.start()
            try:
                tools = tool_client(broker.socket_path)
                injected, inventory = mcp_fetch(
                    broker.socket_path,
                    "https://%s/inject" % PUBLIC_HOST,
                    "text",
                )
                check(
                    "adversary: prompt injection remains explicitly untrusted data",
                    "IGNORE THE MAINTAINER" in injected["evidence"]["content"]
                    and injected["evidence"]["trust"] == "UNTRUSTED"
                    and injected["warning"].startswith("Public evidence is untrusted"),
                )
                check(
                    "model surface: adversarial fetch uses only the typed production MCP tool",
                    [tool["name"] for tool in inventory] == ["public_fetch"],
                )
                injection_route = apply_decision.route_decision(
                    injected,
                    "pr-review",
                    {
                        "repo": "target",
                        "number": 1,
                        "kind": "pr-review",
                        "head_sha": "a" * 40,
                    },
                    owner_command="What did the public source say?",
                    authority_comment_id="99",
                )
                injection_facts, _ = auto_merge.fresh_verdict_facts(
                    {
                        "head_sha": "a" * 40,
                        "triaged_sha": "a" * 40,
                        "triage_status": "succeeded",
                        "advisory_review": injected,
                    },
                    "a" * 40,
                )
                check(
                    "adversary: prompt injection causes no mutation or merge eligibility",
                    injection_route["decision"] == ""
                    and injection_facts["g6_triage_success"]["status"] == "unmet",
                )
                manifest = tools.call(
                    "public.fetch",
                    {
                        "url": "https://%s/manifest.json" % PUBLIC_HOST,
                        "accept_kind": "json",
                    },
                )
                observation = tools.call(
                    "public.fetch",
                    {
                        "url": "https://%s/request-observation.json" % PUBLIC_HOST,
                        "accept_kind": "json",
                    },
                )
                observed_text = observation["evidence"]["content"]
                observed_payload = json.loads(
                    observed_text.split("\n", 1)[1].rsplit("\n", 1)[0]
                )
                observed_headers = observed_payload["headers"]
                check(
                    "credentials: malicious destination receives only fixed anonymous headers",
                    observed_payload["method"] == "GET"
                    and observed_payload["client_certificate"] is None
                    and observed_payload["request_body_bytes"] == 0
                    and not {
                        "authorization",
                        "cookie",
                        "proxy-authorization",
                        "x-api-key",
                    }.intersection(observed_headers)
                    and not any(
                        marker in observed_text
                        for marker in (
                            "public-broker-must-never-see-claude",
                            "public-broker-must-never-see-readonly",
                            "public-broker-must-never-see-github",
                            "public-broker-must-never-see-fleet",
                        )
                    ),
                )
                unavailable(
                    tools.call(
                        "public.fetch",
                        {"url": "https://127.0.0.1/", "accept_kind": "text"},
                    ),
                    "ssrf.non_public",
                )
                unavailable(
                    tools.call(
                        "public.fetch",
                        {"url": "https://10.0.0.1/", "accept_kind": "text"},
                    ),
                    "ssrf.non_public",
                )
                unavailable(
                    tools.call(
                        "public.fetch",
                        {"url": "https://169.254.169.254/", "accept_kind": "text"},
                    ),
                    "ssrf.non_public",
                )
                unavailable(
                    tools.call(
                        "public.fetch",
                        {"url": "https://224.0.0.1/", "accept_kind": "text"},
                    ),
                    "ssrf.non_public",
                )
                unavailable(
                    tools.call(
                        "public.fetch",
                        {"url": "http://example.com/", "accept_kind": "text"},
                    ),
                    "url.scheme",
                )
                unavailable(
                    tools.call(
                        "public.fetch",
                        {
                            "url": "https://%s/?token=credential-canary"
                            % PUBLIC_HOST,
                            "accept_kind": "text",
                        },
                    ),
                    "url.credential_query",
                )
                unavailable(
                    tools.call(
                        "public.fetch",
                        {
                            "url": "https://%s/redirect-private" % PUBLIC_HOST,
                            "accept_kind": "text",
                        },
                    ),
                    "ssrf.non_public",
                )
                unavailable(
                    tools.call(
                        "public.fetch",
                        {
                            "url": "https://%s/redirect-http" % PUBLIC_HOST,
                            "accept_kind": "text",
                        },
                    ),
                    "url.scheme",
                )
                attestation = json.loads(
                    broker.attestation_path.read_text(encoding="utf-8")
                )
                check(
                    "credentials: Bubblewrap process tree, environment, and mounts prove isolation",
                    attestation["isolation_mode"] == "bubblewrap"
                    and attestation["credential_reachable"] is False
                    and not attestation["forbidden_environment_names"]
                    and not attestation["reachable_forbidden_paths"]
                    and not attestation["home_entries"]
                    and len(attestation["process_tree"]) >= 1
                    and "/home/runner/work" not in attestation["mount_points"],
                )
                check(
                    "credentials: parent secret names are absent from the broker dump",
                    not {
                        "CLAUDE_CODE_OAUTH_TOKEN",
                        "READONLY_TOKEN",
                        "GITHUB_TOKEN",
                        "FLEET_TOKEN",
                    }.intersection(attestation["environment_names"]),
                )
                _axi, unrelated = test_generic_vision(
                    broker.receipt_dir, manifest_result=manifest
                )
                test_authority_separation(unrelated)

                snapshot = tools.call(
                    "public.git_snapshot",
                    {
                        "url": "https://github.com/kunchenguid/axi.git",
                        "ref": "cddff0cc1b01adef5f3f6d81360e49e76c27985b",
                    },
                )
                snapshot_receipt = receipt(snapshot)
                check(
                    "git: real PR 106 snapshot is exact, anonymous, depth-1 data",
                    snapshot_receipt["status"] == "complete"
                    and snapshot_receipt["commit"]
                    == "cddff0cc1b01adef5f3f6d81360e49e76c27985b"
                    and snapshot_receipt["depth"] == 1
                    and snapshot_receipt["history_commits"] == 1
                    and snapshot_receipt["bounds"]["history_depth"] == 1
                    and snapshot_receipt["wire_bytes"]
                    <= snapshot_receipt["bounds"]["wire_bytes"],
                )

                unavailable(
                    tools.call(
                        "public.fetch",
                        {
                            "url": "https://%s/rebind" % PUBLIC_HOST,
                            "accept_kind": "text",
                        },
                    ),
                    "ssrf.non_public",
                )
            finally:
                broker.close()
    finally:
        production_cleanup(server, hosts_backup, trust_path)
        sentinel.unlink(missing_ok=True)
        restore_environment(saved)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--production-e2e", action="store_true")
    args = parser.parse_args()
    if args.production_e2e:
        test_production_e2e()
    else:
        test_public_internet_broker()
    print("\nall public-read broker acceptance tests passed")


if __name__ == "__main__":
    main()
