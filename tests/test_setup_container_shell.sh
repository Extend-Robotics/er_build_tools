#!/bin/bash
# SKIP_CHECK — keep this. check_bash.yml `source`s every bash file to syntax-check
# it (skipping those marked SKIP_CHECK). This test file has no BASH_SOURCE guard,
# so sourcing it would run the whole suite at lint time; CI runs it explicitly via
# check_setup_container_shell.yml instead.
# Self-contained tests for bin/setup-container-shell.sh. No bats dependency, no
# ROS install: rosbash fixtures are synthesised and curl is stubbed on PATH.

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${here}/../bin/setup-container-shell.sh"

fail_count=0
pass_count=0

assert_eq() { # name expected actual
  if [ "$2" = "$3" ]; then
    pass_count=$((pass_count + 1))
  else
    fail_count=$((fail_count + 1))
    printf 'FAIL: %s\n  expected: [%s]\n  actual:   [%s]\n' "$1" "$2" "$3" >&2
  fi
}

assert_contains() { # name haystack needle
  case "$2" in
    *"$3"*) pass_count=$((pass_count + 1)) ;;
    *) fail_count=$((fail_count + 1))
       printf 'FAIL: %s\n  expected to contain: [%s]\n  in: [%s]\n' "$1" "$3" "$2" >&2 ;;
  esac
}

base_tmp="$(mktemp -d)"
trap 'status=$?; rm -rf "$base_tmp"; exit $status' EXIT

# curl stub: the real fetch would need network. Writes whatever `-o` names.
fake_bin="${base_tmp}/bin"
mkdir -p "$fake_bin"
cat > "${fake_bin}/curl" <<'STUB'
#!/bin/bash
dest=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) dest="$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf 'FAKE_HELPER_FUNCTIONS\n' > "$dest"
STUB
chmod +x "${fake_bin}/curl"
export PATH="${fake_bin}:${PATH}"

# A rosbash fixture carries the same 13 path filters as the real file: 9 of the
# dotted variant (5 alone, 4 alongside the bare variant used for plain files).
make_rosbash() { # dest
  local dest="$1" i
  mkdir -p "$(dirname "$dest")"
  echo '#!/usr/bin/env bash' > "$dest"
  for i in 1 2 3 4 5; do
    echo "  opts=\$(find -L \$path -type d ! -regex \".*/[.][^./].*\" -print0) # ${i}" >> "$dest"
  done
  for i in 6 7 8 9; do
    echo "  opts=\$(find -L \$path -type d ! -regex \".*/[.][^./].*\" -print0)\$(find -L \$path -type f ! -regex \".*/[.][^.]*\" -print0) # ${i}" >> "$dest"
  done
}

count_occurrences() { # file pattern
  { grep -oF -- "$2" "$1" || true; } | wc -l
}

# --- helper bash functions install ---
home_dir="${base_tmp}/home"
mkdir -p "$home_dir"
out="$(TARGET_HOME="$home_dir" ROSBASH_SEARCH_ROOT="${base_tmp}/no_ros" bash "$SCRIPT")"
assert_eq "helper functions fetched" "FAKE_HELPER_FUNCTIONS" "$(cat "${home_dir}/.helper_bash_functions")"
assert_eq "bashrc sources helpers once" 1 \
  "$(count_occurrences "${home_dir}/.bashrc" "source ${home_dir}/.helper_bash_functions")"
assert_contains "no-ROS1 image skips patch" "$out" "skipping completion patch"

TARGET_HOME="$home_dir" ROSBASH_SEARCH_ROOT="${base_tmp}/no_ros" bash "$SCRIPT" >/dev/null
assert_eq "bashrc source line not duplicated on rerun" 1 \
  "$(count_occurrences "${home_dir}/.bashrc" "source ${home_dir}/.helper_bash_functions")"

# --- rosbash patching ---
ros_root="${base_tmp}/opt/ros"
rosbash_file="${ros_root}/noetic/share/rosbash/rosbash"
make_rosbash "$rosbash_file"
out="$(TARGET_HOME="$home_dir" ROSBASH_SEARCH_ROOT="$ros_root" bash "$SCRIPT")"
assert_contains "reports 13 filters patched" "$out" "Patched 13 path filters"
assert_eq "no dotted full-path filters remain" 0 \
  "$(count_occurrences "$rosbash_file" '! -regex ".*/[.][^./].*"')"
assert_eq "no bare full-path filters remain" 0 \
  "$(count_occurrences "$rosbash_file" '! -regex ".*/[.][^.]*"')"
assert_eq "basename filter applied 13 times" 13 \
  "$(count_occurrences "$rosbash_file" "-not -name '.*'")"

before="$(cat "$rosbash_file")"
out="$(TARGET_HOME="$home_dir" ROSBASH_SEARCH_ROOT="$ros_root" bash "$SCRIPT")"
assert_contains "rerun reports already patched" "$out" "Already patched"
assert_eq "rerun leaves file unchanged" "$before" "$(cat "$rosbash_file")"

# --- every rosbash under the search root is patched ---
multi_root="${base_tmp}/multi/opt/ros"
make_rosbash "${multi_root}/noetic/share/rosbash/rosbash"
make_rosbash "${multi_root}/melodic/share/rosbash/rosbash"
TARGET_HOME="$home_dir" ROSBASH_SEARCH_ROOT="$multi_root" bash "$SCRIPT" >/dev/null
assert_eq "second distro patched too" 13 \
  "$(count_occurrences "${multi_root}/melodic/share/rosbash/rosbash" "-not -name '.*'")"

# --- fail fast on an unexpected rosbash ---
short_root="${base_tmp}/short/opt/ros"
short_file="${short_root}/noetic/share/rosbash/rosbash"
make_rosbash "$short_file"
sed -i '2d' "$short_file"
TARGET_HOME="$home_dir" ROSBASH_SEARCH_ROOT="$short_root" bash "$SCRIPT" >/dev/null 2>&1
assert_eq "wrong filter count exits 1" 1 "$?"

alien_root="${base_tmp}/alien/opt/ros"
alien_file="${alien_root}/noetic/share/rosbash/rosbash"
mkdir -p "$(dirname "$alien_file")"
echo 'nothing recognisable here' > "$alien_file"
TARGET_HOME="$home_dir" ROSBASH_SEARCH_ROOT="$alien_root" bash "$SCRIPT" >/dev/null 2>&1
assert_eq "unrecognisable rosbash exits 1" 1 "$?"

printf '\n%d passed, %d failed\n' "$pass_count" "$fail_count"
[ "$fail_count" -eq 0 ]
