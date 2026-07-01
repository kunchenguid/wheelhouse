#!/usr/bin/env python3
"""
Offline wiring checks for the natural-language decision agent's optional
READONLY_TOKEN search capability. NO network, NO live LLM.

Run: python tests/test_nl_decisions_search.py   (needs PyYAML)
"""

import os
import stat
import sys
import tempfile

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import nl_readonly_search as nls  # noqa: E402

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def load_workflow():
    return yaml.safe_load(read(".github", "workflows", "decision-handler.yml"))


def handle_steps():
    return load_workflow()["jobs"]["handle"]["steps"]


def step_by_id(steps, step_id):
    return next((s for s in steps if s.get("id") == step_id), None)


def step_by_name(steps, name):
    return next((s for s in steps if s.get("name") == name), None)


def step_index(steps, predicate):
    return next((i for i, step in enumerate(steps) if predicate(step)), None)


def claude_steps(steps):
    return [s for s in steps if "claude-code-action" in str(s.get("uses", ""))]


def hardened_shell_env(step):
    env = (step or {}).get("env") or {}
    return (
        env.get("PATH") == "${{ steps.trusted-src.outputs.safe_path }}"
        and env.get("BASH_ENV") == ""
        and env.get("ENV") == ""
        and env.get("LD_PRELOAD") == ""
        and env.get("LD_LIBRARY_PATH") == ""
    )


def test_handle_checkout_does_not_persist_default_token():
    checkouts = [s for s in handle_steps() if "actions/checkout" in str(s.get("uses", ""))]

    check("workflow: handle job checkout exists", len(checkouts) >= 1)
    for checkout in checkouts:
        check(
            "workflow: handle checkout does not persist github.token",
            (checkout.get("with") or {}).get("persist-credentials") is False,
        )


def test_readonly_gate_and_prompt_gating():
    steps = handle_steps()
    gate = step_by_id(steps, "nl-readonly")
    prompt = step_by_id(steps, "nl-prompt")

    check("workflow: nl-readonly gate step exists", gate is not None)
    if gate:
        env = gate.get("env", {})
        run = str(gate.get("run", ""))
        check(
            "workflow: nl-readonly compares the optional READONLY_TOKEN secret",
            env.get("HAS_READONLY_TOKEN") == "${{ secrets.READONLY_TOKEN != '' }}",
        )
        check(
            "workflow: nl-readonly emits an enabled output",
            'echo "enabled=$HAS_READONLY_TOKEN"' in run
            and "$GITHUB_OUTPUT" in run,
        )

    check("workflow: nl-prompt step exists", prompt is not None)
    if prompt:
        env = prompt.get("env", {})
        check(
            "workflow: prompt search language is gated on nl-readonly output",
            env.get("READONLY_SEARCH_ENABLED")
            == "${{ steps.nl-readonly.outputs.enabled }}",
        )


def test_search_wrapper_install_step():
    steps = handle_steps()
    install = step_by_id(steps, "nl-search-tool")
    search = step_by_name(steps, "Claude interprets intent (read-only search)")

    check("workflow: read-only search wrapper install step exists", install is not None)
    if install:
        env = install.get("env", {})
        check(
            "workflow: search wrapper receives owner scope",
            env.get("GITHUB_REPOSITORY_OWNER") == "${{ github.repository_owner }}",
        )
        check(
            "workflow: search wrapper receives target repo",
            env.get("TARGET_REPO") == "${{ steps.nl-gate.outputs.repo }}",
        )
        check(
            "workflow: search wrapper installs from trusted checkout",
            str(install.get("run", "")).strip()
            == "python scripts/nl_readonly_search.py install",
        )
        check(
            "workflow: search wrapper runs only when readonly search is enabled",
            "steps.nl-readonly.outputs.enabled == 'true'" in str(install.get("if", "")),
        )

    install_i = step_index(steps, lambda s: s.get("id") == "nl-search-tool")
    search_i = step_index(
        steps, lambda s: s.get("name") == "Claude interprets intent (read-only search)"
    )
    check(
        "workflow: search wrapper installs before Claude can use it",
        None not in (install_i, search_i) and install_i < search_i,
    )
    check("workflow: search Claude step still exists", search is not None)


def test_claude_steps_split_legacy_vs_search():
    steps = handle_steps()
    llm_steps = claude_steps(steps)
    legacy = step_by_name(steps, "Claude interprets intent")
    search = step_by_name(steps, "Claude interprets intent (read-only search)")

    check("workflow: two mutually exclusive Claude steps exist", len(llm_steps) == 2)
    check("workflow: legacy no-search Claude step exists", legacy is not None)
    check("workflow: read-only search Claude step exists", search is not None)

    if legacy:
        dumped = yaml.safe_dump(legacy)
        args = str((legacy.get("with") or {}).get("claude_args", "")).strip()
        check(
            "workflow: legacy step is the byte-for-byte no-shell tool mode",
            args == "--allowedTools Write\n--max-turns 6",
        )
        check(
            "workflow: legacy step has no GH_TOKEN env",
            "env" not in legacy or "GH_TOKEN" not in (legacy.get("env") or {}),
        )
        check(
            "workflow: legacy step keeps the default action github_token",
            (legacy.get("with") or {}).get("github_token") == "${{ github.token }}",
        )
        check(
            "workflow: legacy step never receives FLEET_TOKEN or READONLY_TOKEN",
            "FLEET_TOKEN" not in dumped and "READONLY_TOKEN" not in dumped,
        )
        check(
            "workflow: legacy step runs only when readonly search is disabled",
            "steps.nl-readonly.outputs.enabled != 'true'" in str(legacy.get("if", "")),
        )

    if search:
        dumped = yaml.safe_dump(search)
        env = search.get("env", {})
        args = str((search.get("with") or {}).get("claude_args", ""))
        check(
            "workflow: search step exposes READONLY_TOKEN as GH_TOKEN",
            env.get("GH_TOKEN") == "${{ secrets.READONLY_TOKEN }}",
        )
        check(
            "workflow: search step uses READONLY_TOKEN as the action github_token",
            (search.get("with") or {}).get("github_token")
            == "${{ secrets.READONLY_TOKEN }}",
        )
        check(
            "workflow: search step does not receive the default write token",
            "${{ github.token }}" not in dumped,
        )
        check(
            "workflow: search step never receives FLEET_TOKEN",
            "FLEET_TOKEN" not in dumped,
        )
        check(
            "workflow: search step runs only when readonly search is enabled",
            "steps.nl-readonly.outputs.enabled == 'true'" in str(search.get("if", "")),
        )
        for pattern in ("Write", "Bash(wheelhouse-search)"):
            check("workflow: search step allows %s" % pattern, pattern in args)
        for forbidden in (
            "FLEET_TOKEN",
            "Bash(gh",
            "Bash(git",
            "Bash(wheelhouse-search *)",
            "gh pr list",
            "gh pr view",
            "gh pr diff",
            "gh issue list",
            "gh issue view",
            "gh search",
            "gh pr merge",
            "gh issue close",
            "gh workflow run",
            "gh api",
            "git push",
            "git commit",
            "git grep",
            "git -C",
            "--open-files-in-pager",
        ):
            check("workflow: search step does not allow %s" % forbidden, forbidden not in args)


def test_search_wrapper_repositories_are_owner_scoped():
    cfg = {"repos": {"target": {}, "fleet": {}, "elsewhere/repo": {}}}
    repos = nls.allowed_repos("owner", "target", cfg)
    check(
        "wrapper: allowed repos include only target and configured owner repos",
        repos == ["owner/target", "owner/fleet"],
    )


def test_search_wrapper_rejects_out_of_scope_repo():
    try:
        nls.handle_request(
            {"op": "pr_view", "repo": "other/repo", "number": 1},
            ["owner/target"],
            lambda args: "unexpected",
        )
    except ValueError as exc:
        blocked = "allowed search scope" in str(exc)
    else:
        blocked = False
    check("wrapper: out-of-scope repo is rejected", blocked)


def test_search_wrapper_hardcodes_repo_flags():
    calls = []

    def fake(args):
        calls.append(args)
        return "ok"

    out = nls.handle_request(
        {"op": "search_prs", "query": "duplicate fix", "limit": 99},
        ["owner/target", "owner/fleet"],
        fake,
    )
    check("wrapper: search runs once per allowed repo", len(calls) == 2)
    check(
        "wrapper: search caps requested limit",
        all(call[4:7] == ["--limit", "50", "--"] for call in calls),
    )
    check(
        "wrapper: search passes only allowed repo flags",
        all(
            call[:3] == ["search", "prs", "--repo"]
            and call[3] in {"owner/target", "owner/fleet"}
            and call[4:7] == ["--limit", "50", "--"]
            for call in calls
        ),
    )
    check(
        "wrapper: search output is grouped by repo",
        "### owner/target" in out and "### owner/fleet" in out,
    )


def test_search_wrapper_places_untrusted_query_after_separator():
    for op, kind in (
        ("search_prs", "prs"),
        ("search_issues", "issues"),
        ("search_code", "code"),
    ):
        calls = []
        query = "--repo=other/private duplicate fix"

        def fake(args):
            calls.append(args)
            return "ok"

        nls.handle_request(
            {"op": op, "query": query},
            ["owner/target"],
            fake,
        )
        expected = [
            ["search", kind, "--repo", "owner/target", "--limit", "20", "--", query]
        ]
        check(
            "wrapper: %s query cannot add repo flags" % op,
            calls == expected,
        )


def test_search_wrapper_rejects_query_scope_qualifiers():
    try:
        nls.handle_request(
            {"op": "search_code", "query": "repo:other/repo duplicate fix"},
            ["owner/target"],
            lambda args: "unexpected",
        )
    except ValueError as exc:
        blocked = "scope qualifiers" in str(exc)
    else:
        blocked = False
    check("wrapper: query repo qualifiers are rejected", blocked)


def test_search_wrapper_installs_non_writable_tool():
    keys = [
        "GITHUB_REPOSITORY_OWNER",
        "TARGET_REPO",
        "WHEELHOUSE_SEARCH_TOOL_DIR",
        "GITHUB_ENV",
        "GITHUB_PATH",
    ]
    old_env = {key: os.environ.get(key) for key in keys}
    with tempfile.TemporaryDirectory() as tmp:
        tool_dir = os.path.join(tmp, "tools")
        tool = os.path.join(tool_dir, "wheelhouse-search")
        env_file = os.path.join(tmp, "env")
        path_file = os.path.join(tmp, "path")
        try:
            os.environ.update(
                {
                    "GITHUB_REPOSITORY_OWNER": "owner",
                    "TARGET_REPO": "target",
                    "WHEELHOUSE_SEARCH_TOOL_DIR": tool_dir,
                    "GITHUB_ENV": env_file,
                    "GITHUB_PATH": path_file,
                }
            )
            nls.cmd_install()
            dir_mode = stat.S_IMODE(os.stat(tool_dir).st_mode)
            tool_mode = stat.S_IMODE(os.stat(tool).st_mode)
            check("wrapper: install creates only wheelhouse-search", os.listdir(tool_dir) == ["wheelhouse-search"])
            check("wrapper: installed directory is executable", bool(dir_mode & stat.S_IXUSR))
            check("wrapper: installed directory is not owner-writable", not bool(dir_mode & stat.S_IWUSR))
            check("wrapper: installed tool is executable", bool(tool_mode & stat.S_IXUSR))
            check("wrapper: installed tool is not owner-writable", not bool(tool_mode & stat.S_IWUSR))
            check("wrapper: install adds immutable directory to PATH", read_file(path_file).strip() == tool_dir)
        finally:
            if os.path.exists(tool):
                os.chmod(tool, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            if os.path.isdir(tool_dir):
                os.chmod(tool_dir, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def read_file(path):
    with open(path) as f:
        return f.read()


def test_claude_output_is_isolated_before_routing():
    steps = handle_steps()
    trusted = step_by_id(steps, "trusted-src")
    preserve = step_by_id(steps, "nl-result")
    route = step_by_id(steps, "route")
    execute = step_by_id(steps, "execute")

    check("workflow: trusted source snapshot step exists", trusted is not None)
    if trusted:
        run = str(trusted.get("run", ""))
        check(
            "workflow: trusted source is copied outside the Claude workspace",
            "${RUNNER_TEMP}/wheelhouse-trusted-src" in run
            and "tar --exclude=.git" in run,
        )
        check(
            "workflow: trusted source is made read-only",
            'find "$trusted" -type f -exec chmod a-w {} +' in run
            and 'find "$trusted" -type d -exec chmod a-w {} +' in run,
        )
        check(
            "workflow: trusted source path is exposed as a step output",
            'echo "path=$trusted"' in run and "$GITHUB_OUTPUT" in run,
        )
        check(
            "workflow: trusted tool paths are captured before Claude",
            "python_path=\"$(command -v python)\"" in run
            and "gh_path=\"$(command -v gh)\"" in run,
        )
        check(
            "workflow: trusted safe PATH is exposed as a step output",
            'echo "python=$python_path"' in run
            and 'echo "safe_path=$safe_path"' in run,
        )

    check("workflow: nl-result step exists", preserve is not None)
    if preserve:
        env = preserve.get("env", {})
        run = str(preserve.get("run", ""))
        check(
            "workflow: nl-result uses trusted shell PATH",
            hardened_shell_env(preserve),
        )
        check(
            "workflow: nl-result stores only decision.json in runner temp",
            "${RUNNER_TEMP}/wheelhouse-nl" in run
            and "cp decision.json \"$out_file\"" in run,
        )
        check(
            "workflow: nl-result rejects symlink or non-file results",
            "[ -L decision.json ]" in run and "[ ! -f decision.json ]" in run,
        )
        check(
            "workflow: nl-result caps the LLM result size",
            "65536" in run and "wc -c < decision.json" in run,
        )

    check(
        "workflow: post-Claude in-place checkout restore is absent",
        step_by_name(steps, "Restore deterministic checkout after Claude") is None,
    )

    check("workflow: nl-route step still exists", route is not None)
    if route:
        env = route.get("env", {})
        run = str(route.get("run", ""))
        check(
            "workflow: nl-route runs from trusted source",
            route.get("working-directory") == "${{ steps.trusted-src.outputs.path }}",
        )
        check(
            "workflow: nl-route uses captured trusted Python",
            env.get("TRUSTED_PYTHON") == "${{ steps.trusted-src.outputs.python }}",
        )
        check(
            "workflow: nl-route uses trusted shell PATH",
            hardened_shell_env(route)
            and env.get("TRUSTED_PATH") == "${{ steps.trusted-src.outputs.safe_path }}",
        )
        check(
            "workflow: nl-route reads the isolated decision file",
            env.get("DECISION_FILE")
            == "${{ runner.temp }}/wheelhouse-nl/decision.json",
        )
        check(
            "workflow: nl-route scrubs inherited model environment",
            "env -i" in run
            and "PYTHONDONTWRITEBYTECODE=1" in run
            and "PYTHONNOUSERSITE=1" in run
            and '"$TRUSTED_PYTHON" scripts/apply_decision.py nl-route' in run,
        )

    check("workflow: execute step still exists", execute is not None)
    if execute:
        env = execute.get("env", {})
        run = str(execute.get("run", ""))
        check(
            "workflow: execute runs from trusted source",
            execute.get("working-directory") == "${{ steps.trusted-src.outputs.path }}",
        )
        check(
            "workflow: execute uses captured trusted Python",
            env.get("TRUSTED_PYTHON") == "${{ steps.trusted-src.outputs.python }}",
        )
        check(
            "workflow: execute uses trusted shell PATH",
            hardened_shell_env(execute)
            and env.get("TRUSTED_PATH") == "${{ steps.trusted-src.outputs.safe_path }}",
        )
        check(
            "workflow: execute scrubs inherited model environment",
            "env -i" in run
            and "PYTHONDONTWRITEBYTECODE=1" in run
            and "PYTHONNOUSERSITE=1" in run
            and 'GH_TOKEN="$GH_TOKEN"' in run
            and '"$TRUSTED_PYTHON" scripts/apply_decision.py execute' in run,
        )

    for name in ("Comment result on card", "Close resolved card", "Post NL reply"):
        step = step_by_name(steps, name)
        check(
            "workflow: %s uses trusted gh PATH" % name,
            step is not None and hardened_shell_env(step),
        )

    trusted_i = step_index(steps, lambda s: s.get("id") == "trusted-src")
    preserve_i = step_index(steps, lambda s: s.get("id") == "nl-result")
    route_i = step_index(steps, lambda s: s.get("id") == "route")
    execute_i = step_index(steps, lambda s: s.get("id") == "execute")
    claude_indexes = [
        i for i, s in enumerate(steps) if "claude-code-action" in str(s.get("uses", ""))
    ]
    check(
        "workflow: trusted source is prepared before every Claude step",
        trusted_i is not None
        and claude_indexes
        and all(trusted_i < i for i in claude_indexes),
    )
    check(
        "workflow: nl-result runs after every Claude step",
        preserve_i is not None
        and claude_indexes
        and all(i < preserve_i for i in claude_indexes),
    )
    check(
        "workflow: trusted deterministic steps run after result isolation",
        None not in (preserve_i, route_i, execute_i) and preserve_i < route_i < execute_i,
    )


def test_route_and_execute_stay_deterministic():
    steps = handle_steps()
    route = step_by_id(steps, "route")
    execute = step_by_id(steps, "execute")

    check("workflow: nl-route step still exists", route is not None)
    if route:
        dumped = yaml.safe_dump(route)
        run = str(route.get("run", ""))
        check(
            "workflow: nl-route still runs the deterministic trust boundary",
            "scripts/apply_decision.py nl-route" in run and "env -i" in run,
        )
        check(
            "workflow: nl-route does not receive READONLY_TOKEN or FLEET_TOKEN",
            "READONLY_TOKEN" not in dumped and "FLEET_TOKEN" not in dumped,
        )

    check("workflow: execute step still exists", execute is not None)
    if execute:
        dumped = yaml.safe_dump(execute)
        env = execute.get("env", {})
        run = str(execute.get("run", ""))
        check(
            "workflow: execute still acts under FLEET_TOKEN",
            env.get("GH_TOKEN") == "${{ secrets.FLEET_TOKEN }}",
        )
        check(
            "workflow: execute never receives READONLY_TOKEN",
            "READONLY_TOKEN" not in dumped,
        )
        check(
            "workflow: execute script is unchanged",
            "scripts/apply_decision.py execute" in run and "env -i" in run,
        )


def main():
    test_handle_checkout_does_not_persist_default_token()
    test_readonly_gate_and_prompt_gating()
    test_search_wrapper_install_step()
    test_claude_steps_split_legacy_vs_search()
    test_search_wrapper_repositories_are_owner_scoped()
    test_search_wrapper_rejects_out_of_scope_repo()
    test_search_wrapper_hardcodes_repo_flags()
    test_search_wrapper_places_untrusted_query_after_separator()
    test_search_wrapper_rejects_query_scope_qualifiers()
    test_search_wrapper_installs_non_writable_tool()
    test_claude_output_is_isolated_before_routing()
    test_route_and_execute_stay_deterministic()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all nl-decisions search tests passed")


if __name__ == "__main__":
    main()
