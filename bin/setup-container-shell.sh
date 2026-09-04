#!/bin/bash
# setup-container-shell.sh — make the interactive shell inside a cortex image
# usable: install the shared helper functions and repair ROS1 tab completion.
#
# Call once per Dockerfile, AFTER the last apt/rosdep step — a reinstalled
# ros-noetic-rosbash would otherwise revert the completion patch:
#
#   RUN wget -qO- https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/setup-container-shell.sh | bash
#
# Idempotent, and a no-op for the completion patch in images without ROS1.

set -euo pipefail

: "${TARGET_HOME:=/root}"
: "${ROSBASH_SEARCH_ROOT:=/opt/ros}"
: "${HELPER_FUNCTIONS_URL:=https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/.helper_bash_functions}"

# rosbash filters completion candidates against the WHOLE path, so a workspace
# under a dot directory (/cortex/.catkin_ws) excludes every candidate beneath it:
# `roslaunch <pkg> <TAB>` completes the package name, then offers no launch files.
# A basename check keeps the original intent (hide dotfiles) without inspecting
# ancestors. Both variants below match a dot-ancestor; ERE-escaped for sed -E.
readonly PATH_FILTER_DOTTED='! -regex "\.\*/\[\.\]\[\^\./\]\.\*"'
readonly PATH_FILTER_BARE='! -regex "\.\*/\[\.\]\[\^\.\]\*"'
readonly BASENAME_FILTER="-not -name '.*'"
readonly EXPECTED_FILTER_COUNT=13

count_path_filters() { # rosbash_file
  { grep -oE "${PATH_FILTER_DOTTED}|${PATH_FILTER_BARE}" "$1" || true; } | wc -l
}

install_helper_bash_functions() {
  local helper_path="${TARGET_HOME}/.helper_bash_functions"
  local bashrc="${TARGET_HOME}/.bashrc"
  local source_line="source ${helper_path}"
  wget -qO "$helper_path" "$HELPER_FUNCTIONS_URL"
  grep -qxF "$source_line" "$bashrc" 2>/dev/null || echo "$source_line" >> "$bashrc"
  echo "Installed ${helper_path} and sourced it from ${bashrc}"
}

patch_one_rosbash() { # rosbash_file
  local rosbash_file="$1" filter_count
  filter_count="$(count_path_filters "$rosbash_file")"
  if [ "$filter_count" -eq 0 ]; then
    if grep -qF -- "$BASENAME_FILTER" "$rosbash_file"; then
      echo "Already patched: ${rosbash_file}"
      return 0
    fi
    echo "ERROR: ${rosbash_file} has neither the expected path filters nor the" >&2
    echo "       basename replacement; rosbash has changed upstream." >&2
    exit 1
  fi
  if [ "$filter_count" -ne "$EXPECTED_FILTER_COUNT" ]; then
    echo "ERROR: expected ${EXPECTED_FILTER_COUNT} path filters in ${rosbash_file}," >&2
    echo "       found ${filter_count}; rosbash has changed upstream." >&2
    exit 1
  fi
  sed -i -E "s|${PATH_FILTER_DOTTED}|${BASENAME_FILTER}|g; s|${PATH_FILTER_BARE}|${BASENAME_FILTER}|g" \
    "$rosbash_file"
  echo "Patched ${filter_count} path filters in ${rosbash_file}"
}

patch_rosbash_completion() {
  local rosbash_files=()
  if [ -d "$ROSBASH_SEARCH_ROOT" ]; then
    mapfile -t rosbash_files < <(find "$ROSBASH_SEARCH_ROOT" -path '*/share/rosbash/rosbash' -type f)
  fi
  if [ "${#rosbash_files[@]}" -eq 0 ]; then
    echo "No rosbash under ${ROSBASH_SEARCH_ROOT}; skipping completion patch (no ROS1 in this image)"
    return 0
  fi
  local rosbash_file
  for rosbash_file in "${rosbash_files[@]}"; do
    patch_one_rosbash "$rosbash_file"
  done
}

main() {
  install_helper_bash_functions
  patch_rosbash_completion
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi
