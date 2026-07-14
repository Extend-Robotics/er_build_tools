#!/bin/bash
# SKIP_CHECK — keep this. check_bash.yml `source`s every bash file to syntax-check
# it (skipping those marked SKIP_CHECK); sourcing this would run the suite.
# Self-contained tests for bin/update-source-repos.sh + bin/update_source_repos.py.
# No docker, no network: docker/curl are PATH shims; the payload is exercised
# against real local git repos via the ER_CATKIN_WS override.

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-${here}/..}"
WRAPPER="${REPO}/bin/update-source-repos.sh"
PAYLOAD="${REPO}/bin/update_source_repos.py"

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

assert_not_contains() { # name haystack needle
  case "$2" in
    *"$3"*) fail_count=$((fail_count + 1))
            printf 'FAIL: %s\n  expected NOT to contain: [%s]\n' "$1" "$3" >&2 ;;
    *) pass_count=$((pass_count + 1)) ;;
  esac
}

base_tmp="$(mktemp -d)"
trap 'status=$?; rm -rf "$base_tmp"; exit $status' EXIT

# Deterministic git identity/branches for all fixture repos
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test \
       GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test
export NO_COLOR=1

# ---------- payload fixture helpers ----------
# new_workspace <name> -> sets $ws, $src, $remotes
new_workspace() {
  ws="$base_tmp/$1"
  src="$ws/src"
  remotes="$base_tmp/$1-remotes"
  mkdir -p "$src" "$remotes"
}

# new_repo <name> [branch] -> bare remote at $remotes/<name>.git, clone at $src/<name>
new_repo() {
  local name="$1" branch="${2:-main}"
  git init -q --bare -b "$branch" "$remotes/$name.git"
  git clone -q "$remotes/$name.git" "$src/$name" 2>/dev/null
  git -C "$src/$name" commit -q --allow-empty -m c1
  git -C "$src/$name" push -q origin "$branch"
}

# advance_remote <name> <branch> -> add a commit to the remote (via a throwaway clone)
advance_remote() {
  local name="$1" branch="$2" tmp
  tmp="$(mktemp -d "$base_tmp/adv.XXXXXX")"
  git clone -q -b "$branch" "$remotes/$name.git" "$tmp/c" 2>/dev/null
  git -C "$tmp/c" commit -q --allow-empty -m "advance-$RANDOM"
  git -C "$tmp/c" push -q origin "$branch"
  rm -rf "$tmp"
}

run_payload() { # [stdin-file] ; uses $ws; sets $out and $rc
  local stdin_src="${1:-/dev/null}"
  out="$(ER_CATKIN_WS="$ws" python3 "$PAYLOAD" < "$stdin_src" 2>&1)"
  rc=$?
}

# ---------- payload: workspace gate ----------
new_workspace gate_stripped
rm -rf "$src"
mkdir -p "$ws/install"
run_payload
assert_eq "stripped image: exit 1" 1 "$rc"
assert_contains "stripped image: message" "$out" "this is not a source code container, the util will not work here"

new_workspace gate_weird
rm -rf "$src"
run_payload
assert_eq "unexpected layout: exit 1" 1 "$rc"
assert_contains "unexpected layout: message" "$out" "unexpected workspace layout"

new_workspace gate_empty_src
run_payload
assert_eq "empty src: exit 10" 10 "$rc"
assert_contains "empty src: message" "$out" "no git repositories found"

# ---------- payload: discovery finds nested repos, doesn't descend into them ----------
new_workspace disco
new_repo top
mkdir -p "$src/group"
git init -q --bare -b main "$remotes/nested.git"
git clone -q "$remotes/nested.git" "$src/group/nested" 2>/dev/null
git -C "$src/group/nested" commit -q --allow-empty -m c1
git -C "$src/group/nested" push -q origin main
run_payload
assert_contains "discovery: finds top-level repo" "$out" "=== top ==="
assert_contains "discovery: finds nested repo" "$out" "=== group/nested ==="

printf '\n%d passed, %d failed\n' "$pass_count" "$fail_count"
[ "$fail_count" -eq 0 ]
