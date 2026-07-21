#!/usr/bin/env python3
"""Focused offline regression tests for wheelhouse-search public_clone."""

import json
import os
import socket
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import apply_decision as ad  # noqa: E402
import nl_readonly_search as nls  # noqa: E402

_failures = []
PUBLIC_IP = "93.184.216.34"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        _failures.append(name)


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def public_resolver(host, port, type=None):
    del host, type
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]


def resolver_for(*addresses):
    def resolve(host, port, type=None):
        del host, type
        rows = []
        for address in addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (
                (address, port, 0, 0)
                if family == socket.AF_INET6
                else (address, port)
            )
            rows.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return rows

    return resolve


def rejected(call, text=""):
    try:
        call()
    except ValueError as exc:
        return not text or text in str(exc)
    return False


class StockGit:
    def __init__(self, clone_returncode=0, retained_size=None, commit=COMMIT):
        self.calls = []
        self.clone_returncode = clone_returncode
        self.retained_size = retained_size
        self.commit = commit

    def __call__(self, args, *, env, cwd=None, timeout=None):
        args = list(args)
        self.calls.append(
            {"args": args, "env": dict(env), "cwd": cwd, "timeout": timeout}
        )
        if "clone" in args:
            source = args[-1]
            os.makedirs(os.path.join(source, ".git", "objects", "pack"), exist_ok=True)
            with open(
                os.path.join(source, ".git", "config"), "w", encoding="utf-8"
            ) as handle:
                handle.write("repository administration\n")
            with open(os.path.join(source, "README.md"), "w", encoding="utf-8") as handle:
                handle.write("public source\n")
            if self.retained_size is not None:
                oversized = os.path.join(source, "oversized.bin")
                with open(oversized, "wb") as handle:
                    handle.truncate(self.retained_size)
            return subprocess.CompletedProcess(
                args, self.clone_returncode, "", "clone output"
            )
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args, 0, self.commit + "\n", "")
        return subprocess.CompletedProcess(args, 1, "", "unexpected stock Git operation")


def clone_root(parent):
    return os.path.realpath(os.path.join(parent, nls.PUBLIC_CLONE_DIR))


def clone_request(runner, root, url="https://git.example/team/repo.git", ref=None):
    request = {"op": "public_clone", "url": url}
    if ref is not None:
        request["ref"] = ref
    return nls.handle_request(
        request,
        [],
        public_runner=runner,
        resolver=public_resolver,
        clone_root=root,
        public_clone_enabled=True,
    )


def test_url_validation_and_public_addresses():
    calls = []

    def resolver(host, port, type=None):
        calls.append((host, port, type))
        return public_resolver(host, port, type=type)

    canonical, addresses = nls.validate_public_git_url(
        "HTTPS://Forge.Example:8443/team/repo.git",
        resolver=resolver,
    )
    check(
        "url: arbitrary public custom HTTPS host and port are accepted",
        canonical == "https://forge.example:8443/team/repo.git"
        and addresses == [PUBLIC_IP]
        and calls == [("forge.example", 8443, socket.SOCK_STREAM)],
    )
    for value in (
        "owner/repo",
        "http://example.com/repo.git",
        "git://example.com/repo.git",
        "ssh://git@example.com/repo.git",
        "file:///tmp/repo.git",
        "git@example.com:repo.git",
        "https://user:secret@example.com/repo.git",
        "https://example.com/",
        "https://bad_host.example/repo.git",
        "https://example.com/repo.git?token=none",
        "https://example.com/repo.git#main",
        "https://example.com/a/../repo.git",
        "https://example.com/repo%0A.git",
    ):
        check(
            "url: unsafe target is rejected: %s" % value,
            rejected(
                lambda value=value: nls.validate_public_git_url(
                    value, resolver=public_resolver
                )
            ),
        )

    for address in (
        "127.0.0.1",
        "10.0.0.1",
        "100.64.0.1",
        "169.254.169.254",
        "192.0.2.1",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "fe80::1",
        "fd00::1",
        "ff02::1",
        "::ffff:10.0.0.1",
    ):
        check(
            "address: non-public target is rejected: %s" % address,
            rejected(
                lambda address=address: nls.validate_public_git_url(
                    "https://forge.example/repo.git",
                    resolver=resolver_for(address),
                ),
                "non-public address",
            ),
        )
    check(
        "address: mixed public and private answers fail closed",
        rejected(
            lambda: nls.validate_public_git_url(
                "https://forge.example/repo.git",
                resolver=resolver_for(PUBLIC_IP, "10.0.0.1"),
            )
        ),
    )


def test_ref_argument_safety():
    for ref in (
        "-c",
        "--upload-pack=evil",
        "../main",
        "refs/../main",
        "a@{b",
        "a:b",
        "a b",
        ".hidden",
        "main.lock",
    ):
        check(
            "ref: unsafe value is rejected: %s" % ref,
            rejected(lambda ref=ref: nls._safe_public_ref(ref)),
        )
    check(
        "ref: ordinary branch is accepted",
        nls._safe_public_ref("release/v1.2") == "release/v1.2",
    )
    with tempfile.TemporaryDirectory() as parent:
        fake = StockGit()
        clone_request(fake, clone_root(parent), ref="release/v1.2")
        clone_args = fake.calls[0]["args"]
        check(
            "ref: safe ref is passed only as stock clone branch data",
            "--branch" in clone_args
            and clone_args[clone_args.index("--branch") + 1] == "release/v1.2"
            and all("upload-pack" not in value for value in clone_args),
        )


def test_exact_stock_clone_argv_environment_and_data_only_result():
    secret_values = {
        "GH_TOKEN": "gh-secret-marker",
        "GITHUB_TOKEN": "github-secret-marker",
        "READONLY_TOKEN": "readonly-secret-marker",
        "FLEET_TOKEN": "fleet-secret-marker",
        "CLAUDE_CODE_OAUTH_TOKEN": "model-secret-marker",
        "ACTIONS_RUNTIME_TOKEN": "runner-secret-marker",
        "AWS_SECRET_ACCESS_KEY": "cloud-secret-marker",
    }
    original = {key: os.environ.get(key) for key in secret_values}
    os.environ.update(secret_values)
    try:
        with tempfile.TemporaryDirectory() as parent:
            root = clone_root(parent)
            fake = StockGit()
            result = json.loads(
                clone_request(
                    fake,
                    root,
                    url="https://Forge.Example:443/team/repo.git",
                )
            )
            git = nls.shutil.which("git")
            source = os.path.join(root, "source")
            expected = [
                git,
                "-c",
                "credential.helper=",
                "-c",
                "credential.interactive=never",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.https.allow=always",
                "-c",
                "protocol.version=2",
                "-c",
                "transfer.bundleURI=false",
                "-c",
                "fetch.bundleURI=",
                "-c",
                "fetch.fsckObjects=true",
                "-c",
                "submodule.recurse=false",
                "-c",
                "fetch.recurseSubmodules=false",
                "-c",
                "http.followRedirects=false",
                "clone",
                "--quiet",
                "--no-tags",
                "--no-recurse-submodules",
                "--depth=1",
                "--single-branch",
                "--filter=blob:limit=%s" % nls.MAX_PUBLIC_CLONE_BYTES,
                "https://forge.example/team/repo.git",
                source,
            ]
            check("git: exactly one stock clone and one stock SHA resolution", len(fake.calls) == 2)
            check("git: hardened clone argv is exact", fake.calls[0]["args"] == expected)
            check(
                "git: clone and SHA resolution have separate bounded timeouts",
                fake.calls[0]["cwd"] == os.path.join(root, "runtime")
                and fake.calls[0]["timeout"] == nls.PUBLIC_CLONE_TIMEOUT_SECONDS
                and fake.calls[1]["cwd"] == source
                and fake.calls[1]["timeout"] == nls.PUBLIC_GIT_LOCAL_TIMEOUT_SECONDS,
            )
            expected_env = {
                "PATH",
                "HOME",
                "TMPDIR",
                "XDG_CONFIG_HOME",
                "LC_ALL",
                "GIT_ASKPASS",
                "SSH_ASKPASS",
                "GIT_TERMINAL_PROMPT",
                "GCM_INTERACTIVE",
                "GIT_CONFIG_NOSYSTEM",
                "GIT_CONFIG_SYSTEM",
                "GIT_CONFIG_GLOBAL",
                "GIT_ATTR_NOSYSTEM",
                "GIT_LFS_SKIP_SMUDGE",
                "GIT_PROTOCOL_FROM_USER",
                "GIT_OPTIONAL_LOCKS",
            }
            env = fake.calls[0]["env"]
            check("git: child environment is an exact safe allowlist", set(env) == expected_env)
            check(
                "git: credentials are scrubbed and anonymous controls are exact",
                all(
                    marker not in str(call["env"])
                    for marker in secret_values.values()
                    for call in fake.calls
                )
                and env["GIT_ASKPASS"] == "/bin/false"
                and env["SSH_ASKPASS"] == "/bin/false"
                and env["GIT_TERMINAL_PROMPT"] == "0"
                and env["GIT_LFS_SKIP_SMUDGE"] == "1"
                and env["GIT_CONFIG_GLOBAL"] == os.devnull
                and "GIT_OBJECT_DIRECTORY" not in env
                and "GIT_EXEC_PATH" not in env,
            )
            check(
                "git: tags, hooks, submodules, and custom transport machinery are disabled",
                "--no-tags" in expected
                and "--no-recurse-submodules" in expected
                and "core.hooksPath=/dev/null" in expected
                and "protocol.https.allow=always" in expected
                and "protocol.allow=never" in expected
                and "fetch.recurseSubmodules=false" in expected,
            )
            check(
                "result: only canonical URL, SHA, data location, and bounded manifest are returned",
                result["url"] == "https://forge.example/team/repo.git"
                and result["commit"] == COMMIT
                and result["location"] == source
                and result["manifest"]["paths"] == ["README.md"]
                and result["manifest"]["file_count"] == 1
                and ".git" not in result["manifest"]["paths"]
                and set(result) == {"op", "url", "commit", "location", "manifest"},
            )
            check(
                "result: retained tree is outside the workspace and non-committable",
                not nls._path_within(result["location"], ROOT)
                and os.path.isfile(os.path.join(result["location"], "README.md"))
                and not os.path.lexists(os.path.join(result["location"], ".git"))
                and subprocess.run(
                    ["git", "-C", result["location"], "status"],
                    capture_output=True,
                ).returncode
                != 0,
            )
            check(
                "cleanup: successful clone remains for model reads until trusted cleanup",
                os.path.isdir(root)
                and nls.cleanup_public_clones(root)
                and not os.path.lexists(root),
            )
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_post_clone_limits_and_deterministic_cleanup():
    with tempfile.TemporaryDirectory() as parent:
        root = clone_root(parent)
        fake = StockGit(retained_size=nls.MAX_PUBLIC_CLONE_BYTES + 1)
        check(
            "bounds: retained-byte overflow is rejected after stock clone",
            rejected(lambda: clone_request(fake, root), "retained byte limit"),
        )
        check("cleanup: byte overflow removes the complete clone root", not os.path.lexists(root))

    original_limit = nls.MAX_PUBLIC_CLONE_FILES
    try:
        nls.MAX_PUBLIC_CLONE_FILES = 0
        with tempfile.TemporaryDirectory() as parent:
            root = clone_root(parent)
            check(
                "bounds: retained-file overflow is rejected after stock clone",
                rejected(lambda: clone_request(StockGit(), root), "retained file limit"),
            )
            check("cleanup: file overflow removes the complete clone root", not os.path.lexists(root))
    finally:
        nls.MAX_PUBLIC_CLONE_FILES = original_limit

    with tempfile.TemporaryDirectory() as parent:
        root = clone_root(parent)
        check(
            "cleanup: failed stock clone removes partial output",
            rejected(
                lambda: clone_request(StockGit(clone_returncode=1), root),
                "clone output",
            )
            and not os.path.lexists(root),
        )


def test_stock_git_output_is_bounded():
    result = nls.run_public_git(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 20000); sys.stderr.write('y' * 20000)",
        ],
        env={"PATH": os.environ.get("PATH", os.defpath)},
        timeout=10,
    )
    check(
        "output: stdout and stderr are captured with independent hard bounds",
        result.returncode == 0
        and "[git stdout truncated]" in result.stdout
        and "[git stderr truncated]" in result.stderr
        and len(result.stdout) <= nls.MAX_PUBLIC_GIT_OUTPUT_CHARS + 32
        and len(result.stderr) <= nls.MAX_PUBLIC_GIT_OUTPUT_CHARS + 32,
    )


def test_operation_scope_documentation_and_same_turn_action():
    with tempfile.TemporaryDirectory() as parent:
        root = clone_root(parent)
        check(
            "scope: public_clone is denied outside nl-decision.search",
            rejected(
                lambda: nls.handle_request(
                    {"op": "public_clone", "url": "https://git.example/repo.git"},
                    [],
                    public_runner=StockGit(),
                    resolver=public_resolver,
                    clone_root=root,
                ),
                "natural-language decisions",
            ),
        )
    check(
        "scope: authenticated gh operations still require their existing allowlist",
        rejected(lambda: nls.handle_request({"op": "pr_list"}, []), "no repositories"),
    )

    source = read("scripts", "nl_readonly_search.py")
    forbidden = (
        "fetch-pack",
        "index-pack",
        "GIT_OBJECT_DIRECTORY",
        "GIT_EXEC_PATH",
        "refs/wheelhouse/public",
        "public-index-pack-pump",
        "_materialize_public_clone",
    )
    check(
        "architecture: no custom fetch, object store, namespace, or materializer remains",
        all(token not in source for token in forbidden),
    )

    workflow = read(".github", "workflows", "claude-model.yml")
    exact = "--allowedTools Read,Grep,Glob,Write,Bash(wheelhouse-search)\\n"
    cleanup = workflow.index("- name: Remove bounded public clones")
    capture = workflow.index("- id: capture")
    check(
        "tools: exact search allowed-tools bytes remain unchanged",
        workflow.count(exact) == 3,
    )
    check(
        "cleanup: trusted always step runs before capture",
        cleanup < capture
        and "always() && steps.hydrate.outputs.action == 'nl-decision.search'"
        in workflow[cleanup:capture]
        and '"$RUNNER_TEMP/wheelhouse-tools/wheelhouse-search" cleanup'
        in workflow[cleanup:capture],
    )

    prompt = ad.build_nl_prompt(
        "card",
        "inspect the public repository and merge if appropriate",
        "pr-review",
        search_enabled=True,
    )
    routed = ad.route_decision(
        {"mode": "action", "action": "merge"},
        "pr-review",
        {"repo": "target", "number": 7, "head_sha": COMMIT},
        owner="owner",
    )
    delivery_doc = read("docs", "READONLY_TOKEN_DELIVERY.md")
    check(
        "same-turn: public clone prompt and existing action route remain available",
        "`public_clone` accepts" in prompt
        and "Never execute cloned files" in prompt
        and routed["mode"] == "action"
        and routed["decision"] == "merge",
    )
    check(
        "documentation: transient stock-Git residual is explicit",
        "may transiently download or write more pack data" in delivery_doc
        and "complete clone root is deterministically" in delivery_doc,
    )


def main():
    test_url_validation_and_public_addresses()
    test_ref_argument_safety()
    test_exact_stock_clone_argv_environment_and_data_only_result()
    test_post_clone_limits_and_deterministic_cleanup()
    test_stock_git_output_is_bounded()
    test_operation_scope_documentation_and_same_turn_action()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all public-clone offline tests passed")


if __name__ == "__main__":
    main()
