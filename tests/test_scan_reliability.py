#!/usr/bin/env python3
"""
Unit-exercise the fleet-scan reliability + correctness hardening, NO network:

- GraphQL retry/backoff on transient 5xx/timeout (`_gh_graphql_data`), including
  transient-vs-fatal classification and preservation of fail-safe semantics.
- Smaller-page cursor pagination: multi-page assembly and mid-page failure ->
  truncated (so reconcile will not self-heal from an incomplete view).
- Consecutive-failure health ledger (`parse_scan_health` / `update_scan_health` /
  `render_scan_health_body` / `cmd_scan_health`): increment/reset, threshold
  alert, run-failing exit, and fail-open on ledger I/O errors.
- UNKNOWN-mergeability policy (`_settle_mergeable` / `_resolve_pr_bucket` +
  build_repo): poll a merge-ready or review-needed candidate whose mergeable
  reads UNKNOWN until it settles to CONFLICTING (-> needs-rebase) or MERGEABLE
  (-> its original worklist bucket); if it never settles, return
  MERGEABILITY_PENDING and freeze the PR
  (`indeterminate_pr_numbers`) so an UNKNOWN reading never flips worklist
  membership. Plus the #111 acceptance that a statically-conflicting PR never
  enters the worklist.

Run: python tests/test_scan_reliability.py
"""

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)
import wheelhouse_core as core  # noqa: E402

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ok_stdout(repository=None):
    return json.dumps({"data": {"repository": repository or {}}})


class _RunPatch:
    """Context manager: feed `_gh_graphql_data` a scripted sequence of FakeProcs
    and record sleeps instead of actually sleeping."""

    def __init__(self, procs):
        self._procs = list(procs)
        self.calls = 0
        self.sleeps = []

    def __enter__(self):
        self._save_run = core.subprocess.run
        self._save_sleep = core._sleep
        it = iter(self._procs)

        def fake_run(args, capture_output=True, text=True):
            self.calls += 1
            return next(it)

        core.subprocess.run = fake_run
        core._sleep = lambda d: self.sleeps.append(d)
        return self

    def __exit__(self, *exc):
        core.subprocess.run = self._save_run
        core._sleep = self._save_sleep
        return False


# --------------------------------------------------------------------------- #
# retry / backoff
# --------------------------------------------------------------------------- #
def test_transient_classification():
    check(
        "transient: HTTP 502 stderr is transient",
        core._is_transient_stderr("gh: Something went wrong (HTTP 502)"),
    )
    check(
        "transient: HTTP 504 stderr is transient",
        core._is_transient_stderr("HTTP 504: Gateway Timeout"),
    )
    check(
        "transient: secondary rate limit is transient",
        core._is_transient_stderr(
            "You have exceeded a rate limit... was submitted too quickly"
        ),
    )
    check(
        "transient: a 404 / not-found is NOT transient",
        not core._is_transient_stderr(
            "Could not resolve to a Repository with the name"
        ),
    )
    check(
        "transient: HTTP 422 validation is NOT transient",
        not core._is_transient_stderr("HTTP 422: Validation Failed"),
    )
    check(
        "transient: GraphQL query-timeout errors are transient",
        core._is_transient_gql_errors(
            [{"message": "Something went wrong while executing your query."}]
        ),
    )
    check(
        "transient: a field error in the errors array is NOT transient",
        not core._is_transient_gql_errors(
            [{"message": "Field 'bogus' doesn't exist on type 'Repository'"}]
        ),
    )


def test_backoff_delay_bounded_and_growing():
    d1 = core._graphql_backoff_delay(1)
    d2 = core._graphql_backoff_delay(2)
    # base 0.5 -> [0.5,1.0); base*2 -> [1.0,1.5): strictly increasing floors.
    check("backoff: attempt 1 in [0.5,1.0)", 0.5 <= d1 < 1.0)
    check("backoff: attempt 2 in [1.0,1.5)", 1.0 <= d2 < 1.5)
    big = core._graphql_backoff_delay(20)
    check(
        "backoff: capped at CAP + jitter",
        big <= core.GRAPHQL_BACKOFF_CAP + core.GRAPHQL_BACKOFF_BASE,
    )


def test_retry_recovers_after_transient_5xx():
    procs = [
        FakeProc(returncode=1, stderr="gh: HTTP 502 Bad Gateway"),
        FakeProc(returncode=1, stderr="gh: HTTP 504 Gateway Timeout"),
        FakeProc(returncode=0, stdout=_ok_stdout({"pullRequests": {"nodes": []}})),
    ]
    with _RunPatch(procs) as rp:
        data = core._gh_graphql_data(["gh", "api", "graphql"])
    check(
        "retry: recovered on 3rd attempt",
        data["data"]["repository"] == {"pullRequests": {"nodes": []}},
    )
    check("retry: made exactly 3 subprocess calls", rp.calls == 3)
    check("retry: slept twice (once per transient retry)", len(rp.sleeps) == 2)


def test_retry_exhausts_then_raises():
    procs = [
        FakeProc(returncode=1, stderr="HTTP 502 Bad Gateway")
    ] * core.GRAPHQL_MAX_ATTEMPTS
    raised = False
    with _RunPatch(procs) as rp:
        try:
            core._gh_graphql_data(["gh", "api", "graphql"])
        except RuntimeError:
            raised = True
    check("retry-exhaust: raises after the last attempt", raised)
    check("retry-exhaust: used all attempts", rp.calls == core.GRAPHQL_MAX_ATTEMPTS)
    check(
        "retry-exhaust: slept attempts-1 times",
        len(rp.sleeps) == core.GRAPHQL_MAX_ATTEMPTS - 1,
    )


def test_non_transient_fails_fast_no_retry():
    procs = [
        FakeProc(returncode=1, stderr="Could not resolve to a Repository"),
        FakeProc(returncode=0, stdout=_ok_stdout()),  # must never be reached
    ]
    raised = False
    with _RunPatch(procs) as rp:
        try:
            core._gh_graphql_data(["gh", "api", "graphql"])
        except RuntimeError:
            raised = True
    check("non-transient: raised", raised)
    check("non-transient: only one attempt (no retry)", rp.calls == 1)
    check("non-transient: never slept", rp.sleeps == [])


def test_transient_graphql_errors_retried():
    procs = [
        FakeProc(
            returncode=0,
            stdout=json.dumps(
                {
                    "errors": [
                        {"message": "Something went wrong while executing your query."}
                    ]
                }
            ),
        ),
        FakeProc(returncode=0, stdout=_ok_stdout({"issues": {"nodes": []}})),
    ]
    with _RunPatch(procs) as rp:
        data = core._gh_graphql_data(["gh", "api", "graphql"])
    check(
        "gql-errors: retried and recovered",
        data["data"]["repository"] == {"issues": {"nodes": []}},
    )
    check("gql-errors: two attempts", rp.calls == 2)


def test_fatal_graphql_errors_not_retried():
    procs = [
        FakeProc(
            returncode=0,
            stdout=json.dumps({"errors": [{"message": "Field 'x' doesn't exist"}]}),
        ),
        FakeProc(returncode=0, stdout=_ok_stdout()),
    ]
    raised = False
    with _RunPatch(procs) as rp:
        try:
            core._gh_graphql_data(["gh", "api", "graphql"])
        except RuntimeError:
            raised = True
    check("fatal-gql: raised", raised)
    check("fatal-gql: not retried", rp.calls == 1)


def test_unparseable_body_retried_as_transient():
    procs = [
        FakeProc(returncode=0, stdout="<html>502 Bad Gateway</html>"),
        FakeProc(returncode=0, stdout=_ok_stdout({"ok": True})),
    ]
    with _RunPatch(procs) as rp:
        data = core._gh_graphql_data(["gh", "api", "graphql"])
    check(
        "unparseable: retried and recovered", data["data"]["repository"] == {"ok": True}
    )
    check("unparseable: two attempts", rp.calls == 2)


# --------------------------------------------------------------------------- #
# small-page cursor pagination
# --------------------------------------------------------------------------- #
def _pr_stub(number):
    return {"number": number}


def test_pagination_multipage_assembly():
    first = {
        "totalCount": 90,
        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
        "nodes": [_pr_stub(i) for i in range(30)],
    }
    pages = {
        "c1": {
            "totalCount": 90,
            "pageInfo": {"hasNextPage": True, "endCursor": "c2"},
            "nodes": [_pr_stub(i) for i in range(30, 60)],
        },
        "c2": {
            "totalCount": 90,
            "pageInfo": {"hasNextPage": False, "endCursor": "c3"},
            "nodes": [_pr_stub(i) for i in range(60, 90)],
        },
    }
    save = core.gh_graphql_pr_page
    core.gh_graphql_pr_page = lambda o, n, after: pages[after]
    try:
        nodes, complete = core._page_open_prs("owner", "demo", first)
    finally:
        core.gh_graphql_pr_page = save
    check("pagination: assembled all 90 nodes across 3 pages", len(nodes) == 90)
    check("pagination: reported complete", complete is True)
    check(
        "pagination: nodes are in-order and complete",
        [x["number"] for x in nodes] == list(range(90)),
    )


def test_pagination_midpage_failure_propagates():
    first = {
        "totalCount": 90,
        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
        "nodes": [_pr_stub(i) for i in range(30)],
    }

    def boom(owner, name, after):
        raise RuntimeError("HTTP 502 on page 2 after retries")

    save = core.gh_graphql_pr_page
    core.gh_graphql_pr_page = boom
    raised = False
    try:
        core._page_open_prs("owner", "demo", first)
    except RuntimeError:
        raised = True
    finally:
        core.gh_graphql_pr_page = save
    check(
        "pagination-fail: mid-page failure propagates (caller marks truncated)", raised
    )


def test_page_queries_keep_closing_references_bounded():
    expected = "closingIssuesReferences(first:%d" % core.CLOSING_REFS_PAGE_SIZE
    check("page-size: initial closing references are bounded", expected in core.GQL)
    check(
        "page-size: continued closing references are bounded",
        expected in core.CLOSING_REFS_PAGE_GQL,
    )


# --------------------------------------------------------------------------- #
# fleet-scan health ledger
# --------------------------------------------------------------------------- #
def test_health_parse_roundtrip():
    counts = {
        "firstmate": {
            "consecutive_failures": 3,
            "last_warning": "scan failed: HTTP 502",
        }
    }
    body = core.render_scan_health_body(counts, updated_at="2026-07-10T00:00:00Z")
    parsed = core.parse_scan_health(body)
    check("health-parse: round-trips the counts", parsed == counts)
    check("health-parse: human summary names the dark repo", "firstmate" in body)
    check(
        "health-parse: missing marker -> empty",
        core.parse_scan_health("no marker here") == {},
    )
    check("health-parse: blank body -> empty", core.parse_scan_health("") == {})
    check(
        "health-parse: garbled marker -> empty",
        core.parse_scan_health("<!-- wheelhouse-scan-health: {not json} -->") == {},
    )


def test_health_increment_reset_and_alert():
    prev = {
        "firstmate": {"consecutive_failures": 2},
        "axi": {"consecutive_failures": 1},
    }
    repos = {
        "firstmate": {"ok": False, "warning": "owner/firstmate scan failed: HTTP 502"},
        "axi": {"ok": True},
        "gnhf": {"ok": False, "warning": "owner/gnhf scan failed: HTTP 504"},
    }
    counts, alerts = core.update_scan_health(prev, repos, threshold=3)
    check(
        "health: ok:false increments", counts["firstmate"]["consecutive_failures"] == 3
    )
    check("health: ok:true resets to 0", counts["axi"]["consecutive_failures"] == 0)
    check(
        "health: newly-failing repo starts at 1",
        counts["gnhf"]["consecutive_failures"] == 1,
    )
    names = [a["name"] for a in alerts]
    check("health: firstmate crosses threshold -> alert", "firstmate" in names)
    check("health: gnhf below threshold -> no alert", "gnhf" not in names)
    check("health: axi (ok) -> no alert", "axi" not in names)
    check(
        "health: alert carries the warning", alerts[0]["warning"].endswith("HTTP 502")
    )


def test_health_recovery_clears_alert():
    prev = {"firstmate": {"consecutive_failures": 5}}
    counts, alerts = core.update_scan_health(
        prev, {"firstmate": {"ok": True}}, threshold=3
    )
    check(
        "health-recovery: reset to 0", counts["firstmate"]["consecutive_failures"] == 0
    )
    check("health-recovery: no alert after recovery", alerts == [])


def test_health_carries_forward_unscanned_repos():
    prev = {"gnhf": {"consecutive_failures": 4}}
    # A partial scan that only saw firstmate must not wipe gnhf's history, and
    # must not alert on the unscanned repo.
    counts, alerts = core.update_scan_health(
        prev, {"firstmate": {"ok": True}}, threshold=3
    )
    check(
        "health-carry: unscanned gnhf preserved",
        counts["gnhf"]["consecutive_failures"] == 4,
    )
    check("health-carry: unscanned repo never alerts", alerts == [])


def test_health_legacy_int_entry_tolerated():
    counts, alerts = core.update_scan_health(
        {"firstmate": 2}, {"firstmate": {"ok": False, "warning": "x"}}, threshold=3
    )
    check(
        "health-legacy: int entry increments to 3",
        counts["firstmate"]["consecutive_failures"] == 3,
    )
    check(
        "health-legacy: alerts at threshold",
        [a["name"] for a in alerts] == ["firstmate"],
    )


def _run_cmd_scan_health(payload, issue, rest_log, threshold=None):
    """Drive cmd_scan_health with a fake gh_rest + a temp scan.json.
    `issue` is the existing ledger issue dict (or None to force create)."""
    import tempfile

    def fake_rest(path, method=None, fields=None, jq=None, paginate=False, slurp=False):
        rest_log.append({"path": path, "method": method, "fields": fields})
        if path.startswith("repos/") and "/issues?" in path and method is None:
            return [issue] if issue else []
        return {}

    fd, scan_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(scan_path, "w") as f:
        json.dump(payload, f)

    save_rest = core.gh_rest
    save_env_repo = os.environ.get("GITHUB_REPOSITORY")
    save_env_thr = os.environ.get("WHEELHOUSE_SCAN_HEALTH_THRESHOLD")
    core.gh_rest = fake_rest
    os.environ["GITHUB_REPOSITORY"] = "owner/cards"
    if threshold is not None:
        os.environ["WHEELHOUSE_SCAN_HEALTH_THRESHOLD"] = str(threshold)
    err = io.StringIO()
    exit_code = None
    try:
        with redirect_stderr(err):
            try:
                core.cmd_scan_health(scan_path)
            except SystemExit as e:
                exit_code = e.code
    finally:
        core.gh_rest = save_rest
        if save_env_repo is None:
            os.environ.pop("GITHUB_REPOSITORY", None)
        else:
            os.environ["GITHUB_REPOSITORY"] = save_env_repo
        if save_env_thr is None:
            os.environ.pop("WHEELHOUSE_SCAN_HEALTH_THRESHOLD", None)
        else:
            os.environ["WHEELHOUSE_SCAN_HEALTH_THRESHOLD"] = save_env_thr
        os.remove(scan_path)
    return exit_code, err.getvalue()


def test_cmd_scan_health_alerts_and_fails_run():
    # Existing ledger already has firstmate at 2 consecutive failures; this scan
    # makes it 3 (== threshold) -> loud ::error:: and a non-zero exit.
    issue = {
        "number": 7,
        "body": core.render_scan_health_body(
            {"firstmate": {"consecutive_failures": 2}}
        ),
    }
    rest_log = []
    payload = {
        "generated_at": "2026-07-10T01:00:00Z",
        "repos": {
            "firstmate": {
                "ok": False,
                "warning": "owner/firstmate scan failed: HTTP 502",
            }
        },
    }
    exit_code, err = _run_cmd_scan_health(payload, issue, rest_log, threshold=3)
    check(
        "cmd-health: emitted ::error:: for the dark repo",
        "::error::" in err and "firstmate" in err,
    )
    check("cmd-health: failed the run (non-zero exit)", exit_code not in (None, 0))
    patched = [c for c in rest_log if c["method"] == "PATCH"]
    check("cmd-health: persisted the updated ledger via PATCH", len(patched) == 1)
    check(
        "cmd-health: re-closes the repaired ledger",
        patched[0]["fields"]["state"] == "closed",
    )
    new_counts = core.parse_scan_health(patched[0]["fields"]["body"])
    check(
        "cmd-health: ledger now records 3 failures",
        new_counts["firstmate"]["consecutive_failures"] == 3,
    )


def test_cmd_scan_health_green_run_no_alert():
    issue = {
        "number": 7,
        "body": core.render_scan_health_body(
            {"firstmate": {"consecutive_failures": 2}}
        ),
    }
    rest_log = []
    payload = {"generated_at": "t", "repos": {"firstmate": {"ok": True}}}
    exit_code, err = _run_cmd_scan_health(payload, issue, rest_log, threshold=3)
    check("cmd-health-green: no ::error::", "::error::" not in err)
    check("cmd-health-green: run not failed", exit_code in (None, 0))
    patched = [c for c in rest_log if c["method"] == "PATCH"]
    check(
        "cmd-health-green: reset persisted",
        core.parse_scan_health(patched[0]["fields"]["body"])["firstmate"][
            "consecutive_failures"
        ]
        == 0,
    )


def test_create_scan_health_issue_creates_label_first():
    events = []
    save_ensure = core._ensure_repo_label
    save_run = core.subprocess.run
    save_rest = core.gh_rest

    def fake_ensure(slug, label):
        events.append(("label", slug, label))

    def fake_run(args, capture_output=True, text=True):
        events.append(("issue", args))
        return FakeProc(stdout=json.dumps({"number": 7}))

    def fake_rest(path, method=None, fields=None, jq=None, paginate=False, slurp=False):
        events.append(("rest", path, method, fields))
        return {}

    core._ensure_repo_label = fake_ensure
    core.subprocess.run = fake_run
    core.gh_rest = fake_rest
    try:
        core._create_scan_health_issue("owner/cards", "ledger")
    finally:
        core._ensure_repo_label = save_ensure
        core.subprocess.run = save_run
        core.gh_rest = save_rest
    check(
        "cmd-health-create: creates the health label before the ledger issue",
        [event[0] for event in events[:2]] == ["label", "issue"],
    )
    check(
        "cmd-health-create: creates the dedicated health label",
        events[0] == ("label", "owner/cards", core.SCAN_HEALTH_LABEL),
    )


def test_find_scan_health_issue_requires_marker_and_paginates():
    manual = {"number": 4, "body": "manually labeled"}
    ledger = {
        "number": 25,
        "body": core.render_scan_health_body(
            {"firstmate": {"consecutive_failures": 1}}
        ),
    }
    calls = []
    save_rest = core.gh_rest

    def fake_rest(path, method=None, fields=None, jq=None, paginate=False, slurp=False):
        calls.append({"path": path, "paginate": paginate, "slurp": slurp})
        return [[manual], [ledger]]

    core.gh_rest = fake_rest
    try:
        found = core._find_scan_health_issue("owner/cards")
    finally:
        core.gh_rest = save_rest
    check("cmd-health-find: ignores manually labeled issues", found == ledger)
    check(
        "cmd-health-find: paginates every labeled issue",
        calls
        == [
            {
                "path": "repos/owner/cards/issues?state=all&labels=wheelhouse%3Ascan-health&per_page=100",
                "paginate": True,
                "slurp": True,
            }
        ],
    )

    core.gh_rest = lambda *args, **kwargs: [[manual]]
    try:
        found = core._find_scan_health_issue("owner/cards")
    finally:
        core.gh_rest = save_rest
    check("cmd-health-find: refuses an unmarked issue", found is None)


def test_checks_command_reads_every_pr_page():
    first = {
        "totalCount": 45,
        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
        "nodes": [_pr_stub(i) for i in range(30)],
    }
    second = {
        "totalCount": 45,
        "pageInfo": {"hasNextPage": False, "endCursor": "c2"},
        "nodes": [_pr_stub(i) for i in range(30, 45)],
    }
    seen = []
    save_owner = core.get_owner
    save_config = core.load_config
    save_graphql = core.gh_graphql
    save_page = core.gh_graphql_pr_page
    save_status = core.check_status
    save_warning = core.config_warning
    core.get_owner = lambda: "owner"
    core.load_config = lambda: {
        "repos": {"demo": {"name": "demo", "compliance_check": "Gate"}}
    }
    core.gh_graphql = lambda owner, name: {"pullRequests": first}
    core.gh_graphql_pr_page = lambda owner, name, after: second

    def fake_status(pr, cfg):
        seen.append(pr["number"])
        return "pass", "green", True, ["check-%d" % pr["number"]]

    core.check_status = fake_status
    core.config_warning = lambda repo, comp, names: None
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            core.cmd_checks("demo")
    finally:
        (
            core.get_owner,
            core.load_config,
            core.gh_graphql,
            core.gh_graphql_pr_page,
            core.check_status,
            core.config_warning,
        ) = (
            save_owner,
            save_config,
            save_graphql,
            save_page,
            save_status,
            save_warning,
        )
    check("checks: reads all paginated PRs", seen == list(range(45)))
    check("checks: includes check names from later pages", "check-44" in out.getvalue())


def test_cmd_scan_health_missing_scanfile_fails_open():
    save_rest = core.gh_rest
    called = []
    core.gh_rest = lambda *a, **k: called.append(a) or {}
    os.environ["GITHUB_REPOSITORY"] = "owner/cards"
    err = io.StringIO()
    exit_code = None
    try:
        with redirect_stderr(err):
            try:
                core.cmd_scan_health("/nonexistent/scan.json")
            except SystemExit as e:
                exit_code = e.code
    finally:
        core.gh_rest = save_rest
        os.environ.pop("GITHUB_REPOSITORY", None)
    check("cmd-health-missing: fails open (no exit failure)", exit_code in (None, 0))
    check("cmd-health-missing: never touched the ledger", called == [])
    check("cmd-health-missing: warned", "::warning::" in err.getvalue())


def test_cmd_scan_health_ledger_io_error_fails_open():
    # gh_rest raises on the issue lookup -> health bookkeeping must NOT fail the run.
    def boom_rest(path, method=None, fields=None, jq=None, paginate=False, slurp=False):
        raise RuntimeError("HTTP 500 listing issues")

    import tempfile

    fd, scan_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(scan_path, "w") as f:
        json.dump({"generated_at": "t", "repos": {"firstmate": {"ok": False}}}, f)
    save_rest = core.gh_rest
    core.gh_rest = boom_rest
    os.environ["GITHUB_REPOSITORY"] = "owner/cards"
    os.environ["WHEELHOUSE_SCAN_HEALTH_THRESHOLD"] = "1"
    err = io.StringIO()
    exit_code = None
    try:
        with redirect_stderr(err):
            try:
                core.cmd_scan_health(scan_path)
            except SystemExit as e:
                exit_code = e.code
    finally:
        core.gh_rest = save_rest
        os.environ.pop("GITHUB_REPOSITORY", None)
        os.environ.pop("WHEELHOUSE_SCAN_HEALTH_THRESHOLD", None)
        os.remove(scan_path)
    check(
        "cmd-health-ioerr: fails open despite threshold=1 dark repo",
        exit_code in (None, 0),
    )
    check(
        "cmd-health-ioerr: warned about the ledger failure",
        "ledger update failed" in err.getvalue(),
    )


# --------------------------------------------------------------------------- #
# UNKNOWN-mergeable hardening
# --------------------------------------------------------------------------- #
def test_settle_mergeable_returns_first_conclusive():
    seq = iter(["UNKNOWN", "CONFLICTING"])
    save_read = core.gh_graphql_pr_mergeable
    save_sleep = core._sleep
    core.gh_graphql_pr_mergeable = lambda o, n, num: next(seq)
    core._sleep = lambda d: None
    try:
        val = core._settle_mergeable("owner", "demo", 5)
    finally:
        core.gh_graphql_pr_mergeable = save_read
        core._sleep = save_sleep
    check("settle: returns first conclusive value", val == "CONFLICTING")


def test_settle_mergeable_fails_open_on_error():
    def boom(o, n, num):
        raise RuntimeError("HTTP 502")

    save_read = core.gh_graphql_pr_mergeable
    save_sleep = core._sleep
    core.gh_graphql_pr_mergeable = boom
    core._sleep = lambda d: None
    try:
        val = core._settle_mergeable("owner", "demo", 5)
    finally:
        core.gh_graphql_pr_mergeable = save_read
        core._sleep = save_sleep
    check("settle-error: fails open to None", val is None)


def _pr_green(number, mergeable):
    return {
        "number": number,
        "isDraft": False,
        "mergeable": mergeable,
    }


def test_resolve_bucket_reread_catches_conflict():
    reads = []

    def fake_read(o, n, num):
        reads.append(num)
        return "CONFLICTING"

    save_read = core.gh_graphql_pr_mergeable
    save_sleep = core._sleep
    core.gh_graphql_pr_mergeable = fake_read
    core._sleep = lambda d: None
    try:
        bucket = core._resolve_pr_bucket(
            "owner",
            "demo",
            _pr_green(5, "UNKNOWN"),
            False,
            "pass",
            "green",
            True,
            False,
        )
    finally:
        core.gh_graphql_pr_mergeable = save_read
        core._sleep = save_sleep
    check("resolve: UNKNOWN merge-ready triggered a re-read", reads == [5])
    check("resolve: settled CONFLICTING -> needs-rebase", bucket == "needs-rebase")


def test_resolve_bucket_unsettled_returns_pending():
    # UNKNOWN that never settles must NOT be classified into any worklist bucket:
    # it returns the pending sentinel so build_repo freezes the PR (no membership
    # flip, no card).
    reads = []
    save_read = core.gh_graphql_pr_mergeable
    save_sleep = core._sleep
    core.gh_graphql_pr_mergeable = lambda o, n, num: reads.append(num) or "UNKNOWN"
    core._sleep = lambda d: None
    try:
        bucket = core._resolve_pr_bucket(
            "owner",
            "demo",
            _pr_green(6, "UNKNOWN"),
            False,
            "pass",
            "green",
            True,
            False,
        )
    finally:
        core.gh_graphql_pr_mergeable = save_read
        core._sleep = save_sleep
    check(
        "resolve-pending: unsettled UNKNOWN returns MERGEABILITY_PENDING",
        bucket == core.MERGEABILITY_PENDING,
    )
    check(
        "resolve-pending: it is NOT a worklist bucket",
        core.MERGEABILITY_PENDING not in core.NEEDS_MAINTAINER,
    )
    check(
        "resolve-pending: polled the full budget",
        len(reads) == core.MERGEABLE_SETTLE_READS,
    )


def test_resolve_bucket_known_mergeable_no_reread():
    called = []
    save_read = core.gh_graphql_pr_mergeable
    core.gh_graphql_pr_mergeable = lambda o, n, num: called.append(num) or "MERGEABLE"
    try:
        bucket = core._resolve_pr_bucket(
            "owner",
            "demo",
            _pr_green(7, "MERGEABLE"),
            False,
            "pass",
            "green",
            True,
            False,
        )
    finally:
        core.gh_graphql_pr_mergeable = save_read
    check(
        "resolve-fast: authoritative MERGEABLE stays merge-ready",
        bucket == "merge-ready",
    )
    check("resolve-fast: no wasteful re-read for a known-mergeable PR", called == [])


def test_resolve_bucket_reread_settles_mergeable():
    save_read = core.gh_graphql_pr_mergeable
    save_sleep = core._sleep
    core.gh_graphql_pr_mergeable = lambda o, n, num: "MERGEABLE"
    core._sleep = lambda d: None
    try:
        bucket = core._resolve_pr_bucket(
            "owner",
            "demo",
            _pr_green(8, "UNKNOWN"),
            False,
            "pass",
            "green",
            True,
            False,
        )
    finally:
        core.gh_graphql_pr_mergeable = save_read
        core._sleep = save_sleep
    check("resolve: re-read proves MERGEABLE -> merge-ready", bucket == "merge-ready")


def test_resolve_review_needed_unknown_settles_or_freezes():
    pr = _pr_green(9, "UNKNOWN")
    checks = (
        ("CONFLICTING", "needs-rebase"),
        ("MERGEABLE", "review-needed"),
        ("UNKNOWN", core.MERGEABILITY_PENDING),
    )
    save_read = core.gh_graphql_pr_mergeable
    save_sleep = core._sleep
    try:
        for settled, expected in checks:
            core.gh_graphql_pr_mergeable = lambda o, n, num, value=settled: value
            core._sleep = lambda d: None
            bucket = core._resolve_pr_bucket(
                "owner", "demo", pr, False, "pass", "none", True, False
            )
            check(
                "resolve-review-needed: UNKNOWN settling %s -> %s"
                % (settled, expected),
                bucket == expected,
            )
    finally:
        core.gh_graphql_pr_mergeable = save_read
        core._sleep = save_sleep


# --------------------------------------------------------------------------- #
# build_repo integration: pagination + UNKNOWN-mergeable in the real scan
# --------------------------------------------------------------------------- #
def _check_run(name, conclusion="SUCCESS"):
    return {
        "__typename": "CheckRun",
        "name": name,
        "conclusion": conclusion,
        "status": "COMPLETED",
    }


def _green_rollup():
    return {
        "state": "SUCCESS",
        "contexts": {"nodes": [_check_run("Gate"), _check_run("test")]},
    }


def _full_pr(number, *, mergeable="MERGEABLE", cross_repo=False, has_tests=True):
    node = {
        "number": number,
        "title": "PR %d" % number,
        "isDraft": False,
        "isCrossRepository": cross_repo,
        "mergeable": mergeable,
        "updatedAt": "2026-01-01T00:00:00Z",
        "changedFiles": 1,
        "author": {"login": "contributor", "__typename": "User"},
        "headRefName": "feature-%d" % number,
        "headRefOid": "sha%d" % number,
        "baseRefName": "main",
        "headRepository": {"name": "demo", "owner": {"login": "owner"}},
        "baseRepository": {"name": "demo", "owner": {"login": "owner"}},
        "labels": {"totalCount": 0, "nodes": []},
        "closingIssuesReferences": {"totalCount": 0, "nodes": []},
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": (
                            _green_rollup()
                            if has_tests
                            else {
                                "state": "SUCCESS",
                                "contexts": {"nodes": [_check_run("Gate")]},
                            }
                        )
                    }
                }
            ]
        },
    }
    return node


def _run_build_repo(
    first_prs,
    *,
    pr_pages=None,
    pr_page_raises=False,
    mergeable_reads=None,
    settlement_events=None,
):
    repo_cfg = {
        "name": "demo",
        "compliance_check": "Gate",
        "test_check_patterns": ["test"],
    }
    posts = []

    first_page = {
        "totalCount": (first_prs.get("totalCount")),
        "pageInfo": first_prs.get("pageInfo", {}),
        "nodes": first_prs["nodes"],
    }

    def fake_graphql(owner, name):
        return {
            "defaultBranchRef": {"name": "main"},
            "pullRequests": first_page,
            "issues": {
                "totalCount": 0,
                "pageInfo": {"hasNextPage": False},
                "nodes": [],
            },
        }

    pr_pages = pr_pages or {}

    def fake_pr_page(owner, name, after):
        if pr_page_raises:
            raise RuntimeError("HTTP 502 mid-page after retries")
        return pr_pages[after]

    reads = list(mergeable_reads or [])

    def fake_mergeable(owner, name, num):
        if settlement_events is not None:
            settlement_events.append(("read", num))
        value = reads.pop(0) if reads else "UNKNOWN"
        if isinstance(value, BaseException):
            raise value
        return value

    def fake_rest(path, method=None, fields=None, jq=None, paginate=False, slurp=False):
        if method == "POST" and "/comments" in path:
            posts.append({"path": path, "body": (fields or {}).get("body", "")})
            return {
                "id": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "body": (fields or {}).get("body", ""),
            }
        return []

    saves = (
        core.gh_graphql,
        core.gh_graphql_pr_page,
        core.gh_graphql_pr_mergeable,
        core.gh_rest,
        core.load_config,
        core._sleep,
        os.environ.get("OWNER"),
        os.environ.get("GITHUB_REPOSITORY_OWNER"),
    )
    core.gh_graphql = fake_graphql
    core.gh_graphql_pr_page = fake_pr_page
    core.gh_graphql_pr_mergeable = fake_mergeable
    core.gh_rest = fake_rest
    core.load_config = lambda: {"repos": {"demo": repo_cfg}, "maintainer": ""}
    if settlement_events is None:
        core._sleep = lambda d: None
    else:
        core._sleep = lambda d: settlement_events.append(("sleep", d))
    os.environ["OWNER"] = "owner"
    os.environ["GITHUB_REPOSITORY_OWNER"] = "owner"
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            result, items = core.build_repo("owner", repo_cfg, False)
    finally:
        (
            core.gh_graphql,
            core.gh_graphql_pr_page,
            core.gh_graphql_pr_mergeable,
            core.gh_rest,
            core.load_config,
            core._sleep,
            old_owner,
            old_repo_owner,
        ) = saves
        os.environ.pop("OWNER", None) if old_owner is None else os.environ.__setitem__(
            "OWNER", old_owner
        )
        os.environ.pop(
            "GITHUB_REPOSITORY_OWNER", None
        ) if old_repo_owner is None else os.environ.__setitem__(
            "GITHUB_REPOSITORY_OWNER", old_repo_owner
        )
    return result, items, posts


def test_build_repo_multipage_ok_and_complete():
    first = {
        "totalCount": 45,
        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
        "nodes": [_full_pr(i) for i in range(30)],
    }
    pages = {
        "c1": {
            "totalCount": 45,
            "pageInfo": {"hasNextPage": False, "endCursor": "c2"},
            "nodes": [_full_pr(i) for i in range(30, 45)],
        }
    }
    result, items, _ = _run_build_repo(first, pr_pages=pages)
    check("build-multipage: ok:true", result["ok"] is True)
    check("build-multipage: not truncated", result.get("truncated") is False)
    check("build-multipage: assembled all 45 PRs", len(result["open_pr_numbers"]) == 45)
    check("build-multipage: all merge-ready cards emitted", len(items) == 45)


def test_build_repo_midpage_failure_truncates():
    first = {
        "totalCount": 45,
        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
        "nodes": [_full_pr(i) for i in range(30)],
    }
    result, items, _ = _run_build_repo(first, pr_page_raises=True)
    check(
        "build-truncate: repo still ok:true (partial view, not blanked)",
        result["ok"] is True,
    )
    check(
        "build-truncate: marked truncated so reconcile won't self-heal close",
        result["truncated"] is True,
    )
    check(
        "build-truncate: only the first page's PRs are visible",
        len(result["open_pr_numbers"]) == 30,
    )


def test_build_repo_unknown_mergeable_conflict_nudges_no_card():
    # A green PR whose bulk mergeable is UNKNOWN, but a targeted re-read proves
    # CONFLICTING -> it must leave the maintainer queue (no card) and get nudged.
    first = {
        "totalCount": 1,
        "pageInfo": {"hasNextPage": False},
        "nodes": [_full_pr(50, mergeable="UNKNOWN")],
    }
    result, items, posts = _run_build_repo(first, mergeable_reads=["CONFLICTING"])
    check("build-unknown-conflict: no merge-ready card emitted", items == [])
    check("build-unknown-conflict: contributor was nudged once", len(posts) == 1)
    check("build-unknown-conflict: repo scan stays ok", result["ok"] is True)


def test_build_repo_unknown_mergeable_settles_mergeable_card():
    # UNKNOWN that settles MERGEABLE -> normal merge-ready card (membership: in).
    first = {
        "totalCount": 1,
        "pageInfo": {"hasNextPage": False},
        "nodes": [_full_pr(52, mergeable="UNKNOWN")],
    }
    result, items, posts = _run_build_repo(
        first, mergeable_reads=["UNKNOWN", "MERGEABLE"]
    )
    check(
        "build-unknown-mergeable: merge-ready card emitted",
        len(items) == 1 and items[0]["bucket"] == "merge-ready",
    )
    check(
        "build-unknown-mergeable: not frozen",
        result.get("indeterminate_pr_numbers") == [],
    )
    check("build-unknown-mergeable: no nudge", posts == [])


def test_build_repo_unknown_mergeable_unsettled_freezes():
    # A green PR whose mergeable never settles: NO false merge-ready, and no
    # membership flip. It emits NO worklist item and is reported as indeterminate
    # so reconcile freezes it. No nudge (an UNKNOWN reading is not CONFLICTING).
    first = {
        "totalCount": 1,
        "pageInfo": {"hasNextPage": False},
        "nodes": [_full_pr(51, mergeable="UNKNOWN")],
    }
    result, items, posts = _run_build_repo(
        first, mergeable_reads=[]
    )  # all reads UNKNOWN
    check("build-unknown-frozen: no worklist item emitted", items == [])
    check(
        "build-unknown-frozen: reported in indeterminate_pr_numbers",
        result["indeterminate_pr_numbers"] == [51],
    )
    check(
        "build-unknown-frozen: PR stays in open_pr_numbers (still open)",
        51 in result["open_pr_numbers"],
    )
    check("build-unknown-frozen: no nudge off an UNKNOWN reading", posts == [])
    check("build-unknown-frozen: repo scan stays ok", result["ok"] is True)


def test_build_repo_mergeability_read_failure_is_unhealthy():
    first = {
        "totalCount": 1,
        "pageInfo": {"hasNextPage": False},
        "nodes": [_full_pr(56, mergeable="UNKNOWN")],
    }
    result, items, posts = _run_build_repo(
        first, mergeable_reads=[RuntimeError("HTTP 502")]
    )
    check("build-mergeability-error: repo is unhealthy", result["ok"] is False)
    check("build-mergeability-error: no worklist item emitted", items == [])
    check("build-mergeability-error: no rebase nudge", posts == [])
    check(
        "build-mergeability-error: failed PR remains indeterminate",
        result["indeterminate_pr_numbers"] == [56],
    )
    check(
        "build-mergeability-error: warning preserves the query failure",
        "mergeability settlement query failed" in result["warning"]
        and "HTTP 502" in result["warning"],
    )


def test_build_repo_mergeability_read_error_that_recovers_is_healthy():
    first = {
        "totalCount": 1,
        "pageInfo": {"hasNextPage": False},
        "nodes": [_full_pr(57, mergeable="UNKNOWN")],
    }
    result, items, _ = _run_build_repo(
        first, mergeable_reads=[RuntimeError("HTTP 502"), "MERGEABLE"]
    )
    check("build-mergeability-recovery: repo remains healthy", result["ok"] is True)
    check(
        "build-mergeability-recovery: settled card emitted",
        len(items) == 1 and items[0]["number"] == 57,
    )


def test_build_repo_review_needed_unknown_settles_conflicting():
    first = {
        "totalCount": 1,
        "pageInfo": {"hasNextPage": False},
        "nodes": [_full_pr(55, mergeable="UNKNOWN", has_tests=False)],
    }
    result, items, posts = _run_build_repo(first, mergeable_reads=["CONFLICTING"])
    check("build-review-needed-conflict: no card emitted", items == [])
    check(
        "build-review-needed-conflict: no frozen PR remains",
        result["indeterminate_pr_numbers"] == [],
    )
    check("build-review-needed-conflict: contributor was nudged once", len(posts) == 1)


def test_build_repo_settles_unknown_prs_in_rounds():
    first = {
        "totalCount": 2,
        "pageInfo": {"hasNextPage": False},
        "nodes": [
            _full_pr(53, mergeable="UNKNOWN"),
            _full_pr(54, mergeable="UNKNOWN"),
        ],
    }
    events = []
    result, items, _ = _run_build_repo(
        first,
        mergeable_reads=["UNKNOWN", "UNKNOWN", "MERGEABLE", "MERGEABLE"],
        settlement_events=events,
    )
    first_sleep = next(i for i, event in enumerate(events) if event[0] == "sleep")
    check(
        "build-unknown-rounds: every first read precedes the first backoff",
        events[:first_sleep] == [("read", 53), ("read", 54)],
    )
    check(
        "build-unknown-rounds: settled PRs retain their worklist cards",
        result["indeterminate_pr_numbers"] == []
        and [item["number"] for item in items] == [53, 54],
    )


def test_build_repo_static_conflict_never_enters_worklist():
    # #111 acceptance: a statically CONFLICTING PR classifies needs-rebase on
    # every readable scan and NEVER emits a worklist item (so it can never mint a
    # duplicate card), across three scan flavors: readable CONFLICTING, and the
    # post-base-push UNKNOWN that settles CONFLICTING on re-read.
    def scan(mergeable, reads):
        first = {
            "totalCount": 1,
            "pageInfo": {"hasNextPage": False},
            "nodes": [_full_pr(111, mergeable=mergeable)],
        }
        return _run_build_repo(first, mergeable_reads=reads)

    r1, items1, _ = scan("CONFLICTING", [])
    r2, items2, _ = scan("UNKNOWN", ["CONFLICTING"])
    r3, items3, _ = scan("UNKNOWN", ["UNKNOWN", "CONFLICTING"])
    check("acceptance: readable CONFLICTING emits no card", items1 == [])
    check("acceptance: UNKNOWN settling CONFLICTING emits no card", items2 == [])
    check("acceptance: UNKNOWN-then-CONFLICTING (2 reads) emits no card", items3 == [])
    check(
        "acceptance: conflicting PR is never frozen-indeterminate once readable",
        r1["indeterminate_pr_numbers"] == []
        and r2["indeterminate_pr_numbers"] == []
        and r3["indeterminate_pr_numbers"] == [],
    )


def main():
    test_transient_classification()
    test_backoff_delay_bounded_and_growing()
    test_retry_recovers_after_transient_5xx()
    test_retry_exhausts_then_raises()
    test_non_transient_fails_fast_no_retry()
    test_transient_graphql_errors_retried()
    test_fatal_graphql_errors_not_retried()
    test_unparseable_body_retried_as_transient()
    test_pagination_multipage_assembly()
    test_pagination_midpage_failure_propagates()
    test_page_queries_keep_closing_references_bounded()
    test_health_parse_roundtrip()
    test_health_increment_reset_and_alert()
    test_health_recovery_clears_alert()
    test_health_carries_forward_unscanned_repos()
    test_health_legacy_int_entry_tolerated()
    test_cmd_scan_health_alerts_and_fails_run()
    test_cmd_scan_health_green_run_no_alert()
    test_create_scan_health_issue_creates_label_first()
    test_find_scan_health_issue_requires_marker_and_paginates()
    test_checks_command_reads_every_pr_page()
    test_cmd_scan_health_missing_scanfile_fails_open()
    test_cmd_scan_health_ledger_io_error_fails_open()
    test_settle_mergeable_returns_first_conclusive()
    test_settle_mergeable_fails_open_on_error()
    test_resolve_bucket_reread_catches_conflict()
    test_resolve_bucket_unsettled_returns_pending()
    test_resolve_bucket_known_mergeable_no_reread()
    test_resolve_bucket_reread_settles_mergeable()
    test_resolve_review_needed_unknown_settles_or_freezes()
    test_build_repo_multipage_ok_and_complete()
    test_build_repo_midpage_failure_truncates()
    test_build_repo_unknown_mergeable_conflict_nudges_no_card()
    test_build_repo_unknown_mergeable_settles_mergeable_card()
    test_build_repo_unknown_mergeable_unsettled_freezes()
    test_build_repo_mergeability_read_failure_is_unhealthy()
    test_build_repo_mergeability_read_error_that_recovers_is_healthy()
    test_build_repo_review_needed_unknown_settles_conflicting()
    test_build_repo_settles_unknown_prs_in_rounds()
    test_build_repo_static_conflict_never_enters_worklist()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all scan-reliability tests passed")


if __name__ == "__main__":
    main()
