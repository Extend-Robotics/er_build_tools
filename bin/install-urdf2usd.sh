#!/bin/bash
# Installer for urdf2usd on glibc-2.31 hosts (Ubuntu 20.04 / ROS Noetic).
#
# Downloads a pre-built rootfs containing urdf-usd-converter from a public
# GitHub Release, extracts it to /opt/urdf-usd-converter-rootfs/, installs
# bubblewrap, and drops the urdf2usd wrapper into /usr/local/bin/.
#
# If --version is not given, the latest urdf2usd-v* GitHub Release is used.
#
# Needs root (writes to /opt and /usr/local/bin). Run with:
#   curl -fsSL https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/main/bin/install-urdf2usd.sh | sudo bash
# Or with a specific version:
#   curl -fsSL .../install-urdf2usd.sh | sudo bash -s -- --version 0.1.3

set -euo pipefail

version=""
repo="Extend-Robotics/er_build_tools"

fetch_latest_release_version() {
    curl -fsSL "https://api.github.com/repos/${repo}/releases" \
        | python3 -c '
import json, sys
releases = json.load(sys.stdin)
tags = [r["tag_name"] for r in releases
        if r["tag_name"].startswith("urdf2usd-v") and not r.get("prerelease")]
print(tags[0][len("urdf2usd-v"):] if tags else "")
'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --version) version="$2"; shift 2;;
        --repo)    repo="$2"; shift 2;;
        -h|--help)
            echo "Usage: install-urdf2usd.sh [--version X.Y.Z] [--repo OWNER/REPO]"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

[ "$(id -u)" -eq 0 ] || {
    echo "ERROR: this installer needs root (writes to /opt and /usr/local/bin)." >&2
    echo "Pipe to sudo bash, e.g.:" >&2
    echo "  curl -fsSL .../install-urdf2usd.sh | sudo bash" >&2
    exit 1
}

for required_command in curl tar gzip apt-get python3; do
    command -v "${required_command}" >/dev/null || {
        echo "ERROR: required command not found: ${required_command}" >&2
        exit 1
    }
done

if [ -z "${version}" ]; then
    echo "==> No version specified; fetching latest urdf2usd release from ${repo}"
    version="$(fetch_latest_release_version)"
    [ -n "${version}" ] || {
        echo "ERROR: no urdf2usd-v* release found in ${repo}" >&2
        exit 1
    }
    echo "==> Latest urdf2usd release: ${version}"
fi

release_tag="urdf2usd-v${version}"
asset_name="urdf-usd-converter-rootfs-${version}.tar.gz"
release_base="https://github.com/${repo}/releases/download/${release_tag}"
asset_url="${release_base}/${asset_name}"
wrapper_url="${release_base}/urdf2usd"

rootfs_dir=/opt/urdf-usd-converter-rootfs
wrapper_path=/usr/local/bin/urdf2usd

if ! command -v bwrap >/dev/null; then
    echo "==> Installing bubblewrap"
    apt-get update
    apt-get install -y bubblewrap
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

echo "==> Downloading rootfs ${version} from ${asset_url}"
curl -fsSL -o "${tmp_dir}/${asset_name}" "${asset_url}"

echo "==> Replacing rootfs at ${rootfs_dir}"
rm -rf "${rootfs_dir}"
mkdir -p "${rootfs_dir}"
tar -xzf "${tmp_dir}/${asset_name}" -C "${rootfs_dir}"

echo "==> Installing wrapper at ${wrapper_path}"
curl -fsSL -o "${wrapper_path}" "${wrapper_url}"
chmod +x "${wrapper_path}"

echo
echo "Done. Test with:"
echo "  urdf2usd --help"
