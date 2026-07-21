#!/usr/bin/env python3
"""Scoped read-only search helper for Wheelhouse's Claude steps.

The workflow installs this file as `wheelhouse-search` only when the optional
READONLY_TOKEN secret is present. Claude can write a JSON request to
`search-request.json` and run that wrapper, but the wrapper controls the actual
command shape: no writes and bounded output. Authenticated `gh` operations stay
limited to the target repo plus owner-scoped repos from `wheelhouse.config.yml`.
The separate `public_clone` operation accepts a complete public HTTPS Git URL,
validates its current addresses, and invokes stock Git anonymously in an
isolated temporary directory. It removes Git administration before a
post-clone retained-tree audit and never executes cloned content.
"""

import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from urllib.parse import unquote, urlsplit, urlunsplit

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
HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
REF_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f~^:?*\\[]")
SENSITIVE_ENV_RE = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|COOKIE|OAUTH|"
    r"^GH_|^GITHUB_|^ACTIONS_|^AWS_|^AZURE_|^GOOGLE_|^CLAUDE_)",
    re.I,
)

PUBLIC_CLONE_DIR = "wheelhouse-public-clones"
MAX_PUBLIC_URL_CHARS = 2048
MAX_PUBLIC_REF_CHARS = 255
PUBLIC_CLONE_TIMEOUT_SECONDS = 90
PUBLIC_GIT_LOCAL_TIMEOUT_SECONDS = 10
PUBLIC_DNS_TIMEOUT_SECONDS = 5
MAX_PUBLIC_DNS_ANSWERS = 32
MAX_PUBLIC_GIT_OUTPUT_CHARS = 8000
MAX_PUBLIC_CLONE_FILES = 20000
MAX_PUBLIC_CLONE_ENTRIES = 30000
MAX_PUBLIC_CLONE_BYTES = 100 * 1024 * 1024
MAX_PUBLIC_MANIFEST_ENTRIES = 200
MAX_PUBLIC_MANIFEST_PATH_BYTES = 20000
MAX_PUBLIC_SYMLINK_BYTES = 4096

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
    matches = [
        repo for repo in allowed if repo.rsplit("/", 1)[1].casefold() == raw.casefold()
    ]
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


def _canonical_public_git_url(value):
    if not isinstance(value, str):
        raise ValueError("url must be a complete HTTPS Git URL")
    raw = value.strip()
    if not raw or len(raw) > MAX_PUBLIC_URL_CHARS or raw != value:
        raise ValueError("url must be a complete HTTPS Git URL")
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in raw):
        raise ValueError("url contains whitespace or control characters")
    if "\\" in raw:
        raise ValueError("url must not contain backslashes")

    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("url has a malformed host or port") from exc
    if parsed.scheme.casefold() != "https" or not parsed.netloc:
        raise ValueError("url must use HTTPS")
    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        raise ValueError("url must not contain embedded credentials or userinfo")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("url has a malformed HTTPS port")
    if parsed.query or parsed.fragment:
        raise ValueError("url must not contain a query or fragment")
    if not parsed.path or parsed.path == "/" or not parsed.path.startswith("/"):
        raise ValueError("url must include a Git repository path")
    if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path):
        raise ValueError("url path contains malformed percent encoding")
    try:
        decoded_path = unquote(parsed.path, errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("url path must be valid UTF-8") from exc
    if any(part in {".", ".."} for part in decoded_path.split("/")):
        raise ValueError("url path must not contain traversal segments")
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in decoded_path):
        raise ValueError("url path contains whitespace or control characters")

    host = parsed.hostname or ""
    host = host[:-1] if host.endswith(".") else host
    if not host:
        raise ValueError("url has a malformed host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("url has a malformed host") from exc
        labels = ascii_host.split(".")
        if len(ascii_host) > 253 or any(
            not label or not HOST_LABEL_RE.fullmatch(label) for label in labels
        ):
            raise ValueError("url has a malformed host")
        canonical_host = ascii_host
    else:
        canonical_host = address.compressed

    authority = "[%s]" % canonical_host if ":" in canonical_host else canonical_host
    if port not in (None, 443):
        authority += ":%s" % port
    canonical_path = re.sub(
        r"%([0-9A-Fa-f]{2})",
        lambda match: "%" + match.group(1).upper(),
        parsed.path,
    )
    return (
        urlunsplit(("https", authority, canonical_path, "", "")),
        canonical_host,
        port or 443,
    )


def _public_addresses(host, port=443, resolver=socket.getaddrinfo):
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        timed_out = False

        def timeout_handler(_signum, _frame):
            raise TimeoutError

        can_alarm = threading.current_thread() is threading.main_thread() and hasattr(
            signal, "setitimer"
        )
        previous_handler = None
        try:
            if can_alarm:
                previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
                signal.setitimer(signal.ITIMER_REAL, PUBLIC_DNS_TIMEOUT_SECONDS)
            rows = resolver(host, port, type=socket.SOCK_STREAM)
        except TimeoutError:
            timed_out = True
            rows = []
        except (OSError, socket.gaierror) as exc:
            raise ValueError("public Git host could not be resolved") from exc
        finally:
            if can_alarm:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)
        if timed_out:
            raise ValueError(
                "public Git host resolution timed out after %s seconds"
                % PUBLIC_DNS_TIMEOUT_SECONDS
            )
        if len(rows) > MAX_PUBLIC_DNS_ANSWERS:
            raise ValueError("public Git host returned too many addresses")
        raw_addresses = [row[4][0].split("%", 1)[0] for row in rows if row[4]]
    else:
        raw_addresses = [str(literal)]

    addresses = []
    for raw in raw_addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise ValueError("public Git host resolved to a malformed address") from exc
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if (
            not address.is_global
            or address.is_private
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            or address.is_loopback
            or address.is_link_local
            or getattr(address, "is_site_local", False)
        ):
            raise ValueError("public Git host resolved to a non-public address")
        normalized = address.compressed
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise ValueError("public Git host did not resolve to an address")
    return addresses


def validate_public_git_url(value, resolver=socket.getaddrinfo):
    """Validate the anonymous public target without consulting the gh allowlist."""
    canonical, host, port = _canonical_public_git_url(value)
    return canonical, _public_addresses(host, port=port, resolver=resolver)


def _safe_public_ref(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("ref must be a branch or tag name")
    ref = value
    if (
        not ref
        or len(ref) > MAX_PUBLIC_REF_CHARS
        or ref.startswith("-")
        or ref.startswith("/")
        or ref.endswith("/")
        or ref.endswith(".")
        or ref == "@"
        or ".." in ref
        or "@{" in ref
        or "//" in ref
        or REF_FORBIDDEN_RE.search(ref)
    ):
        raise ValueError("ref must be a safe branch or tag name")
    for part in ref.split("/"):
        if not part or part.startswith(".") or part.endswith(".lock"):
            raise ValueError("ref must be a safe branch or tag name")
    return ref


def _path_within(path, parent):
    try:
        return os.path.commonpath(
            [os.path.realpath(path), os.path.realpath(parent)]
        ) == os.path.realpath(parent)
    except ValueError:
        return False


def _public_clone_root(explicit=None):
    if explicit is None:
        base = os.environ.get("RUNNER_TEMP", "").strip() or tempfile.gettempdir()
        root = os.path.join(os.path.realpath(base), PUBLIC_CLONE_DIR)
    else:
        root = os.path.realpath(explicit)
    if not os.path.isabs(root) or os.path.basename(root) != PUBLIC_CLONE_DIR:
        raise ValueError("public clone root is not a bounded temporary location")
    workspace = os.environ.get("GITHUB_WORKSPACE", "").strip() or os.getcwd()
    if _path_within(root, workspace):
        raise ValueError("public clone root must be outside the target workspace")
    return root


def cleanup_public_clones(clone_root=None):
    root = _public_clone_root(clone_root)
    if not os.path.lexists(root):
        return False
    st = os.lstat(root)
    if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        shutil.rmtree(root)
    else:
        os.unlink(root)
    return True


def _prepare_public_clone_root(clone_root=None):
    root = _public_clone_root(clone_root)
    try:
        cleanup_public_clones(root)
        os.makedirs(root, mode=stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        runtime = os.path.join(root, "runtime")
        home = os.path.join(runtime, "home")
        tmp = os.path.join(runtime, "tmp")
        config = os.path.join(home, "config")
        for path in (runtime, home, tmp, config):
            os.makedirs(
                path, mode=stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR, exist_ok=True
            )
        return root, runtime, home, tmp, config
    except Exception:
        cleanup_public_clones(root)
        raise


def _public_git_env(home, tmp, config):
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": home,
        "TMPDIR": tmp,
        "XDG_CONFIG_HOME": config,
        "LC_ALL": "C",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    leaked = [name for name in env if SENSITIVE_ENV_RE.search(name)]
    if leaked:
        raise RuntimeError("sensitive environment name reached anonymous Git")
    return env


def _kill_public_git_process(process):
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()


def _read_public_git_stream(stream, captured, truncated):
    while True:
        chunk = stream.read(8192)
        if not chunk:
            return
        remaining = MAX_PUBLIC_GIT_OUTPUT_CHARS - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated.append(True)


def run_public_git(
    args,
    *,
    env,
    cwd=None,
    timeout=PUBLIC_CLONE_TIMEOUT_SECONDS,
):
    process = subprocess.Popen(
        list(args),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    stdout_data = bytearray()
    stderr_data = bytearray()
    stdout_truncated = []
    stderr_truncated = []
    readers = [
        threading.Thread(
            target=_read_public_git_stream,
            args=(process.stdout, stdout_data, stdout_truncated),
            name="wheelhouse-public-git-stdout",
        ),
        threading.Thread(
            target=_read_public_git_stream,
            args=(process.stderr, stderr_data, stderr_truncated),
            name="wheelhouse-public-git-stderr",
        ),
    ]
    for reader in readers:
        reader.start()
    timed_out = None
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        timed_out = exc
        _kill_public_git_process(process)
        process.wait()
    for reader in readers:
        reader.join()
    if timed_out is not None:
        raise ValueError(
            "public Git operation timed out after %s seconds" % timeout
        ) from timed_out

    stdout = bytes(stdout_data).decode("utf-8", errors="replace")
    stderr = bytes(stderr_data).decode("utf-8", errors="replace")
    if stdout_truncated:
        stdout += "\n[git stdout truncated]"
    if stderr_truncated:
        stderr += "\n[git stderr truncated]"
    return subprocess.CompletedProcess(list(args), process.returncode, stdout, stderr)


def _git_output(result, limit=MAX_PUBLIC_GIT_OUTPUT_CHARS):
    output = str(getattr(result, "stdout", "") or "")
    stderr = str(getattr(result, "stderr", "") or "")
    if stderr:
        output += "\n" + stderr
    if len(output) > limit:
        output = output[:limit] + "\n[git output truncated]"
    return output.strip()


def _run_public_git_checked(
    runner,
    args,
    env,
    timeout,
    cwd=None,
    error_limit=MAX_PUBLIC_GIT_OUTPUT_CHARS,
):
    try:
        result = runner(args, env=env, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            "public Git operation timed out after %s seconds" % timeout
        ) from exc
    except OSError as exc:
        raise ValueError("public Git operation could not start") from exc
    if getattr(result, "returncode", 1) != 0:
        detail = _git_output(result, limit=error_limit)
        message = "public Git operation failed"
        if detail:
            message += ": " + detail
        raise ValueError(message)
    return _git_output(result)


def _public_git_args(git):
    return [
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
    ]


def _public_clone_args(git, canonical_url, source, ref):
    args = _public_git_args(git) + [
        "clone",
        "--quiet",
        "--no-tags",
        "--no-recurse-submodules",
        "--depth=1",
        "--single-branch",
        "--filter=blob:limit=%s" % MAX_PUBLIC_CLONE_BYTES,
    ]
    if ref:
        args.extend(["--branch", ref])
    args.extend([canonical_url, source])
    return args


def _remove_git_admin(source):
    git_admin = os.path.join(source, ".git")
    if not os.path.lexists(git_admin):
        return
    info = os.lstat(git_admin)
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        shutil.rmtree(git_admin)
    else:
        os.unlink(git_admin)


def _bounded_clone_manifest(source):
    source = os.path.realpath(source)
    st = os.lstat(source)
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise ValueError("public clone did not produce a directory")

    stack = [("", source)]
    entry_count = 0
    file_count = 0
    retained_bytes = 0
    paths = []
    path_bytes = 0
    while stack:
        prefix, directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ValueError("public clone manifest could not be read") from exc
        child_dirs = []
        for entry in entries:
            rel = "%s/%s" % (prefix, entry.name) if prefix else entry.name
            try:
                rel_bytes = len(json.dumps(rel, ensure_ascii=True).encode("utf-8"))
                child = entry.stat(follow_symlinks=False)
            except (OSError, UnicodeError) as exc:
                raise ValueError("public clone contains an unreadable path") from exc
            entry_count += 1
            if entry_count > MAX_PUBLIC_CLONE_ENTRIES:
                raise ValueError("public clone exceeds the retained entry limit")
            if stat.S_ISDIR(child.st_mode):
                child_dirs.append((rel, entry.path))
                continue
            if not (stat.S_ISREG(child.st_mode) or stat.S_ISLNK(child.st_mode)):
                raise ValueError("public clone contains a special file")
            if stat.S_ISLNK(child.st_mode):
                try:
                    target = os.readlink(entry.path)
                    target_bytes = os.fsencode(target)
                except (OSError, UnicodeError) as exc:
                    raise ValueError("public clone contains an unreadable symlink") from exc
                if len(target_bytes) > MAX_PUBLIC_SYMLINK_BYTES:
                    raise ValueError("public clone contains an oversized symlink")
                if "\x00" in os.fsdecode(target_bytes):
                    raise ValueError("public clone contains an invalid symlink")
                if not _path_within(entry.path, source):
                    raise ValueError("public clone contains an escaping symlink")
            file_count += 1
            retained_bytes += child.st_size
            if file_count > MAX_PUBLIC_CLONE_FILES:
                raise ValueError("public clone exceeds the retained file limit")
            if retained_bytes > MAX_PUBLIC_CLONE_BYTES:
                raise ValueError("public clone exceeds the retained byte limit")
            if (
                len(paths) < MAX_PUBLIC_MANIFEST_ENTRIES
                and path_bytes + rel_bytes <= MAX_PUBLIC_MANIFEST_PATH_BYTES
            ):
                paths.append(rel)
                path_bytes += rel_bytes
        stack.extend(reversed(child_dirs))
    return {
        "entry_count": entry_count,
        "file_count": file_count,
        "retained_bytes": retained_bytes,
        "paths": paths,
        "paths_truncated": len(paths) < file_count,
    }


def _public_clone_request(
    req,
    runner=run_public_git,
    resolver=socket.getaddrinfo,
    clone_root=None,
):
    unexpected = sorted(set(req) - {"op", "url", "ref"})
    if unexpected:
        raise ValueError(
            "public_clone has unsupported fields: %s" % ", ".join(unexpected)
        )
    canonical_url, _ = validate_public_git_url(
        req.get("url"), resolver=resolver
    )
    ref = _safe_public_ref(req.get("ref"))
    root = _public_clone_root(clone_root)
    try:
        root, runtime, home, tmp, config = _prepare_public_clone_root(root)
        source = os.path.join(root, "source")
        git = shutil.which("git")
        if not git:
            raise ValueError("git is unavailable")
        env = _public_git_env(home, tmp, config)
        clone_args = _public_clone_args(git, canonical_url, source, ref)
        _run_public_git_checked(
            runner,
            clone_args,
            env,
            PUBLIC_CLONE_TIMEOUT_SECONDS,
            cwd=runtime,
        )
        commit = _run_public_git_checked(
            runner,
            _public_git_args(git)
            + ["rev-parse", "--verify", "HEAD^{commit}"],
            env,
            PUBLIC_GIT_LOCAL_TIMEOUT_SECONDS,
            cwd=source,
        ).strip().lower()
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
            raise ValueError("public clone did not resolve a valid commit SHA")
        _remove_git_admin(source)
        shutil.rmtree(runtime)
        manifest = _bounded_clone_manifest(source)
        result = {
            "op": "public_clone",
            "url": canonical_url,
            "commit": commit,
            "location": source,
            "manifest": manifest,
        }
        return _cap(json.dumps(result, sort_keys=True, indent=2) + "\n")
    except Exception:
        cleanup_public_clones(root)
        raise

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
        out = [
            kind,
            "list",
            "-R",
            repo,
            "--state",
            state,
            "--limit",
            limit,
            "--json",
            fields,
        ]
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


def handle_request(
    req,
    allowed,
    runner=run_gh,
    public_runner=run_public_git,
    resolver=socket.getaddrinfo,
    clone_root=None,
    public_clone_enabled=False,
):
    if not isinstance(req, dict):
        raise ValueError("request must be a JSON object")
    op = str(req.get("op") or "help").strip().lower().replace("-", "_")
    if op in {"help", "repos"}:
        ops = [
            "repos",
            "pr_list",
            "pr_view",
            "pr_diff",
            "issue_list",
            "issue_view",
            "search_prs",
            "search_issues",
            "search_code",
        ]
        if public_clone_enabled:
            ops.append("public_clone")
        return (
            json.dumps(
                {
                    "allowed_repos": allowed,
                    "request_file": REQUEST_FILE,
                    "ops": ops,
                },
                indent=2,
            )
            + "\n"
        )
    if op == "public_clone":
        if not public_clone_enabled:
            raise ValueError(
                "public_clone is available only to natural-language decisions"
            )
        return _public_clone_request(
            req,
            runner=public_runner,
            resolver=resolver,
            clone_root=clone_root,
        )
    if not allowed:
        raise ValueError("no repositories are allowed for search")

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
                raise ValueError(
                    "search tool directory must contain only wheelhouse-search"
                )
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


def cmd_scope():
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    target_repo = os.environ.get("TARGET_REPO", "").strip()
    repos = allowed_repos(owner, target_repo)
    if not repos:
        sys.exit("no allowed repositories for read-only search")
    print(json.dumps(repos, separators=(",", ":")))


def cmd_cleanup():
    cleanup_public_clones()


def cmd_run():
    try:
        output = handle_request(
            _read_request(),
            _env_allowed_repos(),
            public_clone_enabled=os.environ.get("WHEELHOUSE_PUBLIC_CLONE_ENABLED")
            == "1",
        )
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
    if len(sys.argv) == 2 and sys.argv[1] == "scope":
        cmd_scope()
        return
    if len(sys.argv) == 2 and sys.argv[1] == "cleanup":
        cmd_cleanup()
        return
    if len(sys.argv) == 1:
        cmd_run()
        return
    sys.exit("usage: nl_readonly_search.py [install|scope|cleanup]")


if __name__ == "__main__":
    main()
