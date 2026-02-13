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
    elif [ "${arg}" = "--scripts-branch" ]; then
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
mkdir -p "${SCRIPT_DIR}"

RAW_URL="https://raw.githubusercontent.com/Extend-Robotics/er_build_tools_internal/refs/heads/${scripts_branch}"

echo "Fetching scripts from er_build_tools_internal (branch: ${scripts_branch})..."
curl -sfH "Authorization: token ${gh_token}" "${RAW_URL}/bin/reproduce_ci.sh" -o "${SCRIPT_DIR}/reproduce_ci.sh"
curl -sfH "Authorization: token ${gh_token}" "${RAW_URL}/bin/ci_workspace_setup.sh" -o "${SCRIPT_DIR}/ci_workspace_setup.sh"

chmod +x "${SCRIPT_DIR}/reproduce_ci.sh"
"${SCRIPT_DIR}/reproduce_ci.sh" "$@"
