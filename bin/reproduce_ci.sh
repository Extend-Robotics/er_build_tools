#!/bin/bash
set -euo pipefail

# Public wrapper: fetches the real CI reproduction scripts from er_build_tools_internal (private)
# and runs them. This script is the entry point for remote execution via:
#   bash <(curl -Ls https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/reproduce_ci.sh) \
#     --gh-token ghp_xxx --repo https://github.com/extend-robotics/er_interface

gh_token=""
scripts_branch="main"
for i in $(seq 1 $#); do
    arg="${!i}"
    if [ "${arg}" = "--gh-token" ] || [ "${arg}" = "-t" ]; then
        next=$((i + 1))
        if [ "${next}" -gt "$#" ]; then
            echo "Error: ${arg} requires a value"
            exit 1
        fi
        gh_token="${!next}"
    elif [ "${arg}" = "--scripts-branch" ] || [ "${arg}" = "--scripts_branch" ]; then
        next=$((i + 1))
        if [ "${next}" -gt "$#" ]; then
            echo "Error: ${arg} requires a value"
            exit 1
        fi
        scripts_branch="${!next}"
    fi
done

if [ -z "${gh_token}" ]; then
    echo "Error: --gh-token is required to fetch scripts from er_build_tools_internal"
    echo "Usage: bash <(curl -Ls ...) --gh-token <token> --repo <repo_url> [options]"
    exit 1
fi

SCRIPT_DIR="/tmp/er_reproduce_ci"
echo "Creating script directory: ${SCRIPT_DIR}"
mkdir -p "${SCRIPT_DIR}"

RAW_URL="https://raw.githubusercontent.com/Extend-Robotics/er_build_tools_internal/refs/heads/${scripts_branch}"

fetch_script() {
    local script_name="$1"
    local source_url="${RAW_URL}/bin/${script_name}"
    local destination="${SCRIPT_DIR}/${script_name}"

    echo "Fetching ${script_name}:"
    echo "  From: ${source_url}"
    echo "  To:   ${destination}"

    local http_code
    http_code=$(curl -fL -w "%{http_code}" -H "Authorization: token ${gh_token}" "${source_url}" -o "${destination}" 2>/dev/null) || {
        echo "  FAILED (HTTP ${http_code})"
        echo ""
        echo "Error: Failed to fetch ${script_name}"
        echo "  Check that the branch '${scripts_branch}' exists in er_build_tools_internal"
        echo "  Check that your --gh-token has access to Extend-Robotics/er_build_tools_internal"
        exit 1
    }
    echo "  OK (HTTP ${http_code})"

    if [ ! -s "${destination}" ]; then
        echo "Error: Downloaded file is empty: ${destination}"
        exit 1
    fi
}

echo "Fetching scripts from er_build_tools_internal (branch: ${scripts_branch})..."
fetch_script "reproduce_ci.sh"
fetch_script "ci_workspace_setup.sh"

chmod +x "${SCRIPT_DIR}/reproduce_ci.sh"
echo ""
echo "Running ${SCRIPT_DIR}/reproduce_ci.sh..."
"${SCRIPT_DIR}/reproduce_ci.sh" "$@"
