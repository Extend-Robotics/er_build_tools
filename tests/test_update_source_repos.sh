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

# ---------- payload: on-branch behaviours ----------
new_workspace onbranch
new_repo behind          # will be behind origin -> updated
new_repo current         # already up to date
new_repo dirty           # local tracked modification -> skipped
new_repo diverged        # local commit + remote commit -> skipped
new_repo orphan          # branch with no matching remote branch -> skipped
advance_remote behind main
echo tracked > "$src/dirty/file.txt"
git -C "$src/dirty" add file.txt
git -C "$src/dirty" commit -qm add-file
git -C "$src/dirty" push -q origin main
echo changed > "$src/dirty/file.txt"   # unstaged tracked change
git -C "$src/diverged" commit -q --allow-empty -m local-only
advance_remote diverged main
git -C "$src/orphan" checkout -q -b lonely_branch
run_payload
assert_eq "on-branch mix: exit 0 (something updated)" 0 "$rc"
assert_contains "behind: updated" "$out" "updated     behind"
assert_contains "current: up to date" "$out" "up-to-date  current"
assert_contains "dirty: skipped" "$out" "skipped     dirty"
assert_contains "dirty: reason" "$out" "uncommitted local changes"
assert_contains "diverged: skipped" "$out" "skipped     diverged"
assert_contains "diverged: reason" "$out" "diverged"
assert_contains "orphan: skipped" "$out" "no origin/lonely_branch"
old_dirty_content="$(cat "$src/dirty/file.txt")"
assert_eq "dirty: file untouched" "changed" "$old_dirty_content"

# untracked files must NOT block updates
new_workspace untracked
new_repo repo
advance_remote repo main
echo scratch > "$src/repo/scratch.log"
run_payload
assert_eq "untracked: exit 0" 0 "$rc"
assert_contains "untracked: updated" "$out" "updated     repo"

# all repos already current -> exit 10
new_workspace nochange
new_repo repo
run_payload
assert_eq "nothing to do: exit 10" 10 "$rc"

# missing upstream config: branch exists on remote but no @{u} -> still ffs
new_workspace noupstream
new_repo repo
advance_remote repo main
git -C "$src/repo" branch -q --unset-upstream
run_payload
assert_eq "no upstream: exit 0" 0 "$rc"
assert_contains "no upstream: updated" "$out" "updated     repo"

# ---------- payload: submodules ----------
new_workspace subm
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=protocol.file.allow GIT_CONFIG_VALUE_0=always
new_repo sub
new_repo super
git -C "$src/super" submodule add -q "$remotes/sub.git" thesub
git -C "$src/super" commit -qm add-submodule
git -C "$src/super" push -q origin main
git -C "$src/super" submodule deinit -f -q thesub   # simulate uninitialised submodule
advance_remote super main
run_payload
assert_eq "submodule: exit 0" 0 "$rc"
assert_contains "submodule: super updated" "$out" "updated     super"
sub_head="$(git -C "$src/super/thesub" rev-parse HEAD 2>/dev/null)"
recorded="$(git -C "$src/super" ls-tree HEAD thesub | awk '{print $3}')"
assert_eq "submodule: checked out at recorded commit" "$recorded" "$sub_head"
unset GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0

# ---------- payload: detached HEAD ----------
# (a) exactly one branch contains the commit -> auto-resolve, checkout, pull
new_workspace det_auto
new_repo repo
advance_remote repo main
git -C "$src/repo" fetch -q origin
git -C "$src/repo" checkout -q --detach HEAD   # detach at old commit; only main contains it
git -C "$src/repo" branch -q -D main
advance_remote repo main
run_payload
assert_eq "detached auto: exit 0" 0 "$rc"
assert_contains "detached auto: updated" "$out" "updated     repo"
resolved_branch="$(git -C "$src/repo" symbolic-ref --short HEAD)"
assert_eq "detached auto: back on main" "main" "$resolved_branch"

# (b) ambiguous: commit contained in two branches, tip of neither -> picker; pick 1 (alpha)
new_workspace det_pick
new_repo repo alpha
git -C "$src/repo" push -q origin alpha:zeta   # second branch, same history
git -C "$src/repo" fetch -q origin
git -C "$src/repo" checkout -q --detach HEAD
git -C "$src/repo" branch -q -D alpha
advance_remote repo alpha                      # both branches move past the commit,
advance_remote repo zeta                       # so --points-at finds no tips
printf '1\n' > "$base_tmp/answer_pick"
run_payload "$base_tmp/answer_pick"
assert_eq "detached picker: exit 0" 0 "$rc"
assert_contains "detached picker: offers alpha" "$out" "1) alpha"
assert_contains "detached picker: offers zeta" "$out" "2) zeta"
assert_contains "detached picker: skip option" "$out" "s) skip this repo"
picked_branch="$(git -C "$src/repo" symbolic-ref --short HEAD)"
assert_eq "detached picker: on alpha" "alpha" "$picked_branch"

# (c) ambiguous (two branch tips at the commit), user chooses skip
new_workspace det_skip
new_repo repo alpha
git -C "$src/repo" push -q origin alpha:zeta
git -C "$src/repo" fetch -q origin
git -C "$src/repo" checkout -q --detach HEAD
git -C "$src/repo" branch -q -D alpha
printf 's\n' > "$base_tmp/answer_skip"
run_payload "$base_tmp/answer_skip"
assert_eq "detached skip: exit 10" 10 "$rc"
assert_contains "detached skip: reported" "$out" "skipped     repo"
still_detached="$(git -C "$src/repo" symbolic-ref --short -q HEAD; echo "rc=$?")"
assert_contains "detached skip: still detached" "$still_detached" "rc=1"

# (d) commit on no remote branch -> warn + skip
new_workspace det_orphan
new_repo repo
git -C "$src/repo" checkout -q --detach HEAD
git -C "$src/repo" commit -q --allow-empty -m local-orphan
run_payload
assert_eq "detached orphan: exit 10" 10 "$rc"
assert_contains "detached orphan: reason" "$out" "not on any remote branch"

printf '\n%d passed, %d failed\n' "$pass_count" "$fail_count"
[ "$fail_count" -eq 0 ]
