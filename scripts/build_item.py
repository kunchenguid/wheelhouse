#!/usr/bin/env python3
"""
Wheelhouse - normalize an ingest payload into a decision-card item.

The `ingest` workflow feeds this either a repository_dispatch client_payload
(JSON in $PAYLOAD) or workflow_dispatch inputs ($INPUT_*). It produces a single
normalized item.json that render_card.py can turn into a card.

Expected fields (all but repo/number optional):
  repo, number, kind, head_sha, updated_at, title, author, bucket, comp, tests,
  summary, recommendation, priority, options (list or comma string),
  auto_triage (false as an item-level opt-out for pr-review),
  auto_triage_issues (false as an item-level opt-out for issue-triage)

When omitted, `options` defaults by kind via render_card.CHECKBOX_OPTIONS:
pr-review and issue-triage include the non-consuming `investigate` checkbox;
ci-approval does not. Text-bearing slash commands (`comment`, `decline`, and
pr-review-only `request-changes`) are not checkbox options and should not be
sent in `options`.
When omitted, `auto_triage` follows the global/per-repo config; a false payload
value can only opt this item out. `auto_triage_issues` is the INDEPENDENT
equivalent for issue-triage items - it follows its own global/per-repo config
and a false payload value can only opt that item out; it never affects
`auto_triage` or vice versa. `updated_at` is the issue-triage auto-triage cache
key (issues have no head SHA); omit it and this item is simply never eligible
for automatic issue triage (fail-open, mirroring a pr-review item with no
`head_sha`). The same eligibility gates decide whether render_card.py creates a
brand-new card as a held `pending-triage` placeholder before that first
auto-triage attempt.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_card import CHECKBOX_OPTIONS  # noqa: E402
from wheelhouse_core import (  # noqa: E402
    _auto_triage_enabled,
    _auto_triage_issues_enabled,
    load_config,
)

VALID_KINDS = {"pr-review", "ci-approval", "issue-triage"}


def boolish(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def from_payload():
    raw = os.environ.get("PAYLOAD", "").strip()
    if raw:
        try:
            d = json.loads(raw)
            if isinstance(d, dict) and d:
                return dict(d)
        except ValueError:
            pass
    # fall back to workflow_dispatch inputs
    return {
        "repo": os.environ.get("INPUT_REPO", ""),
        "number": os.environ.get("INPUT_NUMBER", ""),
        "kind": os.environ.get("INPUT_KIND", ""),
        "head_sha": os.environ.get("INPUT_HEAD_SHA", ""),
        "updated_at": os.environ.get("INPUT_UPDATED_AT", ""),
        "title": os.environ.get("INPUT_TITLE", ""),
        "summary": os.environ.get("INPUT_SUMMARY", ""),
        "recommendation": os.environ.get("INPUT_RECOMMENDATION", ""),
        "priority": os.environ.get("INPUT_PRIORITY", ""),
        "options": os.environ.get("INPUT_OPTIONS", ""),
    }


def normalize(d):
    repo = str(d.get("repo", "")).strip()
    number = str(d.get("number", "")).strip()
    if not repo or not number:
        sys.exit("build_item: 'repo' and 'number' are required")
    try:
        number = int(number)
    except ValueError:
        sys.exit("build_item: 'number' must be an integer, got %r" % number)

    kind = (d.get("kind") or "pr-review").strip()
    if kind not in VALID_KINDS:
        kind = "pr-review"

    options = d.get("options")
    if isinstance(options, str):
        options = [o.strip() for o in options.split(",") if o.strip()]
    if not options:
        options = CHECKBOX_OPTIONS.get(kind, ["close", "hold"])

    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    path = "pull" if kind in ("pr-review", "ci-approval") else "issues"
    url = d.get("url") or ("https://github.com/%s/%s/%s/%d" % (owner, repo, path, number)
                           if owner else "")
    try:
        cfg = load_config()
        repo_cfg = cfg["repos"].get(repo, {})
        auto_triage = _auto_triage_enabled(repo_cfg, cfg["auto_triage"])
        auto_triage_issues = _auto_triage_issues_enabled(
            repo_cfg, cfg["auto_triage_issues"]
        )
    except SystemExit:
        auto_triage = True
        auto_triage_issues = True
    if "auto_triage" in d and not boolish(d.get("auto_triage")):
        auto_triage = boolish(d.get("auto_triage"))
    if "auto_triage_issues" in d and not boolish(d.get("auto_triage_issues")):
        auto_triage_issues = boolish(d.get("auto_triage_issues"))

    return {
        "repo": repo,
        "number": number,
        "kind": kind,
        "head_sha": str(d.get("head_sha", "") or ""),
        "updated_at": str(d.get("updated_at", "") or ""),
        "title": str(d.get("title", "") or "(no title)"),
        "author": str(d.get("author", "") or "?"),
        "bucket": str(d.get("bucket", "") or ""),
        "comp": str(d.get("comp", "") or "n/a"),
        "tests": str(d.get("tests", "") or "n/a"),
        "url": url,
        "summary": str(d.get("summary", "") or ""),
        "recommendation": str(d.get("recommendation", "") or "Needs your call."),
        "priority": str(d.get("priority", "") or "med"),
        "options": options,
        "auto_triage": auto_triage,
        "auto_triage_issues": auto_triage_issues,
    }


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "item.json"
    item = normalize(from_payload())
    with open(out, "w") as f:
        json.dump(item, f, indent=2)
    print("wrote %s for %s#%s" % (out, item["repo"], item["number"]))


if __name__ == "__main__":
    main()
