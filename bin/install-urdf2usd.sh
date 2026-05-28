#!/bin/bash
# Installer for urdf2usd on glibc-2.31 hosts (Ubuntu 20.04 / ROS Noetic).
#
# Downloads a pre-built rootfs containing urdf-usd-converter from a public
# GitHub Release, extracts it to /opt/urdf-usd-converter-rootfs/, installs
# bubblewrap, and drops the urdf2usd wrapper into /usr/local/bin/.
#
# Needs root (writes to /opt and /usr/local/bin). Run with:
#   curl -fsSL https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/main/bin/install-urdf2usd.sh | sudo bash
# Or with a specific version:
#   curl -fsSL .../install-urdf2usd.sh | sudo bash -s -- --version 0.1.3

set -euo pipefail

DEFAULT_VERSION="0.1.3"

version="${DEFAULT_VERSION}"
repo="Extend-Robotics/er_build_tools"
branch="main"

while [ $# -gt 0 ]; do
    case "$1" in
        --version) version="$2"; shift 2;;
        --repo)    repo="$2"; shift 2;;
        --branch)  branch="$2"; shift 2;;
        -h|--help)
            echo "Usage: install-urdf2usd.sh [--version X.Y.Z] [--repo OWNER/REPO] [--branch BRANCH]"
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

for required_command in curl tar gzip apt-get; do
    command -v "${required_command}" >/dev/null || {
        echo "ERROR: required command not found: ${required_command}" >&2
        exit 1
    }
done

release_tag="urdf2usd-v${version}"
asset_name="urdf-usd-converter-rootfs-${version}.tar.gz"
asset_url="https://github.com/${repo}/releases/download/${release_tag}/${asset_name}"
wrapper_url="https://raw.githubusercontent.com/${repo}/${branch}/bin/urdf2usd"

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
