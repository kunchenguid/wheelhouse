#!/usr/bin/env python3
"""Offline regression tests for wheelhouse-search public_clone. No network."""

import json
import os
import shutil
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
                (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
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


class FakeGit:
    def __init__(self, clone_returncode=0, clone_output="", files=None):
        self.calls = []
        self.clone_returncode = clone_returncode
        self.clone_output = clone_output
        self.files = files or {"README.md": "public source\n"}

    def __call__(self, args, *, env, cwd=None, timeout=None):
        args = list(args)
        self.calls.append(
            {"args": args, "env": dict(env), "cwd": cwd, "timeout": timeout}
        )
        if "clone" in args:
            source = args[-1]
            os.makedirs(os.path.join(source, ".git"), exist_ok=True)
            with open(
                os.path.join(source, ".git", "config"), "w", encoding="utf-8"
            ) as handle:
                handle.write('[remote "origin"]\n')
            return subprocess.CompletedProcess(
                args,
                self.clone_returncode,
                self.clone_output,
                "",
            )
        if "ls-tree" in args:
            listing = []
            for name, contents in sorted(self.files.items()):
                size = len(contents.encode("utf-8"))
                listing.append(
                    "100644 blob %s %s\t%s\0"
                    % ("0" * 40, size, name)
                )
            return subprocess.CompletedProcess(args, 0, "".join(listing), "")
        if "read-tree" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        if "checkout" in args:
            source = cwd
            for name, contents in self.files.items():
                path = os.path.join(source, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(contents)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, COMMIT + "\n", "")


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


def test_url_validation_and_custom_domains():
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
            "url: forbidden or malformed target is rejected: %s" % value,
            rejected(
                lambda value=value: nls.validate_public_git_url(
                    value, resolver=public_resolver
                )
            ),
        )


def test_private_reserved_and_metadata_addresses_are_rejected():
    for address in (
        "127.0.0.1",
        "10.0.0.1",
        "100.64.0.1",
        "169.254.169.254",
        "192.0.2.1",
        "::1",
        "fe80::1",
        "fd00::1",
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

    def timeout_resolver(host, port, type=None):
        del host, port, type
        raise TimeoutError

    check(
        "address: DNS timeout is a bounded failure",
        rejected(
            lambda: nls.validate_public_git_url(
                "https://forge.example/repo.git",
                resolver=timeout_resolver,
            ),
            "timed out",
        ),
    )
    check(
        "address: excessive DNS answers fail closed",
        rejected(
            lambda: nls.validate_public_git_url(
                "https://forge.example/repo.git",
                resolver=lambda host, port, type=None: public_resolver(host, port, type)
                * (nls.MAX_PUBLIC_DNS_ANSWERS + 1),
            ),
            "too many addresses",
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
        fake = FakeGit()
        clone_request(fake, clone_root(parent), ref="release/v1.2")
        args = fake.calls[0]["args"]
        check(
            "ref: safe ref occupies one argv element before the option separator",
            args[args.index("--branch") : args.index("--branch") + 2]
            == ["--branch", "release/v1.2"]
            and args[-3] == "--",
        )
        nls.cleanup_public_clones(clone_root(parent))


def test_exact_hardened_git_argv_environment_and_manifest():
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
            fake = FakeGit()
            result = json.loads(
                clone_request(
                    fake,
                    root,
                    url="https://Forge.Example:443/team/repo.git",
                )
            )
            git = shutil.which("git")
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
                "submodule.recurse=false",
                "-c",
                "fetch.recurseSubmodules=false",
                "-c",
                "http.followRedirects=false",
                "clone",
                "--quiet",
                "--depth",
                "1",
                "--no-checkout",
                "--no-tags",
                "--single-branch",
                "--no-recurse-submodules",
                "--config",
                "remote.origin.tagOpt=--no-tags",
                "--",
                "https://forge.example/team/repo.git",
                os.path.join(root, "source"),
            ]
            check(
                "git: clone argv is exact and hardened",
                fake.calls[0]["args"] == expected,
            )
            check(
                "git: clone, tree inspection, checkout, and local SHA resolution have bounded timeouts",
                fake.calls[0]["timeout"] == nls.PUBLIC_CLONE_TIMEOUT_SECONDS
                and all(
                    call["timeout"] == nls.PUBLIC_GIT_LOCAL_TIMEOUT_SECONDS
                    for call in fake.calls[1:]
                ),
            )
            check(
                "git: tree limits are checked before checkout",
                "ls-tree" in fake.calls[2]["args"]
                and "read-tree" in fake.calls[3]["args"]
                and "checkout" in fake.calls[4]["args"],
            )
            env = fake.calls[0]["env"]
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
            check(
                "git: child environment is an exact safe allowlist",
                set(env) == expected_env,
            )
            check(
                "git: no model, GitHub, cloud, or runner credential reaches either child",
                all(
                    marker not in str(call["env"])
                    for marker in secret_values.values()
                    for call in fake.calls
                )
                and all(not nls.SENSITIVE_ENV_RE.search(name) for name in env),
            )
            check(
                "git: prompting, helpers, LFS, hooks, redirects, tags, and submodules are disabled",
                env["GIT_TERMINAL_PROMPT"] == "0"
                and env["GIT_ASKPASS"] == "/bin/false"
                and env["GIT_LFS_SKIP_SMUDGE"] == "1"
                and "credential.helper=" in expected
                and "core.hooksPath=/dev/null" in expected
                and "http.followRedirects=false" in expected
                and "--no-tags" in expected
                and "--no-recurse-submodules" in expected,
            )
            check(
                "result: canonical URL, commit, isolated location, and bounded manifest are returned",
                result["url"] == "https://forge.example/team/repo.git"
                and result["commit"] == COMMIT
                and result["location"] == os.path.join(root, "source")
                and result["manifest"]["paths"] == ["README.md"]
                and result["manifest"]["file_count"] == 2
                and result["manifest"]["retained_bytes"] <= nls.MAX_PUBLIC_CLONE_BYTES,
            )
            check(
                "result: DNS resolution addresses are not returned",
                "validated_addresses" not in result
                and "cleanup" not in result
                and "ref" not in result,
            )
            check(
                "result: clone is outside the target workspace and readable",
                not nls._path_within(result["location"], ROOT)
                and open(
                    os.path.join(result["location"], "README.md"), encoding="utf-8"
                ).read()
                == "public source\n",
            )
            check(
                "cleanup: successful clone is retained for model reads",
                os.path.isdir(root),
            )
            check(
                "cleanup: explicit trusted cleanup reports removal",
                nls.cleanup_public_clones(root),
            )
            check(
                "cleanup: trusted cleanup removes the entire clone root",
                not os.path.lexists(root),
            )
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_bounds_failure_output_cap_and_deterministic_cleanup():
    old_files = nls.MAX_PUBLIC_CLONE_FILES
    try:
        nls.MAX_PUBLIC_CLONE_FILES = 1
        with tempfile.TemporaryDirectory() as parent:
            root = clone_root(parent)
            fake = FakeGit(files={"one": "1", "two": "2"})
            check(
                "bounds: pre-checkout file overflow fails closed",
                rejected(lambda: clone_request(fake, root), "file limit"),
            )
            check(
                "cleanup: over-limit clone is removed immediately",
                not os.path.lexists(root),
            )
    finally:
        nls.MAX_PUBLIC_CLONE_FILES = old_files

    old_bytes = nls.MAX_PUBLIC_CLONE_BYTES
    try:
        nls.MAX_PUBLIC_CLONE_BYTES = 8
        with tempfile.TemporaryDirectory() as parent:
            root = clone_root(parent)
            fake = FakeGit(files={"large": "retained bytes exceed the cap"})
            check(
                "bounds: pre-checkout retained-byte overflow fails closed",
                rejected(lambda: clone_request(fake, root), "byte limit"),
            )
            check(
                "cleanup: retained-byte overflow clone is removed immediately",
                not os.path.lexists(root),
            )
    finally:
        nls.MAX_PUBLIC_CLONE_BYTES = old_bytes

    old_bytes = nls.MAX_PUBLIC_CLONE_BYTES
    try:
        nls.MAX_PUBLIC_CLONE_BYTES = 30
        with tempfile.TemporaryDirectory() as parent:
            root = clone_root(parent)
            fake = FakeGit(files={"small": "123456789012345"})
            check(
                "bounds: clone metadata and tree bytes share one aggregate cap",
                rejected(lambda: clone_request(fake, root), "byte limit"),
            )
            check(
                "cleanup: aggregate metadata overflow clone is removed immediately",
                not os.path.lexists(root),
            )
    finally:
        nls.MAX_PUBLIC_CLONE_BYTES = old_bytes

    class TimeoutGit(FakeGit):
        def __call__(self, args, *, env, cwd=None, timeout=None):
            if "clone" in args:
                os.makedirs(args[-1], exist_ok=True)
                raise subprocess.TimeoutExpired(args, timeout)
            return super().__call__(args, env=env, cwd=cwd, timeout=timeout)

    with tempfile.TemporaryDirectory() as parent:
        root = clone_root(parent)
        check(
            "bounds: clone timeout fails closed",
            rejected(lambda: clone_request(TimeoutGit(), root), "timed out"),
        )
        check(
            "cleanup: timed-out clone is removed immediately", not os.path.lexists(root)
        )

    with tempfile.TemporaryDirectory() as parent:
        root = clone_root(parent)
        fake = FakeGit(
            clone_returncode=1, clone_output="x" * (nls.MAX_PUBLIC_GIT_OUTPUT_CHARS * 2)
        )
        try:
            clone_request(fake, root)
        except ValueError as exc:
            bounded = len(
                str(exc)
            ) < nls.MAX_PUBLIC_GIT_OUTPUT_CHARS + 100 and "truncated" in str(exc)
        else:
            bounded = False
        check("bounds: Git diagnostic output is capped", bounded)
        check("cleanup: failed clone is removed immediately", not os.path.lexists(root))

    class TreeErrorGit(FakeGit):
        def __call__(self, args, *, env, cwd=None, timeout=None):
            if "ls-tree" in args:
                result = super().__call__(args, env=env, cwd=cwd, timeout=timeout)
                return subprocess.CompletedProcess(
                    args,
                    1,
                    result.stdout,
                    "x" * (nls.MAX_PUBLIC_TREE_OUTPUT_CHARS * 2),
                )
            return super().__call__(args, env=env, cwd=cwd, timeout=timeout)

    with tempfile.TemporaryDirectory() as parent:
        root = clone_root(parent)
        try:
            clone_request(TreeErrorGit(), root)
        except ValueError as exc:
            bounded = len(str(exc)) < nls.MAX_PUBLIC_GIT_OUTPUT_CHARS + 100
        else:
            bounded = False
        check("bounds: tree diagnostics retain the small error cap", bounded)
        check("cleanup: tree inspection failure is removed immediately", not os.path.lexists(root))

    with tempfile.TemporaryDirectory() as parent:
        root = clone_root(parent)
        first = FakeGit(files={"old.txt": "old"})
        second = FakeGit(files={"new.txt": "new"})
        clone_request(first, root)
        clone_request(second, root)
        check(
            "cleanup: a later public_clone deterministically replaces the prior clone",
            not os.path.exists(os.path.join(root, "source", "old.txt"))
            and os.path.isfile(os.path.join(root, "source", "new.txt")),
        )
        nls.cleanup_public_clones(root)

    with tempfile.TemporaryDirectory() as parent:
        root = clone_root(parent)
        special_name = 'quote"\nname'
        result = json.loads(
            clone_request(FakeGit(files={special_name: "x"}), root)
        )
        check(
            "result: escaped manifest paths remain valid bounded JSON",
            special_name in result["manifest"]["paths"]
            and len(json.dumps(result)) <= nls.MAX_OUTPUT_CHARS,
        )
        nls.cleanup_public_clones(root)


def test_preparation_failure_cleans_partial_root():
    original_makedirs = nls.os.makedirs
    with tempfile.TemporaryDirectory() as parent:
        root = clone_root(parent)
        failure_path = os.path.join(root, "runtime", "home")

        def failing_makedirs(path, *args, **kwargs):
            if path == failure_path:
                raise OSError("injected preparation failure")
            return original_makedirs(path, *args, **kwargs)

        nls.os.makedirs = failing_makedirs
        try:
            preparation_failed = False
            try:
                nls._prepare_public_clone_root(root)
            except OSError:
                preparation_failed = True
            check(
                "cleanup: partial preparation is removed",
                preparation_failed,
            )
        finally:
            nls.os.makedirs = original_makedirs
        check(
            "cleanup: preparation failure leaves no root",
            not os.path.lexists(root),
        )


def test_raw_materialization_ignores_checkout_attributes():
    with tempfile.TemporaryDirectory() as parent:
        repository = os.path.join(parent, "repository")
        source = os.path.join(parent, "source")
        runtime = os.path.join(parent, "runtime")
        home = os.path.join(runtime, "home")
        tmp = os.path.join(runtime, "tmp")
        config = os.path.join(home, "config")
        for path in (repository, home, tmp, config):
            os.makedirs(path, exist_ok=True)
        subprocess.run(["git", "-C", repository, "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", repository, "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repository, "config", "user.name", "test"],
            check=True,
        )
        with open(os.path.join(repository, "eol.txt"), "wb") as handle:
            handle.write(b"Line\n")
        with open(os.path.join(repository, "encoded.txt"), "wb") as handle:
            handle.write(b"L\x00i\x00n\x00e\x00\n\x00")
        with open(
            os.path.join(repository, ".gitattributes"), "w", encoding="utf-8"
        ) as handle:
            handle.write(
                "eol.txt eol=crlf\nencoded.txt working-tree-encoding=UTF-16LE\n"
            )
        subprocess.run(["git", "-C", repository, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", repository, "commit", "-qm", "attributes"],
            check=True,
        )
        subprocess.run(
            ["git", "clone", "-q", "-n", repository, source], check=True
        )
        env = nls._public_git_env(home, tmp, config)
        tree = subprocess.run(
            [
                "git",
                "-C",
                source,
                "ls-tree",
                "-r",
                "-l",
                "-z",
                "--full-tree",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        tree_manifest = nls._bounded_public_tree_manifest(tree.stdout)
        subprocess.run(
            ["git", "-C", source, "read-tree", "--reset", "HEAD"],
            env=env,
            check=True,
        )
        metadata_manifest = nls._bounded_clone_manifest(source)
        nls._check_public_clone_totals(metadata_manifest, tree_manifest)
        nls._materialize_public_clone_raw(
            shutil.which("git"),
            env,
            source,
            tree_manifest["entries"],
            metadata_manifest,
            nls.PUBLIC_GIT_LOCAL_TIMEOUT_SECONDS,
        )
        check(
            "checkout: eol and working-tree-encoding do not transform retained blobs",
            open(os.path.join(source, "eol.txt"), "rb").read() == b"Line\n"
            and open(os.path.join(source, "encoded.txt"), "rb").read()
            == b"Line\n",
        )


def test_operation_scope_allowed_tools_cleanup_and_same_turn_action():
    with tempfile.TemporaryDirectory() as parent:
        root = clone_root(parent)
        check(
            "scope: public_clone is denied outside nl-decision.search",
            rejected(
                lambda: nls.handle_request(
                    {"op": "public_clone", "url": "https://git.example/repo.git"},
                    [],
                    public_runner=FakeGit(),
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

    workflow = read(".github", "workflows", "claude-model.yml")
    exact = "--allowedTools Read,Grep,Glob,Write,Bash(wheelhouse-search)\\n"
    check(
        "tools: search allowed-tools bytes are unchanged in all three Claude steps",
        workflow.count(exact) == 3,
    )
    check(
        "tools: task capability remains only the exact wheelhouse-search Bash command",
        'tools.append("Bash(wheelhouse-search)")'
        in read("agent_runtime", "task_builder.py")
        and "Bash(git" not in workflow
        and "Bash(curl" not in workflow,
    )
    cleanup = workflow.index("- name: Remove bounded public clones")
    capture = workflow.index("- id: capture")
    check(
        "cleanup: trusted always step removes clones after the model and before capture",
        cleanup < capture
        and "always() && steps.hydrate.outputs.action == 'nl-decision.search'"
        in workflow[cleanup:capture]
        and '"$RUNNER_TEMP/wheelhouse-tools/wheelhouse-search" cleanup'
        in workflow[cleanup:capture],
    )
    check(
        "scope: workflow enables public clone only for nl-decision.search",
        'if [ "$ACTION" = "nl-decision.search" ]' in workflow
        and "WHEELHOUSE_PUBLIC_CLONE_ENABLED=1" in workflow,
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
    check(
        "same-turn: prompt exposes only public_clone and data-only inspection",
        "`public_clone` accepts `url` plus an optional safe `ref`" in prompt
        and "Never execute cloned files" in prompt,
    )
    check(
        "same-turn: existing structured route still accepts an authorized mutation",
        routed["mode"] == "action" and routed["decision"] == "merge",
    )

    delivery_doc = read("docs", "READONLY_TOKEN_DELIVERY.md")
    check(
        "dns: accepted validation-to-Git rebinding residual is documented and bounded",
        "DNS rebinding residual" in delivery_doc
        and "validation and Git's connection" in delivery_doc,
    )


def main():
    test_url_validation_and_custom_domains()
    test_private_reserved_and_metadata_addresses_are_rejected()
    test_ref_argument_safety()
    test_exact_hardened_git_argv_environment_and_manifest()
    test_bounds_failure_output_cap_and_deterministic_cleanup()
    test_preparation_failure_cleans_partial_root()
    test_raw_materialization_ignores_checkout_attributes()
    test_operation_scope_allowed_tools_cleanup_and_same_turn_action()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all public-clone offline tests passed")


if __name__ == "__main__":
    main()
