import json
import unittest
from pathlib import Path

from scripts import render_card


HEAD = "a" * 40
BASE = "b" * 40
VISION = "c" * 40


def advisory(**updates):
    value = {
        "result_kind": "AdvisoryReview",
        "public_evidence_influenced": True,
        "acting_authority": False,
        "trusted_projection": True,
        "policy_coverage_complete": True,
        "projection_complete": True,
        "target_head_sha": HEAD,
        "target_base_sha": BASE,
        "vision_blob_sha": VISION,
        "plan_sha256": "d" * 64,
        "verdict": "positive",
        "summary": "Complete current advisory.",
        "obligation_results": [{
            "obligation_id": "O001",
            "trusted_status": "complete-pass",
            "rationale": "Current trusted evidence passed.",
        }],
        "limitations": [],
        "requested_evidence": [],
        "auto_merge_eligible": False,
        "eligibility_facts": None,
    }
    value.update(updates)
    return value


def body():
    state = {
        "repo": "target",
        "number": 1,
        "kind": "pr-review",
        "head_sha": HEAD,
        "options": ["merge", "close", "hold"],
    }
    return "Card\n\n<!-- wheelhouse-state: %s -->" % json.dumps(
        state, separators=(",", ":")
    )


class PublicAdvisoryFreshnessTests(unittest.TestCase):
    def test_incomplete_projection_exposes_no_model_claims(self):
        value = advisory(
            projection_complete=False,
            policy_coverage_complete=False,
            summary="INJECTED SUMMARY",
            obligation_results=[{
                "obligation_id": "O001",
                "trusted_status": "unavailable",
                "rationale": "INJECTED RATIONALE",
            }],
            limitations=["INJECTED LIMITATION"],
            requested_evidence=["INJECTED REQUEST"],
            auto_merge_eligible=True,
        )
        normalized = render_card.normalize_public_advisory(value)
        self.assertIsNotNone(normalized)
        self.assertFalse(normalized["projection_complete"])
        self.assertFalse(normalized["auto_merge_eligible"])
        self.assertEqual(normalized["obligations"], [])
        self.assertNotIn("INJECTED", json.dumps(normalized))
        projected = render_card.body_with_public_advisory(
            body(), HEAD, value, vision_sha=VISION, base_sha=BASE
        )
        self.assertNotIn("INJECTED", projected)
        self.assertNotIn("VISION plan", projected)

    def test_complete_negative_remains_informative_and_nonacting(self):
        value = advisory(
            verdict="negative",
            summary="Complete negative evidence result.",
            obligation_results=[{
                "obligation_id": "O001",
                "trusted_status": "complete-fail",
                "rationale": "A current trusted check failed.",
            }],
        )
        normalized = render_card.normalize_public_advisory(value)
        self.assertEqual(normalized["summary"], value["summary"])
        self.assertFalse(normalized["auto_merge_eligible"])

    def test_head_base_vision_and_lookup_failures_publish_nothing(self):
        current = body()
        for field in ("target_head_sha", "target_base_sha", "vision_blob_sha"):
            stale = advisory(**{field: "e" * 40})
            with self.subTest(field=field):
                self.assertEqual(
                    render_card.body_with_public_advisory(
                        current, HEAD, stale, vision_sha=VISION, base_sha=BASE
                    ),
                    current,
                )
        self.assertEqual(
            render_card.body_with_public_advisory(
                current, HEAD, advisory(), vision_sha="", base_sha=BASE
            ),
            current,
        )

    def test_workflow_rereads_all_three_bindings(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/triage.yml"
        ).read_text(encoding="utf-8")
        for fragment in (
            "EXPECTED_REVISION: ${{ needs.triage.outputs.revision }}",
            "EXPECTED_BASE_SHA: ${{ needs.triage.outputs.base_sha }}",
            "EXPECTED_VISION_SHA: ${{ needs.triage.outputs.vision_sha }}",
            'observed_base="$(jq -r',
            'observed_vision="$(gh api --method GET',
            'observed_base" != "$EXPECTED_BASE_SHA',
            'observed_vision" != "$EXPECTED_VISION_SHA',
        ):
            self.assertIn(fragment, workflow)


if __name__ == "__main__":
    unittest.main()
