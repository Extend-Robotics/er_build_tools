#!/bin/bash
# setup-container-shell.sh — make the interactive shell inside a cortex image
# usable: install the shared helper functions and repair ROS1 tab completion and
# roscat/rosed/roscp for a workspace under a dot directory.
#
# Call once per Dockerfile, AFTER the last apt/rosdep step — a reinstalled
# ros-noetic-rosbash would otherwise revert the rosbash patch:
#
#   RUN curl -fsSL https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/setup-container-shell.sh | bash
#
# Idempotent, and a no-op for the rosbash patch in images without ROS1.

set -euo pipefail

: "${TARGET_HOME:=${HOME:-/root}}"
: "${ROSBASH_SEARCH_ROOT:=/opt/ros}"
: "${ER_BUILD_TOOLS_BRANCH:=main}"
: "${HELPER_FUNCTIONS_URL:=https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/${ER_BUILD_TOOLS_BRANCH}/.helper_bash_functions}"

# rosbash filters files against the WHOLE path, so a workspace under a dot
# directory (/cortex/.catkin_ws) excludes every file beneath it:
# `roslaunch <pkg> <TAB>` completes the package name, then offers no launch files,
# and `roscat <pkg> <file>` reports "That file does not exist in that package."
# A basename check keeps the original intent (hide dotfiles) without inspecting
# ancestors. All three variants below match a dot-ancestor: two in completion,
# one in _roscmd (behind roscat, rosed and roscp). ERE-escaped for sed -E.
readonly PATH_FILTER_DOTTED='! -regex "\.\*/\[\.\]\[\^\./\]\.\*"'
readonly PATH_FILTER_BARE='! -regex "\.\*/\[\.\]\[\^\.\]\*"'
readonly PATH_FILTER_ROSCMD='! -regex \.\*/\[\.\]\.\*'
readonly BASENAME_FILTER="-not -name '.*'"
readonly EXPECTED_FILTER_COUNT=14

count_path_filters() { # rosbash_file
  { grep -oE "${PATH_FILTER_DOTTED}|${PATH_FILTER_BARE}|${PATH_FILTER_ROSCMD}" "$1" || true; } | wc -l
}

count_basename_filters() { # rosbash_file
  { grep -oF -- "$BASENAME_FILTER" "$1" || true; } | wc -l
}

# Machines set up from the README carry the tilde form of this line. It names
# the same file whenever TARGET_HOME is the running user's home, so treat it as
# already present rather than appending a second, absolute-path duplicate.
bashrc_already_sources() { # bashrc source_line
  local bashrc="$1" source_line="$2"
  grep -qxF -- "$source_line" "$bashrc" 2>/dev/null && return 0
  [ "$TARGET_HOME" = "${HOME:-}" ] || return 1
  grep -qxF -- "source ~/.helper_bash_functions" "$bashrc" 2>/dev/null
}

install_helper_bash_functions() {
  local helper_path="${TARGET_HOME}/.helper_bash_functions"
  local bashrc="${TARGET_HOME}/.bashrc"
  local source_line="source ${helper_path}"
  curl -fsSL -o "$helper_path" "$HELPER_FUNCTIONS_URL"
  bashrc_already_sources "$bashrc" "$source_line" || echo "$source_line" >> "$bashrc"
  echo "Installed ${helper_path} and sourced it from ${bashrc}"
}

# `sed -i` writes a temp file alongside the target, so the directory has to be
# writable too. Root in a container always is; an apt-installed ROS on a dev
# host is not, and there only the rewrite is elevated - never the whole script,
# which would leave a root-owned .helper_bash_functions in the user's home.
rosbash_is_writable() { # rosbash_file
  [ -w "$1" ] && [ -w "$(dirname "$1")" ]
}

require_sudo() { # rosbash_file
  local rosbash_file="$1"
  if ! command -v sudo >/dev/null 2>&1; then
    echo "ERROR: ${rosbash_file} needs root to patch, and sudo is not installed." >&2
    exit 1
  fi
  if sudo -n true 2>/dev/null; then
    return 0
  fi
  # No probe for whether sudo can prompt: /dev/tty is readable over ssh without
  # a pty, so it says nothing useful. Announce the prompt and let sudo decide -
  # it fails immediately, and audibly, where it cannot ask.
  echo "${rosbash_file} is owned by root; sudo is needed for the rewrite only."
}

rosbash_sed() { # rosbash_file
  local rosbash_file="$1"
  local script="s|${PATH_FILTER_DOTTED}|${BASENAME_FILTER}|g"
  script="${script}; s|${PATH_FILTER_BARE}|${BASENAME_FILTER}|g"
  script="${script}; s|${PATH_FILTER_ROSCMD}|${BASENAME_FILTER}|g"
  if rosbash_is_writable "$rosbash_file"; then
    sed -i -E "$script" "$rosbash_file"
    return 0
  fi
  require_sudo "$rosbash_file"
  sudo sed -i -E "$script" "$rosbash_file"
}

# Each filter is either still a path filter or already its basename replacement,
# so the two counts sum to the expected total in every legitimate state -
# including a host patched before _roscmd was covered, which carries 1 + 13.
patch_one_rosbash() { # rosbash_file
  local rosbash_file="$1" path_filter_count basename_filter_count
  path_filter_count="$(count_path_filters "$rosbash_file")"
  basename_filter_count="$(count_basename_filters "$rosbash_file")"
  if [ $((path_filter_count + basename_filter_count)) -ne "$EXPECTED_FILTER_COUNT" ]; then
    echo "ERROR: expected ${EXPECTED_FILTER_COUNT} path filters or basename replacements in" >&2
    echo "       ${rosbash_file}, found ${path_filter_count} + ${basename_filter_count};" >&2
    echo "       rosbash has changed upstream." >&2
    exit 1
  fi
  if [ "$path_filter_count" -eq 0 ]; then
    echo "Already patched: ${rosbash_file}"
    return 0
  fi
  rosbash_sed "$rosbash_file"
  echo "Patched ${path_filter_count} path filters in ${rosbash_file}"
}

patch_rosbash_path_filters() {
  local rosbash_files=()
  if [ -d "$ROSBASH_SEARCH_ROOT" ]; then
    mapfile -t rosbash_files < <(find "$ROSBASH_SEARCH_ROOT" -path '*/share/rosbash/rosbash' -type f)
  fi
  if [ "${#rosbash_files[@]}" -eq 0 ]; then
    echo "No rosbash under ${ROSBASH_SEARCH_ROOT}; skipping rosbash patch (no ROS1 in this image)"
    return 0
  fi
  local rosbash_file
  for rosbash_file in "${rosbash_files[@]}"; do
    patch_one_rosbash "$rosbash_file"
  done
}

main() {
  install_helper_bash_functions
  patch_rosbash_path_filters
}

# Run unless sourced. Piped in (`curl ... | bash`) BASH_SOURCE is empty, which
# set -u would make fatal, so default it to $0 - that is the executed case too.
if [ "${BASH_SOURCE[0]:-$0}" = "${0}" ]; then
  main "$@"
fi
