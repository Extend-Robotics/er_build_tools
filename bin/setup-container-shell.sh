#!/bin/bash
# setup-container-shell.sh — make the interactive shell inside a cortex image
# usable: install the shared helper functions and repair rosbash for a workspace
# under a dot directory.
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

# rosbash's find filters match hidden paths against the WHOLE path, so a
# workspace under a dot directory (/cortex/.catkin_ws) hides every file beneath
# it; see docs/setup-container-shell.md. Patterns are ERE-escaped for sed -E and
# written against ros-noetic-rosbash 1.15.10:
# https://github.com/ros/ros/blob/1.15.10/tools/rosbash/rosbash
readonly COMPLETION_PATH_FILTERS='! -regex "\.\*/\[\.\]\[\^\./\]\.\*"|! -regex "\.\*/\[\.\]\[\^\.\]\*"'
readonly BASENAME_FILTER="-not -name '.*'"
readonly EXPECTED_COMPLETION_FILTER_COUNT=13
readonly ROSCMD_PATH_FILTER='(-name [$]2 -type f) ! -regex \.\*/\[\.\]\.\* (! -regex \.\*[$]pkgdir\\/build\\/\.\*) \| uniq'
readonly HIDDEN_DIRECTORY_PRUNE="-name '.*' -prune -o"

count_matches() { # rosbash_file grep_mode pattern
  { grep -o "$2" -- "$3" "$1" || [ $? -eq 1 ]; } | wc -l
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
  local script="s#${COMPLETION_PATH_FILTERS}#${BASENAME_FILTER}#g"
  script="${script}; s#${ROSCMD_PATH_FILTER}#${HIDDEN_DIRECTORY_PRUNE}"' \1 \2 -print | uniq#'
  if rosbash_is_writable "$rosbash_file"; then
    sed -i -E "$script" "$rosbash_file"
    return 0
  fi
  require_sudo "$rosbash_file"
  sudo sed -i -E "$script" "$rosbash_file"
}

patch_one_rosbash() { # rosbash_file
  local rosbash_file="$1"
  local unpatched_completion_count patched_completion_count unpatched_roscmd_count patched_roscmd_count
  unpatched_completion_count="$(count_matches "$rosbash_file" -E "$COMPLETION_PATH_FILTERS")"
  patched_completion_count="$(count_matches "$rosbash_file" -F "$BASENAME_FILTER")"
  unpatched_roscmd_count="$(count_matches "$rosbash_file" -E "$ROSCMD_PATH_FILTER")"
  patched_roscmd_count="$(count_matches "$rosbash_file" -F "$HIDDEN_DIRECTORY_PRUNE")"
  if [ $((unpatched_completion_count + patched_completion_count)) -ne "$EXPECTED_COMPLETION_FILTER_COUNT" ] \
    || [ $((unpatched_roscmd_count + patched_roscmd_count)) -ne 1 ]; then
    echo "ERROR: unrecognised rosbash: ${rosbash_file}" >&2
    echo "       completion filters: ${unpatched_completion_count} unpatched + ${patched_completion_count} patched, expected ${EXPECTED_COMPLETION_FILTER_COUNT}" >&2
    echo "       _roscmd filter: ${unpatched_roscmd_count} unpatched + ${patched_roscmd_count} patched, expected 1" >&2
    echo "       rosbash has changed upstream or was edited; reinstall ros-<distro>-rosbash and re-run." >&2
    exit 1
  fi
  local unpatched_count=$((unpatched_completion_count + unpatched_roscmd_count))
  if [ "$unpatched_count" -eq 0 ]; then
    echo "Already patched: ${rosbash_file}"
    return 0
  fi
  rosbash_sed "$rosbash_file"
  echo "Patched ${rosbash_file}; path filters rewritten: ${unpatched_count}"
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
