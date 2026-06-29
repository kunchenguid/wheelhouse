#!/usr/bin/env python3
"""
Wheelhouse - normalize an ingest payload into a decision-card item.

The `ingest` workflow feeds this either a repository_dispatch client_payload
(JSON in $PAYLOAD) or workflow_dispatch inputs ($INPUT_*). It produces a single
normalized item.json that render_card.py can turn into a card.

Expected fields (all but repo/number optional):
  repo, number, kind, head_sha, title, author, bucket, comp, tests,
  summary, recommendation, priority, options (list or comma string)

When omitted, `options` defaults by kind via render_card.CHECKBOX_OPTIONS:
pr-review and issue-triage include the non-consuming `investigate` checkbox;
ci-approval does not.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_card import CHECKBOX_OPTIONS  # noqa: E402

VALID_KINDS = {"pr-review", "ci-approval", "issue-triage"}


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

    return {
        "repo": repo,
        "number": number,
        "kind": kind,
        "head_sha": str(d.get("head_sha", "") or ""),
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
    }


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "item.json"
    item = normalize(from_payload())
    with open(out, "w") as f:
        json.dump(item, f, indent=2)
    print("wrote %s for %s#%s" % (out, item["repo"], item["number"]))


if __name__ == "__main__":
    main()
