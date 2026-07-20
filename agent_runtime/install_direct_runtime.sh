#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: install_direct_runtime.sh sandbox LOCK | claude LOCK INSTALL_DIR" >&2
  exit 2
}

mode="${1:-}"
lock_file="${2:-}"
if [[ ! -f "$lock_file" || -L "$lock_file" ]]; then
  echo "runtime lock is unavailable or unsafe" >&2
  exit 1
fi

case "$mode" in
  sandbox)
    [[ "$#" -eq 2 ]] || usage
    read -r runner_image ubuntu_release package version < <(
      python - "$lock_file" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1], encoding="utf-8"))["sandbox"]
print(
    lock["runnerImage"],
    lock["ubuntuRelease"],
    lock["bubblewrapPackage"],
    lock["bubblewrapVersion"],
)
PY
    )
    # The workflow pins runs-on separately. Checking /etc/os-release here keeps
    # a future runner-label drift from silently changing package provenance.
    # shellcheck disable=SC1091
    . /etc/os-release
    if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "$ubuntu_release" || "$runner_image" != "ubuntu-$ubuntu_release" ]]; then
      echo "direct runtime requires the pinned $runner_image image" >&2
      exit 1
    fi
    sudo apt-get update -qq
    sudo apt-get install -y -qq "$package=$version"
    observed="$(dpkg-query -W -f='${Version}' "$package")"
    if [[ "$observed" != "$version" ]]; then
      echo "Bubblewrap package version does not match the runtime lock" >&2
      exit 1
    fi
    sandbox="$(command -v bwrap)"
    "$sandbox" --version
    # Exercise the same namespace primitive the production supervisor needs,
    # without credentials, a provider request, or model execution.
    # Hosted Ubuntu 24.04 permits the runner to create a network namespace but
    # denies its unprivileged loopback setup.  Use the same privileged launcher
    # as the production supervisor; Bubblewrap drops all worker capabilities.
    sudo --non-interactive "$sandbox" \
      --die-with-parent \
      --new-session \
      --unshare-all \
      --ro-bind / / \
      --proc /proc \
      --dev-bind /dev /dev \
      -- /bin/true
    ;;
  claude)
    [[ "$#" -eq 3 ]] || usage
    install_dir="$3"
    case "$(uname -m)" in
      x86_64) platform="linux-x64" ;;
      aarch64|arm64) platform="linux-arm64" ;;
      *) echo "unsupported Claude runner architecture" >&2; exit 1 ;;
    esac
    read -r version url digest < <(
      python - "$lock_file" "$platform" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1], encoding="utf-8"))["claude"]
artifact = lock["platforms"][sys.argv[2]]
print(lock["binaryVersion"], artifact["url"], artifact["sha256"])
PY
    )
    mkdir -p "$install_dir"
    download="$install_dir/.claude.download"
    trap 'rm -f "$download"' EXIT
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
      "$url" --output "$download"
    observed="$(sha256sum "$download" | cut -d' ' -f1)"
    if [[ "$observed" != "$digest" ]]; then
      echo "Claude CLI digest does not match the runtime lock" >&2
      exit 1
    fi
    mv "$download" "$install_dir/claude"
    chmod 500 "$install_dir" "$install_dir/claude"
    "$install_dir/claude" --version | grep -Fx "$version (Claude Code)"
    ;;
  *)
    usage
    ;;
esac
