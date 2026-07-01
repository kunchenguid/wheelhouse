#!/usr/bin/env python3
"""Scoped read-only search helper for Wheelhouse's Claude steps.

The workflow installs this file as `wheelhouse-search` only when the optional
READONLY_TOKEN secret is present. Claude can write a JSON request to
`search-request.json` and run that wrapper, but the wrapper controls the actual
`gh` command shape: no writes, no arbitrary repository scope, and bounded output.
Allowed repositories are the target repo plus owner-scoped repos from
`wheelhouse.config.yml`.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import wheelhouse_core as core
except Exception:
    core = None

REQUEST_FILE = "search-request.json"
MAX_REQUEST_BYTES = 16384
MAX_OUTPUT_CHARS = 60000
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SCOPE_QUALIFIER_RE = re.compile(r"(^|\s)(repo|org|user):", re.I)

PR_LIST_FIELDS = "number,title,state,author,url,updatedAt,headRefName,baseRefName"
PR_VIEW_FIELDS = "number,title,state,author,body,url,updatedAt,headRefName,baseRefName"
ISSUE_LIST_FIELDS = "number,title,state,author,url,updatedAt,labels"
ISSUE_VIEW_FIELDS = "number,title,state,author,body,url,updatedAt,labels"


def _valid_part(value):
    return bool(PART_RE.match(str(value or "")))


def normalize_repo(owner, repo):
    owner = str(owner or "").strip()
    raw = str(repo or "").strip()
    if not owner or not _valid_part(owner) or not raw:
        return ""
    if "/" in raw:
        parts = raw.split("/")
        if len(parts) != 2:
            return ""
        raw_owner, name = parts
        if raw_owner.casefold() != owner.casefold():
            return ""
    else:
        name = raw
    if not _valid_part(name):
        return ""
    return "%s/%s" % (owner, name)


def _config_repo_names(config):
    repos = (config or {}).get("repos") or {}
    if isinstance(repos, dict):
        return list(repos.keys())
    names = []
    for repo in repos:
        if isinstance(repo, dict) and repo.get("name"):
            names.append(repo["name"])
    return names


def allowed_repos(owner, target_repo="", config=None):
    repos = []

    def add(repo):
        slug = normalize_repo(owner, repo)
        if slug and slug not in repos:
            repos.append(slug)

    add(target_repo)
    if config is None:
        if core is None:
            config = {}
        else:
            try:
                config = core.load_config()
            except SystemExit:
                config = {}
    for name in _config_repo_names(config):
        add(name)
    return repos


def _env_allowed_repos():
    raw = os.environ.get("WHEELHOUSE_SEARCH_ALLOWED_REPOS", "")
    try:
        data = json.loads(raw)
    except ValueError:
        data = []
    repos = []
    for repo in data if isinstance(data, list) else []:
        parts = str(repo or "").split("/")
        if len(parts) == 2 and _valid_part(parts[0]) and _valid_part(parts[1]):
            slug = "%s/%s" % (parts[0], parts[1])
            if slug not in repos:
                repos.append(slug)
    return repos


def _resolve_repo(value, allowed):
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("repo is required")
    by_slug = {repo.casefold(): repo for repo in allowed}
    if raw.casefold() in by_slug:
        return by_slug[raw.casefold()]
    matches = [repo for repo in allowed if repo.rsplit("/", 1)[1].casefold() == raw.casefold()]
    if len(matches) == 1:
        return matches[0]
    raise ValueError("repo is not in the allowed search scope: %s" % raw)


def _selected_repos(req, allowed):
    if req.get("repo"):
        return [_resolve_repo(req.get("repo"), allowed)]
    if req.get("repos"):
        values = req.get("repos")
        if not isinstance(values, list):
            raise ValueError("repos must be a list")
        repos = []
        for value in values:
            repo = _resolve_repo(value, allowed)
            if repo not in repos:
                repos.append(repo)
        if not repos:
            raise ValueError("repos must not be empty")
        return repos
    return list(allowed)


def _limit(req):
    try:
        value = int(req.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return min(MAX_LIMIT, max(1, value))


def _state(req):
    value = str(req.get("state") or "open").strip().lower()
    return value if value in {"open", "closed", "all"} else "open"


def _number(req):
    try:
        value = int(req.get("number"))
    except (TypeError, ValueError):
        raise ValueError("number must be a positive integer")
    if value <= 0:
        raise ValueError("number must be a positive integer")
    return str(value)


def _query(req):
    value = str(req.get("query") or "").strip()
    if not value:
        raise ValueError("query is required")
    if len(value) > 500:
        raise ValueError("query is too long")
    if SCOPE_QUALIFIER_RE.search(value):
        raise ValueError("query must not include repo, org, or user scope qualifiers")
    return value


def _optional_query(req):
    value = str(req.get("query") or "").strip()
    if len(value) > 500:
        raise ValueError("query is too long")
    if value and SCOPE_QUALIFIER_RE.search(value):
        raise ValueError("query must not include repo, org, or user scope qualifiers")
    return value


def _cap(text):
    text = str(text or "")
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + "\n[output truncated]\n"


def run_gh(args):
    result = subprocess.run(
        ["gh"] + list(args),
        capture_output=True,
        text=True,
    )
    output = result.stdout
    if result.returncode != 0:
        output += "\n[gh exited %s]\n%s\n" % (result.returncode, result.stderr.strip())
    return _cap(output)


def _run_for_repos(repos, build_args, runner):
    chunks = []
    for repo in repos:
        chunks.append("### %s\n%s" % (repo, runner(build_args(repo)).strip()))
    return _cap("\n\n".join(chunks) + "\n")


def _list_request(req, repos, runner, kind):
    limit = str(_limit(req))
    state = _state(req)
    query = _optional_query(req)
    fields = PR_LIST_FIELDS if kind == "pr" else ISSUE_LIST_FIELDS

    def args(repo):
        out = [kind, "list", "-R", repo, "--state", state, "--limit", limit, "--json", fields]
        if query:
            out += ["--search", query]
        return out

    return _run_for_repos(repos, args, runner)


def _view_request(req, runner, kind):
    repo = _resolve_repo(req.get("repo"), req["_allowed"])
    number = _number(req)
    fields = PR_VIEW_FIELDS if kind == "pr" else ISSUE_VIEW_FIELDS
    return _cap(runner([kind, "view", number, "-R", repo, "--json", fields]))


def _search_args(kind, repo, query, limit):
    return ["search", kind, "--repo", repo, "--limit", limit, "--", query]


def handle_request(req, allowed, runner=run_gh):
    if not isinstance(req, dict):
        raise ValueError("request must be a JSON object")
    if not allowed:
        raise ValueError("no repositories are allowed for search")
    op = str(req.get("op") or "help").strip().lower().replace("-", "_")
    if op in {"help", "repos"}:
        return json.dumps(
            {
                "allowed_repos": allowed,
                "request_file": REQUEST_FILE,
                "ops": [
                    "repos",
                    "pr_list",
                    "pr_view",
                    "pr_diff",
                    "issue_list",
                    "issue_view",
                    "search_prs",
                    "search_issues",
                    "search_code",
                ],
            },
            indent=2,
        ) + "\n"

    request = dict(req)
    request["_allowed"] = allowed
    repos = _selected_repos(request, allowed)
    if op == "pr_list":
        return _list_request(request, repos, runner, "pr")
    if op == "issue_list":
        return _list_request(request, repos, runner, "issue")
    if op == "pr_view":
        return _view_request(request, runner, "pr")
    if op == "issue_view":
        return _view_request(request, runner, "issue")
    if op == "pr_diff":
        repo = _resolve_repo(request.get("repo"), allowed)
        return _cap(runner(["pr", "diff", _number(request), "-R", repo]))
    if op == "search_prs":
        query = _query(request)
        limit = str(_limit(request))
        return _run_for_repos(
            repos,
            lambda repo: _search_args("prs", repo, query, limit),
            runner,
        )
    if op == "search_issues":
        query = _query(request)
        limit = str(_limit(request))
        return _run_for_repos(
            repos,
            lambda repo: _search_args("issues", repo, query, limit),
            runner,
        )
    if op == "search_code":
        query = _query(request)
        limit = str(_limit(request))
        return _run_for_repos(
            repos,
            lambda repo: _search_args("code", repo, query, limit),
            runner,
        )
    raise ValueError("unsupported search operation: %s" % op)


def _read_request():
    path = os.environ.get("WHEELHOUSE_SEARCH_REQUEST", REQUEST_FILE)
    try:
        st = os.lstat(path)
    except OSError:
        return {"op": "help"}
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ValueError("%s must be a regular file" % path)
    if st.st_size > MAX_REQUEST_BYTES:
        raise ValueError("%s is too large" % path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("%s must contain a JSON object" % path)
    return data


def _append_line(path, line):
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _prepare_tool_dir(tool_dir):
    if os.path.lexists(tool_dir):
        st = os.lstat(tool_dir)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ValueError("search tool path must be a directory")
        os.chmod(tool_dir, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        for name in os.listdir(tool_dir):
            path = os.path.join(tool_dir, name)
            if name != "wheelhouse-search":
                raise ValueError("search tool directory must contain only wheelhouse-search")
            child = os.lstat(path)
            if stat.S_ISDIR(child.st_mode):
                raise ValueError("wheelhouse-search path must not be a directory")
            os.unlink(path)
    else:
        os.makedirs(tool_dir, mode=stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def cmd_install():
    if core is None:
        sys.exit("wheelhouse_core unavailable")
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    target_repo = os.environ.get("TARGET_REPO", "").strip()
    repos = allowed_repos(owner, target_repo)
    if not repos:
        sys.exit("no allowed repositories for read-only search")
    tool_dir = os.environ.get("WHEELHOUSE_SEARCH_TOOL_DIR", "").strip()
    if not tool_dir:
        tool_dir = os.path.join(os.environ.get("RUNNER_TEMP", "."), "wheelhouse-tools")
    _prepare_tool_dir(tool_dir)
    tool_path = os.path.join(tool_dir, "wheelhouse-search")
    shutil.copyfile(os.path.abspath(__file__), tool_path)
    os.chmod(tool_path, stat.S_IRUSR | stat.S_IXUSR)
    os.chmod(tool_dir, stat.S_IRUSR | stat.S_IXUSR)
    _append_line(
        os.environ.get("GITHUB_ENV"),
        "WHEELHOUSE_SEARCH_ALLOWED_REPOS=%s" % json.dumps(repos, separators=(",", ":")),
    )
    _append_line(os.environ.get("GITHUB_PATH"), tool_dir)
    print("installed wheelhouse-search for %s" % ", ".join(repos))


def cmd_run():
    try:
        output = handle_request(_read_request(), _env_allowed_repos())
    except Exception as exc:
        print("wheelhouse-search error: %s" % exc, file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "install":
        cmd_install()
        return
    if len(sys.argv) == 1:
        cmd_run()
        return
    sys.exit("usage: nl_readonly_search.py [install]")


if __name__ == "__main__":
    main()
