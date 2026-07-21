#!/usr/bin/env python3
"""One bounded network E2E for anonymous public HTTPS Git cloning."""

import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import nl_readonly_search as nls  # noqa: E402

URL = "https://github.com/octocat/Hello-World.git"
SECRET_MARKERS = {
    "GH_TOKEN": "e2e-gh-secret-marker",
    "GITHUB_TOKEN": "e2e-github-secret-marker",
    "CLAUDE_CODE_OAUTH_TOKEN": "e2e-model-secret-marker",
    "ACTIONS_RUNTIME_TOKEN": "e2e-runner-secret-marker",
    "AWS_SECRET_ACCESS_KEY": "e2e-cloud-secret-marker",
}


def main():
    original = {key: os.environ.get(key) for key in SECRET_MARKERS}
    os.environ.update(SECRET_MARKERS)
    try:
        with tempfile.TemporaryDirectory() as parent:
            runner_temp = os.path.join(parent, "runner-temp")
            os.makedirs(runner_temp)
            clone_root = os.path.realpath(
                os.path.join(runner_temp, nls.PUBLIC_CLONE_DIR)
            )
            request = os.path.join(parent, "search-request.json")
            with open(request, "w", encoding="utf-8") as handle:
                json.dump({"op": "public_clone", "url": URL}, handle)
            child_env = dict(os.environ)
            child_env.update(
                {
                    "GITHUB_WORKSPACE": ROOT,
                    "RUNNER_TEMP": runner_temp,
                    "WHEELHOUSE_PUBLIC_CLONE_ENABLED": "1",
                    "WHEELHOUSE_SEARCH_ALLOWED_REPOS": "[]",
                    "WHEELHOUSE_SEARCH_REQUEST": request,
                }
            )
            clone = subprocess.run(
                [
                    sys.executable,
                    os.path.join(ROOT, "scripts", "nl_readonly_search.py"),
                ],
                cwd=ROOT,
                env=child_env,
                text=True,
                capture_output=True,
                timeout=nls.PUBLIC_CLONE_TIMEOUT_SECONDS + 15,
            )
            if clone.returncode != 0:
                raise SystemExit("wheelhouse-search failed: %s" % clone.stderr.strip())
            result = json.loads(clone.stdout)
            readme = os.path.join(result["location"], "README")
            if result["url"] != URL:
                raise SystemExit("canonical URL mismatch")
            if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", result["commit"]):
                raise SystemExit("resolved commit SHA missing")
            if (
                not os.path.isfile(readme)
                or "Hello World" not in open(readme, encoding="utf-8").read()
            ):
                raise SystemExit("cloned source is not inspectable")
            for directory, _, names in os.walk(result["location"]):
                for name in names:
                    path = os.path.join(directory, name)
                    if os.path.islink(path) or os.path.getsize(path) > 1024 * 1024:
                        continue
                    with open(path, "rb") as handle:
                        data = handle.read()
                    if any(
                        marker.encode() in data for marker in SECRET_MARKERS.values()
                    ):
                        raise SystemExit(
                            "credential marker reached retained clone data"
                        )
            cleanup = subprocess.run(
                [
                    sys.executable,
                    os.path.join(ROOT, "scripts", "nl_readonly_search.py"),
                    "cleanup",
                ],
                cwd=ROOT,
                env=child_env,
                text=True,
                capture_output=True,
                timeout=10,
            )
            if cleanup.returncode != 0 or os.path.lexists(clone_root):
                raise SystemExit("deterministic clone cleanup failed")
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("public clone E2E passed: %s" % URL)


if __name__ == "__main__":
    main()
