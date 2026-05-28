#!/bin/bash
# SKIP_CHECK
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

for required_command in curl tar gzip apt-get python3 sha256sum; do
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
sha256_name="${asset_name}.sha256"
release_base="https://github.com/${repo}/releases/download/${release_tag}"
asset_url="${release_base}/${asset_name}"
sha256_url="${release_base}/${sha256_name}"
wrapper_url="${release_base}/urdf2usd"

rootfs_dir=/opt/urdf-usd-converter-rootfs
new_rootfs_dir="${rootfs_dir}.new.$$"
old_rootfs_dir="${rootfs_dir}.old.$$"
wrapper_path=/usr/local/bin/urdf2usd
wrapper_staging_path="${wrapper_path}.new.$$"

if ! command -v bwrap >/dev/null; then
    echo "==> Installing bubblewrap"
    apt-get update
    apt-get install -y bubblewrap
fi

tmp_dir="$(mktemp -d)"
swap_phase=pre_swap

# Phase-aware cleanup so an interrupted install never leaves /opt without
# a rootfs:
#   pre_swap    — nothing in /opt has moved yet; just drop staging.
#   mid_swap    — old rootfs was renamed to .old.$$ but new rename hasn't
#                 happened or hasn't succeeded; restore the old one.
#   swapped     — new rootfs is in place; drop the leftover .old.$$.
install_cleanup() {
    rm -rf "${tmp_dir}" "${wrapper_staging_path}"
    case "${swap_phase}" in
        pre_swap)
            rm -rf "${new_rootfs_dir}"
            ;;
        mid_swap)
            if [ ! -d "${rootfs_dir}" ] && [ -d "${old_rootfs_dir}" ]; then
                echo "==> Install interrupted mid-swap; restoring previous rootfs at ${rootfs_dir}" >&2
                mv "${old_rootfs_dir}" "${rootfs_dir}"
            fi
            rm -rf "${new_rootfs_dir}"
            ;;
        swapped)
            rm -rf "${old_rootfs_dir}"
            ;;
    esac
}
trap install_cleanup EXIT

# Download every release artefact into tmp_dir BEFORE touching /opt or
# /usr/local/bin, so a partial download can never leave a half-installed
# host. The sha256 manifest published by build-and-release-urdf2usd.sh
# covers the rootfs tarball AND the urdf2usd wrapper; both must verify.
echo "==> Downloading rootfs ${version} from ${asset_url}"
curl -fsSL -o "${tmp_dir}/${asset_name}" "${asset_url}"

echo "==> Downloading wrapper from ${wrapper_url}"
curl -fsSL -o "${tmp_dir}/urdf2usd" "${wrapper_url}"

echo "==> Downloading checksum from ${sha256_url}"
curl -fsSL -o "${tmp_dir}/${sha256_name}" "${sha256_url}"

echo "==> Verifying sha256 manifest covers both rootfs tarball and wrapper"
for expected in "${asset_name}" urdf2usd; do
    grep -qE "  ${expected}\$" "${tmp_dir}/${sha256_name}" || {
        echo "ERROR: sha256 manifest does not cover ${expected}; release is malformed." >&2
        exit 1
    }
done

echo "==> Verifying checksums"
( cd "${tmp_dir}" && sha256sum -c "${sha256_name}" )

echo "==> Extracting rootfs to staging dir ${new_rootfs_dir}"
mkdir -p "${new_rootfs_dir}"
tar -xzf "${tmp_dir}/${asset_name}" -C "${new_rootfs_dir}"

[ -x "${new_rootfs_dir}/usr/local/bin/urdf_usd_converter" ] || {
    echo "ERROR: extracted rootfs is missing /usr/local/bin/urdf_usd_converter; tarball is corrupt or wrong shape." >&2
    exit 1
}

# Prepare the wrapper on the same filesystem as ${wrapper_path} so the
# final mv is a single rename(2) and other processes never see a
# half-written or non-executable wrapper file.
cp "${tmp_dir}/urdf2usd" "${wrapper_staging_path}"
chmod +x "${wrapper_staging_path}"

echo "==> Swapping rootfs into place at ${rootfs_dir}"
swap_phase=mid_swap
if [ -d "${rootfs_dir}" ]; then
    mv "${rootfs_dir}" "${old_rootfs_dir}"
fi
mv "${new_rootfs_dir}" "${rootfs_dir}"
swap_phase=swapped

echo "==> Installing wrapper at ${wrapper_path}"
mv "${wrapper_staging_path}" "${wrapper_path}"

echo
echo "Done. Test with:"
echo "  urdf2usd --help"
