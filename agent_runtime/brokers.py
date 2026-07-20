"""Trusted host brokers for provider-only egress and read-only GitHub search."""

from __future__ import annotations

import json
import os
import selectors
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .redaction import sanitize_message

MAX_HEADER = 65536
PUBLIC_BROKER_PROCESS_ALLOWANCE = 64
EXERCISE_BROKER_PROCESS_ALLOWANCE = 32


class BrokerError(ValueError):
    pass


def _uid_process_limit(additional: int, *, proc_root: Path = Path("/proc")) -> int:
    """Bound broker tasks without charging the shared runner's existing tasks."""
    if additional <= 0:
        raise BrokerError("broker process allowance is invalid")
    count = 0
    try:
        entries = list(proc_root.iterdir())
    except OSError as error:
        raise BrokerError("broker process accounting is unavailable") from error
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "status").read_text(
                encoding="utf-8", errors="strict"
            ).splitlines()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as error:
            raise BrokerError("broker process accounting failed closed") from error
        uid_line = next((line for line in fields if line.startswith("Uid:")), None)
        if uid_line is None:
            raise BrokerError("broker process accounting returned malformed status")
        try:
            real_uid = int(uid_line.split()[1])
        except (IndexError, ValueError) as error:
            raise BrokerError("broker process accounting returned malformed UID") from error
        if real_uid != os.getuid():
            continue
        try:
            count += sum(
                1 for task in (entry / "task").iterdir() if task.name.isdigit()
            )
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as error:
            raise BrokerError("broker task accounting failed closed") from error
    if count < 1:
        raise BrokerError("broker process accounting found no runner process")
    return count + additional


def _canonical_leaf(path: str, label: str) -> Path:
    """Resolve only the trusted parent and reject a supplied symlink leaf."""
    requested = Path(path)
    if requested.exists() and requested.is_symlink():
        raise BrokerError("%s path must not be a symlink" % label)
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError as error:
        raise BrokerError("%s parent is unavailable" % label) from error
    canonical = parent / requested.name
    canonical.mkdir(mode=0o700, exist_ok=True)
    metadata = canonical.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or canonical.resolve(strict=True) != canonical
    ):
        raise BrokerError("%s path is not private and canonical" % label)
    return canonical


def _terminate_exact(process: subprocess.Popen[bytes], label: str) -> None:
    """Terminate only the retained child handle and prove it is gone."""
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            if process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
                    pass
    if process.poll() is None:
        raise BrokerError("%s process survived exact-handle cleanup" % label)


def _recv_header(connection: socket.socket) -> tuple[bytes, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_HEADER:
            raise BrokerError("proxy header exceeded its byte bound")
    head, marker, tail = data.partition(b"\r\n\r\n")
    if not marker:
        raise BrokerError("proxy request header is incomplete")
    return head + marker, tail


def _allowed_host(host: str, allowlist: tuple[str, ...]) -> bool:
    candidate = host.rstrip(".").casefold()
    for allowed in allowlist:
        rule = allowed.rstrip(".").casefold()
        if rule.startswith("*."):
            suffix = rule[1:]
            if candidate.endswith(suffix) and candidate != suffix[1:]:
                return True
        elif candidate == rule:
            return True
    return False


def _relay(left: socket.socket, right: socket.socket, initial: bytes = b"") -> None:
    if initial:
        right.sendall(initial)
    selector = selectors.DefaultSelector()
    selector.register(left, selectors.EVENT_READ, right)
    selector.register(right, selectors.EVENT_READ, left)
    try:
        while selector.get_map():
            for key, _ in selector.select(timeout=60):
                source = key.fileobj
                destination = key.data
                try:
                    chunk = source.recv(65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    selector.unregister(source)
                    continue
                destination.sendall(chunk)
    finally:
        selector.close()


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class ProviderProxy:
    """A CONNECT proxy reachable only through a mounted Unix socket."""

    def __init__(self, socket_path: str, allowed_hosts: list[str]) -> None:
        self.socket_path = socket_path
        self.allowed_hosts = tuple(allowed_hosts)
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    header, tail = _recv_header(self.request)
                    lines = header.split(b"\r\n")
                    method, target, _ = lines[0].decode("ascii", "strict").split(" ", 2)
                    if method.upper() == "CONNECT":
                        host, separator, port_text = target.rpartition(":")
                        if not separator or not port_text.isdigit():
                            raise BrokerError("proxy CONNECT target is invalid")
                        port = int(port_text)
                        if port != 443:
                            raise BrokerError("provider proxy permits TLS port 443 only")
                    else:
                        parsed = urllib.parse.urlsplit(target)
                        host = parsed.hostname or ""
                        port = parsed.port or (443 if parsed.scheme == "https" else 80)
                        if parsed.scheme not in ("http", "https"):
                            raise BrokerError("provider proxy request scheme is invalid")
                    if not _allowed_host(host, proxy.allowed_hosts):
                        raise BrokerError("provider endpoint is outside the selected auth profile")
                    upstream = socket.create_connection((host, port), timeout=20)
                    try:
                        if method.upper() == "CONNECT":
                            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                            _relay(self.request, upstream, tail)
                        else:
                            _relay(self.request, upstream, header + tail)
                    finally:
                        upstream.close()
                except Exception:
                    try:
                        self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                    except OSError:
                        pass

        Path(socket_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        self.server = _ThreadingUnixServer(socket_path, Handler)
        os.chmod(socket_path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass


class SearchBroker:
    """Owner-scoped read-only GitHub tool broker.

    Only this trusted host process sees READONLY_TOKEN. Child ``gh`` receives a
    scrubbed environment containing that token and fixed command shapes from the
    existing Wheelhouse search implementation.
    """

    def __init__(self, socket_path: str, owner: str, target_repo: str, token: str, config: dict[str, Any]) -> None:
        if not token:
            raise BrokerError("read-only GitHub search broker credential is missing")
        scripts = str(Path(__file__).resolve().parents[1] / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import nl_readonly_search as search

        self.search = search
        self.token = token
        self.allowed = search.allowed_repos(owner, target_repo, config=config)
        broker = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = self.request.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > search.MAX_REQUEST_BYTES:
                        break
                response: dict[str, Any]
                try:
                    if len(data) > search.MAX_REQUEST_BYTES:
                        raise ValueError("search request exceeded its bound")
                    request = json.loads(data.decode("utf-8"))

                    def runner(arguments: list[str]) -> str:
                        environment = {
                            "GH_TOKEN": broker.token,
                            "GH_PROMPT_DISABLED": "1",
                            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                            "HOME": os.environ.get("RUNNER_TEMP", "/tmp"),
                            "LC_ALL": "C.UTF-8",
                            "TZ": "UTC",
                        }
                        completed = subprocess.run(["gh"] + list(arguments), capture_output=True, text=True, env=environment, timeout=30)
                        output = completed.stdout
                        if completed.returncode:
                            output += "\n[gh read failed]\n"
                        return search._cap(output)

                    text = search.handle_request(request, broker.allowed, runner=runner)
                    response = {"ok": True, "text": text, "truncated": text.endswith("[output truncated]\n")}
                except Exception as error:
                    response = {"ok": False, "error": sanitize_message(str(error), fallback="search request rejected")}
                self.request.sendall(json.dumps(response, separators=(",", ":")).encode("utf-8"))

        Path(socket_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        self.socket_path = socket_path
        self.server = _ThreadingUnixServer(socket_path, Handler)
        os.chmod(socket_path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass


class PublicReadBrokerProcess:
    """Launch the anonymous public broker outside every credentialed process.

    Production requires Bubblewrap and gives the broker private PID, mount, IPC,
    and UTS namespaces while deliberately retaining only public network egress.
    The privileged namespace setup drops to the runner UID/GID before Python
    starts. The narrowly scoped local-test mode still launches the real broker
    module as a separate scrubbed process, but cannot claim Linux mount isolation.
    """

    ALLOWED_ENVIRONMENT = {
        "PATH",
        "PYTHONPATH",
        "HOME",
        "TMPDIR",
        "TZ",
        "LC_ALL",
        "LANG",
    }

    def __init__(
        self,
        root: str,
        execution_id: str,
        task_sha256: str,
        *,
        test_unsandboxed: bool = False,
    ) -> None:
        self.root = _canonical_leaf(root, "public-read broker")
        self._root_identity = (
            self.root.lstat().st_dev,
            self.root.lstat().st_ino,
        )
        self._anchor_identity = (
            self.root.parent.lstat().st_dev,
            self.root.parent.lstat().st_ino,
        )
        self.socket_path = str(self.root / "public.sock")
        self.receipt_dir = self.root / "receipts"
        self.home = self.root / "empty-home"
        self.attestation_path = self.root / "attestation.json"
        self.runtime_root = self.root.parent / (self.root.name + "-runtime")
        self.execution_id = execution_id
        self.task_sha256 = task_sha256
        self.test_unsandboxed = test_unsandboxed
        self.process: subprocess.Popen[bytes] | None = None
        self.stderr_path = self.root / "broker.stderr"
        self._stderr_handle: Any | None = None

    def _validate_trusted_tree(self) -> None:
        """Prove every mutable launch path is under one private runner root."""
        anchor = self.root.parent
        candidates = (anchor, self.root, self.runtime_root)
        if self.root.parent != anchor or self.runtime_root.parent != anchor:
            raise BrokerError("public-read broker paths escaped the trusted root")
        for path in candidates:
            try:
                metadata = path.lstat()
            except OSError as error:
                raise BrokerError("public-read broker launch tree is unavailable") from error
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o022
                or path.resolve(strict=True) != path
            ):
                raise BrokerError("public-read broker launch tree is not private and canonical")
        root_metadata = self.root.lstat()
        if (root_metadata.st_dev, root_metadata.st_ino) != self._root_identity:
            raise BrokerError("public-read broker root was replaced")
        anchor_metadata = anchor.lstat()
        if (anchor_metadata.st_dev, anchor_metadata.st_ino) != self._anchor_identity:
            raise BrokerError("public-read broker anchor was replaced")

    def _stage_runtime(self) -> Path:
        import shutil

        self.runtime_root.mkdir(mode=0o700, exist_ok=True)
        package = self.runtime_root / "agent_runtime"
        package.mkdir(exist_ok=True, mode=0o700)
        source = Path(__file__).resolve().parent
        for name in (
            "__init__.py",
            "contract.py",
            "public_read.py",
            "public_read_broker.py",
        ):
            shutil.copyfile(source / name, package / name)
            os.chmod(package / name, 0o444)
        return self.runtime_root

    def _direct(self) -> tuple[list[str], dict[str, str]]:
        import platform
        import shutil

        runtime = self._stage_runtime()
        environment = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONPATH": str(runtime),
            "HOME": str(self.home),
            "TMPDIR": str(self.root / "tmp"),
            "TZ": "UTC",
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
        }
        python_binary = str(Path(sys.executable).resolve())
        broker_command = [
            python_binary,
            "-m",
            "agent_runtime.public_read_broker",
            "--socket",
            self.socket_path,
            "--receipts",
            str(self.receipt_dir),
            "--home",
            str(self.home),
            "--execution-id",
            self.execution_id,
            "--task-sha256",
            self.task_sha256,
            "--attestation",
            str(self.attestation_path),
            "--isolation-mode",
            "macos-sandbox" if platform.system() == "Darwin" else "local-process-test",
        ]
        if platform.system() == "Darwin":
            sandbox = shutil.which("sandbox-exec")
            if not sandbox:
                raise BrokerError("local public-read broker requires sandbox-exec")
            profile = self.root / "broker.sb"
            rules = [
                "(version 1)",
                "(allow default)",
            ]
            host_home = str(Path.home()).replace("\\", "\\\\").replace('"', '\\"')
            rules.append('(deny file-read* (subpath "%s"))' % host_home)
            python_root = str(Path(python_binary).parents[1]).replace("\\", "\\\\").replace('"', '\\"')
            rules.append('(allow file-read* (subpath "%s"))' % python_root)
            quoted_root = str(self.root).replace("\\", "\\\\").replace('"', '\\"')
            rules.append('(allow file-read* (subpath "%s"))' % quoted_root)
            rules.append('(allow file-write* (subpath "%s"))' % quoted_root)
            quoted_runtime = str(self.runtime_root).replace("\\", "\\\\").replace('"', '\\"')
            rules.append('(allow file-read* (subpath "%s"))' % quoted_runtime)
            profile.write_text("\n".join(rules) + "\n", encoding="utf-8")
            return [sandbox, "-f", str(profile), *broker_command], environment
        return broker_command, environment

    def _sandboxed(self) -> tuple[list[str], dict[str, str]]:
        import platform
        import shutil

        if platform.system() != "Linux":
            raise BrokerError("public-read broker production isolation requires Linux")
        binary = shutil.which("bwrap")
        if not binary:
            raise BrokerError("public-read broker production isolation requires Bubblewrap")
        prlimit = shutil.which("prlimit")
        if not prlimit:
            raise BrokerError("public-read broker production isolation requires prlimit")
        privilege_drop = shutil.which("setpriv")
        if not privilege_drop:
            raise BrokerError("public-read broker production isolation requires setpriv")
        process_limit = _uid_process_limit(PUBLIC_BROKER_PROCESS_ALLOWANCE)
        runtime_root = str(self._stage_runtime())
        command = [
            "sudo",
            "--non-interactive",
            prlimit,
            "--as=1073741824",
            "--cpu=600",
            "--fsize=314572800",
            "--nofile=256",
            "--nproc=%d" % process_limit,
            "--",
            binary,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CAP_SETUID",
            "--cap-add",
            "CAP_SETGID",
            "--clearenv",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--chmod",
            "1777",
            "/tmp",
            "--dir",
            "/runtime",
            "--dir",
            "/run",
            "--dir",
            "/run/wheelhouse",
            "--dir",
            "/home",
            "--dir",
            "/home/broker",
            "--dir",
            "/etc",
            "--ro-bind",
            runtime_root + "/agent_runtime",
            "/runtime/agent_runtime",
            "--bind",
            str(self.root),
            "/run/wheelhouse",
        ]
        for path in (
            "/usr",
            "/bin",
            "/lib",
            "/lib64",
            "/etc/ssl",
            "/etc/ca-certificates",
            "/usr/local/share/ca-certificates",
            "/etc/resolv.conf",
            "/etc/hosts",
            "/etc/nsswitch.conf",
        ):
            if os.path.exists(path):
                command.extend(["--ro-bind", path, path])
        command.extend(
            [
                "--setenv",
                "PATH",
                "/usr/bin:/bin",
                "--setenv",
                "PYTHONPATH",
                "/runtime",
                "--setenv",
                "HOME",
                "/home/broker",
                "--setenv",
                "TMPDIR",
                "/tmp",
                "--setenv",
                "TZ",
                "UTC",
                "--setenv",
                "LC_ALL",
                "C.UTF-8",
                "--setenv",
                "LANG",
                "C.UTF-8",
                "--chdir",
                "/tmp",
                "--",
                privilege_drop,
                "--reuid",
                str(os.getuid()),
                "--regid",
                str(os.getgid()),
                "--clear-groups",
                "--inh-caps=-all",
                "--ambient-caps=-all",
                "--no-new-privs",
                "--",
                "python3",
                "-m",
                "agent_runtime.public_read_broker",
                "--socket",
                "/run/wheelhouse/public.sock",
                "--receipts",
                "/run/wheelhouse/receipts",
                "--home",
                "/home/broker",
                "--execution-id",
                self.execution_id,
                "--task-sha256",
                self.task_sha256,
                "--attestation",
                "/run/wheelhouse/attestation.json",
                "--isolation-mode",
                "bubblewrap",
            ]
        )
        return command, {}

    def start(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True, mode=0o700)
        command, environment = (
            self._direct() if self.test_unsandboxed else self._sandboxed()
        )
        if environment and set(environment) - self.ALLOWED_ENVIRONMENT:
            raise BrokerError("public-read broker launch environment is not scrubbed")
        self._validate_trusted_tree()
        self.stderr_path.touch(mode=0o600, exist_ok=False)
        self._stderr_handle = self.stderr_path.open("wb", buffering=0)
        self.process = subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr_handle,
            cwd=str(self.root),
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        socket_path = Path(self.socket_path)
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                exit_code = self.process.returncode
                stderr = self._admission_stderr()
                self.close()
                raise BrokerError(
                    "public-read broker exited during admission (exit %s): %s"
                    % (exit_code, stderr or "no stderr")
                )
            if socket_path.exists() and self.attestation_path.is_file():
                self._verify_runner_owned_outputs()
                return
            time.sleep(0.05)
        self.close()
        raise BrokerError("public-read broker admission timed out")

    def _admission_stderr(self) -> str:
        handle = self._stderr_handle
        if handle is not None:
            try:
                handle.flush()
            except OSError:
                pass
        try:
            raw = self.stderr_path.read_bytes()[-32768:]
        except OSError:
            return ""
        return raw.decode("utf-8", "replace").replace("\x00", "\\0").strip()

    def _verify_runner_owned_outputs(self) -> None:
        for base, directories, files in os.walk(self.root, followlinks=False):
            for name in [".", *directories, *files]:
                path = Path(base) if name == "." else Path(base) / name
                metadata = path.lstat()
                if metadata.st_uid != os.getuid() or stat.S_ISLNK(metadata.st_mode):
                    raise BrokerError("public-read broker left a privileged output")

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is not None:
            _terminate_exact(process, "public-read broker")
        if self._stderr_handle is not None:
            try:
                self._stderr_handle.close()
            except OSError:
                pass
            self._stderr_handle = None
        self._verify_runner_owned_outputs()
        import shutil

        if self.runtime_root.exists():
            self._validate_trusted_tree()
            shutil.rmtree(self.runtime_root, ignore_errors=True)


class ExerciseBrokerProcess:
    """Launch the small reviewed exercise adapter set with no network namespace."""

    ALLOWED_ENVIRONMENT = PublicReadBrokerProcess.ALLOWED_ENVIRONMENT

    def __init__(
        self,
        root: str,
        evidence_root: str,
        execution_id: str,
        task_sha256: str,
        *,
        test_unsandboxed: bool = False,
    ) -> None:
        self.root = _canonical_leaf(root, "exercise broker")
        supplied_evidence = Path(evidence_root)
        if supplied_evidence.is_symlink():
            raise BrokerError("exercise evidence path must not be a symlink")
        self.evidence_root = supplied_evidence.resolve(strict=True)
        self._root_identity = (self.root.lstat().st_dev, self.root.lstat().st_ino)
        self._anchor_identity = (
            self.root.parent.lstat().st_dev,
            self.root.parent.lstat().st_ino,
        )
        self._evidence_identity = (
            self.evidence_root.lstat().st_dev,
            self.evidence_root.lstat().st_ino,
        )
        self.socket_path = str(self.root / "exercise.sock")
        self.scratch = self.root / "scratch"
        self.attestation_path = self.root / "attestation.json"
        self.stderr_path = self.root / "exercise.stderr"
        self.runtime_root = self.root.parent / (self.root.name + "-runtime")
        self.execution_id = execution_id
        self.task_sha256 = task_sha256
        self.test_unsandboxed = test_unsandboxed
        self.process: subprocess.Popen[bytes] | None = None
        self._stderr_handle: Any | None = None

    def _stage_runtime(self) -> Path:
        import shutil

        self.runtime_root.mkdir(mode=0o700, exist_ok=True)
        package = self.runtime_root / "agent_runtime"
        package.mkdir(exist_ok=True, mode=0o700)
        source = Path(__file__).resolve().parent
        for name in (
            "__init__.py",
            "contract.py",
            "public_read.py",
            "exercise.py",
            "exercise_broker.py",
        ):
            shutil.copyfile(source / name, package / name)
            os.chmod(package / name, 0o444)
        return self.runtime_root

    def _validate_paths(self) -> None:
        anchor = self.root.parent
        for path in (anchor, self.root, self.runtime_root, self.evidence_root):
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o022
                or path.resolve(strict=True) != path
            ):
                raise BrokerError("exercise broker launch tree is not private and canonical")
        if self.root.parent != anchor or self.runtime_root.parent != anchor:
            raise BrokerError("exercise broker paths escaped the trusted root")
        if (self.root.lstat().st_dev, self.root.lstat().st_ino) != self._root_identity:
            raise BrokerError("exercise broker root was replaced")
        if (anchor.lstat().st_dev, anchor.lstat().st_ino) != self._anchor_identity:
            raise BrokerError("exercise broker anchor was replaced")
        if (
            self.evidence_root.lstat().st_dev,
            self.evidence_root.lstat().st_ino,
        ) != self._evidence_identity:
            raise BrokerError("exercise evidence root was replaced")

    def _command(self) -> tuple[list[str], dict[str, str]]:
        import platform
        import shutil

        runtime = self._stage_runtime()
        if self.test_unsandboxed:
            environment = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "PYTHONPATH": str(runtime),
                "HOME": str(self.root),
                "TMPDIR": str(self.scratch),
                "TZ": "UTC",
                "LC_ALL": "C.UTF-8",
                "LANG": "C.UTF-8",
            }
            return [
                str(Path(sys.executable).resolve()),
                "-m",
                "agent_runtime.exercise_broker",
                "--socket",
                self.socket_path,
                "--evidence",
                str(self.evidence_root),
                "--scratch",
                str(self.scratch),
                "--execution-id",
                self.execution_id,
                "--task-sha256",
                self.task_sha256,
                "--attestation",
                str(self.attestation_path),
            ], environment
        if platform.system() != "Linux":
            raise BrokerError("exercise broker production isolation requires Linux")
        binary = shutil.which("bwrap")
        prlimit = shutil.which("prlimit")
        privilege_drop = shutil.which("setpriv")
        if not binary or not prlimit or not privilege_drop:
            raise BrokerError(
                "exercise broker production isolation requires Bubblewrap, prlimit, and setpriv"
            )
        process_limit = _uid_process_limit(EXERCISE_BROKER_PROCESS_ALLOWANCE)
        command = [
            "sudo",
            "--non-interactive",
            prlimit,
            "--as=1073741824",
            "--cpu=120",
            "--fsize=209715200",
            "--nofile=128",
            "--nproc=%d" % process_limit,
            "--",
            binary,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-net",
            "--unshare-ipc",
            "--unshare-uts",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CAP_SETUID",
            "--cap-add",
            "CAP_SETGID",
            "--clearenv",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--chmod",
            "1777",
            "/tmp",
            "--dir",
            "/runtime",
            "--dir",
            "/run",
            "--dir",
            "/run/exercise",
            "--dir",
            "/evidence",
            "--dir",
            "/etc",
            "--ro-bind",
            str(runtime / "agent_runtime"),
            "/runtime/agent_runtime",
            "--bind",
            str(self.root),
            "/run/exercise",
            "--bind",
            str(self.evidence_root),
            "/evidence",
        ]
        for path in (
            "/usr",
            "/bin",
            "/lib",
            "/lib64",
            "/etc/ssl",
            "/etc/ca-certificates",
        ):
            if os.path.exists(path):
                command.extend(["--ro-bind", path, path])
        command.extend(
            [
                "--setenv", "PATH", "/usr/bin:/bin",
                "--setenv", "PYTHONPATH", "/runtime",
                "--setenv", "HOME", "/tmp",
                "--setenv", "TMPDIR", "/tmp",
                "--setenv", "TZ", "UTC",
                "--setenv", "LC_ALL", "C.UTF-8",
                "--setenv", "LANG", "C.UTF-8",
                "--chdir", "/tmp",
                "--",
                privilege_drop,
                "--reuid", str(os.getuid()),
                "--regid", str(os.getgid()),
                "--clear-groups",
                "--inh-caps=-all",
                "--ambient-caps=-all",
                "--no-new-privs",
                "--",
                "python3", "-m", "agent_runtime.exercise_broker",
                "--socket", "/run/exercise/exercise.sock",
                "--evidence", "/evidence",
                "--scratch", "/run/exercise/scratch",
                "--execution-id", self.execution_id,
                "--task-sha256", self.task_sha256,
                "--attestation", "/run/exercise/attestation.json",
            ]
        )
        return command, {}

    def start(self) -> None:
        self.scratch.mkdir(mode=0o700, exist_ok=True)
        command, environment = self._command()
        if environment and set(environment) - self.ALLOWED_ENVIRONMENT:
            raise BrokerError("exercise broker launch environment is not scrubbed")
        self._validate_paths()
        self.stderr_path.touch(mode=0o600, exist_ok=False)
        self._stderr_handle = self.stderr_path.open("wb", buffering=0)
        self.process = subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr_handle,
            cwd=str(self.root),
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                code = self.process.returncode
                stderr = self.stderr_path.read_text(encoding="utf-8", errors="replace")[-32768:].strip()
                self.close()
                raise BrokerError(
                    "exercise broker exited during admission (exit %s): %s"
                    % (code, stderr or "no stderr")
                )
            if Path(self.socket_path).exists() and self.attestation_path.is_file():
                self._verify_runner_owned_outputs()
                return
            time.sleep(0.05)
        self.close()
        raise BrokerError("exercise broker admission timed out")

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is not None:
            _terminate_exact(process, "exercise broker")
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None
        self._verify_runner_owned_outputs()
        import shutil

        if self.runtime_root.exists():
            self._validate_paths()
            shutil.rmtree(self.runtime_root, ignore_errors=True)

    def _verify_runner_owned_outputs(self) -> None:
        for base, directories, files in os.walk(self.root, followlinks=False):
            for name in [".", *directories, *files]:
                path = Path(base) if name == "." else Path(base) / name
                metadata = path.lstat()
                if metadata.st_uid != os.getuid() or stat.S_ISLNK(metadata.st_mode):
                    raise BrokerError("exercise broker left a privileged output")
