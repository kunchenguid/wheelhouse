#!/usr/bin/env python3
"""
Unit-exercise stale pending-contributor-action cleanup with NO network.

Run: python tests/test_pending_contributor_cleanup.py
"""
import contextlib
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import wheelhouse_core as core  # noqa: E402

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


OWNER = {"login": "owner", "__typename": "User"}
MAINTAINER = {"login": "co-maintainer", "__typename": "User"}
CONTRIBUTOR = {"login": "contributor", "__typename": "User"}
OTHER_HUMAN = {"login": "other", "__typename": "User"}
BOT = {"login": "github-actions[bot]", "__typename": "Bot"}
REST_BOT = {"login": "release-please", "type": "Bot"}
BASE = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def ts(days=0, seconds=0):
    return (BASE + timedelta(days=days, seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def issue(labels=None, user=CONTRIBUTOR, state="open", updated_at=None):
    data = {
        "state": state,
        "user": user,
        "labels": [{"name": name} for name in (labels or [])],
    }
    if updated_at is not None:
        data["updated_at"] = updated_at
    return data


def pr(head="sha1", user=CONTRIBUTOR, state="open", updated_at=None):
    data = {"state": state, "head": {"sha": head}, "user": user}
    if updated_at is not None:
        data["updated_at"] = updated_at
    return data


def comment(body, when, user=OWNER, cid=1):
    return {"id": cid, "body": body, "created_at": when, "user": user}


def review(rid, when, user=OWNER, state="CHANGES_REQUESTED", body=""):
    return {
        "id": rid,
        "state": state,
        "submitted_at": when,
        "user": user,
        "body": body,
    }


def review_comment(cid, when, user=CONTRIBUTOR):
    return {"id": cid, "body": "note", "created_at": when, "user": user}


def timeline_event(event, when, actor=CONTRIBUTOR):
    return {"event": event, "created_at": when, "actor": actor}


def reviewed_event(rid, when, user=CONTRIBUTOR, state="changes_requested"):
    # A GitHub `reviewed` timeline event carries `submitted_at`, not `created_at`.
    ev = {"event": "reviewed", "id": rid, "user": user, "state": state}
    if when is not None:
        ev["submitted_at"] = when
    return ev


def commit_event(when, author=CONTRIBUTOR, committer=None):
    event = {"event": "committed", "author": author}
    if when is not None:
        event["created_at"] = when
    if committer is not None:
        event["committer"] = committer
    return event


def body_edit(when, editor=CONTRIBUTOR):
    return {"editedAt": when, "editor": editor}


def request_record(source_id=101, asked_at=None, head="sha1"):
    return core._pending_record(
        "demo",
        7,
        "request-changes",
        asked_at or ts(),
        head,
        "contributor",
        "owner",
        source_id,
    )


def rebase_record(source_id=201, asked_at=None, head="sha1"):
    return core._pending_record(
        "demo",
        7,
        "needs-rebase",
        asked_at or ts(),
        head,
        "contributor",
        "owner",
        source_id,
    )


def pending_comment(record, cid=2, when=None, user=OWNER):
    return comment(
        core._pending_contributor_marker(record), when or ts(seconds=1), user, cid
    )


def reminder_comment(record, cid=3, when=None, user=OWNER, marker_when=None):
    marker_when = marker_when or when or ts(10)
    marker = core._pending_contributor_reminder_marker(record["ask_id"], marker_when)
    return comment("reminder\n\n" + marker, when or ts(10), user, cid)


class FakeGitHub:
    def __init__(
        self,
        *,
        issue_obj=None,
        pr_obj=None,
        comments=None,
        reviews=None,
        review_comments=None,
        timeline=None,
        body_edits=None,
        post_comment_error=False,
        patch_error=False,
        timeline_error=False,
        body_edits_error=False,
        review_rereads=None,
        reread_error=False,
    ):
        self.issue = issue_obj or issue([core.PENDING_CONTRIBUTOR_LABEL])
        self.pr = pr_obj or pr()
        self.comments = list(comments or [])
        self.reviews = list(reviews or [])
        self.review_comments = list(review_comments or [])
        self.timeline = list(timeline or [])
        self.body_edits = list(body_edits or [])
        self.post_comment_error = post_comment_error
        self.patch_error = patch_error
        self.timeline_error = timeline_error
        self.body_edits_error = body_edits_error
        self.review_rereads = dict(review_rereads or {})
        self.reread_error = reread_error
        self.calls = []
        self._fill_target_updated_at()

    def _known_updated_at(self):
        times = []
        items = (
            self.comments
            + self.reviews
            + self.review_comments
            + self.timeline
            + self.body_edits
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("created_at", "submitted_at", "updated_at", "editedAt"):
                dt = core._parse_time(item.get(key))
                if dt is not None:
                    times.append(dt)
            if item.get("event") == "committed":
                for key in ("author", "committer"):
                    actor = item.get(key)
                    if isinstance(actor, dict):
                        dt = core._parse_time(actor.get("date"))
                        if dt is not None:
                            times.append(dt)
        return core._format_time(max(times)) if times else ts()

    def _fill_target_updated_at(self):
        updated_at = self._known_updated_at()
        self.issue.setdefault("updated_at", updated_at)
        self.pr.setdefault("updated_at", updated_at)

    def gh_rest(self, path, method=None, fields=None, jq=None, paginate=False, slurp=False):
        self.calls.append({"path": path, "method": method, "fields": fields})
        if method == "POST" and path.endswith("/comments"):
            if self.post_comment_error:
                raise RuntimeError("comment post failed")
            body = (fields or {}).get("body", "")
            new = comment(body, ts(20), OWNER, len(self.comments) + 1)
            self.comments.append(new)
            return dict(new)
        if method == "PATCH" and path.endswith("/issues/7"):
            if self.patch_error:
                raise RuntimeError("close patch failed")
            self.issue["state"] = "closed"
            self.pr["state"] = "closed"
            return {}
        if method == "DELETE" and "/labels/" in path:
            label = path.rsplit("/", 1)[-1].replace("%3A", ":")
            self.issue["labels"] = [
                item
                for item in self.issue.get("labels", [])
                if item.get("name") != label
            ]
            return {}
        if method == "POST" and path.endswith("/issues/7/labels"):
            label = (fields or {}).get("labels[]")
            self.issue.setdefault("labels", []).append({"name": label})
            return {}
        if method == "POST" and path.endswith("/labels"):
            return {}
        if path.endswith("/issues/7"):
            return self.issue
        if path.endswith("/pulls/7"):
            return self.pr
        if path.endswith("/issues/7/comments?per_page=100"):
            return [self.comments] if slurp else self.comments
        if path.endswith("/pulls/7/reviews?per_page=100"):
            return [self.reviews] if slurp else self.reviews
        m = re.search(r"/pulls/7/reviews/(\d+)$", path)
        if m and method is None:
            if self.reread_error:
                raise RuntimeError("review re-read failed")
            rid = int(m.group(1))
            return self.review_rereads.get(rid, {"id": rid})
        if path.endswith("/pulls/7/comments?per_page=100"):
            return [self.review_comments] if slurp else self.review_comments
        if path.endswith("/issues/7/timeline?per_page=100"):
            if self.timeline_error:
                raise RuntimeError("timeline unavailable")
            return [self.timeline] if slurp else self.timeline
        raise AssertionError("unexpected gh_rest path: %s" % path)

    def gh_graphql_pr_user_content_edits_page(self, owner, name, number, after=None):
        if self.body_edits_error:
            raise RuntimeError("PR edit history unavailable")
        if owner != "owner" or name != "demo" or int(number) != 7:
            raise AssertionError("unexpected edit history target: %s/%s#%s" % (owner, name, number))
        return {
            "nodes": self.body_edits,
            "totalCount": len(self.body_edits),
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }


@contextlib.contextmanager
def patch_rest(fake):
    old_rest = core.gh_rest
    old_edits = core.gh_graphql_pr_user_content_edits_page
    core.gh_rest = fake.gh_rest
    core.gh_graphql_pr_user_content_edits_page = (
        fake.gh_graphql_pr_user_content_edits_page
    )
    try:
        yield
    finally:
        core.gh_rest = old_rest
        core.gh_graphql_pr_user_content_edits_page = old_edits


def enriched_pr(
    labels=None,
    bucket="review-needed",
    head="sha1",
    author_excluded=False,
    mergeable=None,
    kind=None,
):
    node = {
        "number": 7,
        "labels": labels if labels is not None else [core.PENDING_CONTRIBUTOR_LABEL],
        "bucket": bucket,
        "head_sha": head,
        "author_excluded": author_excluded,
    }
    if mergeable is not None:
        node["mergeable"] = mergeable
    if kind is not None:
        node["kind"] = kind
    return node


def run(fake, pr_node=None, now_days=0):
    pr_node = pr_node or enriched_pr()
    with patch_rest(fake):
        return core.sweep_pending_contributor_actions(
            "owner",
            {"name": "demo"},
            [pr_node],
            {"owner", "co-maintainer"},
            enabled=True,
            reminder_days=10,
            cleanup_days=14,
            targets={"pr"},
            now=BASE + timedelta(days=now_days),
        )


def base_request_state(*, comments=None, reviews=None, **kwargs):
    record = request_record()
    comments = list(comments) if comments is not None else [pending_comment(record)]
    reviews = list(reviews) if reviews is not None else [review(101, ts())]
    return record, FakeGitHub(comments=comments, reviews=reviews, **kwargs)


def test_config_defaults_off_and_per_repo_override():
    repo_cfg = {}
    check("config: cleanup defaults off",
          core._pending_contributor_cleanup_enabled(repo_cfg, False) is False)
    check("config: per-repo override can enable cleanup",
          core._pending_contributor_cleanup_enabled({"pending_contributor_cleanup": True}, False) is True)
    check("config: default targets are PRs",
          core._pending_contributor_cleanup_targets({}, ["pr"]) == {"pr"})
    check("config: explicit empty global targets stay empty",
          core._pending_contributor_cleanup_targets({}, []) == set())
    check("config: explicit empty repo targets stay empty",
          core._pending_contributor_cleanup_targets({"pending_contributor_cleanup_targets": []}, ["pr"]) == set())
    check("config: explicit empty set targets stay empty",
          core._pending_contributor_cleanup_targets({"pending_contributor_cleanup_targets": set()}, ["pr"]) == set())
    check("config: explicit null global targets stay empty",
          core._pending_contributor_cleanup_targets({}, None) == set())
    check("config: explicit null repo targets stay empty",
          core._pending_contributor_cleanup_targets({"pending_contributor_cleanup_targets": None}, ["pr"]) == set())
    check("config: explicit invalid global targets stay empty",
          core._pending_contributor_cleanup_targets({}, False) == set())
    check("config: explicit invalid repo targets stay empty",
          core._pending_contributor_cleanup_targets({"pending_contributor_cleanup_targets": {}}, ["pr"]) == set())


def test_no_action_before_reminder_threshold():
    _, fake = base_request_state()
    closed = run(fake, now_days=9)
    check("clock: before reminder threshold no close", closed == set())
    check("clock: before reminder threshold no comment posted",
          not any(c["method"] == "POST" and c["path"].endswith("/comments") for c in fake.calls))


def test_reminder_at_threshold_and_idempotent():
    record, fake = base_request_state()
    run(fake, now_days=10)
    reminder_posts = [
        c for c in fake.calls
        if c["method"] == "POST" and c["path"].endswith("/comments")
        and core.PENDING_CONTRIBUTOR_REMINDER_PREFIX in c["fields"]["body"]
    ]
    check("clock: reminder threshold posts one reminder", len(reminder_posts) == 1)
    run(fake, now_days=11)
    reminder_posts = [
        c for c in fake.calls
        if c["method"] == "POST" and c["path"].endswith("/comments")
        and core.PENDING_CONTRIBUTOR_REMINDER_PREFIX in c["fields"]["body"]
    ]
    check("clock: existing reminder prevents duplicate", len(reminder_posts) == 1)
    check("clock: reminder is for the active ask",
          record["ask_id"] in reminder_posts[0]["fields"]["body"])


def test_close_threshold_requires_prior_reminder():
    _, fake = base_request_state()
    closed = run(fake, now_days=14)
    check("clock: close threshold without reminder does not close", closed == set())
    check("clock: close threshold without reminder nudges first",
          any(core.PENDING_CONTRIBUTOR_REMINDER_PREFIX in c["fields"].get("body", "")
              for c in fake.calls if c["method"] == "POST"))


def test_close_threshold_requires_proven_reminder():
    cases = [
        (
            "untrusted author",
            lambda record: reminder_comment(record, user=BOT),
        ),
        (
            "pre-ask comment",
            lambda record: reminder_comment(record, when=ts(seconds=-1)),
        ),
        (
            "pre-ask marker time",
            lambda record: reminder_comment(
                record, when=ts(10), marker_when=ts(seconds=-1)
            ),
        ),
    ]
    for label, make_reminder in cases:
        record = request_record()
        fake = FakeGitHub(
            comments=[pending_comment(record), make_reminder(record)],
            reviews=[review(101, ts())],
        )
        closed = run(fake, now_days=14)
        reminder_posts = [
            c for c in fake.calls
            if c["method"] == "POST" and c["path"].endswith("/comments")
            and core.PENDING_CONTRIBUTOR_REMINDER_PREFIX in c["fields"]["body"]
        ]
        check("proof: %s reminder does not close" % label, closed == set())
        check("proof: %s reminder is replaced with a real nudge" % label,
              len(reminder_posts) == 1)


def test_close_after_prior_reminder_and_comment_content():
    record = request_record()
    fake = FakeGitHub(
        comments=[pending_comment(record), reminder_comment(record)],
        reviews=[review(101, ts())],
    )
    closed = run(fake, now_days=14)
    close_posts = [
        c for c in fake.calls
        if c["method"] == "POST" and c["path"].endswith("/comments")
        and "I am closing this because I requested changes on 2026-01-01" in c["fields"]["body"]
    ]
    check("close: PR number returned as closed", closed == {7})
    check("close: clear first-person close comment posted", len(close_posts) == 1)
    check("close: comment tells contributor how to continue",
          "reopen it or open a new PR" in close_posts[0]["fields"]["body"])
    patch_calls = [c for c in fake.calls if c["method"] == "PATCH" and c["path"].endswith("/issues/7")]
    check("close: target closes after the comment", len(patch_calls) == 1)


def test_close_comment_failure_fails_open():
    record = request_record()
    fake = FakeGitHub(
        comments=[pending_comment(record), reminder_comment(record)],
        reviews=[review(101, ts())],
        post_comment_error=True,
    )
    closed = run(fake, now_days=14)
    check("fail-open: close comment failure does not close", closed == set())
    check("fail-open: close patch not attempted after comment failure",
          not any(c["method"] == "PATCH" for c in fake.calls))


def test_close_patch_failure_does_not_repost_close_comment():
    record = request_record()
    fake = FakeGitHub(
        comments=[pending_comment(record), reminder_comment(record)],
        reviews=[review(101, ts())],
        patch_error=True,
    )
    closed = run(fake, now_days=14)
    check("fail-open: close patch failure does not report closed", closed == set())
    closed = run(fake, now_days=15)
    close_posts = [
        c for c in fake.calls
        if c["method"] == "POST" and c["path"].endswith("/comments")
        and "I am closing this because I requested changes" in c["fields"]["body"]
    ]
    patch_calls = [
        c for c in fake.calls
        if c["method"] == "PATCH" and c["path"].endswith("/issues/7")
    ]
    check("fail-open: close patch retry still does not report closed", closed == set())
    check("fail-open: close comment is not reposted on patch retry",
          len(close_posts) == 1)
    check("fail-open: close patch is retried without another close comment",
          len(patch_calls) == 2)


def test_existing_unmarked_close_comment_is_idempotent():
    record = request_record()
    fake = FakeGitHub(
        comments=[
            pending_comment(record),
            reminder_comment(record),
            comment(core._pending_close_body(record), ts(14), OWNER, 9),
        ],
        reviews=[review(101, ts())],
    )
    closed = run(fake, now_days=15)
    close_posts = [
        c for c in fake.calls
        if c["method"] == "POST" and c["path"].endswith("/comments")
        and "I am closing this because I requested changes" in c["fields"]["body"]
    ]
    check("close: existing unmarked close comment still closes", closed == {7})
    check("close: existing unmarked close comment is not reposted", close_posts == [])


def test_contributor_activity_blocks_and_clears_pending_label():
    record = request_record()
    fake = FakeGitHub(
        comments=[
            pending_comment(record),
            comment("I pushed a fix", ts(1), CONTRIBUTOR, 4),
        ],
        reviews=[review(101, ts())],
    )
    closed = run(fake, now_days=14)
    labels = [item["name"] for item in fake.issue["labels"]]
    check("activity: contributor comment blocks close", closed == set())
    check("activity: pending label removed after contributor follow-up",
          core.PENDING_CONTRIBUTOR_LABEL not in labels)


def test_maintainer_and_bot_activity_do_not_reset_clock():
    record = request_record()
    fake = FakeGitHub(
        comments=[
            pending_comment(record),
            comment("owner note", ts(1), OWNER, 4),
            comment("bot note", ts(2), BOT, 5),
            comment("REST bot note", ts(3), REST_BOT, 6),
            reminder_comment(record),
        ],
        reviews=[review(101, ts())],
    )
    closed = run(fake, now_days=14)
    check("activity: maintainer and bot comments do not reset", closed == {7})


def test_exact_timestamp_equality_does_not_count_as_followup():
    record = request_record()
    fake = FakeGitHub(
        comments=[
            pending_comment(record),
            comment("same second", ts(), CONTRIBUTOR, 4),
            reminder_comment(record),
        ],
        reviews=[review(101, ts())],
    )
    closed = run(fake, now_days=14)
    check("activity: contributor comment at exact ask timestamp does not reset",
          closed == {7})


def test_review_comment_and_pr_body_edit_activity_block_close():
    record, fake = base_request_state(
        review_comments=[review_comment(9, ts(1), CONTRIBUTOR)]
    )
    closed = run(fake, now_days=14)
    check("activity: contributor review comment blocks close", closed == set())

    record = request_record()
    fake = FakeGitHub(
        issue_obj=issue([core.PENDING_CONTRIBUTOR_LABEL], updated_at=ts(12)),
        pr_obj=pr(updated_at=ts(12)),
        comments=[pending_comment(record), reminder_comment(record)],
        reviews=[review(101, ts())],
    )
    closed = run(fake, now_days=14)
    check("fail-open: unaccounted PR body edit blocks close", closed == set())

    record = request_record()
    fake = FakeGitHub(
        issue_obj=issue([core.PENDING_CONTRIBUTOR_LABEL], updated_at=ts(10)),
        pr_obj=pr(updated_at=ts(1)),
        comments=[
            pending_comment(record),
            comment("owner note", ts(2), OWNER, 8),
            reminder_comment(record),
        ],
        reviews=[review(101, ts())],
    )
    closed = run(fake, now_days=14)
    check("fail-open: later visible issue activity does not mask PR body edit",
          closed == set())


def test_pr_body_edit_history_blocks_close_when_updated_at_is_masked():
    record = request_record()
    fake = FakeGitHub(
        issue_obj=issue([core.PENDING_CONTRIBUTOR_LABEL], updated_at=ts(10)),
        pr_obj=pr(updated_at=ts(10)),
        comments=[
            pending_comment(record),
            comment("owner note after hidden edit", ts(10), OWNER, 8),
            reminder_comment(record, cid=9, when=ts(10, seconds=1)),
        ],
        reviews=[review(101, ts())],
        body_edits=[body_edit(ts(1), CONTRIBUTOR)],
    )
    closed = run(fake, now_days=14)
    labels = [item["name"] for item in fake.issue["labels"]]
    check("activity: contributor PR body edit blocks close after later activity",
          closed == set())
    check("activity: contributor PR body edit clears pending label",
          core.PENDING_CONTRIBUTOR_LABEL not in labels)


def test_pr_push_activity_blocks_close():
    record = request_record()
    fake = FakeGitHub(
        comments=[pending_comment(record), reminder_comment(record)],
        reviews=[review(101, ts())],
        timeline=[commit_event(ts(1), CONTRIBUTOR)],
    )
    closed = run(fake, now_days=14)
    labels = [item["name"] for item in fake.issue["labels"]]
    check("activity: contributor commit blocks close", closed == set())
    check("activity: contributor commit clears pending label",
          core.PENDING_CONTRIBUTOR_LABEL not in labels)

    record = request_record()
    fake = FakeGitHub(
        comments=[pending_comment(record), reminder_comment(record)],
        reviews=[review(101, ts())],
        timeline=[commit_event(ts(1), MAINTAINER)],
    )
    closed = run(fake, now_days=14)
    labels = [item["name"] for item in fake.issue["labels"]]
    check("activity: actorless committed event blocks close", closed == set())
    check("activity: actorless committed event clears pending label",
          core.PENDING_CONTRIBUTOR_LABEL not in labels)

    record = request_record()
    fake = FakeGitHub(
        comments=[pending_comment(record), reminder_comment(record)],
        reviews=[review(101, ts())],
        timeline=[timeline_event("head_ref_force_pushed", ts(1), CONTRIBUTOR)],
    )
    closed = run(fake, now_days=14)
    labels = [item["name"] for item in fake.issue["labels"]]
    check("activity: contributor force-push blocks close", closed == set())
    check("activity: contributor force-push clears pending label",
          core.PENDING_CONTRIBUTOR_LABEL not in labels)


def test_unhandled_contributor_timeline_activity_blocks_close():
    record = request_record()
    fake = FakeGitHub(
        comments=[pending_comment(record), reminder_comment(record)],
        reviews=[review(101, ts())],
        timeline=[timeline_event("ready_for_review", ts(1), CONTRIBUTOR)],
    )
    closed = run(fake, now_days=14)
    labels = [item["name"] for item in fake.issue["labels"]]
    check("activity: contributor ready_for_review blocks close", closed == set())
    check("activity: contributor ready_for_review clears pending label",
          core.PENDING_CONTRIBUTOR_LABEL not in labels)


def test_unknown_push_activity_fails_open():
    record = request_record()
    fake = FakeGitHub(
        comments=[pending_comment(record), reminder_comment(record)],
        reviews=[review(101, ts())],
        timeline=[commit_event(ts(1), {})],
    )
    closed = run(fake, now_days=14)
    check("fail-open: ambiguous post-ask commit skips cleanup", closed == set())


def test_head_change_blocks_and_clears_pending_label():
    record = request_record(head="oldsha")
    fake = FakeGitHub(
        pr_obj=pr(head="newsha"),
        comments=[pending_comment(record)],
        reviews=[review(101, ts())],
    )
    closed = run(fake, now_days=14)
    labels = [item["name"] for item in fake.issue["labels"]]
    check("activity: head change blocks close", closed == set())
    check("activity: head change clears pending label",
          core.PENDING_CONTRIBUTOR_LABEL not in labels)


def test_keep_open_and_unknown_timeline_fail_open():
    record = request_record()
    fake = FakeGitHub(
        issue_obj=issue([core.PENDING_CONTRIBUTOR_LABEL, core.PENDING_CONTRIBUTOR_KEEP_OPEN_LABEL]),
        comments=[pending_comment(record), reminder_comment(record)],
        reviews=[review(101, ts())],
    )
    closed = run(fake, now_days=14)
    check("hold: keep-open label skips cleanup", closed == set())

    _, fake = base_request_state(timeline_error=True)
    closed = run(fake, now_days=14)
    check("fail-open: unreadable timeline skips cleanup", closed == set())

    _, fake = base_request_state(body_edits_error=True)
    closed = run(fake, now_days=14)
    check("fail-open: unreadable PR edit history skips cleanup", closed == set())


def test_unknown_author_fails_open():
    record = request_record()
    fake = FakeGitHub(
        comments=[
            pending_comment(record),
            {"id": 4, "body": "mystery", "created_at": ts(1), "user": {}},
        ],
        reviews=[review(101, ts())],
    )
    closed = run(fake, now_days=14)
    check("fail-open: ambiguous post-ask author skips cleanup", closed == set())


def test_legacy_rebase_marker_reminds_then_closes_when_provable():
    marker = core._rebase_nudge_marker("sha1")
    fake = FakeGitHub(
        issue_obj=issue([]),
        comments=[comment("please rebase\n\n" + marker, ts(), OWNER, 201)],
        reviews=[],
    )
    closed = run(fake, enriched_pr(labels=[], bucket="needs-rebase"), now_days=14)
    check("retro: legacy rebase marker gets reminder first", closed == set())
    check("retro: reminder added pending label for future scans",
          any(item["name"] == core.PENDING_CONTRIBUTOR_LABEL for item in fake.issue["labels"]))

    legacy_record = rebase_record(source_id=201)
    fake = FakeGitHub(
        issue_obj=issue([]),
        comments=[
            comment("please rebase\n\n" + marker, ts(), OWNER, 201),
            reminder_comment(legacy_record),
        ],
        reviews=[],
    )
    closed = run(fake, enriched_pr(labels=[], bucket="needs-rebase"), now_days=14)
    check("retro: legacy rebase marker with reminder can close", closed == {7})


def test_legacy_rebase_skip_when_timestamp_missing():
    marker = core._rebase_nudge_marker("sha1")
    fake = FakeGitHub(
        issue_obj=issue([]),
        comments=[{"id": 201, "body": "please rebase\n\n" + marker, "user": OWNER}],
        reviews=[],
    )
    closed = run(fake, enriched_pr(labels=[], bucket="needs-rebase"), now_days=14)
    check("retro: legacy marker without timestamp skips", closed == set())


def test_legacy_rebase_requires_trusted_marker_author():
    marker = core._rebase_nudge_marker("sha1")
    legacy_record = rebase_record(source_id=201)
    fake = FakeGitHub(
        issue_obj=issue([]),
        comments=[
            comment("forged\n\n" + marker, ts(), CONTRIBUTOR, 201),
            reminder_comment(legacy_record),
        ],
        reviews=[],
    )
    closed = run(fake, enriched_pr(labels=[], bucket="needs-rebase"), now_days=14)
    check("retro: untrusted legacy marker skips", closed == set())


def test_rebase_cleanup_clears_when_pr_no_longer_conflicted():
    record = rebase_record(source_id=201)
    body = "please rebase\n\n%s\n%s" % (
        core._rebase_nudge_marker("sha1"),
        core._pending_contributor_marker(record),
    )
    fake = FakeGitHub(
        comments=[comment(body, ts(), OWNER, 201), reminder_comment(record)],
        reviews=[],
    )
    closed = run(fake, enriched_pr(bucket="review-needed"), now_days=14)
    labels = [item["name"] for item in fake.issue["labels"]]
    check("rebase: unblocked PR does not close", closed == set())
    check("rebase: unblocked PR clears stale pending label",
          core.PENDING_CONTRIBUTOR_LABEL not in labels)


def test_legacy_rebase_cleanup_clears_when_pr_no_longer_conflicted():
    marker = core._rebase_nudge_marker("sha1")
    legacy_record = rebase_record(source_id=201)
    fake = FakeGitHub(
        comments=[
            comment("please rebase\n\n" + marker, ts(), OWNER, 201),
            reminder_comment(legacy_record),
        ],
        reviews=[],
    )
    closed = run(fake, enriched_pr(bucket="review-needed"), now_days=14)
    labels = [item["name"] for item in fake.issue["labels"]]
    check("legacy rebase: unblocked PR does not close", closed == set())
    check("legacy rebase: unblocked PR clears stale pending label",
          core.PENDING_CONTRIBUTOR_LABEL not in labels)


def test_legacy_rebase_cleanup_clears_after_head_move_and_unblock():
    marker = core._rebase_nudge_marker("oldsha")
    legacy_record = rebase_record(source_id=201, head="oldsha")
    fake = FakeGitHub(
        pr_obj=pr(head="newsha"),
        comments=[
            comment("please rebase\n\n" + marker, ts(), OWNER, 201),
            reminder_comment(legacy_record),
        ],
        reviews=[],
    )
    closed = run(fake, enriched_pr(bucket="review-needed", head="newsha"), now_days=14)
    labels = [item["name"] for item in fake.issue["labels"]]
    check("legacy rebase: moved unblocked PR does not close", closed == set())
    check("legacy rebase: moved unblocked PR clears stale pending label",
          core.PENDING_CONTRIBUTOR_LABEL not in labels)


def test_untrusted_pending_marker_skips_even_with_label():
    record = request_record()
    fake = FakeGitHub(
        comments=[pending_comment(record, user=CONTRIBUTOR), reminder_comment(record)],
        reviews=[review(101, ts())],
    )
    closed = run(fake, now_days=14)
    check("proof: untrusted marker comment skips cleanup", closed == set())


def test_truncated_scan_labels_fall_back_to_target_label_read():
    record = request_record()
    fake = FakeGitHub(
        comments=[pending_comment(record), reminder_comment(record)],
        reviews=[review(101, ts())],
    )
    pr_node = enriched_pr(labels=["first-20-only"], bucket="review-needed")
    pr_node["labels_truncated"] = True
    closed = run(fake, pr_node, now_days=14)
    check("candidate: truncated scan labels still verify target label", closed == {7})


def test_non_arming_signals_do_not_enter_cleanup():
    record = request_record()
    fake = FakeGitHub(comments=[pending_comment(record)], reviews=[review(101, ts())])
    closed = run(fake, enriched_pr(labels=[], bucket="fix-tests"), now_days=14)
    check("non-arm: fix-tests without pending label is ignored", closed == set())
    check("non-arm: no target reads for non-candidate",
          fake.calls == [])


def test_ci_approval_and_disabled_cleanup_are_out_of_scope():
    record = request_record()
    fake = FakeGitHub(comments=[pending_comment(record)], reviews=[review(101, ts())])
    with patch_rest(fake):
        closed = core.sweep_pending_contributor_actions(
            "owner",
            {"name": "demo"},
            [enriched_pr(bucket="needs-ci-approval")],
            {"owner", "co-maintainer"},
            enabled=False,
            targets={"pr"},
            now=BASE + timedelta(days=14),
        )
    check("scope: disabled cleanup does nothing", closed == set())
    check("scope: disabled cleanup performs no target reads", fake.calls == [])

    fake = FakeGitHub(comments=[pending_comment(record)], reviews=[review(101, ts())])
    with patch_rest(fake):
        closed = core.sweep_pending_contributor_actions(
            "owner",
            {"name": "demo"},
            [enriched_pr(bucket="needs-ci-approval")],
            {"owner", "co-maintainer"},
            enabled=True,
            targets={"pr"},
            now=BASE + timedelta(days=14),
        )
    check("scope: ci-approval targets are ignored even with a pending label",
          closed == set() and fake.calls == [])

    fake = FakeGitHub(comments=[pending_comment(record)], reviews=[review(101, ts())])
    with patch_rest(fake):
        closed = core.sweep_pending_contributor_actions(
            "owner",
            {"name": "demo"},
            [enriched_pr()],
            {"owner", "co-maintainer"},
            enabled=True,
            targets=set(),
            now=BASE + timedelta(days=14),
        )
    check("scope: explicit empty cleanup targets do nothing", closed == set())
    check("scope: explicit empty cleanup targets perform no target reads",
          fake.calls == [])

    fake = FakeGitHub(comments=[pending_comment(record)], reviews=[review(101, ts())])
    with patch_rest(fake):
        closed = core.sweep_pending_contributor_actions(
            "owner",
            {"name": "demo"},
            [enriched_pr()],
            {"owner", "co-maintainer"},
            enabled=True,
            targets=False,
            now=BASE + timedelta(days=14),
        )
    check("scope: explicit invalid cleanup targets do nothing", closed == set())
    check("scope: explicit invalid cleanup targets perform no target reads",
          fake.calls == [])


def test_reviewed_timeline_event_uses_submitted_at():
    # A `reviewed` timeline event carries `submitted_at`, not `created_at`.
    ev = reviewed_event(555, ts(2), user=MAINTAINER)
    check("reviewed event time read from submitted_at",
          core._timeline_event_time(ev, "reviewed") == core._parse_time(ts(2)))
    check("non-reviewed event time still read from created_at",
          core._timeline_event_time(timeline_event("labeled", ts(3)), "labeled")
          == core._parse_time(ts(3)))

    # Reproduction of the observed "reviewed event missing timestamp" fail-open:
    # a maintainer review recorded in the timeline must not block the close.
    asked = core._parse_time(ts())
    state = {
        "issue": {"state": "open", "updated_at": ts(2)},
        "pr": {"state": "open", "updated_at": ts(2)},
        "comments": [],
        "reviews": [review(555, ts(2), user=MAINTAINER)],
        "review_comments": [],
        "timeline": [reviewed_event(555, ts(2), user=MAINTAINER)],
        "body_edits": [],
    }
    check("reviewed maintainer event no longer raises missing-timestamp",
          core._has_qualifying_contributor_activity(state, asked, {"owner", "co-maintainer"})
          is False)

    # A contributor review in the timeline is still detected as activity.
    state_contrib = dict(state)
    state_contrib["reviews"] = [review(556, ts(2), user=CONTRIBUTOR)]
    state_contrib["timeline"] = [reviewed_event(556, ts(2), user=CONTRIBUTOR)]
    check("reviewed contributor event counts as qualifying activity",
          core._has_qualifying_contributor_activity(
              state_contrib, asked, {"owner", "co-maintainer"}) is True)


def test_reviewed_event_missing_timestamp_recovered_by_reread():
    # A nudged backlog PR whose maintainer review carries no usable timestamp in
    # either the reviews list or the timeline: the authoritative time is
    # recovered by re-reading the review by id, so the provable ask is not
    # discarded over one missing field.
    marker = core._rebase_nudge_marker("sha1")
    legacy_record = rebase_record(source_id=201)
    fake = FakeGitHub(
        issue_obj=issue([]),
        comments=[
            comment("please rebase\n\n" + marker, ts(), OWNER, 201),
            reminder_comment(legacy_record),
        ],
        reviews=[{"id": 555, "state": "CHANGES_REQUESTED", "user": MAINTAINER}],
        timeline=[reviewed_event(555, None, user=MAINTAINER)],
        review_rereads={555: {"id": 555, "submitted_at": ts(2)}},
    )
    closed = run(fake, enriched_pr(labels=[], bucket="needs-rebase"), now_days=14)
    check("reread: recovered review timestamp lets close proceed", closed == {7})
    check("reread: review re-read was issued exactly once",
          sum(1 for c in fake.calls if re.search(r"/pulls/7/reviews/555$", c["path"]))
          == 1)


def test_reviewed_event_reread_failure_fails_open():
    marker = core._rebase_nudge_marker("sha1")
    legacy_record = rebase_record(source_id=201)
    fake = FakeGitHub(
        issue_obj=issue([]),
        comments=[
            comment("please rebase\n\n" + marker, ts(), OWNER, 201),
            reminder_comment(legacy_record),
        ],
        reviews=[{"id": 555, "state": "CHANGES_REQUESTED", "user": MAINTAINER}],
        timeline=[reviewed_event(555, None, user=MAINTAINER)],
        reread_error=True,
    )
    closed = run(fake, enriched_pr(labels=[], bucket="needs-rebase"), now_days=14)
    check("reread: an unreadable review timestamp fails open (no close)",
          closed == set())


def test_unattributable_update_from_review_becomes_attributable():
    # Reproduction of "issue updated after ask without attributable activity":
    # the target updated_at reflects a maintainer review whose time, once read,
    # attributes the update so the clock is not reset.
    asked = core._parse_time(ts())
    state = {
        "issue": {"state": "open", "updated_at": ts(2)},
        "pr": {"state": "open", "updated_at": ts(2)},
        "comments": [],
        "reviews": [review(555, ts(2), user=MAINTAINER)],
        "review_comments": [],
        "timeline": [reviewed_event(555, ts(2), user=MAINTAINER)],
        "body_edits": [],
    }
    # No raise => the update is attributable and this is not qualifying activity.
    check("update from a maintainer review is attributable, not fail-open",
          core._has_qualifying_contributor_activity(
              state, asked, {"owner", "co-maintainer"}) is False)

    # A genuinely unexplained target update still fails open (safety preserved).
    unexplained = dict(state)
    unexplained["issue"] = {"state": "open", "updated_at": ts(5)}
    unexplained["pr"] = {"state": "open", "updated_at": ts(5)}
    raised = False
    try:
        core._has_qualifying_contributor_activity(
            unexplained, asked, {"owner", "co-maintainer"})
    except RuntimeError:
        raised = True
    check("a truly unattributable update still fails open", raised)


def test_widened_backlog_nudged_pr_reminds_then_closes():
    # The already-nudged backlog: an un-armed (no pending marker, no pending
    # label) conflicting fork PR that a maintainer once reviewed. Before the fix
    # the maintainer review blocked it forever; now it enters the two-phase
    # reminder-then-close lifecycle.
    marker = core._rebase_nudge_marker("sha1")
    reviewed = reviewed_event(555, ts(2), user=MAINTAINER)
    fake = FakeGitHub(
        issue_obj=issue([]),
        comments=[comment("please rebase\n\n" + marker, ts(), OWNER, 201)],
        reviews=[review(555, ts(2), user=MAINTAINER)],
        timeline=[reviewed],
    )
    closed = run(fake, enriched_pr(labels=[], bucket="needs-rebase"), now_days=14)
    labels = [item["name"] for item in fake.issue["labels"]]
    check("widen: reviewed backlog nudge reminds first (two-phase)", closed == set())
    check("widen: reminder arms pending label for the later close",
          core.PENDING_CONTRIBUTOR_LABEL in labels)

    legacy_record = rebase_record(source_id=201)
    fake = FakeGitHub(
        issue_obj=issue([]),
        comments=[
            comment("please rebase\n\n" + marker, ts(), OWNER, 201),
            reminder_comment(legacy_record),
        ],
        reviews=[review(555, ts(2), user=MAINTAINER)],
        timeline=[reviewed_event(555, ts(2), user=MAINTAINER)],
    )
    closed = run(fake, enriched_pr(labels=[], bucket="needs-rebase"), now_days=14)
    check("widen: reviewed backlog nudge closes on the later scan", closed == {7})


def test_widened_backlog_respects_keep_open():
    marker = core._rebase_nudge_marker("sha1")
    legacy_record = rebase_record(source_id=201)
    fake = FakeGitHub(
        issue_obj=issue([core.PENDING_CONTRIBUTOR_KEEP_OPEN_LABEL]),
        comments=[
            comment("please rebase\n\n" + marker, ts(), OWNER, 201),
            reminder_comment(legacy_record),
        ],
        reviews=[review(555, ts(2), user=MAINTAINER)],
        timeline=[reviewed_event(555, ts(2), user=MAINTAINER)],
    )
    closed = run(fake, enriched_pr(labels=[], bucket="needs-rebase"), now_days=14)
    check("widen: keep-open opt-out blocks the widened backlog close",
          closed == set())


def test_widened_backlog_contributor_activity_fails_open():
    # A contributor pushed/commented after the ask on a widened-set PR: the clock
    # resets, it must not close (uncertainty / real activity refuses to close).
    marker = core._rebase_nudge_marker("sha1")
    legacy_record = rebase_record(source_id=201)
    fake = FakeGitHub(
        issue_obj=issue([]),
        comments=[
            comment("please rebase\n\n" + marker, ts(), OWNER, 201),
            comment("still working on it", ts(3), CONTRIBUTOR, 9),
            reminder_comment(legacy_record),
        ],
        reviews=[review(555, ts(2), user=MAINTAINER)],
        timeline=[reviewed_event(555, ts(2), user=MAINTAINER)],
    )
    closed = run(fake, enriched_pr(labels=[], bucket="needs-rebase"), now_days=14)
    check("widen: contributor follow-up on a nudged PR does not close",
          closed == set())


def test_widened_ci_noop_conflicting_pr_reminds_then_closes():
    # A ci-noop fork PR: no CI so it routes to needs-ci-approval, but it is
    # CONFLICTING and got a rebase nudge (sibling opportunity #3, which
    # deliberately does not arm). The nudge - not the bucket - is the ask, so it
    # must enter the two-phase reminder-then-close clock from the nudge evidence.
    marker = core._rebase_nudge_marker("sha1")
    fake = FakeGitHub(
        issue_obj=issue([]),
        comments=[comment("please rebase\n\n" + marker, ts(), OWNER, 201)],
        reviews=[],
    )
    closed = run(
        fake,
        enriched_pr(labels=[], bucket="needs-ci-approval", kind="ci-approval",
                    mergeable="CONFLICTING"),
        now_days=14,
    )
    labels = [item["name"] for item in fake.issue["labels"]]
    check("ci-noop: nudged conflicting ci-approval PR reminds first",
          closed == set())
    check("ci-noop: reminder arms pending label for the later close",
          core.PENDING_CONTRIBUTOR_LABEL in labels)

    legacy_record = rebase_record(source_id=201)
    fake = FakeGitHub(
        issue_obj=issue([]),
        comments=[
            comment("please rebase\n\n" + marker, ts(), OWNER, 201),
            reminder_comment(legacy_record),
        ],
        reviews=[],
    )
    closed = run(
        fake,
        enriched_pr(labels=[], bucket="needs-ci-approval", kind="ci-approval",
                    mergeable="CONFLICTING"),
        now_days=14,
    )
    check("ci-noop: nudged conflicting ci-approval PR closes on the later scan",
          closed == {7})


def test_ci_approval_pr_without_fresh_conflict_stays_out_of_scope():
    # The same nudge, but the PR is not authoritatively CONFLICTING this scan
    # (resolved, mergeable, or still recomputing): the fast security-gate lane is
    # out of scope, so nothing is read and nothing is closed - the close path is
    # fail-closed on a fresh, authoritative conflict.
    marker = core._rebase_nudge_marker("sha1")
    legacy_record = rebase_record(source_id=201)
    for mergeable in (None, "MERGEABLE", "UNKNOWN"):
        fake = FakeGitHub(
            issue_obj=issue([]),
            comments=[
                comment("please rebase\n\n" + marker, ts(), OWNER, 201),
                reminder_comment(legacy_record),
            ],
            reviews=[],
        )
        closed = run(
            fake,
            enriched_pr(labels=[], bucket="needs-ci-approval", kind="ci-approval",
                        mergeable=mergeable),
            now_days=14,
        )
        check("ci-noop: non-conflicting ci-approval PR (%s) is not closed"
              % mergeable, closed == set())
        check("ci-noop: non-conflicting ci-approval PR (%s) performs no reads"
              % mergeable, fake.calls == [])


def test_widened_ci_noop_respects_keep_open():
    marker = core._rebase_nudge_marker("sha1")
    legacy_record = rebase_record(source_id=201)
    fake = FakeGitHub(
        issue_obj=issue([core.PENDING_CONTRIBUTOR_KEEP_OPEN_LABEL]),
        comments=[
            comment("please rebase\n\n" + marker, ts(), OWNER, 201),
            reminder_comment(legacy_record),
        ],
        reviews=[],
    )
    closed = run(
        fake,
        enriched_pr(labels=[], bucket="needs-ci-approval", kind="ci-approval",
                    mergeable="CONFLICTING"),
        now_days=14,
    )
    check("ci-noop: keep-open opt-out blocks the widened ci-approval close",
          closed == set())


def main():
    test_config_defaults_off_and_per_repo_override()
    test_no_action_before_reminder_threshold()
    test_reminder_at_threshold_and_idempotent()
    test_close_threshold_requires_prior_reminder()
    test_close_threshold_requires_proven_reminder()
    test_close_after_prior_reminder_and_comment_content()
    test_close_comment_failure_fails_open()
    test_close_patch_failure_does_not_repost_close_comment()
    test_existing_unmarked_close_comment_is_idempotent()
    test_contributor_activity_blocks_and_clears_pending_label()
    test_maintainer_and_bot_activity_do_not_reset_clock()
    test_exact_timestamp_equality_does_not_count_as_followup()
    test_review_comment_and_pr_body_edit_activity_block_close()
    test_pr_body_edit_history_blocks_close_when_updated_at_is_masked()
    test_pr_push_activity_blocks_close()
    test_unhandled_contributor_timeline_activity_blocks_close()
    test_unknown_push_activity_fails_open()
    test_head_change_blocks_and_clears_pending_label()
    test_keep_open_and_unknown_timeline_fail_open()
    test_unknown_author_fails_open()
    test_legacy_rebase_marker_reminds_then_closes_when_provable()
    test_legacy_rebase_skip_when_timestamp_missing()
    test_legacy_rebase_requires_trusted_marker_author()
    test_rebase_cleanup_clears_when_pr_no_longer_conflicted()
    test_legacy_rebase_cleanup_clears_when_pr_no_longer_conflicted()
    test_legacy_rebase_cleanup_clears_after_head_move_and_unblock()
    test_untrusted_pending_marker_skips_even_with_label()
    test_truncated_scan_labels_fall_back_to_target_label_read()
    test_non_arming_signals_do_not_enter_cleanup()
    test_ci_approval_and_disabled_cleanup_are_out_of_scope()
    test_reviewed_timeline_event_uses_submitted_at()
    test_reviewed_event_missing_timestamp_recovered_by_reread()
    test_reviewed_event_reread_failure_fails_open()
    test_unattributable_update_from_review_becomes_attributable()
    test_widened_backlog_nudged_pr_reminds_then_closes()
    test_widened_backlog_respects_keep_open()
    test_widened_backlog_contributor_activity_fails_open()
    test_widened_ci_noop_conflicting_pr_reminds_then_closes()
    test_ci_approval_pr_without_fresh_conflict_stays_out_of_scope()
    test_widened_ci_noop_respects_keep_open()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all pending contributor cleanup tests passed")


if __name__ == "__main__":
    main()
