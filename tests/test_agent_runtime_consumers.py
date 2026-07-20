#!/usr/bin/env python3
"""Compatibility fixtures for every existing trusted output consumer."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path("scripts").resolve()))

import apply_decision
import render_card
from agent_runtime.consumer import export_value, result_text
from agent_runtime_testlib import default_final, run_fake

FAILURES = []


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def main():
    actions = [
        "triage.issue.local",
        "triage.issue.search",
        "triage.pr.local",
        "triage.pr.search",
        "triage.schema-repair",
        "deep-review.local",
        "deep-review.search",
        "nl-decision.local",
        "nl-decision.search",
        "nl-decision.schema-repair",
    ]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for action in actions:
            run_root = root / action.replace(".", "-")
            result = run_fake(run_root, action)
            path = run_root / "bundle" / "result.json"
            check("parity %s: normalized execution succeeds" % action, result["status"] == "succeeded")
            check("parity %s: final delivery readable" % action, bool(result_text(str(path), require_success=True)))
            check("parity %s: no fallback used" % action, result["selection"]["fallbackUsed"] is False)

        triage_path = root / "triage-issue-local" / "bundle" / "result.json"
        text = render_card.extract_claude_result(str(triage_path))
        parsed = render_card.parse_triage_json(text)
        check("triage consumer: AgentResult feeds existing tolerant parser", parsed is not None)
        check("triage consumer: existing normalization preserved", render_card.normalize_triage(parsed)["summary"] == "A bounded fixture request.")
        target = root / "triage-issue-local" / "target.txt"
        check("triage consumer: existing evidence anchor preserved", render_card._triage_evidence_verified(parsed, str(target)))

        pr_final = default_final("triage.pr.local")
        pr_final["recommended_action"] = "merge"
        pr_final["automerge"] = {
            "behavior_class": "A",
            "aligns_with_vision": True,
            "changes_existing_or_default_behavior": False,
            "optin_default_off": False,
            "recommend_merge": True,
        }
        pr_root = root / "pr-automerge"
        result = run_fake(pr_root, "triage.pr.local", final=pr_final)
        parsed = render_card.parse_triage_json(render_card.extract_claude_result(str(pr_root / "bundle" / "result.json")))
        normalized = render_card.normalize_triage(parsed)
        check("automerge consumer: normalized verdict preserved", normalized["automerge_verdict"]["behavior_class"] == "A")
        check("automerge consumer: deterministic verdict fields preserved", normalized["automerge_verdict"]["recommend_merge"] is True)

        deep_path = root / "deep-review-local" / "bundle" / "result.json"
        check("deep-review consumer: typed text unwraps to Markdown", result_text(str(deep_path), require_success=True).startswith("HOLD"))

        nl_path = root / "nl-decision-local" / "bundle" / "result.json"
        decision_file = root / "decision.json"
        check("NL consumer: final object exports atomically", export_value(str(nl_path), str(decision_file)))
        proposal = apply_decision._load_llm_result(str(decision_file))
        state = {"repo": "target", "number": 7, "head_sha": "abc1234"}
        routed = apply_decision.route_decision(
            proposal,
            "pr-review",
            state,
            owner="owner",
            owner_command="Show me the status.",
            authority_comment_id="1",
        )
        check("NL consumer: answer stays non-acting", routed["mode"] == "answer" and not routed["decision"])
        forged = apply_decision.route_decision({"mode": "action", "action": "approve-ci"}, "pr-review", state, owner="owner")
        check("NL consumer: existing action allowlist rejects forged action", forged["mode"] == "clarify" and not forged["decision"])

        invalid_root = root / "invalid-delivered"
        invalid = {"summary": "delivered but structurally invalid"}
        result = run_fake(invalid_root, "triage.issue.local", final=invalid)
        delivered_text = render_card.extract_claude_result(str(invalid_root / "bundle" / "result.json"))
        plan = render_card.plan_triage_repair(delivered_text, "issue-triage")
        check("repair consumer: failed runtime result still delivers candidate", result["error"]["code"] == "output.schema_invalid" and bool(delivered_text))
        check("repair consumer: schema miss still triggers exactly one repair task", plan["repair_needed"] is True)

    if FAILURES:
        raise SystemExit("%d agent runtime consumer checks failed" % len(FAILURES))
    print("\nall agent runtime consumer tests passed")


if __name__ == "__main__":
    main()
