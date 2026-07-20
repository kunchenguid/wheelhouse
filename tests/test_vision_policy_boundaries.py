import json
import tempfile
import unittest
from pathlib import Path

from agent_runtime.config import resolve_selection
from agent_runtime.contract import canonical_sha256
from agent_runtime.task_builder import build_task
from agent_runtime.vision_policy import (
    AUDIT_VERSION,
    PLAN_VERSION,
    VisionPolicyError,
    validate_policy_artifacts,
    vision_unit_document,
)


class VisionPolicyBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vision = self.root / "VISION.md"
        self.vision.write_text("Published manifests must be verified.\n", encoding="utf-8")
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("Classify every exact policy unit.\n", encoding="utf-8")
        self.head_sha = "a" * 40
        self.base_sha = "c" * 40
        self.blob_sha = "d" * 40
        document = vision_unit_document(
            self.vision.read_text(encoding="utf-8"),
            target_head_sha=self.head_sha,
            target_base_sha=self.base_sha,
            vision_blob_sha=self.blob_sha,
        )
        unit = {
            **document["units"][0],
            "classification": "evidence-obligation",
            "semantic_status": "recognized",
            "normative": True,
            "decision_relevant": True,
            "condition_strength": "required",
            "conditions": ["verification is mandatory"],
        }
        obligation = {
            "obligation_id": "O001",
            "unit_id": unit["unit_id"],
            "operation": "public.fetch",
            "requirement": unit["text"],
            "semantic_status": "recognized",
        }
        self.plan = {
            "version": PLAN_VERSION,
            "target_head_sha": self.head_sha,
            "target_base_sha": self.base_sha,
            "vision_blob_sha": self.blob_sha,
            "vision_sha256": document["vision_sha256"],
            "units": [unit],
            "obligations": [obligation],
        }
        self.audit = {
            "version": AUDIT_VERSION,
            "target_head_sha": self.head_sha,
            "target_base_sha": self.base_sha,
            "vision_blob_sha": self.blob_sha,
            "vision_sha256": document["vision_sha256"],
            "units": [dict(unit)],
            "obligations": [dict(obligation)],
            "complete": True,
            "disagreements": [],
        }

    def tearDown(self):
        self.temporary.cleanup()

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def build(self, action, name, **kwargs):
        bundle = self.root / name
        return build_task(
            action=action,
            selection=resolve_selection(action),
            prompt_path=str(self.prompt),
            bundle_dir=str(bundle),
            output_path=str(bundle / "task.json"),
            owner="owner", repo="repo", number=1, target_kind="pr-review",
            revision=self.head_sha, base_revision=self.base_sha,
            vision_blob_sha=self.blob_sha, wheelhouse_revision="b" * 40,
            event_key=canonical_sha256(name), vision_file=str(self.vision),
            **kwargs,
        )

    def test_three_tasks_have_distinct_input_boundaries_and_schemas(self):
        derive = self.build("policy-derive.public", "derive")
        plan_path = self.write_json("plan.json", self.plan)
        audit = self.build("policy-audit.public", "audit", policy_plan_file=str(plan_path))
        audit_path = self.write_json("audit.json", self.audit)
        target = self.root / "target.txt"
        target.write_text("untrusted target\n", encoding="utf-8")
        advisory = self.build(
            "advisory-review.public", "advisory", target_file=str(target),
            policy_plan_file=str(plan_path), policy_audit_file=str(audit_path),
        )
        self.assertEqual({row["id"] for row in derive["spec"]["inputs"]}, {"vision", "vision-units", "policy-binding"})
        self.assertEqual({row["id"] for row in audit["spec"]["inputs"]}, {"vision", "vision-units", "policy-binding", "policy-derivation"})
        self.assertEqual(
            {row["id"] for row in advisory["spec"]["inputs"]},
            {"vision", "vision-units", "policy-binding", "policy-derivation", "policy-audit", "target"},
        )
        self.assertEqual(len({task["metadata"]["executionId"] for task in (derive, audit, advisory)}), 3)
        self.assertEqual(len({task["spec"]["output"]["schemaSha256"] for task in (derive, audit, advisory)}), 3)
        for directory, task, max_turns in (("derive", derive, 5), ("audit", audit, 6)):
            self.assertEqual(task["spec"]["limits"]["maxTurns"], max_turns)
            self.assertEqual(task["spec"]["limits"]["maxToolCalls"], 4)
            schema = json.loads(
                (self.root / directory / task["spec"]["output"]["schemaArtifact"]).read_text()
            )
            self.assertEqual(schema["properties"]["target_head_sha"]["const"], self.head_sha)
            self.assertEqual(schema["properties"]["target_base_sha"]["const"], self.base_sha)
            self.assertEqual(schema["properties"]["vision_blob_sha"]["const"], self.blob_sha)

    def test_all_context_optionalization_and_disagreement_fail_closed(self):
        context_plan = {**self.plan, "obligations": []}
        context_plan["units"] = [{
            **self.plan["units"][0], "classification": "context-only",
            "semantic_status": "recognized-local", "normative": False,
            "decision_relevant": False, "condition_strength": "none", "conditions": [],
        }]
        with self.assertRaises(VisionPolicyError):
            validate_policy_artifacts(
                self.vision.read_text(), context_plan, self.audit,
                target_head_sha=self.head_sha, target_base_sha=self.base_sha,
                vision_blob_sha=self.blob_sha,
            )

        optional = json.loads(json.dumps(self.plan))
        optional["obligations"][0]["required"] = False
        optional_path = self.write_json("optional.json", optional)
        audit_path = self.write_json("audit.json", self.audit)
        with self.assertRaises(Exception):
            self.build("advisory-review.public", "optional", policy_plan_file=str(optional_path), policy_audit_file=str(audit_path))

        disagreement = json.loads(json.dumps(self.audit))
        disagreement["units"][0]["condition_strength"] = "advisory"
        disagreement["complete"] = False
        _, _, complete = validate_policy_artifacts(
            self.vision.read_text(), self.plan, disagreement,
            target_head_sha=self.head_sha, target_base_sha=self.base_sha,
            vision_blob_sha=self.blob_sha,
        )
        self.assertFalse(complete)

    def test_every_policy_binding_mismatch_fails_closed(self):
        for field in ("target_head_sha", "target_base_sha", "vision_blob_sha"):
            for artifact_name in ("plan", "audit"):
                plan = json.loads(json.dumps(self.plan))
                audit = json.loads(json.dumps(self.audit))
                artifact = plan if artifact_name == "plan" else audit
                artifact[field] = "f" * 40
                with self.subTest(field=field, artifact=artifact_name):
                    with self.assertRaises(VisionPolicyError):
                        validate_policy_artifacts(
                            self.vision.read_text(), plan, audit,
                            target_head_sha=self.head_sha,
                            target_base_sha=self.base_sha,
                            vision_blob_sha=self.blob_sha,
                        )


if __name__ == "__main__":
    unittest.main()
