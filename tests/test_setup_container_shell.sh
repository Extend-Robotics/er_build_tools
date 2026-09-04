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

# curl stub: the real fetch would need network. Writes whatever `-o` names and
# records the URL it was asked for, so tests can assert on the resolved branch.
fake_bin="${base_tmp}/bin"
mkdir -p "$fake_bin"
cat > "${fake_bin}/curl" <<'STUB'
#!/bin/bash
dest=""
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) dest="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
[ -n "${CURL_URL_LOG:-}" ] && printf '%s\n' "$url" >> "$CURL_URL_LOG"
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

# a machine set up from the old README already sources the tilde form
tilde_home="${base_tmp}/tildehome"
mkdir -p "$tilde_home"
printf 'source ~/.helper_bash_functions\n' > "${tilde_home}/.bashrc"
HOME="$tilde_home" TARGET_HOME="$tilde_home" ROSBASH_SEARCH_ROOT="${base_tmp}/no_ros" \
  bash "$SCRIPT" >/dev/null
assert_eq "tilde form is not duplicated" 1 \
  "$(count_occurrences "${tilde_home}/.bashrc" ".helper_bash_functions")"
assert_eq "tilde form is left as written" 1 \
  "$(count_occurrences "${tilde_home}/.bashrc" "source ~/.helper_bash_functions")"

# ... but only when TARGET_HOME really is the running user's home
other_home="${base_tmp}/otherhome"
mkdir -p "$other_home"
printf 'source ~/.helper_bash_functions\n' > "${other_home}/.bashrc"
HOME="$tilde_home" TARGET_HOME="$other_home" ROSBASH_SEARCH_ROOT="${base_tmp}/no_ros" \
  bash "$SCRIPT" >/dev/null
assert_eq "unrelated tilde line is not trusted" 2 \
  "$(count_occurrences "${other_home}/.bashrc" ".helper_bash_functions")"

# --- invocation modes ---
# `curl ... | bash` is how the Dockerfiles call it, and piping leaves BASH_SOURCE
# unset, which set -u turns fatal unless the run-unless-sourced guard defaults it.
piped_out="$(TARGET_HOME="$home_dir" ROSBASH_SEARCH_ROOT="${base_tmp}/no_ros" \
  bash < "$SCRIPT" 2>&1)"
assert_contains "piped into bash runs main" "$piped_out" "Installed"
assert_contains "piped into bash has no unbound variable" "$piped_out" "skipping completion patch"

sourced_out="$(bash -c "source '$SCRIPT'" 2>&1)"
assert_eq "sourcing produces no output and no side effects" "" "$sourced_out"

# --- helper URL follows ER_BUILD_TOOLS_BRANCH ---
export CURL_URL_LOG="${base_tmp}/urls.txt"
: > "$CURL_URL_LOG"
TARGET_HOME="$home_dir" ROSBASH_SEARCH_ROOT="${base_tmp}/no_ros" bash "$SCRIPT" >/dev/null
assert_contains "defaults to the main branch" "$(cat "$CURL_URL_LOG")" "/refs/heads/main/.helper_bash_functions"

: > "$CURL_URL_LOG"
TARGET_HOME="$home_dir" ROSBASH_SEARCH_ROOT="${base_tmp}/no_ros" \
  ER_BUILD_TOOLS_BRANCH="a-feature-branch" bash "$SCRIPT" >/dev/null
assert_contains "branch override reaches the URL" "$(cat "$CURL_URL_LOG")" \
  "/refs/heads/a-feature-branch/.helper_bash_functions"

# an explicit URL still wins over the branch
: > "$CURL_URL_LOG"
TARGET_HOME="$home_dir" ROSBASH_SEARCH_ROOT="${base_tmp}/no_ros" \
  ER_BUILD_TOOLS_BRANCH="ignored" HELPER_FUNCTIONS_URL="https://example.invalid/helpers" \
  bash "$SCRIPT" >/dev/null
assert_eq "explicit HELPER_FUNCTIONS_URL wins" "https://example.invalid/helpers" \
  "$(cat "$CURL_URL_LOG")"
unset CURL_URL_LOG

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

# --- elevation is scoped to the rewrite ---
# require_sudo only needs builtins, so emptying PATH inside the shell isolates
# the "no sudo" case (emptying it for the bash invocation would lose bash too).
no_sudo_out="$(bash -c "PATH=''; source '$SCRIPT'; require_sudo /opt/ros/x" 2>&1)"
assert_eq "no sudo exits 1" 1 "$?"
assert_contains "no sudo says why" "$no_sudo_out" "sudo is not installed"

writable_probe="${base_tmp}/writable/rosbash"
mkdir -p "$(dirname "$writable_probe")"
: > "$writable_probe"
bash -c "source '$SCRIPT'; rosbash_is_writable '$writable_probe'"
assert_eq "writable rosbash needs no elevation" 0 "$?"

bash -c "source '$SCRIPT'; rosbash_is_writable '${base_tmp}/absent/rosbash'"
assert_eq "unreachable rosbash is not writable" 1 "$?"

# root ignores file permissions, so the read-only case can only be exercised
# unprivileged - which is how CI runs.
if [ "$(id -u)" -ne 0 ]; then
  ro_file="${base_tmp}/readonly/rosbash"
  mkdir -p "$(dirname "$ro_file")"
  make_rosbash "$ro_file"
  chmod 555 "$(dirname "$ro_file")"
  bash -c "source '$SCRIPT'; rosbash_is_writable '$ro_file'"
  assert_eq "root-owned rosbash needs elevation" 1 "$?"
  chmod 755 "$(dirname "$ro_file")"
else
  printf '  (root: skipping the read-only rosbash assertion)\n'
fi

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
