#!/usr/bin/env python3
"""
Unit-exercise Wheelhouse scan-time auto-merge (V1) with NO network and NO writes
to any target repository.

Run: python tests/test_auto_merge_v1.py   (needs PyYAML; no network)

Auto-merge is a strict subset of the manual merge gate: a merge-ready pr-review
PR is merged automatically ONLY when every deterministic gate passes AND a fresh
structured behavior verdict for the current head SHA assigns an eligible A/B/C
class and recommends merge. Any missing/stale/malformed/uncertain/unreadable
input HOLDS for human review. These tests cover, end-to-end through the
`act_on_scan` orchestrator with every gh call stubbed:

  * the config + exclusion helpers (`_auto_merge_enabled`, `_auto_merge_exclusions`);
  * the pure behavior-verdict gate (A/B/C eligibility, class-C opt-in/default-off,
    malformed/stale/absent verdicts, fail-closed defaults);
  * the blast-radius caps at the exact 20-file and 1000-line boundaries;
  * every deterministic gate G0-G6 (repo opt-in, base-branch VISION.md presence,
    returning contributor, unconditional file exclusions incl. VISION.md
    self-authorization, live green+CLEAN mergeability, blast radius) via
    representative live-card fixtures walked through PASS and HOLD outcomes;
  * the G7 live head + merge-state re-check immediately before acting;
  * same-closing-issue ambiguity at scan time and at the final act boundary;
  * the per-PR `wheelhouse:no-auto-merge` escape hatch and the global/per-repo
    kill switches;
  * the durable audit ledger (parse/append/render + cap) and the resolved
    decision record, plus the per-merge ::notice::/::warning:: audit lines;
  * base-branch-ONLY VISION.md reads (never the PR head);
  * the fleet-wide `auto_merge: true` switch this fork commits: the global flag
    alone opts a repo in (no per-repo key needed) yet a base-branch VISION.md is
    still required (no claim, no merge, and no vision-bound verdict without it), the
    self-authorization exclusion and base-branch read still hold under global
    true, and the shipped code default stays OFF when the global key is absent;
  * the DELIBERATE ABSENCE of an open-PR same-file overlap gate and of any
    per-contributor / per-scan rate cap (captain override).
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)
import wheelhouse_core as core  # noqa: E402
import render_card  # noqa: E402
import apply_decision  # noqa: E402
import auto_merge as am  # noqa: E402
import automerge_criteria as schema  # noqa: E402

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


BEHAVIOR_ADMISSION = {
    "version": 1,
    "contradicts_existing_contract": False,
}
CLASS_B_ADMISSION = {
    **BEHAVIOR_ADMISSION,
    "corrected_defect": "Daemon restart lost an open monitored run.",
    "intended_behavior_restored": (
        "An open monitored run remains recoverable after restart."
    ),
}
ELIGIBLE_A = {
    "behavior_class": "A",
    "behavior_admission": BEHAVIOR_ADMISSION,
    "aligns_with_vision": True,
    "changes_existing_or_default_behavior": False,
    "recommend_merge": True,
    "vision_sha": "vsha",
    "base_sha": "b1" * 20,
}
ELIGIBLE_B = dict(
    ELIGIBLE_A,
    behavior_class="B",
    behavior_admission=CLASS_B_ADMISSION,
)
ELIGIBLE_C = dict(ELIGIBLE_A, behavior_class="C", optin_default_off=True)
INDEPENDENT_A = {
    "behavior_class": "A",
    "behavior_admission": BEHAVIOR_ADMISSION,
    "changes_existing_or_default_behavior": False,
    "optin_default_off": False,
}


# --------------------------------------------------------------------------- #
# fixture world: a controllable stub of every live read + the merge act
# --------------------------------------------------------------------------- #
def make_pr(
    head="h1" * 20,
    mergeable=True,
    mergeable_state="clean",
    additions=10,
    deletions=10,
    changed_files=2,
    author="alice",
    author_type="User",
    labels=None,
    merged=False,
    state="open",
    base="b1" * 20,
    merge_commit_sha="mc" * 20,
):
    return {
        "head": {"sha": head},
        "base": {"sha": base},
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
        "additions": additions,
        "deletions": deletions,
        "changed_files": changed_files,
        "user": {"login": author, "type": author_type},
        "labels": [{"name": n} for n in (labels or [])],
        "merged": merged,
        "state": state,
        "merge_commit_sha": merge_commit_sha,
    }


def make_card(
    card_issue,
    repo,
    number,
    head,
    triage_status="succeeded",
    automerge_verdict=None,
    held=False,
    kind="pr-review",
    triage_recommendation="merge",
    labels=None,
    author=am.CARD_AUTOMATION_AUTHOR,
):
    state = {
        "repo": repo,
        "number": number,
        "kind": kind,
        "head_sha": head,
        "triaged_sha": head,
        "triage_status": triage_status,
    }
    if held:
        state["held"] = True
    if automerge_verdict is not None:
        state["automerge_verdict"] = automerge_verdict
    if triage_recommendation:
        state["triage_recommendation"] = {
            "action": triage_recommendation,
            "reason": "",
        }
    body = "Card\n\n<!-- wheelhouse-state: %s -->" % json.dumps(state)
    if labels is None:
        labels = [
            "needs-decision",
            "repo:%s" % repo,
            "kind:%s" % kind,
            "priority:med",
            "target:%s-%s" % (repo, number),
        ]
        if held:
            labels.append("pending-triage")
        else:
            labels.extend(["processing", am.AUTO_MERGE_CLAIM_LABEL])
    return {
        "number": card_issue,
        "body": body,
        "labels": [{"name": n} for n in labels],
        "author": author,
        "updatedAt": "2026-07-10T00:00:00Z",
        "comments": [],
    }


def make_item(repo, number, head, comp="pass", tests="green", bucket="merge-ready"):
    return {
        "repo": repo,
        "number": number,
        "kind": "pr-review",
        "bucket": bucket,
        "head_sha": head,
        "comp": comp,
        "tests": tests,
        "same_closing_issue_overlap": "",
    }


class World:
    def __init__(self):
        self.owner = "owner"
        self.maintainers = {"owner"}
        self.global_auto_merge = True
        self.repos = {}  # repo -> repo_cfg dict
        self.vision = {}  # repo -> (present, sha)
        self.vision_seq = {}
        self.merged_authors = {}  # (slug, author) -> bool
        self.pr_seq = {}  # (slug, str(number)) -> [pr, ...]
        self.last_pr = {}
        self.files = {}  # (slug, str(number)) -> (files, ok, complete)
        self.check_status = {}
        self.check_status_seq = {}
        self.closing_issue_overlap = {}
        self.closing_issue_overlap_seq = {}
        self.do_merge_calls = []
        self.do_merge_clean_guards = []
        self.do_merge_final_guards = []
        self.card_token_reads = []
        self.merge_tokens = []
        self.do_merge_returns = {}  # (repo, number) -> (message, terminal)
        self.workflow_gate_results = {}  # (repo, number) -> structured final gate
        self.merge_commits = {}  # (repo, number) -> merge endpoint sha
        self.final_prs = {}
        self.before_final_guard = None

    def set_pr(self, slug, number, prs):
        self.pr_seq[(slug, str(number))] = prs if isinstance(prs, list) else [prs]

    def live_pr(self, slug, number):
        seq = self.pr_seq.get((slug, str(number)))
        if not seq:
            return None
        pr = seq.pop(0) if len(seq) > 1 else seq[0]
        self.last_pr[(slug, str(number))] = pr
        return pr

    def vision_on_default_branch(self, slug):
        repo = slug.split("/")[-1]
        seq = self.vision_seq.get(repo)
        if seq:
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return self.vision.get(repo, (False, ""))

    def immutable_compare_files(self, slug, base, head, expected):
        for (pr_slug, number), pr in self.last_pr.items():
            if pr_slug != slug:
                continue
            if (
                str((pr.get("base") or {}).get("sha") or "") == base
                and str((pr.get("head") or {}).get("sha") or "") == head
            ):
                return self.files.get((slug, number), ([], True, True))
        return ([], False, False)

    def live_check_status(self, owner, repo, number, head, repo_cfg):
        key = (owner, repo, str(number), str(head))
        sequence = self.check_status_seq.get(key)
        if sequence:
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]
        return self.check_status.get(key, (True, "comp=pass tests=green"))

    def same_closing_issue_overlap(self, owner, repo, number):
        key = (owner, repo, str(number))
        sequence = self.closing_issue_overlap_seq.get(key)
        if sequence:
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]
        return self.closing_issue_overlap.get(key, (True, ""))

    def do_merge(
        self,
        owner,
        repo,
        number,
        head,
        return_merge_commit=False,
        return_workflow_gate=False,
        expected_base_sha=None,
        require_clean_merge_state=False,
        auto_merge_guard=None,
    ):
        self.do_merge_calls.append((owner, repo, number, head, expected_base_sha))
        self.do_merge_clean_guards.append(require_clean_merge_state)
        self.merge_tokens.append(os.environ.get("GH_TOKEN"))
        if self.before_final_guard:
            self.before_final_guard()
        if auto_merge_guard is not None:
            final_pr = self.final_prs.get(
                ("%s/%s" % (owner, repo), str(number)),
                self.last_pr.get(("%s/%s" % (owner, repo), str(number)), {}),
            )
            guard_ok, guard_reason = auto_merge_guard(final_pr)
            self.do_merge_final_guards.append((guard_ok, guard_reason))
            if guard_ok is not True:
                result = ["HOLD: %s" % guard_reason, "blocked"]
                if return_merge_commit:
                    result.append("")
                if return_workflow_gate:
                    result.append(None)
                return tuple(result)
        result = self.do_merge_returns.get(
            (repo, str(number)), ("Merged %s#%s (squash)." % (repo, number), "resolved")
        )
        values = list(result)
        if return_merge_commit:
            values.append(self.merge_commits.get((repo, str(number)), "c" * 40))
        if return_workflow_gate:
            values.append(self.workflow_gate_results.get((repo, str(number))))
        return tuple(values)


def run_act(world, items, cards, has_token=True, has_card_token=True):
    """Install stubs, run act_on_scan, capture stderr, restore. Returns
    (payload, stderr_text)."""
    saved = {
        "vision": am.vision_on_default_branch,
        "prior": am.has_prior_merged_pr,
        "live": am.live_pr,
        "compare": am.immutable_compare_files,
        "checks": am.live_check_status,
        "closing_overlap": core.same_closing_issue_overlap,
        "domerge": apply_decision.do_merge,
        "get_card": render_card.get_card,
        "edit_body": render_card._edit_issue_body,
        "closed_intents": am.closed_audit_intent_entries,
        "cfg": core.load_config,
        "maint": core.maintainers,
        "owner": core.get_owner,
        "token": os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN"),
        "card_token": os.environ.get("WHEELHOUSE_CARD_TOKEN"),
    }
    am.vision_on_default_branch = world.vision_on_default_branch
    am.has_prior_merged_pr = lambda slug, author: world.merged_authors.get(
        (slug, author), False
    )
    am.live_pr = world.live_pr
    am.immutable_compare_files = world.immutable_compare_files
    am.live_check_status = world.live_check_status
    core.same_closing_issue_overlap = world.same_closing_issue_overlap
    apply_decision.do_merge = world.do_merge
    am.closed_audit_intent_entries = lambda card_token: getattr(
        world, "closed_audit_intents", {}
    )
    core.load_config = lambda: {
        "auto_merge": world.global_auto_merge,
        "repos": world.repos,
    }
    core.maintainers = lambda: set(world.maintainers)
    core.get_owner = lambda: world.owner
    cards_by_number = {str(card["number"]): card for card in cards}

    def get_card(number):
        world.card_token_reads.append(os.environ.get("GH_TOKEN"))
        sequence = getattr(world, "card_seq", {}).get(str(number))
        if sequence:
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]
        return cards_by_number.get(str(number))

    render_card.get_card = get_card

    def edit_body(number, body, remove_labels=None):
        card = cards_by_number.get(str(number))
        if card is not None:
            card["body"] = body

    render_card._edit_issue_body = edit_body
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true" if has_token else "false"
    if has_card_token:
        os.environ["WHEELHOUSE_CARD_TOKEN"] = "card-token"
    else:
        os.environ.pop("WHEELHOUSE_CARD_TOKEN", None)
    scan = {
        "repos": {
            it["repo"]: world.repos_scan.get(it["repo"], {"ok": True}) for it in items
        }
        if hasattr(world, "repos_scan")
        else {it["repo"]: {"ok": True} for it in items},
        "items": items,
    }
    buf = io.StringIO()
    try:
        with redirect_stderr(buf):
            payload = am.act_on_scan(scan, cards)
    finally:
        am.vision_on_default_branch = saved["vision"]
        am.has_prior_merged_pr = saved["prior"]
        am.live_pr = saved["live"]
        am.immutable_compare_files = saved["compare"]
        am.live_check_status = saved["checks"]
        core.same_closing_issue_overlap = saved["closing_overlap"]
        apply_decision.do_merge = saved["domerge"]
        render_card.get_card = saved["get_card"]
        render_card._edit_issue_body = saved["edit_body"]
        am.closed_audit_intent_entries = saved["closed_intents"]
        core.load_config = saved["cfg"]
        core.maintainers = saved["maint"]
        core.get_owner = saved["owner"]
        if saved["token"] is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = saved["token"]
        if saved["card_token"] is None:
            os.environ.pop("WHEELHOUSE_CARD_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_CARD_TOKEN"] = saved["card_token"]
    return payload, buf.getvalue()


def default_world(head="h1" * 20, verdict=None, repo="fmt", number=5):
    """A world where everything passes; individual tests knock out one gate."""
    w = World()
    slug = "owner/%s" % repo
    w.repos = {repo: {"auto_merge": True}}
    w.vision = {repo: (True, "vsha")}
    w.merged_authors = {(slug, "alice"): True}
    w.files[(slug, str(number))] = (["src/a.py", "README.md"], True, True)
    w.set_pr(slug, number, make_pr(head=head))
    cards = [
        make_card(101, repo, number, head, automerge_verdict=verdict or ELIGIBLE_A)
    ]
    items = [make_item(repo, number, head)]
    return w, items, cards


# --------------------------------------------------------------------------- #
# config + exclusion helpers
# --------------------------------------------------------------------------- #
def test_auto_merge_enabled_default_off_and_overrides():
    check("config: absent -> default off", core._auto_merge_enabled({}, False) is False)
    check(
        "config: per-repo true overrides global false",
        core._auto_merge_enabled({"auto_merge": True}, False) is True,
    )
    check(
        "config: per-repo false is the one-repo kill switch",
        core._auto_merge_enabled({"auto_merge": False}, True) is False,
    )
    check(
        "config: absent falls through to global",
        core._auto_merge_enabled({}, True) is True,
    )
    for value in ("true", "false", 1, 0, [], {}):
        check(
            "config: malformed global %r fails closed" % (value,),
            core._auto_merge_enabled({}, value) is False,
        )
        check(
            "config: malformed repo value %r fails closed" % (value,),
            core._auto_merge_enabled({"auto_merge": value}, True) is False,
        )


def test_load_config_auto_merge_requires_real_boolean_true():
    saved = core.config_path
    try:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "wheelhouse.config.yml")
            core.config_path = lambda: path
            for value, expected in (
                ("true", True),
                ("false", False),
                ('"true"', False),
                ('"false"', False),
                ("1", False),
            ):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("repos: []\nauto_merge: %s\n" % value)
                check(
                    "config: loaded auto_merge %s is %s" % (value, expected),
                    core.load_config()["auto_merge"] is expected,
                )
    finally:
        core.config_path = saved


def test_exclusions_cover_every_category():
    cases = {
        "workflow-action": ".github/workflows/ci.yml",
        "governance": ".github/CODEOWNERS",
        "dependency": "package-lock.json",
        "release": "release-please-config.json",
        "security": "SECURITY.md",
        "authentication": "lib/auth/session.go",
        "billing": "app/billing/stripe.py",
        "migration": "db/migrate/001_init.rb",
        "persistence": "db/schema.sql",
        "install-bootstrap": "Dockerfile",
        "public-default": "config/app.yml",
        "vision": "VISION.md",
    }
    for category, path in cases.items():
        hits = core._auto_merge_exclusions([path])
        check(
            "exclusion: %s (%s) held" % (category, path),
            any(h.startswith(category + ":") for h in hits),
        )
    check(
        "exclusion: ordinary source/docs/tests are NOT excluded",
        core._auto_merge_exclusions(["src/x.py", "docs/g.md", "tests/test_x.py"]) == [],
    )
    check(
        "exclusion: action.yml at any depth is a pwn-request hold",
        core._auto_merge_exclusions(["nested/action.yaml"]),
    )
    for path, category in (
        ("src/auth.py", "authentication"),
        ("src/authentication.py", "authentication"),
        ("src/permissions.py", "authentication"),
        ("src/permission.ts", "authentication"),
        ("src/iam.py", "authentication"),
        ("src/rbac.go", "authentication"),
        ("src/acl.rs", "authentication"),
        ("src/access_control.py", "authentication"),
        ("src/access-control.ts", "authentication"),
        ("src/security.py", "security"),
        ("pom.xml", "dependency"),
        ("build.gradle", "dependency"),
        ("build.gradle.kts", "dependency"),
        ("settings.gradle", "dependency"),
        ("settings.gradle.kts", "dependency"),
        ("uv.lock", "dependency"),
        ("pylock.toml", "dependency"),
        ("bun.lock", "dependency"),
        ("bun.lockb", "dependency"),
        ("deno.lock", "dependency"),
        ("mix.lock", "dependency"),
        ("gradle.lockfile", "dependency"),
        ("packages.lock.json", "dependency"),
        ("Package.resolved", "dependency"),
        (".terraform.lock.hcl", "dependency"),
        ("Podfile.lock", "dependency"),
        ("Cartfile.resolved", "dependency"),
        ("flake.lock", "dependency"),
        ("Package.swift", "dependency"),
        ("pubspec.lock", "dependency"),
        ("requirements-dev.txt", "dependency"),
        ("tool.lock.toml", "dependency"),
        ("constraints-production.txt", "dependency"),
        (".gitmodules", "dependency"),
        ("scripts/migrate.py", "migration"),
        ("scripts/migrate_data.py", "migration"),
        ("tools/migrate.py", "migration"),
        ("scripts/migrate_users.ts", "migration"),
        ("db/migrate_users.js", "migration"),
        ("migrate_roles.rb", "migration"),
        ("db/migrate/001_users.sql", "migration"),
        ("migrations/001_users.go", "migration"),
    ):
        check(
            "exclusion: component filename %s is held" % path,
            any(
                hit.startswith(category + ":")
                for hit in core._auto_merge_exclusions([path])
            ),
        )


# --------------------------------------------------------------------------- #
# pure behavior-verdict gate: A/B/C, class-C opt-in, malformed/stale/absent
# --------------------------------------------------------------------------- #
def test_verdict_classes_ABC():
    for label, v in (("A", ELIGIBLE_A), ("B", ELIGIBLE_B), ("C", ELIGIBLE_C)):
        ok, cls, _ = am.verdict_eligible(v)
        check("verdict: class %s eligible" % label, ok is True and cls == label)


def test_class_b_semantic_admission_boundary():
    defect_quote = "Daemon restart lost an open monitored run."
    restored_quote = "An open monitored run remains recoverable after restart."
    product_claim = (
        "Narrow corrective fix restoring documented recovery behavior "
        "without changing default behavior"
    )
    restoration = {
        "corrected_defect": defect_quote,
        "corrected_defect_evidence": {
            "source": "target.txt",
            "quote": defect_quote,
        },
        "intended_behavior_restored": restored_quote,
        "intended_behavior_restored_evidence": {
            "source": "target-src/lib/recovery.py",
            "quote": restored_quote,
        },
    }

    def candidate(**overrides):
        value = {
            "summary": "Fixes recovery after a daemon restart.",
            "product_implications": product_claim + ".",
            "recommended_action": "merge",
            "recommended_reason": "The narrow recovery regression is covered.",
            "evidence": "target.txt: '%s'" % defect_quote,
            "automerge": {
                "behavior_class": "B",
                "behavior_assertions": [
                    {
                        "claim": product_claim,
                        "subject": "default_behavior",
                        "effect": "unchanged",
                        "evidence": {
                            "source": "target.txt",
                            "quote": product_claim,
                        },
                    }
                ],
                "class_b_restoration": restoration,
                "changes_existing_or_default_behavior": False,
                "optin_default_off": False,
                "aligns_with_vision": True,
                "recommend_merge": True,
            },
        }
        value.update(overrides)
        return value

    def normalize(value, verified=None):
        bounded = dict(value)
        bounded[render_card._VERIFIED_EVIDENCE_SPANS_FIELD] = tuple(
            (
                ("target.txt", render_card._normalize_evidence_text(defect_quote)),
                ("target.txt", render_card._normalize_evidence_text(product_claim)),
                (
                    "target-src/lib/recovery.py",
                    render_card._normalize_evidence_text(restored_quote),
                ),
            )
            if verified is None
            else verified
        )
        return render_card.normalize_triage(bounded)

    valid = normalize(candidate())["automerge_verdict"]
    check(
        "class B admission: narrow corrective restoration remains eligible",
        am.verdict_eligible(valid)[0] is True,
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        target_file = os.path.join(temp_dir, "target.txt")
        target_src = os.path.join(temp_dir, "target-src")
        os.makedirs(os.path.join(target_src, "lib"))
        with open(target_file, "w", encoding="utf-8") as source_file:
            source_file.write(defect_quote + "\n" + product_claim)
        with open(
            os.path.join(target_src, "lib", "recovery.py"),
            "w",
            encoding="utf-8",
        ) as source_file:
            source_file.write(restored_quote)
        bound = render_card._bind_verified_evidence_spans(
            candidate(), target_file, target_src
        )
        check(
            "class B admission: target-src restoration reference is verified",
            am.verdict_eligible(
                render_card.normalize_triage(bound)["automerge_verdict"]
            )[0]
            is True,
        )
    with tempfile.TemporaryDirectory() as temp_dir:
        repository = os.path.join(temp_dir, "repository")
        bundle = os.path.join(temp_dir, "bundle")
        os.makedirs(os.path.join(repository, "lib"))
        subprocess.run(["git", "init", "-q", repository], check=True)
        subprocess.run(
            ["git", "-C", repository, "config", "user.name", "fixture"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repository, "config", "user.email", "fixture@example.com"],
            check=True,
        )
        with open(
            os.path.join(repository, "lib", "recovery.py"),
            "w",
            encoding="utf-8",
        ) as source_file:
            source_file.write(restored_quote)
        subprocess.run(["git", "-C", repository, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", repository, "commit", "-qm", "fixture"],
            check=True,
        )
        revision = subprocess.run(
            ["git", "-C", repository, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        render_card.build_target_source_evidence(repository, bundle, revision)
        manifest = os.path.join(bundle, "manifest.json")
        files = os.path.join(bundle, "files")
        check(
            "source evidence: exact-revision manifest verifies",
            render_card.verify_target_source_evidence(files, manifest, revision)
            is not None,
        )
        check(
            "source evidence: revision mismatch fails closed",
            render_card.verify_target_source_evidence(
                files, manifest, "0" * len(revision)
            )
            is None,
        )
        with open(
            os.path.join(files, "lib", "recovery.py"),
            "a",
            encoding="utf-8",
        ) as source_file:
            source_file.write("tampered")
        check(
            "source evidence: digest mismatch fails closed",
            render_card.verify_target_source_evidence(files, manifest, revision)
            is None,
        )

    observed_input = candidate(
        summary="Adds a pre-push Codex review gate to direct-PR delivery.",
        product_implications=(
            "Routine maintainer decision - tightens an existing delivery "
            "contract without changing a user-facing flag or default."
        ),
    )
    observed_input["automerge"] = dict(observed_input["automerge"])
    observed_input["automerge"]["behavior_assertions"] = [
        {
            "claim": (
                "Routine maintainer decision - tightens an existing delivery "
                "contract without changing a user-facing flag or default"
            ),
            "subject": "delivery_contract",
            "effect": "tightened",
            "evidence": {
                "source": "target.txt",
                "quote": "Existing delivery contract is tightened for review.",
            },
        },
        {
            "claim": "Existing delivery contract is tightened for review",
            "subject": "delivery_contract",
            "effect": "tightened",
            "evidence": {
                "source": "target.txt",
                "quote": "Existing delivery contract is tightened for review.",
            },
        },
    ]
    observed = normalize(
        observed_input,
        verified=(
            ("target.txt", render_card._normalize_evidence_text(defect_quote)),
            (
                "target-src/lib/recovery.py",
                render_card._normalize_evidence_text(restored_quote),
            ),
            (
                "target.txt",
                render_card._normalize_evidence_text(
                    "Existing delivery contract is tightened for review."
                ),
            ),
        ),
    )["automerge_verdict"]
    observed_facts, _ = am.behavior_verdict_facts(observed)
    check(
        "WH-AUD-05: card 1620 contradiction is denied by semantic admission",
        am.verdict_eligible(observed)[0] is False
        and observed_facts["g6_behavior_class"]["status"]
        == schema.STATUS_UNMET,
    )
    mislabeled_input = json.loads(json.dumps(observed_input))
    for assertion in mislabeled_input["automerge"]["behavior_assertions"]:
        assertion.update(subject="documentation_or_tests", effect="unchanged")
    mislabeled = normalize(
        mislabeled_input,
        verified=(
            ("target.txt", render_card._normalize_evidence_text(defect_quote)),
            (
                "target-src/lib/recovery.py",
                render_card._normalize_evidence_text(restored_quote),
            ),
            (
                "target.txt",
                render_card._normalize_evidence_text(
                    "Existing delivery contract is tightened for review."
                ),
            ),
        ),
    )["automerge_verdict"]
    check(
        "semantic admission: mislabeled card-1620 assertion is unavailable",
        am.verdict_eligible(mislabeled)[0] is False
        and am.behavior_verdict_facts(mislabeled)[0]["g6_behavior_class"]["status"]
        == schema.STATUS_UNAVAILABLE,
    )

    for label, text in (
        ("active change", "Changes the existing direct-PR workflow"),
        ("passive change", "The existing direct-PR workflow is changed"),
        ("new requirement", "The existing workflow now requires review"),
        ("modal requirement", "The default workflow must now require review"),
        ("as-well-as evasion", "Disables tests as well as the existing workflow"),
        (
            "mixed documentation clause",
            "The existing workflow now requires approval and documentation is updated",
        ),
        (
            "reversed mixed documentation clause",
            "Existing workflow documentation is updated and the existing workflow now requires approval",
        ),
    ):
        contradictory = normalize(candidate(summary=text + "."))["automerge_verdict"]
        check(
            "semantic admission: %s is denied" % label,
            am.verdict_eligible(contradictory)[0] is False,
        )

    missing_input = candidate()
    missing_input["automerge"] = dict(missing_input["automerge"])
    missing_input["automerge"].pop("class_b_restoration")
    missing = normalize(missing_input)["automerge_verdict"]
    missing_facts, _ = am.behavior_verdict_facts(missing)
    check(
        "class B admission: missing restoration evidence is unavailable",
        am.verdict_eligible(missing)[0] is False
        and missing_facts["g6_behavior_class"]["status"]
        == schema.STATUS_UNAVAILABLE,
    )

    ambiguous_input = candidate()
    ambiguous_input["automerge"] = dict(ambiguous_input["automerge"])
    ambiguous_input["automerge"]["class_b_restoration"] = {
        "corrected_defect": "The same vague behavior statement.",
        "corrected_defect_evidence": restoration["corrected_defect_evidence"],
        "intended_behavior_restored": "The same vague behavior statement.",
        "intended_behavior_restored_evidence": restoration[
            "intended_behavior_restored_evidence"
        ],
    }
    ambiguous = normalize(ambiguous_input)["automerge_verdict"]
    check(
        "class B admission: ambiguous restoration evidence is unavailable",
        am.verdict_eligible(ambiguous)[0] is False
        and am.behavior_verdict_facts(ambiguous)[0]["g6_behavior_class"]["status"]
        == schema.STATUS_UNAVAILABLE,
    )

    fabricated_spans = normalize(candidate(), verified=())["automerge_verdict"]
    check(
        "class B admission: separately fabricated matching spans are unavailable",
        am.verdict_eligible(fabricated_spans)[0] is False
        and am.behavior_verdict_facts(fabricated_spans)[0]["g6_behavior_class"][
            "status"
        ]
        == schema.STATUS_UNAVAILABLE,
    )

    incidental_input = candidate()
    incidental_input["automerge"] = dict(incidental_input["automerge"])
    incidental_input["automerge"]["class_b_restoration"] = {
        "corrected_defect": "Restart logging lost buffered records.",
        "corrected_defect_evidence": {
            "source": "target.txt",
            "quote": "Restart logging lost buffered records.",
        },
        "intended_behavior_restored": (
            "Restart authentication recovers expired sessions."
        ),
        "intended_behavior_restored_evidence": {
            "source": "target-src/lib/recovery.py",
            "quote": "Restart authentication recovers expired sessions.",
        },
    }
    incidental = normalize(
        incidental_input,
        verified=(
            (
                "target.txt",
                render_card._normalize_evidence_text(
                    "Restart logging lost buffered records."
                ),
            ),
            (
                "target-src/lib/recovery.py",
                render_card._normalize_evidence_text(
                    "Restart authentication recovers expired sessions."
                ),
            ),
            ("target.txt", render_card._normalize_evidence_text(product_claim)),
        ),
    )["automerge_verdict"]
    check(
        "class B admission: incidental one-token overlap is unavailable",
        am.verdict_eligible(incidental)[0] is False
        and am.behavior_verdict_facts(incidental)[0]["g6_behavior_class"]["status"]
        == schema.STATUS_UNAVAILABLE,
    )

    unsupported_input = candidate()
    unsupported_input["automerge"] = dict(unsupported_input["automerge"])
    unsupported_input["automerge"]["class_b_restoration"] = {
        "corrected_defect": (
            "Open monitored run diagnostic timestamps disappear."
        ),
        "corrected_defect_evidence": {
            "source": "target.txt",
            "quote": "Open monitored run archival metadata persists.",
        },
        "intended_behavior_restored": (
            "Open monitored run authentication sessions recover."
        ),
        "intended_behavior_restored_evidence": {
            "source": "target-src/lib/recovery.py",
            "quote": "Open monitored run process metadata persists.",
        },
    }
    unsupported = normalize(
        unsupported_input,
        verified=(
            (
                "target.txt",
                render_card._normalize_evidence_text(
                    "Open monitored run archival metadata persists."
                ),
            ),
            (
                "target-src/lib/recovery.py",
                render_card._normalize_evidence_text(
                    "Open monitored run process metadata persists."
                ),
            ),
            ("target.txt", render_card._normalize_evidence_text(product_claim)),
        ),
    )["automerge_verdict"]
    check(
        "class B admission: subject-only spans do not support claim content",
        am.verdict_eligible(unsupported)[0] is False
        and am.behavior_verdict_facts(unsupported)[0]["g6_behavior_class"]["status"]
        == schema.STATUS_UNAVAILABLE,
    )

    polarity_input = candidate()
    polarity_input["automerge"] = dict(polarity_input["automerge"])
    polarity_input["automerge"]["class_b_restoration"] = {
        "corrected_defect": (
            "Open monitored runs lose authentication sessions."
        ),
        "corrected_defect_evidence": {
            "source": "target.txt",
            "quote": "Open monitored runs never lose authentication sessions.",
        },
        "intended_behavior_restored": (
            "Open monitored runs restore authentication sessions."
        ),
        "intended_behavior_restored_evidence": {
            "source": "target-src/lib/recovery.py",
            "quote": (
                "Open monitored runs do not restore authentication sessions."
            ),
        },
    }
    polarity = normalize(
        polarity_input,
        verified=(
            (
                "target.txt",
                render_card._normalize_evidence_text(
                    "Open monitored runs never lose authentication sessions."
                ),
            ),
            (
                "target-src/lib/recovery.py",
                render_card._normalize_evidence_text(
                    "Open monitored runs do not restore authentication sessions."
                ),
            ),
            ("target.txt", render_card._normalize_evidence_text(product_claim)),
        ),
    )["automerge_verdict"]
    check(
        "class B admission: restoration polarity inversion is unavailable",
        am.verdict_eligible(polarity)[0] is False
        and am.behavior_verdict_facts(polarity)[0]["g6_behavior_class"]["status"]
        == schema.STATUS_UNAVAILABLE,
    )
    contracted_polarity_input = json.loads(json.dumps(polarity_input))
    contracted_restoration = contracted_polarity_input["automerge"][
        "class_b_restoration"
    ]
    contracted_restoration["corrected_defect_evidence"]["quote"] = (
        "Open monitored runs don't lose authentication sessions."
    )
    contracted_restoration["intended_behavior_restored_evidence"]["quote"] = (
        "Open monitored runs don't restore authentication sessions."
    )
    contracted_polarity = normalize(
        contracted_polarity_input,
        verified=(
            (
                "target.txt",
                render_card._normalize_evidence_text(
                    "Open monitored runs don't lose authentication sessions."
                ),
            ),
            (
                "target-src/lib/recovery.py",
                render_card._normalize_evidence_text(
                    "Open monitored runs don't restore authentication sessions."
                ),
            ),
            ("target.txt", render_card._normalize_evidence_text(product_claim)),
        ),
    )["automerge_verdict"]
    check(
        "class B admission: contracted polarity inversion is unavailable",
        am.verdict_eligible(contracted_polarity)[0] is False
        and am.behavior_verdict_facts(contracted_polarity)[0][
            "g6_behavior_class"
        ]["status"]
        == schema.STATUS_UNAVAILABLE,
    )
    cannot_polarity_input = json.loads(json.dumps(contracted_polarity_input))
    cannot_restoration = cannot_polarity_input["automerge"][
        "class_b_restoration"
    ]
    cannot_restoration["corrected_defect_evidence"]["quote"] = (
        "Open monitored runs can't lose authentication sessions."
    )
    cannot_restoration["intended_behavior_restored_evidence"]["quote"] = (
        "Open monitored runs can't restore authentication sessions."
    )
    cannot_polarity = normalize(
        cannot_polarity_input,
        verified=(
            (
                "target.txt",
                render_card._normalize_evidence_text(
                    "Open monitored runs can't lose authentication sessions."
                ),
            ),
            (
                "target-src/lib/recovery.py",
                render_card._normalize_evidence_text(
                    "Open monitored runs can't restore authentication sessions."
                ),
            ),
            ("target.txt", render_card._normalize_evidence_text(product_claim)),
        ),
    )["automerge_verdict"]
    check(
        "class B admission: cannot polarity inversion is unavailable",
        am.verdict_eligible(cannot_polarity)[0] is False
        and am.behavior_verdict_facts(cannot_polarity)[0][
            "g6_behavior_class"
        ]["status"]
        == schema.STATUS_UNAVAILABLE,
    )

    hidden_contract_input = candidate()
    hidden_contract_input["automerge"] = dict(hidden_contract_input["automerge"])
    hidden_contract_input["automerge"]["class_b_restoration"] = {
        "corrected_defect": (
            "Existing direct-PR workflow now requires Codex review."
        ),
        "corrected_defect_evidence": {
            "source": "target.txt",
            "quote": "Existing direct-PR workflow now requires Codex review.",
        },
        "intended_behavior_restored": (
            "Existing direct-PR workflow restores Codex review routing."
        ),
        "intended_behavior_restored_evidence": {
            "source": "target-src/lib/recovery.py",
            "quote": "Existing direct-PR workflow restores Codex review routing.",
        },
    }
    hidden_contract = normalize(
        hidden_contract_input,
        verified=(
            (
                "target.txt",
                render_card._normalize_evidence_text(
                    "Existing direct-PR workflow now requires Codex review."
                ),
            ),
            (
                "target-src/lib/recovery.py",
                render_card._normalize_evidence_text(
                    "Existing direct-PR workflow restores Codex review routing."
                ),
            ),
            ("target.txt", render_card._normalize_evidence_text(product_claim)),
        ),
    )["automerge_verdict"]
    check(
        "class B admission: restoration-only contract change is unavailable",
        am.verdict_eligible(hidden_contract)[0] is False
        and am.behavior_verdict_facts(hidden_contract)[0]["g6_behavior_class"][
            "status"
        ]
        == schema.STATUS_UNAVAILABLE,
    )

    changed_default = dict(valid, changes_existing_or_default_behavior=True)
    changed_facts, _ = am.behavior_verdict_facts(changed_default)
    check(
        "class B control: independently declared behavior change remains UNMET",
        changed_facts["g6_behavior_class"]["status"] == schema.STATUS_MET
        and changed_facts["g6_default_behavior"]["status"]
        == schema.STATUS_UNMET
        and am.verdict_eligible(changed_default)[0] is False,
    )

    legacy = dict(ELIGIBLE_B)
    legacy.pop("behavior_admission")
    check(
        "class B compatibility: historical verdict without evidence is unavailable",
        am.verdict_eligible(legacy)[0] is False
        and am.behavior_verdict_facts(legacy)[0]["g6_behavior_class"]["status"]
        == schema.STATUS_UNAVAILABLE,
    )

    for label, text, subject in (
        (
            "passive tests",
            "Tests for the existing workflow are changed",
            "documentation_or_tests",
        ),
        (
            "workflow documentation",
            "Changes the existing workflow documentation only",
            "documentation_or_tests",
        ),
        (
            "explicit no-change",
            "The existing default workflow remains unchanged",
            "existing_workflow",
        ),
        (
            "negative modal",
            "The existing workflow will not change",
            "existing_workflow",
        ),
        (
            "negative passive",
            "The existing workflow is not changed",
            "existing_workflow",
        ),
        (
            "delivery contract documentation",
            "Changes documentation for the delivery contract",
            "documentation_or_tests",
        ),
        (
            "existing mode tests",
            "Changes tests for the existing mode",
            "documentation_or_tests",
        ),
        (
            "default behavior examples",
            "Changes examples of the default behavior",
            "documentation_or_tests",
        ),
    ):
        neutral_input = candidate(summary=text + ".")
        neutral_input["automerge"] = dict(neutral_input["automerge"])
        neutral_input["automerge"]["behavior_assertions"] = list(
            neutral_input["automerge"]["behavior_assertions"]
        ) + [
            {
                "claim": text,
                "subject": subject,
                "effect": "changed"
                if subject == "documentation_or_tests"
                else "unchanged",
                "evidence": {"source": "target.txt", "quote": text},
            }
        ]
        neutral = normalize(
            neutral_input,
            verified=(
                ("target.txt", render_card._normalize_evidence_text(defect_quote)),
                ("target.txt", render_card._normalize_evidence_text(product_claim)),
                (
                    "target-src/lib/recovery.py",
                    render_card._normalize_evidence_text(restored_quote),
                ),
                ("target.txt", render_card._normalize_evidence_text(text)),
            ),
        )["automerge_verdict"]
        check(
            "semantic admission: %s remains eligible" % label,
            am.verdict_eligible(neutral)[0] is True,
        )

    preserved_text = (
        "Changes workflow documentation and preserves the existing workflow"
    )
    preserved_input = candidate(summary=preserved_text + ".")
    preserved_input["automerge"] = dict(preserved_input["automerge"])
    preserved_input["automerge"]["behavior_assertions"] = list(
        preserved_input["automerge"]["behavior_assertions"]
    ) + [
        {
            "claim": preserved_text,
            "subject": "documentation_or_tests",
            "effect": "changed",
            "evidence": {"source": "target.txt", "quote": preserved_text},
        },
        {
            "claim": preserved_text,
            "subject": "existing_workflow",
            "effect": "unchanged",
            "evidence": {"source": "target.txt", "quote": preserved_text},
        },
    ]
    preserved = normalize(
        preserved_input,
        verified=(
            ("target.txt", render_card._normalize_evidence_text(defect_quote)),
            ("target.txt", render_card._normalize_evidence_text(product_claim)),
            (
                "target-src/lib/recovery.py",
                render_card._normalize_evidence_text(restored_quote),
            ),
            ("target.txt", render_card._normalize_evidence_text(preserved_text)),
        ),
    )["automerge_verdict"]
    check(
        "semantic admission: preserved workflow with docs change remains eligible",
        am.verdict_eligible(preserved)[0] is True,
    )

    for label, text, assertions in (
        (
            "generic existing contract mapping",
            (
                "Updates documentation for the delivery contract and "
                "tightens the existing contract"
            ),
            (
                ("documentation_or_tests", "changed"),
                ("delivery_contract", "tightened"),
            ),
        ),
        (
            "workflow tests verb mapping",
            "The existing workflow tests credentials and now requires approval",
            (("existing_workflow", "new_requirement"),),
        ),
    ):
        mapped_input = candidate(summary=text + ".")
        mapped_input["automerge"] = dict(mapped_input["automerge"])
        mapped_input["automerge"]["behavior_assertions"] = list(
            mapped_input["automerge"]["behavior_assertions"]
        ) + [
            {
                "claim": text,
                "subject": subject,
                "effect": effect,
                "evidence": {"source": "target.txt", "quote": text},
            }
            for subject, effect in assertions
        ]
        mapped = normalize(
            mapped_input,
            verified=(
                ("target.txt", render_card._normalize_evidence_text(defect_quote)),
                ("target.txt", render_card._normalize_evidence_text(product_claim)),
                (
                    "target-src/lib/recovery.py",
                    render_card._normalize_evidence_text(restored_quote),
                ),
                ("target.txt", render_card._normalize_evidence_text(text)),
            ),
        )["automerge_verdict"]
        mapped_facts, _ = am.behavior_verdict_facts(mapped)
        check(
            "semantic admission: %s is denied" % label,
            am.verdict_eligible(mapped)[0] is False
            and mapped_facts["g6_behavior_class"]["status"]
            == schema.STATUS_UNMET,
        )

    for behavior_class, optin in (("A", False), ("C", True)):
        contradictory_input = candidate(
            summary="Changes the existing delivery workflow before each run."
        )
        contradictory_input["automerge"] = dict(
            contradictory_input["automerge"]
        )
        contradictory_input["automerge"].update(
            {
                "behavior_class": behavior_class,
                "optin_default_off": optin,
            }
        )
        contradictory_input["automerge"].pop("class_b_restoration")
        contradictory_non_b = normalize(contradictory_input)["automerge_verdict"]
        check(
            "semantic admission: contradictory class %s is denied" % behavior_class,
            am.verdict_eligible(contradictory_non_b)[0] is False,
        )

        unaffected_input = candidate(
            summary="No existing or default product behavior changes."
        )
        unaffected_input["automerge"] = dict(unaffected_input["automerge"])
        unaffected_input["automerge"].update(
            {
                "behavior_class": behavior_class,
                "optin_default_off": optin,
            }
        )
        unaffected_input["automerge"].pop("class_b_restoration")
        unaffected_input["automerge"]["behavior_assertions"] = [
            {
                "claim": "No existing or default product behavior changes",
                "subject": "default_behavior",
                "effect": "unchanged",
                "evidence": {
                    "source": "target.txt",
                    "quote": "No existing or default product behavior changes",
                },
            },
            candidate()["automerge"]["behavior_assertions"][0],
        ]
        unaffected = normalize(
            unaffected_input,
            verified=(
                ("target.txt", render_card._normalize_evidence_text(defect_quote)),
                ("target.txt", render_card._normalize_evidence_text(product_claim)),
                (
                    "target.txt",
                    render_card._normalize_evidence_text(
                        "No existing or default product behavior changes"
                    ),
                ),
            ),
        )["automerge_verdict"]
        check(
            "semantic admission: valid class %s remains eligible" % behavior_class,
            am.verdict_eligible(unaffected)[0] is True,
        )


def test_verdict_class_C_requires_optin_default_off():
    ok, cls, reason = am.verdict_eligible(
        dict(ELIGIBLE_A, behavior_class="C")  # no optin_default_off
    )
    check("verdict: class C w/o opt-in held", ok is False and cls == "C")
    check("verdict: class C w/o opt-in reason", "opt-in" in reason)


def test_verdict_ineligible_and_fail_closed_defaults():
    ok, cls, _ = am.verdict_eligible(dict(ELIGIBLE_A, behavior_class="D"))
    check("verdict: non-ABC class held", ok is False and cls == "")
    ok, _, _ = am.verdict_eligible(dict(ELIGIBLE_A, aligns_with_vision=False))
    check("verdict: not-aligned held", ok is False)
    ok, _, _ = am.verdict_eligible(
        dict(ELIGIBLE_A, changes_existing_or_default_behavior=True)
    )
    check("verdict: ineligible existing/default behavior change held", ok is False)
    ok, _, _ = am.verdict_eligible(dict(ELIGIBLE_A, recommend_merge=False))
    check("verdict: not-recommended held", ok is False)
    for bad in (None, {}, {"behavior_class": "A"}, "merge", 3):
        ok, _, _ = am.verdict_eligible(bad)
        check("verdict: malformed %r held" % (bad,), ok is False)


def test_vision_independent_facts_are_evaluated_without_granting_eligibility():
    facts, cls = am.behavior_verdict_facts(INDEPENDENT_A)
    check(
        "verdict split: independent class is evaluated",
        cls == "A" and facts["g6_behavior_class"]["status"] == schema.STATUS_MET,
    )
    check(
        "verdict split: default behavior and class-C mode are evaluated",
        facts["g6_default_behavior"]["status"] == schema.STATUS_MET
        and facts["g6_class_c_mode"]["status"] == schema.STATUS_MET,
    )
    check(
        "verdict split: vision facts remain unavailable",
        facts["g6_vision_alignment"]["status"] == schema.STATUS_UNAVAILABLE
        and facts["g6_verdict_merge"]["status"] == schema.STATUS_UNAVAILABLE,
    )
    ok, _, reason = am.verdict_eligible(INDEPENDENT_A)
    check(
        "verdict split: independent facts alone never grant eligibility",
        ok is False and "VISION.md" in reason,
    )

    default_change = dict(
        INDEPENDENT_A, changes_existing_or_default_behavior=True
    )
    class_c_not_opted_in = dict(
        INDEPENDENT_A, behavior_class="C", optin_default_off=False
    )
    check(
        "verdict split: real default behavior failure is UNMET",
        am.behavior_verdict_facts(default_change)[0]["g6_default_behavior"]["status"]
        == schema.STATUS_UNMET,
    )
    check(
        "verdict split: real class-C mode failure is UNMET",
        am.behavior_verdict_facts(class_c_not_opted_in)[0]["g6_class_c_mode"][
            "status"
        ]
        == schema.STATUS_UNMET,
    )


def test_verdict_normalization_and_persistence_fail_closed():
    # A missing required boolean means the verdict is never persisted (hold).
    bad = {"behavior_class": "A", "aligns_with_vision": True, "recommend_merge": True}
    check(
        "verdict: normalize drops verdict missing a required field",
        render_card.normalize_automerge_verdict(bad) is None,
    )
    # A well-formed sub-object round-trips with booleans coerced.
    good = render_card.normalize_automerge_verdict(
        {
            "behavior_class": "b",
            "aligns_with_vision": "true",
            "changes_existing_or_default_behavior": "false",
            "recommend_merge": "true",
        }
    )
    check(
        "verdict: normalize coerces + upper-cases class",
        good and good["behavior_class"] == "B" and good["aligns_with_vision"] is True,
    )
    independent = render_card.normalize_automerge_verdict(
        dict(INDEPENDENT_A, behavior_assertions=[]),
        triage_data={
            "summary": "No product behavior change.",
            "product_implications": "Routine internal change.",
            "evidence": "target.txt: 'internal refactor only'",
        },
    )
    check(
        "verdict split: normalizer preserves complete-diff independent facts",
        independent == INDEPENDENT_A,
    )


# --------------------------------------------------------------------------- #
# blast-radius caps at the exact boundaries
# --------------------------------------------------------------------------- #
def test_blast_radius_boundaries():
    ok, _ = am.blast_radius_ok(20, 500, 500)
    check("blast: 20 files / 1000 lines EXACTLY -> ok", ok is True)
    ok, _ = am.blast_radius_ok(21, 1, 1)
    check("blast: 21 files -> held", ok is False)
    ok, _ = am.blast_radius_ok(5, 600, 401)
    check("blast: 1001 total lines -> held", ok is False)
    ok, _ = am.blast_radius_ok(1, 1000, 0)
    check("blast: 1000 additions + 0 deletions -> ok", ok is True)
    ok, _ = am.blast_radius_ok(None, 1, 1)
    check("blast: missing file count -> held (fail-closed)", ok is False)


# --------------------------------------------------------------------------- #
# end-to-end PASS path (A/B/C) via act_on_scan
# --------------------------------------------------------------------------- #
def test_happy_path_class_A_merges():
    w, items, cards = default_world()
    payload, err = run_act(w, items, cards)
    check("act: class A merge-ready PR is merged", len(payload["merges"]) == 1)
    check("act: do_merge was called once", len(w.do_merge_calls) == 1)
    m = payload["merges"][0]
    check("act: merge record carries behavior class", m["behavior_class"] == "A")
    check("act: merge record carries contributor proof", bool(m["contributor_proof"]))
    check(
        "act: merge record carries head + vision + commit",
        m["head_sha"] and m["vision_sha"] == "vsha" and m["merge_commit"],
    )
    check(
        "act: do_merge receives the reviewed base SHA",
        w.do_merge_calls[0][-1] == "b1" * 20,
    )
    check(
        "act: do_merge enables its final CLEAN guard",
        w.do_merge_clean_guards == [True],
    )
    check(
        "act: do_merge receives the final auto-merge guard",
        w.do_merge_final_guards == [(True, "")],
    )
    check("act: ::notice:: audit line emitted", "auto-merge merged" in err)


def test_happy_path_classes_B_and_C():
    for label, v in (("B", ELIGIBLE_B), ("C", ELIGIBLE_C)):
        w, items, cards = default_world(verdict=v)
        payload, _ = run_act(w, items, cards)
        check(
            "act: class %s PR merges" % label,
            len(payload["merges"]) == 1
            and payload["merges"][0]["behavior_class"] == label,
        )


def test_reconcile_absence_state_cannot_change_auto_merge_eligibility():
    w, items, cards = default_world()
    state = core.parse_state_block(cards[0]["body"])
    state[render_card.RECONCILE_ABSENCE_FIELD] = {
        "version": 1,
        "threshold": 2,
        "count": 1,
    }
    cards[0]["body"] = render_card._replace_state_block(cards[0]["body"], state)
    payload, _ = run_act(w, items, cards)
    check(
        "act: reconcile absence is non-authoritative and eligibility is unchanged",
        len(payload["merges"]) == 1 and len(w.do_merge_calls) == 1,
    )


def test_class_C_without_optin_holds_end_to_end():
    w, items, cards = default_world(verdict=dict(ELIGIBLE_A, behavior_class="C"))
    payload, err = run_act(w, items, cards)
    check("act: class C w/o opt-in holds", not payload["merges"] and payload["holds"])
    check("act: no merge attempted", not w.do_merge_calls)


def test_same_closing_issue_overlap_holds_end_to_end():
    w, items, cards = default_world()
    items[0]["same_closing_issue_overlap"] = "overlaps PR(s) #6 (all close issue #42)"
    payload, _ = run_act(w, items, cards)
    check(
        "act: same-closing-issue ambiguity holds",
        not payload["merges"] and bool(payload["holds"]),
    )
    check("act: same-closing-issue ambiguity never merges", not w.do_merge_calls)


def test_no_same_closing_issue_overlap_still_merges():
    w, items, cards = default_world()
    payload, _ = run_act(w, items, cards)
    check(
        "act: proven absence of same-closing-issue overlap still merges",
        len(payload["merges"]) == 1,
    )
    check(
        "act: final same-closing-issue guard passes on a complete clear read",
        w.do_merge_final_guards == [(True, "")],
    )


def test_unknown_scan_same_closing_issue_overlap_holds_fail_closed():
    for label, value in (
        ("missing", None),
        ("null", None),
        ("boolean", False),
        ("object", {}),
    ):
        w, items, cards = default_world()
        if label == "missing":
            items[0].pop("same_closing_issue_overlap")
        else:
            items[0]["same_closing_issue_overlap"] = value
        payload, _ = run_act(w, items, cards)
        check(
            "act: %s scan overlap evidence holds fail closed" % label,
            not payload["merges"]
            and "unavailable" in _held_reason(payload)
            and not w.do_merge_calls,
        )


def test_G7_same_closing_issue_overlap_is_rechecked_at_merge_boundary():
    w, items, cards = default_world()
    w.closing_issue_overlap[("owner", "fmt", "5")] = (
        True,
        "overlaps PR(s) #6 (all close issue #42)",
    )
    payload, _ = run_act(w, items, cards)
    check(
        "G7: overlap appearing after the clear scan is held",
        not payload["merges"]
        and "same-closing-issue ambiguity" in _held_reason(payload),
    )
    check(
        "G7: overlap is checked inside the final do_merge guard",
        w.do_merge_final_guards
        == [
            (
                False,
                "same-closing-issue ambiguity: overlaps PR(s) #6 (all close issue #42)",
            )
        ],
    )


def test_G7_unreadable_same_closing_issue_overlap_holds_fail_closed():
    w, items, cards = default_world()
    w.closing_issue_overlap[("owner", "fmt", "5")] = (False, "")
    payload, _ = run_act(w, items, cards)
    check(
        "G7: unreadable overlap re-read is held",
        not payload["merges"] and "could not be re-read" in _held_reason(payload),
    )
    check(
        "G7: unreadable overlap re-read fails the final guard",
        w.do_merge_final_guards
        == [(False, "same-closing-issue overlap could not be re-read")],
    )


def test_G7_malformed_later_closing_ref_page_holds_fail_closed():
    cases = (
        (
            "malformed",
            {
                "totalCount": 2,
                "pageInfo": {},
                "nodes": [{"number": 43}],
            },
        ),
        (
            "raced",
            {
                "totalCount": 3,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"number": 43}],
            },
        ),
    )
    original_overlap = core.same_closing_issue_overlap
    saved_open = core.gh_graphql_open_pr_closing_refs_page
    saved_closing = core.gh_graphql_closing_refs_page
    try:
        for label, second_closing_page in cases:
            w, items, cards = default_world()
            w.same_closing_issue_overlap = lambda owner, repo, number: original_overlap(
                owner, repo, number
            )

            def open_pr_page(owner, repo, after=None):
                return {
                    "totalCount": 1,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "number": 5,
                            "closingIssuesReferences": {
                                "totalCount": 2,
                                "pageInfo": {
                                    "hasNextPage": True,
                                    "endCursor": "close-2",
                                },
                                "nodes": [{"number": 42}],
                            },
                        }
                    ],
                }

            core.gh_graphql_open_pr_closing_refs_page = open_pr_page
            core.gh_graphql_closing_refs_page = (
                lambda owner, repo, number, after: second_closing_page
            )
            payload, _ = run_act(w, items, cards)
            check(
                "G7: %s later closing-ref page holds fail closed" % label,
                not payload["merges"]
                and "could not be re-read" in _held_reason(payload),
            )
            check(
                "G7: %s later closing-ref page never clears" % label,
                w.do_merge_final_guards
                == [(False, "same-closing-issue overlap could not be re-read")],
            )
    finally:
        core.gh_graphql_open_pr_closing_refs_page = saved_open
        core.gh_graphql_closing_refs_page = saved_closing


def test_G7_malformed_open_pr_first_page_nodes_hold_fail_closed():
    cases = (
        ("missing", object()),
        ("null", None),
        ("object", {}),
        ("string", "not-a-list"),
    )
    original_overlap = core.same_closing_issue_overlap
    saved_open = core.gh_graphql_open_pr_closing_refs_page
    try:
        for label, value in cases:
            w, items, cards = default_world()
            w.same_closing_issue_overlap = lambda owner, repo, number: original_overlap(
                owner, repo, number
            )

            def open_pr_page(owner, repo, after=None):
                if after is None:
                    page = {
                        "totalCount": 1,
                        "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                    }
                    if label != "missing":
                        page["nodes"] = value
                    return page
                return {
                    "totalCount": 1,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "number": 5,
                            "closingIssuesReferences": {
                                "totalCount": 1,
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                                "nodes": [{"number": 42}],
                            },
                        }
                    ],
                }

            core.gh_graphql_open_pr_closing_refs_page = open_pr_page
            complete, note = original_overlap("owner", "fmt", 5)
            check(
                "G7 read: %s first-page nodes fail closed" % label,
                complete is False and note == "",
            )
            payload, _ = run_act(w, items, cards)
            check(
                "G7: %s first-page nodes hold fail closed" % label,
                not payload["merges"]
                and "could not be re-read" in _held_reason(payload),
            )
            check(
                "G7: %s first-page nodes never clear" % label,
                w.do_merge_final_guards
                == [(False, "same-closing-issue overlap could not be re-read")],
            )
    finally:
        core.gh_graphql_open_pr_closing_refs_page = saved_open


def test_live_same_closing_issue_overlap_reuses_scan_note_computation():
    def pr(number, closes):
        return {
            "number": number,
            "closingIssuesReferences": {
                "totalCount": len(closes),
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"number": issue} for issue in closes],
            },
        }

    saved = core._open_pr_closing_refs
    try:
        core._open_pr_closing_refs = lambda owner, repo: [pr(5, [42]), pr(6, [42])]
        complete, note = core.same_closing_issue_overlap("owner", "fmt", 5)
        check(
            "G7 read: live overlap uses the existing presentation note",
            complete and note == "overlaps PR(s) #6 (all close issue #42)",
        )

        core._open_pr_closing_refs = lambda owner, repo: [pr(5, [42]), pr(6, [43])]
        complete, note = core.same_closing_issue_overlap("owner", "fmt", 5)
        check("G7 read: a complete non-overlap is clear", complete and note == "")

        def unreadable(owner, repo):
            raise RuntimeError("unreadable")

        core._open_pr_closing_refs = unreadable
        complete, note = core.same_closing_issue_overlap("owner", "fmt", 5)
        check(
            "G7 read: unreadable open-PR evidence fails closed",
            complete is False and note == "",
        )
    finally:
        core._open_pr_closing_refs = saved


def test_live_same_closing_issue_overlap_open_pr_pagination_is_strict():
    def refs(number, issue):
        return {
            "number": number,
            "closingIssuesReferences": {
                "totalCount": 1,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"number": issue}],
            },
        }

    first = {
        "totalCount": 2,
        "pageInfo": {"hasNextPage": True, "endCursor": "next"},
        "nodes": [refs(5, 42)],
    }
    second = {
        "totalCount": 2,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [refs(6, 42)],
    }
    calls = []
    saved = core.gh_graphql_open_pr_closing_refs_page
    try:

        def page(owner, repo, after=None):
            calls.append(after)
            return first if after is None else second

        core.gh_graphql_open_pr_closing_refs_page = page
        complete, note = core.same_closing_issue_overlap("owner", "fmt", 5)
        check(
            "G7 read: every open-PR page participates in overlap detection",
            complete
            and note == "overlaps PR(s) #6 (all close issue #42)"
            and calls == [None, "next"],
        )

        second["totalCount"] = 3
        calls.clear()
        complete, note = core.same_closing_issue_overlap("owner", "fmt", 5)
        check(
            "G7 read: changed pagination totals fail closed",
            complete is False and note == "" and calls == [None, "next"],
        )
    finally:
        core.gh_graphql_open_pr_closing_refs_page = saved


# --------------------------------------------------------------------------- #
# each deterministic gate holds end-to-end (fail-closed)
# --------------------------------------------------------------------------- #
def _held_reason(payload):
    return payload["holds"][0]["hold_reason"] if payload["holds"] else ""


def test_G0_repo_not_opted_in_is_silently_skipped():
    # A non-opted-in repo is NOT an auto-merge candidate: silent skip (no merge,
    # no hold entry, no ::warning:: spam), not a logged hold.
    w, items, cards = default_world()
    w.repos = {"fmt": {"auto_merge": False}}
    payload, err = run_act(w, items, cards)
    check(
        "G0: per-repo auto_merge off -> silent skip",
        not payload["merges"] and not payload["holds"],
    )
    check("G0: per-repo off -> no merge", not w.do_merge_calls)
    check("G0: per-repo off -> no warning spam", "auto-merge held" not in err)
    # But evaluate_candidate still fails closed on G0a as defense in depth.
    r = am.evaluate_candidate(
        "owner",
        items[0],
        {"issue": 1, "state": {}, "labels": {"needs-decision"}},
        {"auto_merge": False},
        False,
        set(),
    )
    check("G0: evaluate_candidate G0a defense-in-depth", "G0" in r["hold_reason"])


def test_G0_global_off_is_silently_skipped():
    w, items, cards = default_world()
    w.global_auto_merge = False
    w.repos = {"fmt": {}}  # no per-repo override -> global (off)
    payload, _ = run_act(w, items, cards)
    check(
        "G0: global off + no override -> silent skip",
        not payload["merges"] and not payload["holds"] and not w.do_merge_calls,
    )


def test_G0_no_vision_holds():
    w, items, cards = default_world()
    w.vision = {}  # no VISION.md on default branch
    payload, _ = run_act(w, items, cards)
    check("G0: missing VISION.md holds", "VISION.md" in _held_reason(payload))
    check("G0: missing VISION.md -> no merge", not w.do_merge_calls)


def test_G3_non_returning_contributor_holds():
    w, items, cards = default_world()
    w.merged_authors = {}  # author has no prior merged PR
    payload, _ = run_act(w, items, cards)
    check("G3: no prior merged PR holds", "G3" in _held_reason(payload))


def test_G3_bot_and_maintainer_author_hold():
    w, items, cards = default_world(head="hb" * 20)
    slug = "owner/fmt"
    w.set_pr(slug, 5, make_pr(head="hb" * 20, author="dependabot[bot]"))
    w.merged_authors = {(slug, "dependabot[bot]"): True}
    payload, _ = run_act(w, items, cards)
    check("G3: bot author holds", "G3" in _held_reason(payload))

    w_type, items_type, cards_type = default_world(head="bt" * 20)
    w_type.set_pr(
        slug,
        5,
        make_pr(head="bt" * 20, author="automation", author_type="Bot"),
    )
    w_type.merged_authors = {(slug, "automation"): True}
    payload_type, _ = run_act(w_type, items_type, cards_type)
    check("G3: REST Bot type author holds", "G3" in _held_reason(payload_type))

    w2, items2, cards2 = default_world(head="hm" * 20)
    w2.set_pr("owner/fmt", 5, make_pr(head="hm" * 20, author="owner"))
    w2.merged_authors = {("owner/fmt", "owner"): True}
    payload2, _ = run_act(w2, items2, cards2)
    check("G3: maintainer author holds", "G3" in _held_reason(payload2))


def test_G2_excluded_file_holds():
    w, items, cards = default_world()
    w.files[("owner/fmt", "5")] = ([".github/workflows/ci.yml", "src/a.py"], True, True)
    payload, _ = run_act(w, items, cards)
    check(
        "G2: workflow file holds",
        "G2" in _held_reason(payload) and "excluded" in _held_reason(payload),
    )
    check("G2: excluded file -> no merge", not w.do_merge_calls)


def test_G2_vision_self_authorization_excluded():
    w, items, cards = default_world()
    # A PR that edits the very rubric it is judged against must never auto-merge.
    w.files[("owner/fmt", "5")] = (["VISION.md", "src/a.py"], True, True)
    payload, _ = run_act(w, items, cards)
    check(
        "G2: PR editing VISION.md is held (self-authorization guard)",
        "G2" in _held_reason(payload),
    )


def test_G2_unreadable_file_list_fails_closed():
    w, items, cards = default_world()
    w.files[("owner/fmt", "5")] = ([], False, False)  # gh could not list files
    payload, _ = run_act(w, items, cards)
    check("G2: unreadable file list holds", "G2" in _held_reason(payload))


def test_G2_uses_complete_immutable_base_head_comparison():
    saved = core.gh_rest
    calls = []
    base = "a" * 40
    head = "b" * 40

    def gh_rest(path, **kwargs):
        calls.append(path)
        return {"files": [{"filename": "src/safe.py"}]}

    core.gh_rest = gh_rest
    try:
        files, ok, complete = am.immutable_compare_files("owner/fmt", base, head, 1)
        check(
            "G2: changed paths use the immutable base...head comparison",
            files == ["src/safe.py"]
            and ok
            and complete
            and calls == ["/repos/owner/fmt/compare/%s...%s" % (base, head)],
        )
        _, ok, complete = am.immutable_compare_files("owner/fmt", base, head, 2)
        check("G2: incomplete immutable file list holds", ok and not complete)
        _, ok, complete = am.immutable_compare_files("owner/fmt", "not-a-sha", head, 1)
        check("G2: invalid immutable comparison inputs hold", not ok and not complete)
    finally:
        core.gh_rest = saved


def test_G2_immutable_comparison_checks_renamed_old_path():
    saved = core.gh_rest
    base = "a" * 40
    head = "b" * 40

    def gh_rest(path, **kwargs):
        return {
            "files": [
                {
                    "filename": "src/util.py",
                    "previous_filename": "src/auth.py",
                }
            ]
        }

    core.gh_rest = gh_rest
    try:
        files, ok, complete = am.immutable_compare_files("owner/fmt", base, head, 1)
        check(
            "G2: renamed paths include both the old and new filename",
            files == ["src/util.py", "src/auth.py"] and ok and complete,
        )
        check(
            "G2: a rename from an excluded old path holds",
            any(
                hit.startswith("authentication:src/auth.py")
                for hit in core._auto_merge_exclusions(files)
            ),
        )
    finally:
        core.gh_rest = saved


def test_G2_missing_live_base_sha_holds():
    w, items, cards = default_world(head="ba" * 20)
    w.set_pr("owner/fmt", 5, make_pr(head="ba" * 20, base=""))
    payload, _ = run_act(w, items, cards)
    check("G2: missing live base SHA holds", "G2" in _held_reason(payload))


def test_G4_not_mergeable_or_not_clean_holds():
    for label, pr in (
        ("mergeable false", make_pr(head="h4" * 20, mergeable=False)),
        ("state blocked", make_pr(head="h4" * 20, mergeable_state="blocked")),
        ("state behind", make_pr(head="h4" * 20, mergeable_state="behind")),
        (
            "state unknown",
            make_pr(head="h4" * 20, mergeable=None, mergeable_state="unknown"),
        ),
    ):
        w, items, cards = default_world(head="h4" * 20)
        w.set_pr("owner/fmt", 5, pr)
        payload, _ = run_act(w, items, cards)
        check("G4: %s holds" % label, "G4" in _held_reason(payload))
        check("G4: %s -> no merge" % label, not w.do_merge_calls)


def test_G5_blast_radius_holds_and_boundary_merges():
    w, items, cards = default_world(head="h5" * 20)
    w.set_pr("owner/fmt", 5, make_pr(head="h5" * 20, changed_files=21))
    payload, _ = run_act(w, items, cards)
    check("G5: 21 files holds", "G5" in _held_reason(payload))

    w2, items2, cards2 = default_world(head="h6" * 20)
    w2.set_pr("owner/fmt", 5, make_pr(head="h6" * 20, additions=600, deletions=401))
    payload2, _ = run_act(w2, items2, cards2)
    check("G5: 1001 lines holds", "G5" in _held_reason(payload2))

    # Exact boundary: 20 files + 1000 lines merges.
    w3, items3, cards3 = default_world(head="h7" * 20)
    w3.set_pr(
        "owner/fmt",
        5,
        make_pr(head="h7" * 20, changed_files=20, additions=500, deletions=500),
    )
    payload3, _ = run_act(w3, items3, cards3)
    check("G5: 20 files / 1000 lines boundary merges", len(payload3["merges"]) == 1)


def test_G6_stale_absent_and_held_verdict_hold():
    # Stale: card triaged_sha != current head.
    w, items, cards = default_world(head="cur" * 13 + "x")
    cards[0] = make_card(101, "fmt", 5, "old" * 13 + "y", automerge_verdict=ELIGIBLE_A)
    # item/live head is the current one; card is for the old one.
    payload, _ = run_act(w, items, cards)
    check("G6: stale verdict (head mismatch) holds", "G6" in _held_reason(payload))

    # Issue 621 shape: ordinary triage succeeded for the current head and its
    # top-level recommendation is merge, but the default branch has no VISION.md,
    # so triage did not produce a structured behavior verdict.
    w2, items2, cards2 = default_world(head="ab" * 20)
    w2.vision = {}
    cards2[0] = make_card(101, "fmt", 5, "ab" * 20, automerge_verdict=None)
    payload2, _ = run_act(w2, items2, cards2)
    check(
        "G6: issue 621 missing verdict keeps the first decisive hold reason",
        _held_reason(payload2) == "G6 no structured behavior verdict",
    )
    check(
        "G6: issue 621 missing verdict never calls merge",
        not payload2["merges"] and not w2.do_merge_calls,
    )

    # triage_status not succeeded.
    w3, items3, cards3 = default_world(head="qd" * 20)
    cards3[0] = make_card(
        101, "fmt", 5, "qd" * 20, triage_status="queued", automerge_verdict=ELIGIBLE_A
    )
    payload3, _ = run_act(w3, items3, cards3)
    check("G6: queued (not succeeded) verdict holds", "G6" in _held_reason(payload3))

    # Held card (auto-triage not published) never auto-merges.
    w4, items4, cards4 = default_world(head="he" * 20)
    cards4[0] = make_card(
        101, "fmt", 5, "he" * 20, held=True, automerge_verdict=ELIGIBLE_A
    )
    payload4, _ = run_act(w4, items4, cards4)
    check("G6: held card holds", "held" in _held_reason(payload4).lower())

    w5, items5, cards5 = default_world(head="rm" * 20)
    cards5[0] = make_card(
        101,
        "fmt",
        5,
        "rm" * 20,
        automerge_verdict=ELIGIBLE_A,
        triage_recommendation="hold",
    )
    payload5, _ = run_act(w5, items5, cards5)
    check(
        "G6: non-merge triage recommendation contradicts verdict and holds",
        "top-level triage recommendation" in _held_reason(payload5),
    )


def test_G1_no_card_holds():
    w, items, cards = default_world()
    payload, _ = run_act(w, items, [])  # no card at all
    check("G1: no decision card holds", "G1" in _held_reason(payload))


def test_G1_non_pure_card_holds():
    # A card already mid-decision (processing/resolved/blocked) must never race
    # the manual/decision-handler path.
    for lbl in ("processing", "resolved", "blocked"):
        w, items, cards = default_world(head="pu" * 20)
        cards[0] = make_card(
            101,
            "fmt",
            5,
            "pu" * 20,
            automerge_verdict=ELIGIBLE_A,
            labels=["needs-decision", lbl],
        )
        payload, _ = run_act(w, items, cards)
        check("G1: %s card is not pure -> holds" % lbl, "G1" in _held_reason(payload))
        check("G1: %s card -> no merge" % lbl, not w.do_merge_calls)


def test_G1_rejects_untrusted_or_unmanaged_card():
    w, items, cards = default_world()
    cards[0]["author"] = "contributor"
    payload, _ = run_act(w, items, cards)
    check("G1: contributor-authored forged card holds", not payload["merges"])

    w2, items2, cards2 = default_world()
    cards2[0]["labels"] = [{"name": "needs-decision"}]
    payload2, _ = run_act(w2, items2, cards2)
    check("G1: card without managed target labels holds", not payload2["merges"])


def test_claim_author_duality_normalizes_only_github_actions():
    # Fleet-wide bug (report §5.1): the claim/validate/recover path re-reads each
    # card LIVE via render_card.get_card (`gh issue view --json ...,author`), whose
    # `author.login` is the GraphQL bot-actor spelling "app/github-actions", while
    # cards.json is built from the REST issues API whose `.user.login` is
    # "github-actions[bot]". The equality check against CARD_AUTOMATION_AUTHOR
    # therefore rejected EVERY live-re-read card, so no card was ever claimed and
    # auto-merge never merged anything. These fixtures are real get_card-shaped
    # (author is a dict `{"login": ...}`, exactly as `gh issue view --json author`
    # returns) and everything but the author login is held constant, so trust
    # turns solely on the author normalization at the comparison boundary.
    def card_with_author(login):
        card = make_card(
            101,
            "fmt",
            5,
            "cd" * 20,
            automerge_verdict=ELIGIBLE_A,
            labels=[
                "needs-decision",
                "repo:fmt",
                "kind:pr-review",
                "priority:med",
                "target:fmt-5",
            ],
        )
        card["author"] = {"login": login}  # real `gh issue view --json author` shape
        return card

    def is_trusted(login):
        return ("fmt", "5") in am._card_index([card_with_author(login)])

    # The one documented duality: the live GraphQL spelling is accepted, as is the
    # REST spelling cards.json already carries.
    check(
        "claim: live 'app/github-actions' get_card author normalizes and is trusted",
        is_trusted("app/github-actions"),
    )
    check(
        "claim: canonical 'github-actions[bot]' author stays trusted",
        is_trusted("github-actions[bot]"),
    )

    # Fail-closed everywhere else: no prefix stripping, no case folding, no
    # allowlist growth. Every other identity - a human, a different app slug, the
    # bare form, and lookalikes - is still rejected.
    for login in (
        "contributor",  # a human login
        "app/other-bot",  # a different app slug (prefix must NOT be stripped)
        "github-actions",  # bare form (no [bot] suffix)
        "app/github-actions-bot",  # lookalike suffix
        "App/GitHub-Actions",  # case-folded lookalike (no case folding)
        "github-actions[bot]-evil",  # canonical-prefixed lookalike
        "xapp/github-actions",  # lookalike prefix
    ):
        check(
            "claim: untrusted author %r stays rejected (fail-closed)" % login,
            not is_trusted(login),
        )

    # And the normalizer itself collapses ONLY the exact duality string.
    check(
        "claim: _canonical_card_author maps exactly the duality string",
        am._canonical_card_author("app/github-actions") == am.CARD_AUTOMATION_AUTHOR,
    )
    for login in (
        "contributor",
        "app/other-bot",
        "github-actions",
        "App/GitHub-Actions",
    ):
        check(
            "claim: _canonical_card_author leaves %r unchanged" % login,
            am._canonical_card_author(login) == login,
        )


def test_claim_rechecks_and_locks_current_card():
    w, items, cards = default_world()
    initial = make_card(
        101,
        "fmt",
        5,
        items[0]["head_sha"],
        automerge_verdict=ELIGIBLE_A,
        labels=[
            "needs-decision",
            "repo:fmt",
            "kind:pr-review",
            "priority:med",
            "target:fmt-5",
        ],
    )
    current = dict(initial)
    initial["comments"] = 0
    current["comments"] = []
    current["state"] = "OPEN"
    calls = []
    token = os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN")
    saved = {
        "get": render_card.get_card,
        "ensure": render_card.ensure_labels,
        "gh": render_card._gh,
        "cfg": core.load_config,
        "owner": core.get_owner,
        "maint": core.maintainers,
    }
    core.load_config = lambda: {
        "auto_merge": True,
        "repos": {"fmt": {"auto_merge": True}},
    }
    core.get_owner = lambda: "owner"
    core.maintainers = lambda: {"owner"}
    render_card.get_card = lambda number: current
    render_card.ensure_labels = lambda labels: calls.append(("ensure", labels))
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true"

    def claim(args, check=False):
        calls.append(("edit", args))
        current["labels"] += [
            {"name": "processing"},
            {"name": am.AUTO_MERGE_CLAIM_LABEL},
        ]
        return type("R", (), {"returncode": 0})()

    render_card._gh = claim
    scan = {"items": items}
    try:
        claims = am.claim_cards(scan, [initial])
        check(
            "claim: current card is re-read and locked",
            len(claims) == 1
            and "processing" in am._card_label_names(claims[0])
            and am.AUTO_MERGE_CLAIM_LABEL in am._card_label_names(claims[0]),
        )
    finally:
        render_card.get_card = saved["get"]
        render_card.ensure_labels = saved["ensure"]
        render_card._gh = saved["gh"]
        core.load_config = saved["cfg"]
        core.get_owner = saved["owner"]
        core.maintainers = saved["maint"]
        if token is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = token


def test_claim_vetoes_owner_comment_since_scan_snapshot():
    _, items, _ = default_world()
    initial = make_card(
        101,
        "fmt",
        5,
        items[0]["head_sha"],
        automerge_verdict=ELIGIBLE_A,
        labels=[
            "needs-decision",
            "repo:fmt",
            "kind:pr-review",
            "priority:med",
            "target:fmt-5",
        ],
    )
    initial["comments"] = 0
    current = dict(
        initial,
        state="OPEN",
        comments=[{"author": {"login": "owner"}, "body": "/hold"}],
    )
    calls = []
    saved = {
        "get": render_card.get_card,
        "ensure": render_card.ensure_labels,
        "gh": render_card._gh,
        "cfg": core.load_config,
    }
    token = os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN")
    core.load_config = lambda: {
        "auto_merge": True,
        "repos": {"fmt": {"auto_merge": True}},
    }
    render_card.get_card = lambda number: current
    render_card.ensure_labels = lambda labels: calls.append(("ensure", labels))
    render_card._gh = lambda *args, **kwargs: calls.append(("edit", args))
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true"
    try:
        claims = am.claim_cards({"items": items}, [initial])
        check(
            "claim: owner comment since scan snapshot vetoes the claim",
            claims == [] and calls == [],
        )
    finally:
        render_card.get_card = saved["get"]
        render_card.ensure_labels = saved["ensure"]
        render_card._gh = saved["gh"]
        core.load_config = saved["cfg"]
        if token is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = token


def test_claim_vetoes_existing_trusted_hold_or_close_comment():
    _, items, _ = default_world()
    initial = make_card(
        101,
        "fmt",
        5,
        items[0]["head_sha"],
        automerge_verdict=ELIGIBLE_A,
        labels=[
            "needs-decision",
            "repo:fmt",
            "kind:pr-review",
            "priority:med",
            "target:fmt-5",
        ],
    )
    initial["comments"] = [
        {"author": {"login": "owner"}, "body": "Please hold this PR."}
    ]
    current = dict(initial, state="OPEN")
    calls = []
    saved = {
        "get": render_card.get_card,
        "ensure": render_card.ensure_labels,
        "gh": render_card._gh,
        "cfg": core.load_config,
        "owner": core.get_owner,
        "maint": core.maintainers,
    }
    token = os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN")
    core.load_config = lambda: {
        "auto_merge": True,
        "repos": {"fmt": {"auto_merge": True}},
    }
    core.get_owner = lambda: "owner"
    core.maintainers = lambda: {"owner"}
    render_card.get_card = lambda number: current
    render_card.ensure_labels = lambda labels: calls.append(("ensure", labels))
    render_card._gh = lambda *args, **kwargs: calls.append(("edit", args))
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true"
    try:
        slash_veto, _ = am._card_has_pending_owner_action(
            dict(
                current,
                comments=[{"author": {"login": "owner"}, "body": "/close"}],
            )
        )
        natural_veto, _ = am._card_has_pending_owner_action(current)
        claims = am.claim_cards({"items": items}, [initial])
        check(
            "claim: existing trusted slash and natural-language actions veto the claim",
            slash_veto and natural_veto and claims == [] and calls == [],
        )
    finally:
        render_card.get_card = saved["get"]
        render_card.ensure_labels = saved["ensure"]
        render_card._gh = saved["gh"]
        core.load_config = saved["cfg"]
        core.get_owner = saved["owner"]
        core.maintainers = saved["maint"]
        if token is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = token


def test_claim_rejects_changed_decision_card():
    w, items, cards = default_world()
    initial = make_card(
        101,
        "fmt",
        5,
        items[0]["head_sha"],
        automerge_verdict=ELIGIBLE_A,
        labels=[
            "needs-decision",
            "repo:fmt",
            "kind:pr-review",
            "priority:med",
            "target:fmt-5",
        ],
    )
    current = dict(initial)
    current["state"] = "OPEN"
    current["body"] += "\n- [x] Hold <!-- opt:hold -->"
    saved = {"get": render_card.get_card, "cfg": core.load_config}
    token = os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN")
    core.load_config = lambda: {
        "auto_merge": True,
        "repos": {"fmt": {"auto_merge": True}},
    }
    render_card.get_card = lambda number: current
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true"
    try:
        claims = am.claim_cards({"items": items}, [initial])
        check("claim: changed decision card is not claimed", claims == [])
    finally:
        render_card.get_card = saved["get"]
        core.load_config = saved["cfg"]
        if token is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = token


def test_claim_rechecks_owner_selection_after_locking():
    w, items, _ = default_world()
    initial = make_card(
        101,
        "fmt",
        5,
        items[0]["head_sha"],
        automerge_verdict=ELIGIBLE_A,
        labels=[
            "needs-decision",
            "repo:fmt",
            "kind:pr-review",
            "priority:med",
            "target:fmt-5",
        ],
    )
    current = dict(initial, state="OPEN")
    saved = {
        "get": render_card.get_card,
        "ensure": render_card.ensure_labels,
        "gh": render_card._gh,
        "cfg": core.load_config,
    }
    token = os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN")
    core.load_config = lambda: {
        "auto_merge": True,
        "repos": {"fmt": {"auto_merge": True}},
    }
    render_card.get_card = lambda number: current
    render_card.ensure_labels = lambda labels: None

    def edit(args, check=False):
        if "--add-label" in args:
            current["labels"] += [
                {"name": "processing"},
                {"name": am.AUTO_MERGE_CLAIM_LABEL},
            ]
            current["body"] += "\n- [x] Hold <!-- opt:hold -->"
        elif "--remove-label" in args:
            remove = {
                args[i + 1]
                for i, arg in enumerate(args[:-1])
                if arg == "--remove-label"
            }
            current["labels"] = [
                label for label in current["labels"] if label.get("name") not in remove
            ]
        return type("R", (), {"returncode": 0, "stderr": ""})()

    render_card._gh = edit
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true"
    try:
        claims = am.claim_cards({"items": items}, [initial])
        labels = am._card_label_names(current)
        check(
            "claim: post-lock owner selection cancels the claim",
            claims == []
            and "processing" not in labels
            and am.AUTO_MERGE_CLAIM_LABEL not in labels,
        )
    finally:
        render_card.get_card = saved["get"]
        render_card.ensure_labels = saved["ensure"]
        render_card._gh = saved["gh"]
        core.load_config = saved["cfg"]
        if token is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = token


def test_claim_rechecks_owner_comment_activity_after_locking():
    _, items, _ = default_world()
    initial = make_card(
        101,
        "fmt",
        5,
        items[0]["head_sha"],
        automerge_verdict=ELIGIBLE_A,
        labels=[
            "needs-decision",
            "repo:fmt",
            "kind:pr-review",
            "priority:med",
            "target:fmt-5",
        ],
    )
    current = dict(initial, state="OPEN")
    saved = {
        "get": render_card.get_card,
        "ensure": render_card.ensure_labels,
        "gh": render_card._gh,
        "cfg": core.load_config,
    }
    token = os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN")
    core.load_config = lambda: {
        "auto_merge": True,
        "repos": {"fmt": {"auto_merge": True}},
    }
    render_card.get_card = lambda number: current
    render_card.ensure_labels = lambda labels: None

    def edit(args, check=False):
        if "--add-label" in args:
            current["labels"] += [
                {"name": "processing"},
                {"name": am.AUTO_MERGE_CLAIM_LABEL},
            ]
            current["comments"] = [{"id": "owner-hold"}]
        elif "--remove-label" in args:
            remove = {
                args[i + 1]
                for i, arg in enumerate(args[:-1])
                if arg == "--remove-label"
            }
            current["labels"] = [
                label for label in current["labels"] if label.get("name") not in remove
            ]
        return type("R", (), {"returncode": 0, "stderr": ""})()

    render_card._gh = edit
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true"
    try:
        claims = am.claim_cards({"items": items}, [initial])
        labels = am._card_label_names(current)
        check(
            "claim: post-lock owner comment cancels the claim",
            claims == []
            and "processing" not in labels
            and am.AUTO_MERGE_CLAIM_LABEL not in labels,
        )
    finally:
        render_card.get_card = saved["get"]
        render_card.ensure_labels = saved["ensure"]
        render_card._gh = saved["gh"]
        core.load_config = saved["cfg"]
        if token is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = token


def test_claim_requires_fresh_verdict_before_locking_card():
    _, items, _ = default_world()
    initial = make_card(
        101,
        "fmt",
        5,
        items[0]["head_sha"],
        automerge_verdict=None,
        labels=[
            "needs-decision",
            "repo:fmt",
            "kind:pr-review",
            "priority:med",
            "target:fmt-5",
        ],
    )
    current = dict(initial, state="OPEN")
    calls = []
    saved = {
        "get": render_card.get_card,
        "ensure": render_card.ensure_labels,
        "gh": render_card._gh,
        "cfg": core.load_config,
    }
    token = os.environ.get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN")
    core.load_config = lambda: {
        "auto_merge": True,
        "repos": {"fmt": {"auto_merge": True}},
    }
    render_card.get_card = lambda number: current
    render_card.ensure_labels = lambda labels: calls.append(("ensure", labels))
    render_card._gh = lambda *args, **kwargs: calls.append(("edit", args))
    os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = "true"
    try:
        claims = am.claim_cards({"items": items}, [initial])
        check(
            "claim: a successful triage without a verdict leaves no claim churn",
            claims == [] and calls == [],
        )
    finally:
        render_card.get_card = saved["get"]
        render_card.ensure_labels = saved["ensure"]
        render_card._gh = saved["gh"]
        core.load_config = saved["cfg"]
        if token is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_HAS_TOKEN"] = token


def test_validate_claimed_card_rechecks_owner_selection_before_acting():
    claimed = make_card(101, "fmt", 5, "vc" * 20, automerge_verdict=ELIGIBLE_A)
    current = dict(claimed, state="OPEN")
    current["body"] += "\n- [x] Hold <!-- opt:hold -->"
    saved = {"get": render_card.get_card, "gh": render_card._gh}
    render_card.get_card = lambda number: current

    def edit(args, check=False):
        remove = {
            args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--remove-label"
        }
        current["labels"] = [
            label for label in current["labels"] if label.get("name") not in remove
        ]
        return type("R", (), {"returncode": 0, "stderr": ""})()

    render_card._gh = edit
    try:
        validated = am.validate_claimed_cards([claimed])
        labels = am._card_label_names(current)
        check(
            "validate: a new owner selection releases the claim before acting",
            validated == []
            and "processing" not in labels
            and am.AUTO_MERGE_CLAIM_LABEL not in labels,
        )
    finally:
        render_card.get_card = saved["get"]
        render_card._gh = saved["gh"]


def test_validate_claimed_card_rejects_pending_decisions_and_newer_activity():
    saved = {"get": render_card.get_card, "gh": render_card._gh}
    try:
        for label, current_change in (
            (
                "newer card activity",
                lambda card: card.update({"updatedAt": "2026-07-10T00:01:00Z"}),
            ),
            (
                "same-second card comment",
                lambda card: card.update({"comments": [{"id": "owner-hold"}]}),
            ),
            (
                "decision label",
                lambda card: card["labels"].append({"name": "decision:hold"}),
            ),
        ):
            claimed = make_card(101, "fmt", 5, "vd" * 20, automerge_verdict=ELIGIBLE_A)
            current = dict(claimed, state="OPEN", labels=list(claimed["labels"]))
            current_change(current)
            render_card.get_card = lambda number: current

            def edit(args, check=False):
                remove = {
                    args[i + 1]
                    for i, arg in enumerate(args[:-1])
                    if arg == "--remove-label"
                }
                current["labels"] = [
                    value
                    for value in current["labels"]
                    if value.get("name") not in remove
                ]
                return type("R", (), {"returncode": 0, "stderr": ""})()

            render_card._gh = edit
            validated = am.validate_claimed_cards([claimed])
            labels = am._card_label_names(current)
            check(
                "validate: %s releases the claim" % label,
                validated == []
                and "processing" not in labels
                and am.AUTO_MERGE_CLAIM_LABEL not in labels,
            )
    finally:
        render_card.get_card = saved["get"]
        render_card._gh = saved["gh"]


def test_stale_claim_release_failure_is_recovered_on_next_scan():
    stale = make_card(101, "fmt", 5, "st" * 20, automerge_verdict=ELIGIBLE_A)
    current = dict(stale, state="OPEN")
    saved = {"get": render_card.get_card, "gh": render_card._gh}
    attempts = [0]
    render_card.get_card = lambda number: current

    def edit(args, check=False):
        attempts[0] += 1
        if attempts[0] == 1:
            return type("R", (), {"returncode": 1, "stderr": "transient failure"})()
        remove = {
            args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--remove-label"
        }
        current["labels"] = [
            label for label in current["labels"] if label.get("name") not in remove
        ]
        return type("R", (), {"returncode": 0, "stderr": ""})()

    render_card._gh = edit
    try:
        failed = False
        try:
            am._release_card_claim(101)
        except RuntimeError:
            failed = True
        recovered = am.recover_stale_card_claims([stale])
        labels = am._card_label_names(current)
        check("claim: release failure is surfaced", failed)
        check(
            "claim: next scan releases the stale claim",
            recovered == [101]
            and "processing" not in labels
            and am.AUTO_MERGE_CLAIM_LABEL not in labels,
        )
    finally:
        render_card.get_card = saved["get"]
        render_card._gh = saved["gh"]


# --------------------------------------------------------------------------- #
# G7: live head + merge-state re-check immediately before acting
# --------------------------------------------------------------------------- #
def test_G7_head_moved_before_acting_holds():
    w, items, cards = default_world(head="hh" * 20)
    good = make_pr(head="hh" * 20)
    moved = make_pr(head="ZZ" * 20)  # head advanced between gate read and act
    w.set_pr("owner/fmt", 5, [good, moved])
    payload, _ = run_act(w, items, cards)
    check("G7: head moved before acting holds", not payload["merges"])
    check("G7: head moved -> no merge call", not w.do_merge_calls)


def test_G7_escape_hatch_appears_before_acting_holds():
    w, items, cards = default_world(head="ee" * 20)
    good = make_pr(head="ee" * 20)
    tagged = make_pr(head="ee" * 20, labels=[core.NO_AUTO_MERGE_LABEL])
    w.set_pr("owner/fmt", 5, [good, tagged])
    payload, _ = run_act(w, items, cards)
    check("G7: escape hatch appearing before acting holds", not payload["merges"])


def test_G7_base_changed_before_acting_holds():
    w, items, cards = default_world(
        head="bd" * 20, verdict=dict(ELIGIBLE_A, base_sha="a1" * 20)
    )
    good = make_pr(head="bd" * 20, base="a1" * 20)
    moved = make_pr(head="bd" * 20, base="b2" * 20)
    w.set_pr("owner/fmt", 5, [good, moved])
    payload, _ = run_act(w, items, cards)
    check("G7: base moved before acting holds", not payload["merges"])
    check("G7: base moved -> no merge call", not w.do_merge_calls)


def test_post_merge_missing_endpoint_sha_fails_audit_handoff():
    for bad_sha in ("", "not-a-sha"):
        w, items, cards = default_world(head="ms" * 20)
        w.merge_commits[("fmt", "5")] = bad_sha
        payload, _ = run_act(w, items, cards)
        check(
            "audit: invalid merge endpoint SHA is never recorded",
            not payload["merges"],
        )
        check(
            "audit: invalid merge endpoint SHA retains the recovery intent",
            payload["post_merge_errors"]
            and payload["releases"] == []
            and am._audit_intent_record(core.parse_state_block(cards[0]["body"]), 101),
        )


def test_G7_card_changed_before_acting_holds_and_releases_claim():
    w, items, cards = default_world(head="cc" * 20)
    changed = dict(cards[0])
    changed["body"] += "\n- [x] Hold <!-- opt:hold -->"
    w.card_seq = {"101": [changed]}
    payload, _ = run_act(w, items, cards)
    check("G7: card changed before acting holds", not payload["merges"])
    check("G7: card change before acting does not call merge", not w.do_merge_calls)
    check(
        "G7: card change is queued for default-token claim release",
        payload["releases"] == [{"card_issue": 101}],
    )


def test_G7_card_activity_after_claim_holds_and_releases_claim():
    w, items, cards = default_world(head="ua" * 20)
    changed = dict(cards[0], updatedAt="2026-07-10T00:01:00Z")
    w.card_seq = {"101": [changed]}
    payload, _ = run_act(w, items, cards)
    check("G7: later card activity holds", not payload["merges"])
    check("G7: later card activity does not call merge", not w.do_merge_calls)
    check(
        "G7: later card activity is queued for claim release",
        payload["releases"] == [{"card_issue": 101}],
    )


def test_G7_same_second_card_comment_after_claim_holds_and_releases_claim():
    w, items, cards = default_world(head="uc" * 20)
    changed = dict(cards[0], comments=[{"id": "owner-hold"}])
    w.card_seq = {"101": [changed]}
    payload, _ = run_act(w, items, cards)
    check("G7: same-second card comment holds", not payload["merges"])
    check("G7: same-second card comment does not call merge", not w.do_merge_calls)
    check(
        "G7: same-second card comment is queued for claim release",
        payload["releases"] == [{"card_issue": 101}],
    )


def test_card_reader_includes_comment_activity():
    saved = render_card._gh
    calls = []

    def read(args, check=False):
        calls.append(args)
        return type(
            "R",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "number": 101,
                        "body": "Card",
                        "labels": [],
                        "state": "OPEN",
                        "updatedAt": "2026-07-10T00:00:00Z",
                        "author": {"login": am.CARD_AUTOMATION_AUTHOR},
                        "comments": [{"id": "owner-hold"}],
                    }
                ),
            },
        )()

    render_card._gh = read
    try:
        card = render_card.get_card(101)
        check(
            "claim: card reads include comment activity",
            calls
            and any("comments" in str(arg) for arg in calls[0])
            and card.get("comments") == [{"id": "owner-hold"}],
        )
    finally:
        render_card._gh = saved


def test_card_index_accepts_listing_comment_count():
    listed = make_card(101, "fmt", 5, "co" * 20, automerge_verdict=ELIGIBLE_A)
    listed["comments"] = 2
    indexed = am._card_index([listed]).get(("fmt", "5"))
    check(
        "claim: card-list comment count is preserved for validation",
        indexed is not None and indexed["comment_count"] == 2,
    )


def test_G7_card_read_uses_dedicated_default_token():
    w, items, cards = default_world(head="tk" * 20)
    saved = os.environ.get("GH_TOKEN")
    try:
        os.environ["GH_TOKEN"] = "fleet-token"
        payload, _ = run_act(w, items, cards)
        check(
            "G7: card re-read uses only the dedicated default token",
            bool(payload["merges"])
            and w.card_token_reads == ["card-token"] * 4
            and w.merge_tokens == ["fleet-token"],
        )
    finally:
        if saved is None:
            os.environ.pop("GH_TOKEN", None)
        else:
            os.environ["GH_TOKEN"] = saved


def test_G7_missing_default_card_token_holds():
    w, items, cards = default_world(head="nt" * 20)
    payload, _ = run_act(w, items, cards, has_card_token=False)
    check("G7: missing default card token holds", not payload["merges"])
    check("G7: missing default card token does not merge", not w.do_merge_calls)


def test_G7_rechecks_configured_status_contexts():
    w, items, cards = default_world(head="cs" * 20)
    w.check_status[("owner", "fmt", "5", "cs" * 20)] = (
        False,
        "configured checks are comp=pass tests=fail",
    )
    payload, _ = run_act(w, items, cards)
    check("G7: failed configured check before merge holds", not payload["merges"])
    check("G7: failed configured check does not call merge", not w.do_merge_calls)


def test_G7_final_guard_rechecks_escape_hatch_and_owner_decision():
    head = "fg" * 20
    w, items, cards = default_world(head=head)
    w.final_prs[("owner/fmt", "5")] = make_pr(
        head=head, labels=[core.NO_AUTO_MERGE_LABEL]
    )
    payload, _ = run_act(w, items, cards)
    check(
        "G7: final escape hatch guard holds and releases the claim",
        not payload["merges"]
        and payload["releases"] == [{"card_issue": 101}]
        and w.do_merge_final_guards
        == [(False, "escape hatch label appeared before merging")],
    )

    w, items, cards = default_world(head=head)

    def add_owner_decision():
        cards[0]["labels"].append({"name": "decision:merge"})

    w.before_final_guard = add_owner_decision
    payload, _ = run_act(w, items, cards)
    check(
        "G7: final owner-decision guard holds and releases the claim",
        not payload["merges"]
        and payload["releases"] == [{"card_issue": 101}]
        and w.do_merge_final_guards
        and w.do_merge_final_guards[0][0] is False
        and "pending owner decision" in w.do_merge_final_guards[0][1],
    )


def test_premerge_audit_intent_retains_an_ambiguous_handoff():
    head = "ir" * 20
    w, items, cards = default_world(head=head)
    w.do_merge_returns[("fmt", "5")] = ("merge request interrupted", "error")
    payload, _ = run_act(w, items, cards)
    intent = am._audit_intent_record(core.parse_state_block(cards[0]["body"]), 101)
    check(
        "audit: merge errors retain a durable pre-merge intent",
        not payload["merges"]
        and not payload["releases"]
        and bool(payload["ambiguous_outcomes"])
        and bool(intent),
    )
    w.set_pr(
        "owner/fmt",
        5,
        make_pr(
            head=head,
            merged=True,
            state="closed",
            merge_commit_sha="d" * 40,
        ),
    )
    recovered, _ = run_act(w, [], cards)
    check(
        "audit: later target merge remains held without proof of our merge",
        not recovered["merges"]
        and not recovered["releases"]
        and bool(recovered["ambiguous_outcomes"]),
    )


def test_open_pr_audit_intent_releases_claim_without_remerge():
    head = "op" * 20
    w, items, cards = default_world(head=head)
    state = core.parse_state_block(cards[0]["body"])
    state[am.AUDIT_INTENT_FIELD] = {
        "repo": "fmt",
        "number": "5",
        "card_issue": 101,
        "head_sha": head,
        "merge_commit": "",
        "merged_at": "",
        "contributor": "alice",
        "contributor_proof": "has >=1 prior merged PR in fmt",
        "vision_sha": "vsha",
        "behavior_class": "A",
        "behavior_verdict": ELIGIBLE_A,
        "gates": {},
        "detail": "",
    }
    cards[0]["body"] = render_card._replace_state_block(cards[0]["body"], state)
    w.set_pr(
        "owner/fmt",
        5,
        make_pr(head=head, labels=[core.NO_AUTO_MERGE_LABEL]),
    )
    payload, _ = run_act(w, items, cards)
    check(
        "audit: proven-open target releases its stale audit intent claim",
        payload["releases"] == [{"card_issue": 101}] and not w.do_merge_calls,
    )
    current = dict(cards[0], state="OPEN")
    released = []
    saved = {
        "get": render_card.get_card,
        "edit": render_card._edit_issue_body,
        "pending": am.pending_audit_records,
        "release": am.release_card_claim,
    }
    try:
        render_card.get_card = lambda number: current
        render_card._edit_issue_body = (
            lambda number, body, remove_labels=None: current.update(body=body)
        )
        am.pending_audit_records = lambda: []
        am.release_card_claim = lambda record: released.append(record["card_issue"])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "results.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"merges": [], "releases": payload["releases"]}, f)
            am.cmd_record(path)
        check(
            "audit: record clears the proven-open intent and releases its claim",
            not am._audit_intent_record(core.parse_state_block(current["body"]), 101)
            and released == [101],
        )
    finally:
        render_card.get_card = saved["get"]
        render_card._edit_issue_body = saved["edit"]
        am.pending_audit_records = saved["pending"]
        am.release_card_claim = saved["release"]


def test_successful_merge_stages_audit_before_result_handoff():
    w, items, cards = default_world(head="ah" * 20)
    payload, _ = run_act(w, items, cards)
    staged = am._pending_audit_record(core.parse_state_block(cards[0]["body"]), 101)
    check(
        "audit: completed merge is staged before the results handoff",
        len(payload["merges"]) == 1
        and staged is not None
        and staged["merge_commit"] == "c" * 40
        and not am._audit_intent_record(core.parse_state_block(cards[0]["body"]), 101),
    )


def test_pending_audit_recovery_accepts_a_partial_audited_close():
    record = _merge_record()
    card = make_card(
        101,
        "fmt",
        5,
        record["head_sha"],
        labels=[
            "processing",
            "resolved",
            am.AUTO_MERGE_CLAIM_LABEL,
            "repo:fmt",
            "kind:pr-review",
            "priority:med",
            "target:fmt-5",
        ],
    )
    card["user"] = {"login": card.pop("author")}
    state = core.parse_state_block(card["body"])
    state[am.AUDIT_PENDING_FIELD] = record
    card["body"] = render_card._replace_state_block(card["body"], state)
    saved_rest = core.gh_rest
    saved_repo = os.environ.get("GITHUB_REPOSITORY")
    saved_ledger = am.append_to_ledger
    saved_resolve = am.resolve_card
    saved_release = am.release_card_claim
    retried = {"ledger": [], "resolved": [], "released": []}
    core.gh_rest = lambda *args, **kwargs: [[card]]
    am.append_to_ledger = lambda records: retried["ledger"].extend(records)
    am.resolve_card = lambda value: retried["resolved"].append(value)
    am.release_card_claim = lambda value: retried["released"].append(
        value["card_issue"]
    )
    os.environ["GITHUB_REPOSITORY"] = "owner/wheelhouse"
    try:
        check(
            "audit: partial audited close remains discoverable for retry",
            am.pending_audit_records() == [record],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "results.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"merges": [], "releases": []}, f)
            am.cmd_record(path)
        check(
            "audit: partial audited close retries the record and close",
            retried == {"ledger": [record], "resolved": [record], "released": [101]},
        )
    finally:
        core.gh_rest = saved_rest
        am.append_to_ledger = saved_ledger
        am.resolve_card = saved_resolve
        am.release_card_claim = saved_release
        if saved_repo is None:
            os.environ.pop("GITHUB_REPOSITORY", None)
        else:
            os.environ["GITHUB_REPOSITORY"] = saved_repo


def test_closed_card_can_stage_and_recover_pending_audit():
    record = _merge_record()
    card = make_card(101, "fmt", 5, record["head_sha"])
    card["state"] = "closed"
    saved_get = render_card.get_card
    saved_edit = render_card._edit_issue_body
    saved_rest = core.gh_rest
    saved_repo = os.environ.get("GITHUB_REPOSITORY")
    calls = []
    try:
        render_card.get_card = lambda number: card
        render_card._edit_issue_body = (
            lambda number, body, remove_labels=None: card.update(body=body)
        )
        am.stage_pending_audit(record)
        core.gh_rest = lambda path, **kwargs: (calls.append(path) or [[card]])
        os.environ["GITHUB_REPOSITORY"] = "owner/wheelhouse"
        check(
            "audit: closed card persists and recovers its pending record",
            am._pending_audit_record(core.parse_state_block(card["body"]), 101)
            == record
            and am.pending_audit_records() == [record]
            and calls
            and "state=all" in calls[0],
        )
    finally:
        render_card.get_card = saved_get
        render_card._edit_issue_body = saved_edit
        core.gh_rest = saved_rest
        if saved_repo is None:
            os.environ.pop("GITHUB_REPOSITORY", None)
        else:
            os.environ["GITHUB_REPOSITORY"] = saved_repo


def test_closed_audit_intent_backfills_ledger_without_relabeling_card():
    head = "ci" * 20
    card = make_card(101, "fmt", 5, head)
    card["state"] = "CLOSED"
    state = core.parse_state_block(card["body"])
    intent = _merge_record()
    intent.update({"head_sha": head, "merge_commit": "", "merged_at": ""})
    state[am.AUDIT_INTENT_FIELD] = intent
    card["body"] = render_card._replace_state_block(card["body"], state)
    original_labels = list(card["labels"])
    saved = {
        "rest": core.gh_rest,
        "repo": os.environ.get("GITHUB_REPOSITORY"),
        "ledger": am.append_to_ledger,
        "pending": am.pending_audit_records,
        "get": render_card.get_card,
        "edit": render_card._edit_issue_body,
        "resolve": am.resolve_card,
        "release": am.release_card_claim,
    }
    ledger = []
    resolved = []
    released = []
    try:
        core.gh_rest = lambda path, **kwargs: [[card]]
        os.environ["GITHUB_REPOSITORY"] = "owner/wheelhouse"
        closed_intents = am.closed_audit_intent_entries("card-token")
        w = World()
        w.set_pr(
            "owner/fmt",
            5,
            make_pr(
                head=head,
                merged=True,
                state="closed",
                merge_commit_sha="d" * 40,
            ),
        )
        w.closed_audit_intents = closed_intents
        payload, _ = run_act(w, [], [])
        recovered = payload["merges"]
        am.append_to_ledger = lambda records: ledger.extend(records)
        am.pending_audit_records = lambda: []
        render_card.get_card = lambda number: card
        render_card._edit_issue_body = (
            lambda number, body, remove_labels=None: card.update(body=body)
        )
        am.resolve_card = lambda record: resolved.append(record)
        am.release_card_claim = lambda record: released.append(record)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "results.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"merges": recovered, "releases": []}, f)
            am.cmd_record(path)
        check(
            "audit: closed intent confirmed merged backfills the ledger only",
            len(recovered) == 1
            and recovered[0].get("merge_commit") == "d" * 40
            and recovered[0].get("_closed_intent_recovery") is True
            and ledger == recovered
            and resolved == []
            and released == []
            and card["labels"] == original_labels
            and not am._audit_intent_record(core.parse_state_block(card["body"]), 101),
        )
    finally:
        core.gh_rest = saved["rest"]
        am.append_to_ledger = saved["ledger"]
        am.pending_audit_records = saved["pending"]
        render_card.get_card = saved["get"]
        render_card._edit_issue_body = saved["edit"]
        am.resolve_card = saved["resolve"]
        am.release_card_claim = saved["release"]
        if saved["repo"] is None:
            os.environ.pop("GITHUB_REPOSITORY", None)
        else:
            os.environ["GITHUB_REPOSITORY"] = saved["repo"]


def test_live_check_status_reads_current_configured_contexts():
    saved = core._gh_graphql_data
    calls = []
    head = "a" * 40

    def graphql(args):
        calls.append(args)
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "headRefOid": head,
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "statusCheckRollup": {
                                            "state": "FAILURE",
                                            "contexts": {
                                                "totalCount": 2,
                                                "pageInfo": {"hasNextPage": False},
                                                "nodes": [
                                                    {
                                                        "__typename": "CheckRun",
                                                        "name": "Gate",
                                                        "conclusion": "SUCCESS",
                                                        "status": "COMPLETED",
                                                    },
                                                    {
                                                        "__typename": "CheckRun",
                                                        "name": "test unit",
                                                        "conclusion": "FAILURE",
                                                        "status": "COMPLETED",
                                                    },
                                                ],
                                            },
                                        }
                                    }
                                }
                            ]
                        },
                    }
                }
            }
        }

    core._gh_graphql_data = graphql
    try:
        ok, reason = am.live_check_status(
            "owner",
            "fmt",
            5,
            head,
            {"compliance_check": "Gate", "test_check_patterns": ["test"]},
        )
        check(
            "G7: live configured check read fails closed",
            not ok and "tests=fail" in reason,
        )
        check(
            "G7: live configured check read targets the exact PR",
            calls and "number=5" in calls[0],
        )
    finally:
        core._gh_graphql_data = saved


def test_live_check_status_holds_for_paginated_contexts():
    saved = core._gh_graphql_data
    head = "p" * 40

    def graphql(args):
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "headRefOid": head,
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "statusCheckRollup": {
                                            "state": "SUCCESS",
                                            "contexts": {
                                                "totalCount": 101,
                                                "pageInfo": {"hasNextPage": True},
                                                "nodes": [],
                                            },
                                        }
                                    }
                                }
                            ]
                        },
                    }
                }
            }
        }

    core._gh_graphql_data = graphql
    try:
        ok, reason = am.live_check_status(
            "owner",
            "fmt",
            5,
            head,
            {"compliance_check": "Gate", "test_check_patterns": ["test"]},
        )
        check(
            "G7: paginated check contexts hold before merge",
            not ok and "incomplete" in reason,
        )
    finally:
        core._gh_graphql_data = saved


def test_vision_is_rechecked_per_candidate_and_before_acting():
    w, items, cards = default_world(head="vv" * 20)
    w.vision_seq = {"fmt": [(True, "vsha"), (True, "new-vision")]}
    payload, _ = run_act(w, items, cards)
    check("G7: changed VISION.md before acting holds", not payload["merges"])
    check("G7: changed VISION.md does not call merge", not w.do_merge_calls)

    w2 = World()
    w2.repos = {"fmt": {"auto_merge": True}}
    w2.merged_authors = {("owner/fmt", "alice"): True}
    w2.vision_seq = {"fmt": [(True, "vsha"), (True, "vsha"), (True, "new-vision")]}
    w2.files[("owner/fmt", "5")] = (["src/one.py"], True, True)
    w2.files[("owner/fmt", "6")] = (["src/two.py"], True, True)
    w2.set_pr("owner/fmt", 5, make_pr(head="v5" * 20))
    w2.set_pr("owner/fmt", 6, make_pr(head="v6" * 20))
    items2 = [make_item("fmt", 5, "v5" * 20), make_item("fmt", 6, "v6" * 20)]
    cards2 = [
        make_card(101, "fmt", 5, "v5" * 20, automerge_verdict=ELIGIBLE_A),
        make_card(102, "fmt", 6, "v6" * 20, automerge_verdict=ELIGIBLE_A),
    ]
    payload2, _ = run_act(w2, items2, cards2)
    check(
        "G0: each candidate reads its own current VISION.md revision",
        len(payload2["merges"]) == 1
        and payload2["merges"][0]["number"] == "5"
        and any(
            hold["number"] == "6" and "VISION.md revision" in hold["hold_reason"]
            for hold in payload2["holds"]
        ),
    )


def test_head_moved_scan_vs_live_holds():
    # The scan/verdict head and the live head disagree at gate time.
    w, items, cards = default_world(head="sc" * 20)
    w.set_pr("owner/fmt", 5, make_pr(head="LV" * 20))  # live head differs
    payload, _ = run_act(w, items, cards)
    check("gate: scan/live head mismatch holds", "head moved" in _held_reason(payload))


# --------------------------------------------------------------------------- #
# escape hatch + kill switches
# --------------------------------------------------------------------------- #
def test_escape_hatch_label_holds():
    w, items, cards = default_world(head="lb" * 20)
    w.set_pr("owner/fmt", 5, make_pr(head="lb" * 20, labels=[core.NO_AUTO_MERGE_LABEL]))
    payload, _ = run_act(w, items, cards)
    check(
        "kill: wheelhouse:no-auto-merge label holds",
        "escape hatch" in _held_reason(payload),
    )


def test_G6_token_kill_switch_holds():
    w, items, cards = default_world()
    payload, _ = run_act(w, items, cards, has_token=False)
    check(
        "G6: absent triage token holds persisted verdict",
        "TOKEN" in _held_reason(payload),
    )


def test_G6_vision_revision_mismatch_holds():
    w, items, cards = default_world(verdict=dict(ELIGIBLE_A, vision_sha="old"))
    payload, _ = run_act(w, items, cards)
    check(
        "G6: stale VISION.md revision holds",
        "VISION.md revision" in _held_reason(payload),
    )


def test_G6_base_revision_missing_or_mismatch_holds():
    w, items, cards = default_world(verdict=dict(ELIGIBLE_A, base_sha=""))
    payload, _ = run_act(w, items, cards)
    check("G6: verdict without a base SHA holds", "base SHA" in _held_reason(payload))

    w2, items2, cards2 = default_world(verdict=dict(ELIGIBLE_A, base_sha="a2" * 20))
    payload2, _ = run_act(w2, items2, cards2)
    check(
        "G6: verdict for a superseded base SHA holds",
        "current base SHA" in _held_reason(payload2),
    )


def test_triage_persists_trusted_policy_revisions():
    item = make_item("fmt", 5, "vv" * 20)
    triage = {
        "summary": "A focused change.",
        "product_implications": "No broad behavior change.",
        "evidence": "target.txt: quoted a line from the change",
        "recommended_next_step": "merge - narrow and safe.",
        "automerge": ELIGIBLE_A,
    }
    body = render_card.body_with_triage_result(
        render_card.render(item)["body"],
        item["head_sha"],
        triage=triage,
        vision_sha="trusted-vision-sha",
        base_sha="b" * 40,
        automerge_behavior_available=True,
    )
    verdict = core.parse_state_block(body).get("automerge_verdict")
    check(
        "triage: persists trusted VISION.md and base revisions with verdict",
        verdict
        and verdict.get("vision_sha") == "trusted-vision-sha"
        and verdict.get("base_sha") == "b" * 40,
    )


def test_complete_diff_triage_persists_independent_facts_without_vision():
    item = make_item("fmt", 5, "iv" * 20)
    triage = {
        "summary": "A focused change.",
        "product_implications": "No broad behavior change.",
        "evidence": "target.txt: quoted a line from the change",
        "recommended_next_step": "merge - narrow and safe.",
        "automerge": INDEPENDENT_A,
    }
    body = render_card.body_with_triage_result(
        render_card.render(item)["body"],
        item["head_sha"],
        triage=triage,
        vision_sha="",
        base_sha="b" * 40,
        automerge_behavior_available=True,
    )
    state = core.parse_state_block(body)
    verdict = state.get("automerge_verdict")
    check(
        "triage split: no-VISION result persists independent behavior facts",
        verdict == INDEPENDENT_A,
    )
    check(
        "triage split: no-VISION result keeps base cache but no vision binding",
        state.get("triaged_base_sha") == "b" * 40
        and "triaged_vision_sha" not in state
        and "vision_sha" not in verdict
        and "base_sha" not in verdict,
    )
    check(
        "triage split: persisted independent record remains ineligible",
        am.verdict_eligible(verdict)[0] is False,
    )


# --------------------------------------------------------------------------- #
# repo-state freeze invariants (ok:false / truncated / indeterminate)
# --------------------------------------------------------------------------- #
def _run_with_scan(world, items, cards, repos_scan):
    world.repos_scan = repos_scan
    return run_act(world, items, cards)


def test_scan_freeze_invariants():
    for label, scan in (
        ("ok:false", {"fmt": {"ok": False}}),
        ("truncated", {"fmt": {"ok": True, "truncated": True}}),
        (
            "indeterminate",
            {"fmt": {"ok": True, "indeterminate_pr_numbers": [5]}},
        ),
    ):
        w, items, cards = default_world(head="fz" * 20)
        payload, _ = _run_with_scan(w, items, cards, scan)
        check("freeze: %s repo/PR never auto-merges" % label, not w.do_merge_calls)
        check("freeze: %s held" % label, bool(payload["holds"]))


# --------------------------------------------------------------------------- #
# G7 do_merge race outcomes (already-merged / not-open / error)
# --------------------------------------------------------------------------- #
def test_do_merge_race_and_error_outcomes():
    w, items, cards = default_world(head="rc" * 20)
    w.do_merge_returns = {
        ("fmt", "5"): ("Target fmt#5 is already merged - nothing to do.", "resolved")
    }
    payload, _ = run_act(w, items, cards)
    check(
        "act: already-merged race is NOT recorded as our merge", not payload["merges"]
    )

    w2, items2, cards2 = default_world(head="er" * 20)
    w2.do_merge_returns = {("fmt", "5"): ("Merge of fmt#5 failed: boom", "error")}
    payload2, err2 = run_act(w2, items2, cards2)
    check(
        "act: do_merge error is a hold, not a recorded merge",
        not payload2["merges"] and payload2["holds"],
    )
    check("act: error emits a ::warning::", "auto-merge held" in err2)

    w3, items3, cards3 = default_world(head="rt" * 20)
    w3.do_merge_returns = {("fmt", "5"): ("workflow review required", "blocked")}
    payload3, err3 = run_act(w3, items3, cards3)
    check(
        "act: blocked workflow merge outcome is held, not an error",
        not payload3["merges"]
        and payload3["holds"]
        and not payload3["ambiguous_outcomes"],
    )
    check(
        "act: blocked workflow outcome emits a ::warning::", "auto-merge held" in err3
    )

    # Recoverable merge-conflict terminal "none" is held (claim released), not
    # an ambiguous error outcome that withholds release.
    w4, items4, cards4 = default_world(head="cf" * 20)
    w4.do_merge_returns = {
        ("fmt", "5"): (
            "Merge of fmt#5 failed because the PR has a merge conflict.",
            "none",
        )
    }
    payload4, err4 = run_act(w4, items4, cards4)
    check(
        "act: recoverable merge conflict is held, not recorded merge",
        not payload4["merges"]
        and payload4["holds"]
        and not payload4["ambiguous_outcomes"],
    )
    check(
        "act: recoverable merge conflict releases the claim",
        any(r.get("card_issue") for r in payload4.get("releases") or []),
    )
    check(
        "act: recoverable merge conflict emits a ::warning::",
        "auto-merge held" in err4,
    )


# --------------------------------------------------------------------------- #
# DELIBERATE ABSENCE of an overlap gate and any rate cap (captain override)
# --------------------------------------------------------------------------- #
def test_no_open_pr_file_overlap_gate():
    # Two open merge-ready PRs whose file sets fully overlap BOTH merge - there
    # is intentionally no open-PR file-overlap gate in V1.
    w = World()
    w.repos = {"fmt": {"auto_merge": True}}
    w.vision = {"fmt": (True, "vsha")}
    w.merged_authors = {("owner/fmt", "alice"): True}
    same_files = (["src/shared.py", "README.md"], True, True)
    w.files[("owner/fmt", "5")] = same_files
    w.files[("owner/fmt", "6")] = same_files
    w.set_pr("owner/fmt", 5, make_pr(head="o5" * 20))
    w.set_pr("owner/fmt", 6, make_pr(head="o6" * 20))
    items = [make_item("fmt", 5, "o5" * 20), make_item("fmt", 6, "o6" * 20)]
    cards = [
        make_card(101, "fmt", 5, "o5" * 20, automerge_verdict=ELIGIBLE_A),
        make_card(102, "fmt", 6, "o6" * 20, automerge_verdict=ELIGIBLE_A),
    ]
    payload, _ = run_act(w, items, cards)
    check(
        "absence: overlapping-file PRs BOTH auto-merge (no same-file gate)",
        len(payload["merges"]) == 2,
    )
    check(
        "absence: no same-file overlap helper exists in auto_merge",
        not any("file_overlap" in n for n in dir(am)),
    )


def test_no_rate_cap_same_contributor_or_scan():
    # The SAME contributor's several PRs in ONE scan all merge - no per-
    # contributor daily cap and no per-scan cap exist in V1.
    w = World()
    w.repos = {"fmt": {"auto_merge": True}}
    w.vision = {"fmt": (True, "vsha")}
    w.merged_authors = {("owner/fmt", "alice"): True}
    items, cards = [], []
    for i, n in enumerate((5, 6, 7, 8)):
        head = ("r%d" % n) * 20
        w.files[("owner/fmt", str(n))] = (["src/f%d.py" % n], True, True)
        w.set_pr("owner/fmt", n, make_pr(head=head, author="alice"))
        items.append(make_item("fmt", n, head))
        cards.append(make_card(100 + i, "fmt", n, head, automerge_verdict=ELIGIBLE_A))
    payload, _ = run_act(w, items, cards)
    check(
        "absence: 4 PRs from one contributor in one scan all merge",
        len(payload["merges"]) == 4,
    )
    check(
        "absence: no rate/cap helper exists in auto_merge",
        not any(("cap" in n or "rate" in n) for n in dir(am)),
    )


# --------------------------------------------------------------------------- #
# base-branch-ONLY VISION.md read (never the PR head)
# --------------------------------------------------------------------------- #
def test_vision_read_is_base_branch_only():
    captured = {}

    def fake_gh_api(path):
        captured["path"] = path
        payload = {
            "type": "file",
            "sha": "visionsha",
            "size": len(b"Our vision."),
            "encoding": "base64",
            "content": __import__("base64").b64encode(b"Our vision.").decode(),
        }
        return type("R", (), {"returncode": 0, "stdout": json.dumps(payload)})()

    save = am._gh_api
    am._gh_api = fake_gh_api
    try:
        present, sha = am.vision_on_default_branch("owner/fmt")
    finally:
        am._gh_api = save
    check(
        "vision: present read from default branch",
        present is True and sha == "visionsha",
    )
    check(
        "vision: read uses the contents API with NO ?ref (default branch = base)",
        captured["path"] == "/repos/owner/fmt/contents/VISION.md"
        and "ref=" not in captured["path"]
        and "head" not in captured["path"],
    )


def test_vision_absent_and_empty_fail_closed():
    def gh_404(path):
        return type("R", (), {"returncode": 1, "stdout": ""})()

    def gh_empty(path):
        payload = {
            "type": "file",
            "sha": "s",
            "encoding": "base64",
            "size": 3,
            "content": __import__("base64").b64encode(b"   ").decode(),
        }
        return type("R", (), {"returncode": 0, "stdout": json.dumps(payload)})()

    save = am._gh_api
    try:
        am._gh_api = gh_404
        check(
            "vision: 404 -> not present",
            am.vision_on_default_branch("o/r") == (False, ""),
        )
        am._gh_api = gh_empty
        check(
            "vision: blank VISION.md -> not present (fail-closed)",
            am.vision_on_default_branch("o/r") == (False, ""),
        )
    finally:
        am._gh_api = save


# --------------------------------------------------------------------------- #
# Global switch ON (this fork commits `auto_merge: true`) - VISION.md is the
# practical per-repo opt-in and every safety gate is unchanged.
# --------------------------------------------------------------------------- #
def test_global_switch_on_is_the_practical_optin_via_vision():
    # This fork's committed config sets the fleet-wide `auto_merge: true`, so a
    # repo carrying NO per-repo override is opted in by the global switch alone;
    # a committed default-branch VISION.md then becomes the real per-repo signal.
    check(
        "global switch on: a repo with no per-repo key is opted in by the global",
        core._auto_merge_enabled({}, True) is True,
    )
    check(
        "global switch on: per-repo auto_merge:false is still the one-repo opt-out",
        core._auto_merge_enabled({"auto_merge": False}, True) is False,
    )
    # The SHIPPED CODE DEFAULT is untouched: an absent global key is still OFF, so
    # a fresh fork-and-own inherits auto-merge disabled - only this fork's
    # committed config VALUE flipped, never `_auto_merge_enabled`'s default.
    check(
        "shipped code default unchanged: absent global key -> OFF",
        core._auto_merge_enabled({}, False) is False,
    )


def test_global_true_without_vision_holds_no_claim_merge_or_vision_verdict():
    # A repo opted in purely by the fleet-wide switch (no per-repo override) still
    # auto-merges NOTHING until it commits a default-branch VISION.md. G0 holds:
    # no merge attempt, no claim, and no behavior-verdict spend.
    w, items, cards = default_world()
    w.global_auto_merge = True
    w.repos = {"fmt": {}}  # pure fleet-wide opt-in, no per-repo override
    w.vision = {}  # no committed VISION.md on the default branch
    payload, _ = run_act(w, items, cards)
    check(
        "global true + no VISION.md: G0 holds on the missing rubric",
        "VISION.md" in _held_reason(payload),
    )
    check(
        "global true + no VISION.md: no merge attempt",
        not w.do_merge_calls and not payload["merges"],
    )

    # No CLAIM: complete-diff triage persists the independent behavior facts,
    # but the vision-bound alignment and merge recommendation stay absent. The
    # claim phase still requires the complete eligible verdict.
    no_vision_state = {
        "repo": "fmt",
        "number": 5,
        "kind": "pr-review",
        "head_sha": "h1" * 20,
        "triaged_sha": "h1" * 20,
        "triage_status": "succeeded",
        "triage_recommendation": {"action": "merge", "reason": ""},
        "automerge_verdict": INDEPENDENT_A,
    }
    ok, _, _ = am._fresh_verdict_for_head(no_vision_state, "h1" * 20)
    check(
        "global true + no VISION.md: claim gate refuses independent facts alone",
        ok is False,
    )
    no_vision_facts, _ = am.fresh_verdict_facts(no_vision_state, "h1" * 20)
    check(
        "global true + no VISION.md: independent rows remain informative",
        no_vision_facts["g6_default_behavior"]["status"] == schema.STATUS_MET
        and no_vision_facts["g6_class_c_mode"]["status"] == schema.STATUS_MET,
    )

    # No VISION-BOUND VERDICT: the base-branch VISION SHA that binds alignment and
    # final merge recommendation stays empty when no VISION.md exists.
    saved = core.gh_rest
    try:

        def _absent(path):
            raise RuntimeError("404 no VISION.md")

        core.gh_rest = _absent
        check(
            "global true + no VISION.md: vision-bound verdict has no policy SHA",
            core._default_branch_vision_sha("owner/fmt") == "",
        )
        core.gh_rest = lambda path: {"type": "file", "sha": "vsha"}
        check(
            "with VISION.md present: vision SHA is attached (verdict may be produced)",
            core._default_branch_vision_sha("owner/fmt") == "vsha",
        )
    finally:
        core.gh_rest = saved


def test_global_true_self_authorization_still_excluded():
    # Even with the fleet-wide switch ON, a PR cannot author the very rubric it is
    # judged against: a PR that ADDS or CHANGES VISION.md is unconditionally
    # excluded, and the rubric is read from the BASE default branch, never the head.
    check(
        "self-authorization: a PR ADDING VISION.md is a vision: exclusion",
        any(
            h.startswith("vision:") for h in core._auto_merge_exclusions(["VISION.md"])
        ),
    )
    check(
        "self-authorization: VISION.md CHANGED among other files still excludes",
        any(
            h.startswith("vision:")
            for h in core._auto_merge_exclusions(["src/a.py", "VISION.md", "docs/x.md"])
        ),
    )
    # End-to-end, with the global switch ON: a merge-ready PR touching VISION.md
    # holds at G2 and is never merged.
    w, items, cards = default_world()
    w.global_auto_merge = True
    w.repos = {"fmt": {}}  # opted in by the fleet-wide switch alone
    w.files[("owner/fmt", "5")] = (["VISION.md", "src/a.py"], True, True)
    payload, _ = run_act(w, items, cards)
    check(
        "global true: PR editing VISION.md holds at G2 (self-authorization guard)",
        "G2" in _held_reason(payload) and not w.do_merge_calls,
    )

    # The VISION.md rubric read is base-branch-only (contents API, no ?ref), so a
    # PR-head edit of VISION.md can never be the version judged/gated against.
    captured = {}

    def fake_gh_api(path):
        captured["path"] = path
        return type("R", (), {"returncode": 1, "stdout": ""})()

    save = am._gh_api
    am._gh_api = fake_gh_api
    try:
        am.vision_on_default_branch("owner/fmt")
    finally:
        am._gh_api = save
    check(
        "self-authorization: rubric read targets the default branch (no ?ref, no head)",
        captured.get("path") == "/repos/owner/fmt/contents/VISION.md"
        and "ref=" not in captured.get("path", "")
        and "head" not in captured.get("path", ""),
    )


def test_vision_oversized_or_incomplete_fails_closed():
    saved = am._gh_api

    def content(payload):
        return lambda path: type(
            "R", (), {"returncode": 0, "stdout": json.dumps(payload)}
        )()

    try:
        oversized = b"x" * (am.MAX_VISION_BYTES + 1)
        am._gh_api = content(
            {
                "type": "file",
                "sha": "oversized",
                "size": len(oversized),
                "encoding": "base64",
                "content": __import__("base64").b64encode(oversized).decode(),
            }
        )
        check(
            "vision: oversized policy is unavailable for auto-merge",
            am.vision_on_default_branch("o/r") == (False, ""),
        )
        am._gh_api = content(
            {
                "type": "file",
                "sha": "incomplete",
                "size": 10,
                "encoding": "base64",
                "content": __import__("base64").b64encode(b"short").decode(),
            }
        )
        check(
            "vision: incomplete policy is unavailable for auto-merge",
            am.vision_on_default_branch("o/r") == (False, ""),
        )
    finally:
        am._gh_api = saved


# --------------------------------------------------------------------------- #
# audit trail: ledger (pure) + resolved decision record + record CLI
# --------------------------------------------------------------------------- #
def _merge_record(number=5, card=101):
    return {
        "repo": "fmt",
        "number": str(number),
        "card_issue": card,
        "head_sha": "h" * 40,
        "merge_commit": "c" * 40,
        "merged_at": "2026-07-10T00:00:00Z",
        "contributor": "alice",
        "contributor_proof": "has >=1 prior merged PR in fmt",
        "vision_sha": "vsha",
        "behavior_class": "A",
        "behavior_verdict": ELIGIBLE_A,
        "gates": {"blast_radius": "2 files / 20 lines within caps"},
    }


def test_ledger_parse_append_render_and_cap():
    entries = am.append_ledger_entries([], [_merge_record(5), _merge_record(6, 102)])
    body = am.render_ledger_body(entries, "2026-07-10T00:00:00Z")
    parsed = am.parse_ledger(body)
    check("ledger: two entries round-trip", len(parsed) == 2)
    check(
        "ledger: entry carries all audit fields",
        parsed[0]["contributor"] == "alice"
        and parsed[0]["head_sha"]
        and parsed[0]["vision_sha"] == "vsha"
        and parsed[0]["behavior_class"] == "A"
        and parsed[0]["merge_commit"],
    )
    check("ledger: human summary names the merged PR", "fmt#5" in body)
    check(
        "ledger: missing/blank ledger parses to []",
        am.parse_ledger("") == [] and am.parse_ledger("no marker here") == [],
    )
    # Cap keeps only the most recent entries.
    many = [_merge_record(n) for n in range(300)]
    capped = am.append_ledger_entries([], many, cap=am.LEDGER_ENTRY_CAP)
    check(
        "ledger: stored history is bounded by count and serialized size",
        capped
        and len(capped) <= am.LEDGER_ENTRY_CAP
        and capped[-1]["number"] == "299"
        and len(am.render_ledger_body(capped).encode("utf-8"))
        <= am.LEDGER_MAX_BODY_BYTES,
    )
    oversized = []
    for n in range(12):
        record = _merge_record(n)
        record["gates"] = {"detail": "x" * 20000}
        oversized.append(record)
    byte_capped = am.append_ledger_entries([], oversized)
    byte_body = am.render_ledger_body(byte_capped, "2026-07-10T00:00:00Z")
    check(
        "ledger: serialized body stays within the GitHub-safe byte cap",
        len(byte_body.encode("utf-8")) <= am.LEDGER_MAX_BODY_BYTES,
    )
    check(
        "ledger: byte cap trims oldest records first",
        byte_capped
        and byte_capped[-1]["number"] == "11"
        and len(byte_capped) < len(oversized),
    )
    duplicate = am.append_ledger_entries(entries, [_merge_record(5)])
    check(
        "ledger: retrying an already-persisted merge does not duplicate it",
        len(duplicate) == len(entries),
    )


def test_audit_comment_explains_why_it_qualified():
    text = am.audit_comment(_merge_record())
    for token in (
        "Auto-merged fmt#5",
        "alice",
        "Behavior class: A",
        "Merge commit",
        "VISION.md SHA",
        "never auto-reverts",
    ):
        check("audit-comment: mentions %r" % token, token in text)


def test_record_cli_resolves_card_and_appends_ledger():
    calls = {"ledger": [], "resolved": [], "released": []}
    saved = {
        "ledger": am.append_to_ledger,
        "release": am.release_card_claim,
        "pending": am.pending_audit_records,
        "stage": am.stage_pending_audit,
        "get": render_card.get_card,
        "close": am._strict_audited_close_card,
    }
    am.append_to_ledger = lambda records: calls["ledger"].extend(records)
    am.release_card_claim = lambda record: calls["released"].append(
        record["card_issue"]
    )
    am.pending_audit_records = lambda: []
    am.stage_pending_audit = lambda record: record
    render_card.get_card = lambda n: {"state": "OPEN", "labels": []}
    am._strict_audited_close_card = lambda n, msg, close_issue=True: calls[
        "resolved"
    ].append((n, close_issue))
    tmp = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "am_results_%d.json" % os.getpid()
    )
    try:
        with open(tmp, "w") as f:
            json.dump({"merges": [_merge_record()]}, f)
        am.cmd_record(tmp)
        check("record: ledger got the merge", len(calls["ledger"]) == 1)
        check("record: card resolved+closed", calls["resolved"] == [(101, True)])
        check("record: merged card claim is released", calls["released"] == [101])
    finally:
        os.unlink(tmp)
        am.append_to_ledger = saved["ledger"]
        am.release_card_claim = saved["release"]
        am.pending_audit_records = saved["pending"]
        am.stage_pending_audit = saved["stage"]
        render_card.get_card = saved["get"]
        am._strict_audited_close_card = saved["close"]


def test_record_retains_and_retries_failed_audits_before_releasing_claims():
    saved = {
        "ledger": am.append_to_ledger,
        "release": am.release_card_claim,
        "pending": am.pending_audit_records,
        "stage": am.stage_pending_audit,
        "resolve": am.resolve_card,
    }
    pending = []
    released = []
    resolved = []
    record = _merge_record()
    try:
        am.pending_audit_records = lambda: list(pending)

        def stage(value):
            pending[:] = [value]
            return value

        am.stage_pending_audit = stage
        am.append_to_ledger = lambda records: (_ for _ in ()).throw(
            RuntimeError("HTTP 422 ledger write failed")
        )
        am.resolve_card = lambda value: resolved.append(value)
        am.release_card_claim = lambda value: released.append(value["card_issue"])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "results.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"merges": [record], "releases": [record]}, f)
            failed = False
            try:
                am.cmd_record(path)
            except RuntimeError:
                failed = True
            check(
                "audit: failed ledger write retains the pending record and claim",
                failed and pending == [record] and released == [] and resolved == [],
            )
            am.append_to_ledger = lambda records: None
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"merges": [], "releases": []}, f)
            am.cmd_record(path)
        check(
            "audit: pending record retries before releasing its claim",
            resolved == [record] and released == [record["card_issue"]],
        )
    finally:
        am.append_to_ledger = saved["ledger"]
        am.release_card_claim = saved["release"]
        am.pending_audit_records = saved["pending"]
        am.stage_pending_audit = saved["stage"]
        am.resolve_card = saved["resolve"]


def test_audit_writes_retry_transient_failures_and_surface_unrecoverable_ones():
    saved = {
        "this_repo": core._this_repo_slug,
        "find": am._find_ledger_issue,
        "rest": core.gh_rest,
        "get": render_card.get_card,
        "ensure": core._ensure_repo_label,
        "gh": render_card._gh,
        "sleep": am._audit_sleep,
    }
    ledger_attempts = [0]
    card_attempts = [0]
    am._audit_sleep = lambda seconds: None
    core._this_repo_slug = lambda: "owner/wheelhouse"
    am._find_ledger_issue = lambda slug: {"number": 77, "body": ""}

    def patch_ledger(*args, **kwargs):
        ledger_attempts[0] += 1
        if ledger_attempts[0] == 1:
            raise RuntimeError("HTTP 503 service unavailable")
        return {}

    core.gh_rest = patch_ledger
    render_card.get_card = lambda number: {"state": "OPEN"}
    core._ensure_repo_label = lambda *args, **kwargs: None

    def gh(args, check=True):
        if args[:2] == ["issue", "comment"]:
            card_attempts[0] += 1
        if args[:2] == ["issue", "comment"] and card_attempts[0] == 1:
            raise RuntimeError("HTTP 502 bad gateway")
        return type("R", (), {"returncode": 0, "stderr": ""})()

    render_card._gh = gh
    try:
        am.append_to_ledger([_merge_record()])
        am.resolve_card(_merge_record())
        check("audit: ledger write retries a transient failure", ledger_attempts == [2])
        check(
            "audit: card resolution retries a transient failure", card_attempts == [2]
        )

        core.gh_rest = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("HTTP 422 validation failed")
        )
        stderr = io.StringIO()
        failed = False
        with redirect_stderr(stderr):
            try:
                am.append_to_ledger([_merge_record()])
            except RuntimeError:
                failed = True
        check(
            "audit: unrecoverable ledger write emits an error and fails",
            failed and "::error::" in stderr.getvalue(),
        )
    finally:
        core._this_repo_slug = saved["this_repo"]
        am._find_ledger_issue = saved["find"]
        core.gh_rest = saved["rest"]
        render_card.get_card = saved["get"]
        core._ensure_repo_label = saved["ensure"]
        render_card._gh = saved["gh"]
        am._audit_sleep = saved["sleep"]


def test_resolve_card_errors_fail_the_record_path():
    saved_get, saved_close = render_card.get_card, am._strict_audited_close_card
    closed = []
    am._strict_audited_close_card = lambda n, m, close_issue=True: closed.append(
        (n, close_issue)
    )
    try:
        render_card.get_card = lambda n: {"state": "CLOSED"}
        am.resolve_card(_merge_record())
        check(
            "record: closed card still receives its audit record without re-close",
            closed == [(101, False)],
        )
        am.resolve_card(dict(_merge_record(), card_issue=None))
        check("record: no card issue -> no-op", closed == [(101, False)])

        def boom(n):
            raise RuntimeError("gh down")

        render_card.get_card = boom
        failed = False
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            try:
                am.resolve_card(_merge_record())
            except RuntimeError:
                failed = True
        check(
            "record: card audit failure is surfaced",
            failed and closed == [(101, False)] and "::error::" in stderr.getvalue(),
        )
    finally:
        render_card.get_card = saved_get
        am._strict_audited_close_card = saved_close


def test_strict_audited_close_propagates_all_card_write_failures():
    saved = {
        "this_repo": core._this_repo_slug,
        "ensure": core._ensure_repo_label,
        "gh": render_card._gh,
        "get": render_card.get_card,
        "sleep": am._audit_sleep,
    }
    calls = []
    core._this_repo_slug = lambda: "owner/wheelhouse"
    core._ensure_repo_label = lambda *args: calls.append(("label", args))
    render_card.get_card = lambda number: {"state": "OPEN"}
    am._audit_sleep = lambda seconds: None

    def gh(args, check=True):
        calls.append(("gh", args, check))
        if args[:2] == ["issue", "close"]:
            raise RuntimeError("HTTP 422 close failed")
        return type("R", (), {"returncode": 0, "stderr": ""})()

    render_card._gh = gh
    try:
        failed = False
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            try:
                am.resolve_card(_merge_record())
            except RuntimeError:
                failed = True
        command_calls = [entry for entry in calls if entry[0] == "gh"]
        check(
            "audit: strict close runs comment, label update, and issue close",
            [entry[1][:2] for entry in command_calls[:3]]
            == [["issue", "comment"], ["issue", "edit"], ["issue", "close"]],
        )
        check(
            "audit: strict close failures emit an error and fail",
            failed and "::error::" in stderr.getvalue(),
        )
    finally:
        core._this_repo_slug = saved["this_repo"]
        core._ensure_repo_label = saved["ensure"]
        render_card._gh = saved["gh"]
        render_card.get_card = saved["get"]
        am._audit_sleep = saved["sleep"]


def test_atomic_results_handoff_fails_loudly_and_releases_claims_by_fallback():
    saved = {
        "act": am.act_on_scan,
        "write": am._write_json_atomically,
        "release": am.release_card_claim,
        "ledger": am.append_to_ledger,
        "result_env": os.environ.get("WHEELHOUSE_AUTOMERGE_RESULTS"),
    }
    payload = {"merges": [_merge_record()], "holds": [], "releases": []}
    calls = {"released": [], "ledger": []}
    am.act_on_scan = lambda scan, cards: payload
    am.release_card_claim = lambda record: calls["released"].append(
        record["card_issue"]
    )
    am.append_to_ledger = lambda records: calls["ledger"].extend(records)
    try:
        with tempfile.TemporaryDirectory() as directory:
            scan_path = os.path.join(directory, "scan.json")
            cards_path = os.path.join(directory, "cards.json")
            results_path = os.path.join(directory, "automerge.json")
            claims_path = os.path.join(directory, "automerge-valid-claims.json")
            for path, data in (
                (scan_path, {}),
                (cards_path, []),
                (claims_path, [{"number": 101}]),
            ):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            os.environ["WHEELHOUSE_AUTOMERGE_RESULTS"] = results_path
            try:
                am.cmd_act(scan_path, cards_path)
                with open(results_path, encoding="utf-8") as f:
                    recorded = json.load(f)
                check(
                    "handoff: atomic results file contains the complete payload",
                    recorded == payload,
                )
                check(
                    "handoff: atomic write leaves no temporary result file",
                    not any(
                        name.startswith(".automerge-") for name in os.listdir(directory)
                    ),
                )

                am._write_json_atomically = lambda path, data: (_ for _ in ()).throw(
                    OSError("disk full")
                )
                failed = False
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    try:
                        am.cmd_act(scan_path, cards_path)
                    except RuntimeError:
                        failed = True
                check(
                    "handoff: write failure emits an error and fails the act step",
                    failed and "::error::" in stderr.getvalue(),
                )
                os.unlink(results_path)
                with redirect_stderr(io.StringIO()):
                    am.cmd_record(results_path, claims_path)
                check(
                    "handoff: missing results release validated card claims",
                    calls["released"] == [101],
                )
                check(
                    "handoff: missing results do not fabricate ledger entries",
                    calls["ledger"] == [],
                )
            finally:
                if saved["result_env"] is None:
                    os.environ.pop("WHEELHOUSE_AUTOMERGE_RESULTS", None)
                else:
                    os.environ["WHEELHOUSE_AUTOMERGE_RESULTS"] = saved["result_env"]
    finally:
        am.act_on_scan = saved["act"]
        am._write_json_atomically = saved["write"]
        am.release_card_claim = saved["release"]
        am.append_to_ledger = saved["ledger"]


def test_atomic_claim_handoffs_release_claims_and_fail_loudly():
    saved = {
        "claim": am.claim_cards,
        "validate": am.validate_claimed_cards,
        "write": am._write_json_atomically,
        "release": am._release_card_claim,
        "claims_env": os.environ.get("WHEELHOUSE_AUTOMERGE_CLAIMS"),
        "validated_env": os.environ.get("WHEELHOUSE_AUTOMERGE_VALIDATED_CLAIMS"),
    }
    card = {"number": 101}
    released = []
    am.claim_cards = lambda scan, cards: [card]
    am.validate_claimed_cards = lambda cards: [card]
    am._write_json_atomically = lambda path, data: (_ for _ in ()).throw(
        OSError("disk full")
    )
    am._release_card_claim = lambda number: released.append(number)
    try:
        with tempfile.TemporaryDirectory() as directory:
            scan_path = os.path.join(directory, "scan.json")
            cards_path = os.path.join(directory, "cards.json")
            with open(scan_path, "w", encoding="utf-8") as f:
                json.dump({}, f)
            with open(cards_path, "w", encoding="utf-8") as f:
                json.dump([], f)
            for label, env_key, command in (
                (
                    "claims",
                    "WHEELHOUSE_AUTOMERGE_CLAIMS",
                    lambda: am.cmd_claim(scan_path, cards_path),
                ),
                (
                    "validated claims",
                    "WHEELHOUSE_AUTOMERGE_VALIDATED_CLAIMS",
                    lambda: am.cmd_validate(cards_path),
                ),
            ):
                os.environ[env_key] = os.path.join(directory, "%s.json" % label)
                released.clear()
                failed = False
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    try:
                        command()
                    except RuntimeError:
                        failed = True
                check(
                    "handoff: failed %s write releases known claims and fails loudly"
                    % label,
                    failed and released == [101] and "::error::" in stderr.getvalue(),
                )
    finally:
        am.claim_cards = saved["claim"]
        am.validate_claimed_cards = saved["validate"]
        am._write_json_atomically = saved["write"]
        am._release_card_claim = saved["release"]
        for key, value in (
            ("WHEELHOUSE_AUTOMERGE_CLAIMS", saved["claims_env"]),
            ("WHEELHOUSE_AUTOMERGE_VALIDATED_CLAIMS", saved["validated_env"]),
        ):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_missing_merge_sha_fails_act_after_persisting_claim_releases():
    saved = {
        "act": am.act_on_scan,
        "result_env": os.environ.get("WHEELHOUSE_AUTOMERGE_RESULTS"),
    }
    payload = {
        "merges": [],
        "holds": [],
        "releases": [{"card_issue": 101}],
        "post_merge_errors": ["fmt#5: merge endpoint returned no SHA"],
    }
    am.act_on_scan = lambda scan, cards: payload
    try:
        with tempfile.TemporaryDirectory() as directory:
            scan_path = os.path.join(directory, "scan.json")
            cards_path = os.path.join(directory, "cards.json")
            results_path = os.path.join(directory, "automerge.json")
            for path, data in ((scan_path, {}), (cards_path, [])):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            os.environ["WHEELHOUSE_AUTOMERGE_RESULTS"] = results_path
            failed = False
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                try:
                    am.cmd_act(scan_path, cards_path)
                except RuntimeError:
                    failed = True
            with open(results_path, encoding="utf-8") as f:
                written = json.load(f)
            check(
                "audit: missing merge SHA writes claim releases then fails loudly",
                failed and written == payload and "::error::" in stderr.getvalue(),
            )
    finally:
        am.act_on_scan = saved["act"]
        if saved["result_env"] is None:
            os.environ.pop("WHEELHOUSE_AUTOMERGE_RESULTS", None)
        else:
            os.environ["WHEELHOUSE_AUTOMERGE_RESULTS"] = saved["result_env"]


# --------------------------------------------------------------------------- #
# non-candidates: only merge-ready pr-review items are considered
# --------------------------------------------------------------------------- #
def test_only_merge_ready_pr_review_items_are_candidates():
    w, items, cards = default_world(head="nc" * 20)
    items[0]["bucket"] = "review-needed"  # not merge-ready
    payload, _ = run_act(w, items, cards)
    check(
        "scope: non-merge-ready item is not a candidate",
        not payload["merges"] and not payload["holds"],
    )

    w2, items2, cards2 = default_world(head="ci" * 20)
    items2[0]["kind"] = "ci-approval"
    payload2, _ = run_act(w2, items2, cards2)
    check(
        "scope: ci-approval item is not a candidate",
        not payload2["merges"] and not payload2["holds"],
    )


# --------------------------------------------------------------------------- #
# workflow wiring (offline YAML/script inspection) - token discipline + VISION
# --------------------------------------------------------------------------- #
_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_scan_backstop_wiring_and_token_discipline():
    import yaml

    doc = yaml.safe_load(_read(".github/workflows/scan-backstop.yml"))
    steps = doc["jobs"]["reconcile"]["steps"]
    by_name = {s.get("name"): s for s in steps if isinstance(s, dict)}
    listed = by_name.get("List open cards")
    claim = by_name.get("Claim auto-merge decision cards")
    validate = by_name.get("Validate auto-merge decision cards")
    act = by_name.get("Auto-merge eligible PRs")
    rec = by_name.get("Record auto-merges")
    check("wiring: auto-merge act step exists", act is not None)
    check(
        "wiring: card list records card author provenance",
        listed and "author: (.user.login" in listed.get("run", ""),
    )
    check(
        "wiring: card list records comment count for claim validation",
        listed and "comments: (.comments // 0)" in listed.get("run", ""),
    )
    check("wiring: current cards are claimed before auto-merge", claim is not None)
    check("wiring: record step exists", rec is not None)
    check(
        "wiring: the MERGE runs on FLEET_TOKEN (cross-repo write)",
        act and "FLEET_TOKEN" in (act.get("env") or {}).get("GH_TOKEN", ""),
    )
    check(
        "wiring: claim runs under github.token before the FLEET_TOKEN act",
        claim
        and "github.token" in (claim.get("env") or {}).get("GH_TOKEN", "")
        and "auto_merge.py claim scan.json cards.json" in claim.get("run", ""),
    )
    check(
        "wiring: claimed cards are revalidated under github.token before acting",
        validate
        and "github.token" in (validate.get("env") or {}).get("GH_TOKEN", "")
        and "auto_merge.py validate automerge-claims.json" in validate.get("run", ""),
    )
    check(
        "wiring: act consumes only final validated claims",
        act
        and "auto_merge.py act scan.json automerge-valid-claims.json"
        in act.get("run", ""),
    )
    check(
        "wiring: authoritative criteria inspect the original card snapshot",
        act
        and "auto_merge.py act scan.json automerge-valid-claims.json cards.json"
        in act.get("run", ""),
    )
    check(
        "wiring: reconcile renders the criteria handoff from automerge.json",
        "reconcile.py scan.json cards.json automerge.json"
        in (by_name.get("Reconcile the queue", {}).get("run", "")),
    )
    check(
        "wiring: act re-reads cards with a separate default-token credential",
        act
        and "FLEET_TOKEN" in (act.get("env") or {}).get("GH_TOKEN", "")
        and "github.token" in (act.get("env") or {}).get("WHEELHOUSE_CARD_TOKEN", ""),
    )
    check(
        "wiring: absent triage token disables both claim and act",
        claim
        and act
        and "CLAUDE_CODE_OAUTH_TOKEN != ''"
        in (claim.get("env") or {}).get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", "")
        and "CLAUDE_CODE_OAUTH_TOKEN != ''"
        in (act.get("env") or {}).get("WHEELHOUSE_AUTOMERGE_HAS_TOKEN", ""),
    )
    check(
        "wiring: the AUDIT record runs on github.token (this-repo bookkeeping)",
        rec and "github.token" in (rec.get("env") or {}).get("GH_TOKEN", ""),
    )
    check(
        "wiring: missing act results release validated claims under github.token",
        rec
        and rec.get("if", "").startswith("${{ always()")
        and "record automerge.json automerge-valid-claims.json" in rec.get("run", ""),
    )
    check(
        "wiring: record does NOT get FLEET_TOKEN",
        rec and "FLEET_TOKEN" not in json.dumps(rec.get("env") or {}),
    )
    order = [s.get("name") for s in steps if isinstance(s, dict)]
    check(
        "wiring: order is act -> record -> reconcile",
        order.index("Claim auto-merge decision cards")
        < order.index("Validate auto-merge decision cards")
        < order.index("Auto-merge eligible PRs")
        < order.index("Record auto-merges")
        < order.index("Reconcile the queue"),
    )
    check(
        "wiring: reconcile still runs when audit recovery fails",
        by_name.get("Reconcile the queue", {})
        .get("if", "")
        .startswith("${{ always()"),
    )
    handler = yaml.safe_load(_read(".github/workflows/decision-handler.yml"))
    handler_steps = handler["jobs"]["handle"]["steps"]
    handler_by_name = {
        s.get("name") or s.get("id"): s for s in handler_steps if isinstance(s, dict)
    }
    current = handler_by_name.get("current-card")
    check(
        "wiring: manual decisions share the auto-merge concurrency lock",
        handler.get("concurrency", {}).get("group") == "wheelhouse-backstop",
    )
    check(
        "wiring: shared lock queues manual decisions instead of replacing them",
        doc.get("concurrency", {}).get("queue") == "max"
        and handler.get("concurrency", {}).get("queue") == "max",
    )
    check(
        "wiring: manual target actions re-read claim state before execution",
        current
        and "wheelhouse:auto-merge-claim" in current.get("run", "")
        and "TRIGGER_BODY" in (current.get("env") or {})
        and "TRIGGER_UPDATED_AT" in (current.get("env") or {})
        and "TRIGGER_LABELS" in (current.get("env") or {})
        and "TRIGGER_COMMENTS" in (current.get("env") or {})
        and "body,updatedAt,comments" in current.get("run", "")
        and "trigger_updated_at" in current.get("run", "")
        and '--argjson trigger_labels "$TRIGGER_LABELS"' in current.get("run", "")
        and "wheelhouse:auto-merge-claim" in current.get("run", "")
        and "$stable_labels == $stable_trigger_labels" in current.get("run", "")
        and "steps.current-card.outputs.allowed == 'true'" in json.dumps(handler_steps),
    )


def test_triage_reads_vision_from_base_only_and_asks_verdict():
    text = _read(".github/workflows/triage.yml")
    check(
        "triage: VISION.md is read via the contents API",
        "contents/VISION.md" in text,
    )
    check(
        "triage: VISION.md read passes NO ?ref (default branch = base, never head)",
        "contents/VISION.md?ref=" not in text and "VISION.md?" not in text,
    )
    check(
        "triage: asks for A/B/C, restoration, and independent behavior facts",
        '"behavior_class"' in text
        and '"behavior_assertions"' in text
        and '"class_b_restoration"' in text
        and '"corrected_defect"' in text
        and '"corrected_defect_evidence"' in text
        and '"intended_behavior_restored"' in text
        and '"intended_behavior_restored_evidence"' in text
        and '"optin_default_off"' in text,
    )
    check(
        "triage: complete diffs produce independent facts without VISION.md",
        'if [ "$DIFF_COMPLETE" = "true" ]; then' in text
        and "AUTOMERGE_BEHAVIOR_AVAILABLE=true" in text
        and 'elif [ "$AUTOMERGE_BEHAVIOR_AVAILABLE" = "true" ]; then' in text,
    )
    check(
        "triage: alignment and final merge remain gated on trusted VISION.md",
        'if [ "$VISION_PRESENT" = "true" ] \\\n              && [ "$TARGET_FACTS_PRESENT" = "true" ] \\\n              && [ "$AUTOMERGE_BEHAVIOR_AVAILABLE" = "true" ]; then'
        in text
        and "AUTOMERGE_VERDICT_AVAILABLE=true" in text,
    )
    check(
        "triage: the VISION policy is labeled TRUSTED owner-authored (not head)",
        "TRUSTED owner policy from the default branch" in text,
    )
    check(
        "triage: binds verdict storage to the fetched VISION.md SHA",
        "vision_sha=$VISION_SHA" in text
        and '--vision-sha "$VISION_SHA"' in text
        and "base_sha=$BASE_SHA" in text
        and '--base-sha "$BASE_SHA"' in text,
    )
    check(
        "triage: incomplete diffs suppress every behavior fact",
        "AUTOMERGE_BEHAVIOR_AVAILABLE=false" in text,
    )
    check(
        "triage: unavailable assessments retain the base revision for cache freshness",
        'VISION_SHA=""\n              BASE_SHA=""' not in text,
    )
    check(
        "triage: oversized or incomplete VISION.md is unavailable for auto-merge",
        "VISION_SIZE=" in text
        and '"$VISION_SIZE" -le "$vision_limit_bytes"' in text
        and '"$vision_bytes" = "$VISION_SIZE"' in text
        and 'head -c "$vision_limit_bytes" > vision.md' not in text,
    )
    check(
        "triage: binary, LFS, and submodule diff input suppresses auto-merge verdict storage",
        "Binary files .+ differ" in text
        and "GIT binary patch" in text
        and '"$diff_size" -eq 0' in text
        and "git-lfs\\.github\\.com/spec/v1" in text
        and "Subproject commit" in text
        and "data unavailable for auto-merge assessment" in text,
    )
    check(
        "triage: behavior verdict diff is fetched from immutable base and dispatched head SHAs",
        "repos/$SLUG/compare/$BASE_SHA...$HEAD_SHA" in text
        and "HEAD_SHA: ${{ steps.resolve.outputs.revision }}" in text
        and 'gh pr diff "$NUMBER"' not in text,
    )
    check(
        "triage: in-job source evidence uses the actual workspace checkout",
        "TARGET_SRC_DIR: ${{ github.workspace }}/target-src" in text
        and '--target-src-dir "${TARGET_SRC_DIR:-}"' in text,
    )
    check(
        "triage: cross-job source evidence is bounded and manifest-bound",
        "source-evidence-build" in text
        and "target-src-evidence" in text
        and "target_source_revision:" in text
        and '--target-src-manifest "${TARGET_SRC_MANIFEST:-}"' in text
        and '--target-src-revision "${TARGET_SRC_REVISION:-}"' in text,
    )


def test_triage_rejects_lfs_and_submodule_diff_markers():
    text = _read(".github/workflows/triage.yml")
    match = re.search(r"grep -Eq '([^']+)' \"\$diff_raw\"", text)
    pattern = match.group(1) if match else ""
    for label, diff in (
        (
            "LFS pointer context line",
            " version https://git-lfs.github.com/spec/v1\n"
            "-oid sha256:0123456789abcdef\n"
            "+oid sha256:fedcba9876543210\n",
        ),
        (
            "submodule gitlink",
            "-Subproject commit 0123456789abcdef\n"
            "+Subproject commit fedcba9876543210\n",
        ),
    ):
        result = subprocess.run(
            ["grep", "-Eq", pattern], input=diff, text=True, capture_output=True
        )
        check(
            "triage: %s marker makes the diff unavailable" % label,
            bool(pattern) and result.returncode == 0,
        )


# --------------------------------------------------------------------------- #
def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if _failures:
        print("FAILURES: %d" % len(_failures))
        for f in _failures:
            print("  - " + f)
        sys.exit(1)
    print("all auto-merge V1 tests passed")


if __name__ == "__main__":
    main()
