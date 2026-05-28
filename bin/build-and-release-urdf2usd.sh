#!/bin/bash
# Build the urdf2usd rootfs from a pinned urdf-usd-converter version and
# publish it as a GitHub Release asset. The asset is consumed at install
# time by the (consumer-side) installer, which downloads the tarball,
# extracts it to /opt/urdf-usd-converter-rootfs/, and runs the converter
# under bwrap so no docker is needed on the consumer machine.
#
# Run with:
#   bin/build-and-release-urdf2usd.sh [<converter-version>] [--repo OWNER/REPO] [--force]
# If converter-version is omitted, the latest release on PyPI is used.

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
Usage: build-and-release-urdf2usd.sh [<converter-version>] [--repo OWNER/REPO] [--force]

Builds a docker image with urdf-usd-converter==<converter-version> on a
python:3.12-slim base, exports its filesystem as a gzip tarball, and uploads
it as a GitHub Release asset.

If <converter-version> is omitted, the latest release of urdf-usd-converter
on PyPI is used.

Requires docker, gh (authenticated), tar, gzip, curl, and python3 on the
local machine.

Options:
  --repo OWNER/REPO  Target GitHub repository
                     (default: Extend-Robotics/er_build_tools)
  --force            Delete an existing release with the same tag and re-create it
EOF
    exit 1
}

fetch_latest_pypi_version() {
    curl -fsSL https://pypi.org/pypi/urdf-usd-converter/json \
        | python3 -c 'import json,sys;print(json.load(sys.stdin)["info"]["version"])'
}

converter_version=""
repo="Extend-Robotics/er_build_tools"
force=0

while [ $# -gt 0 ]; do
    case "$1" in
        --repo) repo="$2"; shift 2;;
        --force) force=1; shift;;
        -h|--help) usage;;
        -*) echo "Unknown option: $1" >&2; usage;;
        *)
            if [ -z "${converter_version}" ]; then
                converter_version="$1"
                shift
            else
                echo "Unexpected positional argument: $1" >&2
                usage
            fi
            ;;
    esac
done

for required_command in docker gh tar gzip curl python3; do
    command -v "${required_command}" >/dev/null || {
        echo "ERROR: required command not found in PATH: ${required_command}" >&2
        exit 1
    }
done

if [ -z "${converter_version}" ]; then
    echo "==> No converter version specified; fetching latest from PyPI"
    converter_version="$(fetch_latest_pypi_version)"
    [ -n "${converter_version}" ] || {
        echo "ERROR: failed to fetch latest urdf-usd-converter version from PyPI" >&2
        exit 1
    }
    echo "==> Latest urdf-usd-converter on PyPI: ${converter_version}"
fi

gh auth status >/dev/null 2>&1 || {
    echo "ERROR: gh is not authenticated. Run 'gh auth login' first." >&2
    exit 1
}

script_dir="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
dockerfile_path="${script_dir}/../dockerfiles/urdf_usd_converter.Dockerfile"
dockerfile_context="${script_dir}/../dockerfiles"
wrapper_path="${script_dir}/urdf2usd"

for required_file in "${dockerfile_path}" "${wrapper_path}"; do
    [ -f "${required_file}" ] || {
        echo "ERROR: required file not found: ${required_file}" >&2
        exit 1
    }
done

release_tag="urdf2usd-v${converter_version}"
asset_name="urdf-usd-converter-rootfs-${converter_version}.tar.gz"

if gh release view "${release_tag}" --repo "${repo}" >/dev/null 2>&1; then
    if [ "${force}" -eq 1 ]; then
        echo "==> Deleting existing release ${release_tag}"
        gh release delete "${release_tag}" --repo "${repo}" --yes
    else
        echo "ERROR: release ${release_tag} already exists in ${repo}." >&2
        echo "       Bump the version, or pass --force to overwrite." >&2
        exit 1
    fi
fi

build_dir="$(mktemp -d)"
builder_image="urdf-usd-converter-rootfs-builder:${converter_version}"
builder_container="urdf-usd-converter-rootfs-builder-${converter_version//./-}-$$"

trap 'docker rm -f "${builder_container}" >/dev/null 2>&1 || true; rm -rf "${build_dir}"' EXIT

echo "==> Building ${builder_image} from ${dockerfile_path}"
docker build \
    --build-arg "CONVERTER_VERSION=${converter_version}" \
    -t "${builder_image}" \
    -f "${dockerfile_path}" \
    "${dockerfile_context}"

echo "==> Exporting and compressing rootfs"
docker create --name "${builder_container}" "${builder_image}" >/dev/null
docker export "${builder_container}" | gzip -9 > "${build_dir}/${asset_name}"

asset_size_mb=$(du -m "${build_dir}/${asset_name}" | cut -f1)
echo "==> Tarball: ${asset_name} (${asset_size_mb} MB)"

echo "==> Creating release ${release_tag} in ${repo}"
gh release create "${release_tag}" \
    "${build_dir}/${asset_name}" \
    "${wrapper_path}" \
    --repo "${repo}" \
    --title "urdf2usd rootfs ${converter_version}" \
    --notes "Rootfs containing urdf-usd-converter==${converter_version} built on python:3.12-slim (Debian bookworm, glibc 2.36) for use with the bwrap-based urdf2usd wrapper."

echo
echo "Done. Asset URLs:"
echo "  https://github.com/${repo}/releases/download/${release_tag}/${asset_name}"
echo "  https://github.com/${repo}/releases/download/${release_tag}/urdf2usd"
