#!/bin/bash
# Setup script for ci_tool and helper bash functions.
#
# Run with:
#   bash <(curl -fsSL https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/setup.sh)

set -euo pipefail

Red='\033[0;31m'
Green='\033[0;32m'
Yellow='\033[0;33m'
Cyan='\033[0;36m'
Bold='\033[1m'
Color_Off='\033[0m'

BRANCH="main"
BASE_URL="https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/${BRANCH}"
HELPER_URL="${BASE_URL}/.helper_bash_functions"
MERGE_VARS_URL="${BASE_URL}/bin/merge_helper_vars.py"
HELPER_PATH="${HOME}/.helper_bash_functions"

echo -e "${Bold}${Cyan}"
echo "╔══════════════════════════════════════════════╗"
echo "║       ci_tool Setup — Extend Robotics        ║"
echo "╚══════════════════════════════════════════════╝"
echo -e "${Color_Off}"

# --- Step 1: Install/update .helper_bash_functions ---

echo -e "${Bold}[1/4] Installing helper bash functions...${Color_Off}"
if [ -f "${HELPER_PATH}" ]; then
    echo -e "${Yellow}  Existing ~/.helper_bash_functions found — updating while preserving your variables...${Color_Off}"
    tmp_new=$(mktemp)
    curl -fsSL "${HELPER_URL}" -o "${tmp_new}"
    merge_script=$(curl -fsSL "${MERGE_VARS_URL}")
    python3 <(echo "${merge_script}") "${HELPER_PATH}" "${tmp_new}"
    cp "${tmp_new}" "${HELPER_PATH}"
    rm -f "${tmp_new}"
    echo -e "${Green}  Updated ~/.helper_bash_functions (custom variables preserved).${Color_Off}"
else
    curl -fsSL "${HELPER_URL}" -o "${HELPER_PATH}"
    echo -e "${Green}  Installed ~/.helper_bash_functions${Color_Off}"
fi

# --- Step 2: GitHub token ---

echo ""
echo -e "${Bold}[2/4] GitHub token${Color_Off}"
echo -e "  ci_tool needs a GitHub token with ${Bold}repo${Color_Off} scope to access private repos."
echo -e "  Create one at: ${Cyan}https://github.com/settings/tokens${Color_Off}"
echo ""

current_token=""
if [ -n "${GH_TOKEN:-}" ]; then
    current_token="${GH_TOKEN}"
    echo -e "  ${Green}GH_TOKEN is already set in your environment.${Color_Off}"
    echo -n "  Keep current token? [Y/n] "
    read -r keep_token
    if [[ "${keep_token}" =~ ^[nN] ]]; then
        current_token=""
    fi
fi

if [ -z "${current_token}" ]; then
    echo -n "  Enter your GitHub token (ghp_...): "
    read -r current_token
    if [ -z "${current_token}" ]; then
        echo -e "  ${Yellow}Skipped. Set it later by editing ~/.helper_bash_functions${Color_Off}"
    fi
fi

if [ -n "${current_token}" ]; then
    merge_script=$(curl -fsSL "${MERGE_VARS_URL}")
    python3 <(echo "${merge_script}") --set --export "GH_TOKEN=\"${current_token}\"" "${HELPER_PATH}"
fi

# --- Step 3: Shell integration ---

echo ""
echo -e "${Bold}[3/4] Shell integration${Color_Off}"
BASHRC="${HOME}/.bashrc"
if [ -f "${BASHRC}" ] && grep -q 'source ~/.helper_bash_functions' "${BASHRC}"; then
    echo -e "  ${Green}Already sourced in ~/.bashrc${Color_Off}"
else
    echo 'source ~/.helper_bash_functions' >> "${BASHRC}"
    echo -e "  ${Green}Added 'source ~/.helper_bash_functions' to ~/.bashrc${Color_Off}"
fi

# --- Step 4: Claude Code authentication ---

echo ""
echo -e "${Bold}[4/4] Claude Code authentication${Color_Off}"
echo -e "  ci_tool uses Claude Code to autonomously fix CI failures."
echo -e "  Claude must be installed and authenticated on your host machine."
echo ""

if command -v claude &> /dev/null; then
    echo -e "  ${Green}Claude Code is installed.${Color_Off}"
    if claude -p "say ok" --max-turns 1 &> /dev/null; then
        echo -e "  ${Green}Claude authentication is working.${Color_Off}"
    else
        echo -e "  ${Yellow}Claude is installed but not authenticated.${Color_Off}"
        echo -e "  Run ${Bold}claude${Color_Off} in another terminal to authenticate, then press Enter."
        echo -n "  Press Enter when done (or 's' to skip): "
        read -r auth_response
        if [[ ! "${auth_response}" =~ ^[sS] ]]; then
            if claude -p "say ok" --max-turns 1 &> /dev/null; then
                echo -e "  ${Green}Claude authentication verified!${Color_Off}"
            else
                echo -e "  ${Yellow}Still not working — you can fix this later.${Color_Off}"
            fi
        fi
    fi
else
    echo -e "  ${Yellow}Claude Code is not installed.${Color_Off}"
    echo -e "  Install with: ${Bold}npm install -g @anthropic-ai/claude-code${Color_Off}"
    echo -e "  Then run ${Bold}claude${Color_Off} to authenticate."
fi

# --- Done ---

echo ""
echo -e "${Bold}${Green}Setup complete!${Color_Off}"
echo ""
echo -e "  Reload your shell or run:"
echo -e "    ${Bold}source ~/.helper_bash_functions${Color_Off}"
echo ""
echo -e "  Then start ci_tool:"
echo -e "    ${Bold}ci_tool${Color_Off}          Interactive menu"
echo -e "    ${Bold}ci_fix${Color_Off}           Fix CI failures with Claude"
echo -e "    ${Bold}ci_tool reproduce${Color_Off} Reproduce CI locally"
echo ""
