#!/usr/bin/env python3
"""
Unit-exercise stale pending-contributor-action cleanup with NO network.

Run: python tests/test_pending_contributor_cleanup.py
"""
import contextlib
import os
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


def commit_event(when, author=CONTRIBUTOR, committer=None):
    event = {"event": "committed", "author": author}
    if when is not None:
        event["created_at"] = when
    if committer is not None:
        event["committer"] = committer
    return event


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
        post_comment_error=False,
        patch_error=False,
        timeline_error=False,
    ):
        self.issue = issue_obj or issue([core.PENDING_CONTRIBUTOR_LABEL])
        self.pr = pr_obj or pr()
        self.comments = list(comments or [])
        self.reviews = list(reviews or [])
        self.review_comments = list(review_comments or [])
        self.timeline = list(timeline or [])
        self.post_comment_error = post_comment_error
        self.patch_error = patch_error
        self.timeline_error = timeline_error
        self.calls = []
        self._fill_target_updated_at()

    def _known_updated_at(self):
        times = []
        for item in self.comments + self.reviews + self.review_comments + self.timeline:
            if not isinstance(item, dict):
                continue
            for key in ("created_at", "submitted_at", "updated_at"):
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
        if path.endswith("/pulls/7/comments?per_page=100"):
            return [self.review_comments] if slurp else self.review_comments
        if path.endswith("/issues/7/timeline?per_page=100"):
            if self.timeline_error:
                raise RuntimeError("timeline unavailable")
            return [self.timeline] if slurp else self.timeline
        raise AssertionError("unexpected gh_rest path: %s" % path)


@contextlib.contextmanager
def patch_rest(fake):
    old = core.gh_rest
    core.gh_rest = fake.gh_rest
    try:
        yield
    finally:
        core.gh_rest = old


def enriched_pr(labels=None, bucket="review-needed", head="sha1", author_excluded=False):
    return {
        "number": 7,
        "labels": labels if labels is not None else [core.PENDING_CONTRIBUTOR_LABEL],
        "bucket": bucket,
        "head_sha": head,
        "author_excluded": author_excluded,
    }


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
        timeline=[timeline_event("head_ref_force_pushed", ts(1), CONTRIBUTOR)],
    )
    closed = run(fake, now_days=14)
    labels = [item["name"] for item in fake.issue["labels"]]
    check("activity: contributor force-push blocks close", closed == set())
    check("activity: contributor force-push clears pending label",
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
    test_pr_push_activity_blocks_close()
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
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all pending contributor cleanup tests passed")


if __name__ == "__main__":
    main()
