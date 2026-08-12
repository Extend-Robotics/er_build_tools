#!/bin/bash
# SKIP_CHECK — keep this. check_bash.yml `source`s every bash file to syntax-check
# it (skipping those marked SKIP_CHECK); sourcing this would run the suite.
# Self-contained tests for bin/update-source-repos.sh + bin/update_source_repos.py.
# No docker, no network: docker/curl are PATH shims; the payload is exercised
# against real local git repos via the ER_CATKIN_WS / ER_WORKSPACES overrides.

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

# Every payload invocation points the image-wide branch record at a nonexistent
# file so a real /cortex/rosinstall_branches.yaml can never leak into a test.
run_payload() { # [stdin-file] ; uses $ws; sets $out and $rc
  local stdin_src="${1:-/dev/null}"
  out="$(ER_CATKIN_WS="$ws" \
         ER_IMAGE_WIDE_BRANCH_RECORD="${IMAGE_WIDE_RECORD_OVERRIDE:-$base_tmp/no_such_record.yaml}" \
         python3 "$PAYLOAD" < "$stdin_src" 2>&1)"
  rc=$?
}

run_payload_multi() { # <colon-separated-workspaces> [stdin-file] ; sets $out and $rc
  local ws_list="$1" stdin_src="${2:-/dev/null}"
  out="$(ER_WORKSPACES="$ws_list" \
         ER_IMAGE_WIDE_BRANCH_RECORD="${IMAGE_WIDE_RECORD_OVERRIDE:-$base_tmp/no_such_record.yaml}" \
         python3 "$PAYLOAD" < "$stdin_src" 2>&1)"
  rc=$?
}

# new_root_repo <name> [branch] -> bare remote at $remotes/<name>.git, clone at $ws/<name>
new_root_repo() {
  local name="$1" branch="${2:-main}"
  git init -q --bare -b "$branch" "$remotes/$name.git"
  git clone -q "$remotes/$name.git" "$ws/$name" 2>/dev/null
  git -C "$ws/$name" commit -q --allow-empty -m c1
  git -C "$ws/$name" push -q origin "$branch"
}

# detach_at_stale_commit <name> <branch> -> detach the clone at a commit the
# remote branch has since moved past, and delete the local branch (CI-image shape)
detach_at_stale_commit() {
  local name="$1" branch="$2"
  git -C "$src/$name" fetch -q origin
  git -C "$src/$name" checkout -q --detach HEAD
  git -C "$src/$name" branch -q -D "$branch"
  advance_remote "$name" "$branch"
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

# (e) detached exactly at the tip of one branch -> auto-resolve without prompting
new_workspace det_tip
new_repo repo
advance_remote repo main
git -C "$src/repo" fetch -q origin
git -C "$src/repo" checkout -q --detach origin/main
git -C "$src/repo" branch -q -D main
run_payload
assert_eq "detached tip: exit 10 (already at tip)" 10 "$rc"
assert_contains "detached tip: auto message" "$out" "tip of origin/main"
tip_branch="$(git -C "$src/repo" symbolic-ref --short HEAD)"
assert_eq "detached tip: on main" "main" "$tip_branch"

# ---------- payload: baked branch record resolves detached HEADs ----------
# (a) .repos map shape in <workspace>/build_branches.yaml decides an ambiguous
#     detached HEAD (two candidate branches) without prompting
new_workspace rec_map
new_repo repo alpha
git -C "$src/repo" push -q origin alpha:zeta
git -C "$src/repo" fetch -q origin
git -C "$src/repo" checkout -q --detach HEAD
git -C "$src/repo" branch -q -D alpha
advance_remote repo alpha
advance_remote repo zeta
printf 'repositories:\n  repo:\n    version: zeta\n' > "$ws/build_branches.yaml"
run_payload
assert_eq "record map shape: exit 0" 0 "$rc"
assert_contains "record map shape: updated" "$out" "updated     repo"
assert_contains "record map shape: used the record" "$out" "branch record"
assert_not_contains "record map shape: no picker" "$out" "Choose a branch"
record_branch="$(git -C "$src/repo" symbolic-ref --short HEAD)"
assert_eq "record map shape: on zeta" "zeta" "$record_branch"

# (b) wstool-list shape in the image-wide record (per-workspace file absent)
new_workspace rec_wstool
new_repo repo alpha
git -C "$src/repo" push -q origin alpha:zeta
git -C "$src/repo" fetch -q origin
git -C "$src/repo" checkout -q --detach HEAD
git -C "$src/repo" branch -q -D alpha
advance_remote repo alpha
advance_remote repo zeta
printf -- '- git:\n    local-name: repo\n    uri: https://example.invalid/repo.git\n    version: alpha\n' \
  > "$base_tmp/rosinstall_branches.yaml"
IMAGE_WIDE_RECORD_OVERRIDE="$base_tmp/rosinstall_branches.yaml" run_payload
assert_eq "record wstool shape: exit 0" 0 "$rc"
assert_contains "record wstool shape: updated" "$out" "updated     repo"
assert_not_contains "record wstool shape: no picker" "$out" "Choose a branch"
record_branch="$(git -C "$src/repo" symbolic-ref --short HEAD)"
assert_eq "record wstool shape: on alpha" "alpha" "$record_branch"

# (c) corrupt record degrades loudly to branch guessing, run still succeeds
new_workspace rec_corrupt
new_repo repo
detach_at_stale_commit repo main
printf '{{{not yaml\n' > "$ws/build_branches.yaml"
run_payload
assert_eq "corrupt record: exit 0 (guessing still updates)" 0 "$rc"
assert_contains "corrupt record: unreadable error" "$out" "is unreadable"
assert_contains "corrupt record: loud fallback" "$out" "falling back to branch guessing"
assert_contains "corrupt record: updated via guessing" "$out" "updated     repo"

# (d) record names a branch origin no longer has -> warn, guess instead
new_workspace rec_gone
new_repo repo
detach_at_stale_commit repo main
printf 'repositories:\n  repo:\n    version: deleted_branch\n' > "$ws/build_branches.yaml"
run_payload
assert_eq "record branch gone: exit 0" 0 "$rc"
assert_contains "record branch gone: warned" "$out" "origin has no such branch"
record_branch="$(git -C "$src/repo" symbolic-ref --short HEAD)"
assert_eq "record branch gone: guessed main" "main" "$record_branch"

# (e) record must not clobber local commits on a detached HEAD
new_workspace rec_localcommit
new_repo repo
git -C "$src/repo" checkout -q --detach HEAD
git -C "$src/repo" commit -q --allow-empty -m local-orphan
printf 'repositories:\n  repo:\n    version: main\n' > "$ws/build_branches.yaml"
run_payload
assert_eq "record with local commits: exit 10" 10 "$rc"
assert_contains "record with local commits: left alone" "$out" "not on any remote branch"
still_detached="$(git -C "$src/repo" symbolic-ref --short -q HEAD; echo "rc=$?")"
assert_contains "record with local commits: still detached" "$still_detached" "rc=1"

# ---------- payload: multi-workspace iteration ----------
new_workspace multi_a
new_repo repo_a
advance_remote repo_a main
multi_a_ws="$ws"
new_workspace multi_b
new_repo repo_b
advance_remote repo_b main
multi_b_ws="$ws"
run_payload_multi "$multi_a_ws:$multi_b_ws"
assert_eq "multi-ws: exit 11 (updates, but no ros1 workspace)" 11 "$rc"
assert_contains "multi-ws: workspace banner a" "$out" "===== workspace $multi_a_ws ====="
assert_contains "multi-ws: workspace banner b" "$out" "===== workspace $multi_b_ws ====="
assert_contains "multi-ws: repo_a updated" "$out" "updated     $multi_a_ws: repo_a"
assert_contains "multi-ws: repo_b updated" "$out" "updated     $multi_b_ws: repo_b"
assert_contains "multi-ws: manual rebuild warning a" "$out" "workspace $multi_a_ws was updated — rebuild it manually"
assert_contains "multi-ws: manual rebuild warning b" "$out" "workspace $multi_b_ws was updated — rebuild it manually"

# a workspace whose basename is .catkin_ws is the ros1 workspace -> exit 0,
# and only the other workspace gets the manual-rebuild warning
new_workspace "ros1home/.catkin_ws"
new_repo repo_r1
advance_remote repo_r1 main
ros1_ws="$ws"
new_workspace multi_c
new_repo repo_c
advance_remote repo_c main
run_payload_multi "$ros1_ws:$ws"
assert_eq "multi-ws with ros1: exit 0" 0 "$rc"
assert_contains "multi-ws with ros1: ros2-style ws warned" "$out" "workspace $ws was updated — rebuild it manually"
assert_not_contains "multi-ws with ros1: ros1 ws not warned" "$out" "workspace $ros1_ws was updated"

# ER_WORKSPACES naming a missing directory fails fast
run_payload_multi "$base_tmp/does_not_exist"
assert_eq "missing workspace: exit 1" 1 "$rc"
assert_contains "missing workspace: message" "$out" "not found on disk"

# ---------- payload: repos at the workspace root (beside src/) ----------
new_workspace rootrepo
new_repo inside_src
new_root_repo beside_src
advance_remote inside_src main
advance_remote beside_src main
run_payload
assert_eq "root repo: exit 0" 0 "$rc"
assert_contains "root repo: src repo updated" "$out" "updated     inside_src"
assert_contains "root repo: root repo updated" "$out" "updated     beside_src"

# ---------- payload: submodule recovery & safety ----------
# stale submodule pointer from an interrupted previous run must self-heal, not dirty-skip
new_workspace subm_stale
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=protocol.file.allow GIT_CONFIG_VALUE_0=always
new_repo sub
new_repo super
git -C "$src/super" submodule add -q "$remotes/sub.git" thesub
git -C "$src/super" commit -qm add-submodule
git -C "$src/super" push -q origin main
advance_remote sub main
rm -rf "$src/sub"   # the seeding clone would itself be discovered and updated
tmpc="$(mktemp -d "$base_tmp/subadv.XXXXXX")"
git clone -q "$remotes/super.git" "$tmpc/c" 2>/dev/null
git -C "$tmpc/c" submodule update -q --init
git -C "$tmpc/c/thesub" fetch -q origin
git -C "$tmpc/c/thesub" checkout -q origin/main
git -C "$tmpc/c" add thesub
git -C "$tmpc/c" commit -qm bump-sub
git -C "$tmpc/c" push -q origin main
rm -rf "$tmpc"
# simulate the interrupted run: superproject ff'd but submodule update never ran
git -C "$src/super" fetch -q origin
git -C "$src/super" merge -q --ff-only origin/main >/dev/null
run_payload
assert_eq "stale submodule: exit 10" 10 "$rc"
assert_contains "stale submodule: not dirty-skipped" "$out" "up-to-date  super"
sub_head2="$(git -C "$src/super/thesub" rev-parse HEAD)"
recorded2="$(git -C "$src/super" ls-tree HEAD thesub | awk '{print $3}')"
assert_eq "stale submodule: healed to recorded commit" "$recorded2" "$sub_head2"

# genuinely dirty submodule content must skip, never be clobbered
new_workspace subm_dirty
new_repo sub2
tmpc="$(mktemp -d "$base_tmp/subf.XXXXXX")"
git clone -q "$remotes/sub2.git" "$tmpc/c" 2>/dev/null
echo v1 > "$tmpc/c/f.txt"
git -C "$tmpc/c" add f.txt
git -C "$tmpc/c" commit -qm add-f
git -C "$tmpc/c" push -q origin main
rm -rf "$tmpc"
git -C "$src/sub2" pull -q --ff-only origin main
new_repo super2
git -C "$src/super2" submodule add -q "$remotes/sub2.git" thesub
git -C "$src/super2" commit -qm add-submodule
git -C "$src/super2" push -q origin main
advance_remote super2 main
echo hacked > "$src/super2/thesub/f.txt"
run_payload
assert_eq "dirty submodule: exit 10" 10 "$rc"
assert_contains "dirty submodule: skipped" "$out" "skipped     super2"
assert_contains "dirty submodule: reason" "$out" "inside a submodule"
dirty_sub_content="$(cat "$src/super2/thesub/f.txt")"
assert_eq "dirty submodule: edit untouched" "hacked" "$dirty_sub_content"
unset GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0

# ---------- payload: untracked-file collision & missing origin ----------
# upstream adds a file that exists untracked locally -> skip with a clear reason
new_workspace clash
new_repo repo
tmpc="$(mktemp -d "$base_tmp/clash.XXXXXX")"
git clone -q -b main "$remotes/repo.git" "$tmpc/c" 2>/dev/null
echo upstream > "$tmpc/c/clash.txt"
git -C "$tmpc/c" add clash.txt
git -C "$tmpc/c" commit -qm add-clash
git -C "$tmpc/c" push -q origin main
rm -rf "$tmpc"
echo local > "$src/repo/clash.txt"
run_payload
assert_eq "untracked collision: exit 10" 10 "$rc"
assert_contains "untracked collision: skipped" "$out" "skipped     repo"
assert_contains "untracked collision: reason" "$out" "untracked file"
assert_not_contains "untracked collision: not called diverged" "$out" "diverged"
clash_content="$(cat "$src/repo/clash.txt")"
assert_eq "untracked collision: file untouched" "local" "$clash_content"

# a repo with no origin remote is skipped, not a run-wide failure
new_workspace noorigin
new_repo good
advance_remote good main
mkdir "$src/local_only"
git init -q -b main "$src/local_only"
git -C "$src/local_only" commit -q --allow-empty -m c1
run_payload
assert_eq "no origin: exit 0 (good repo still updates)" 0 "$rc"
assert_contains "no origin: skipped" "$out" "skipped     local_only"
assert_contains "no origin: reason" "$out" "no 'origin' remote"
assert_contains "no origin: other repo updated" "$out" "updated     good"

# ---------- wrapper: shims ----------
shims="$base_tmp/shims"
mkdir -p "$shims"

cat > "$shims/docker" <<'EOF'
#!/bin/bash
echo "docker $*" >> "$DOCKER_LOG"
case "$1" in
  container)  # container inspect -f ... er_robot
    if [ "${FAKE_CONTAINER_RUNNING:-true}" = "true" ]; then echo "true"; exit 0; fi
    exit 1 ;;
  cp) exit "${FAKE_CP_RC:-0}" ;;
  exec)
    case "$*" in
      *python3*) exit "${FAKE_PAYLOAD_RC:-0}" ;;
      *"rm -f"*) exit 0 ;;
      *colcon_build*) exit "${FAKE_BUILD_RC:-0}" ;;
      *) exit 0 ;;
    esac ;;
  *) exit 0 ;;
esac
EOF

cat > "$shims/curl" <<'EOF'
#!/bin/bash
# API check has -w '%{http_code}' and its config on stdin; payload fetch has -o <file>.
echo "curl $*" >> "${CURL_LOG:-/dev/null}"
args="$*"
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && [ "$a" != "/dev/null" ] && out="$a"; prev="$a"; done
if [[ "$args" == *"%{http_code}"* ]]; then cat > /dev/null; printf '%s' "${FAKE_HTTP_CODE:-200}"; exit 0; fi
if [ -n "$out" ]; then
  [ "${FAKE_FETCH_RC:-0}" -ne 0 ] && exit "${FAKE_FETCH_RC}"
  echo "print('fake payload')" > "$out"; exit 0
fi
exit 0
EOF
chmod +x "$shims/docker" "$shims/curl"

run_wrapper() { # args... ; uses env knobs; sets $out and $rc
  export DOCKER_LOG="$base_tmp/docker.log.$RANDOM"
  export CURL_LOG="$base_tmp/curl.log.$RANDOM"
  : > "$DOCKER_LOG"
  : > "$CURL_LOG"
  out="$(PATH="$shims:$PATH" PAYLOAD="${PAYLOAD_OVERRIDE:-$PAYLOAD}" NO_COLOR=1 \
         bash "$WRAPPER" "$@" 2>&1 < /dev/null)"
  rc=$?
}

good_pat="ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"   # ghp_ + 36 chars
fg_pat="github_pat_$(printf 'a%.0s' $(seq 1 82))"      # fine-grained shape

# ---------- wrapper: PAT validation ----------
run_wrapper
assert_eq "no pat: exit 1" 1 "$rc"
assert_contains "no pat: usage" "$out" "Usage: er_update_source_repos"

run_wrapper "notatoken"
assert_eq "bad prefix: exit 1" 1 "$rc"
assert_contains "bad prefix: message" "$out" "does not look like a GitHub PAT"

run_wrapper "ghp_tooshort"
assert_eq "bad length: exit 1" 1 "$rc"

run_wrapper "  ${good_pat}
"
assert_eq "whitespace-wrapped pat accepted" 0 "$rc"

run_wrapper "$fg_pat"
assert_eq "fine-grained pat accepted" 0 "$rc"

GITHUB_PAT="$good_pat" run_wrapper
assert_eq "pat via env accepted" 0 "$rc"

# ---------- wrapper: PAT live check ----------
FAKE_HTTP_CODE=401 run_wrapper "$good_pat"
assert_eq "401: exit 1" 1 "$rc"
assert_contains "401: message" "$out" "rejected the PAT"

FAKE_HTTP_CODE=000 run_wrapper "$good_pat"
assert_eq "network down: continues" 0 "$rc"
assert_contains "network down: warns" "$out" "could not reach api.github.com"

# ---------- wrapper: container / fetch failures ----------
FAKE_CONTAINER_RUNNING=false run_wrapper "$good_pat"
assert_eq "container down: exit 1" 1 "$rc"
assert_contains "container down: message" "$out" "er_robot"

PAYLOAD_OVERRIDE="/nonexistent" FAKE_FETCH_RC=22 run_wrapper "$good_pat"
assert_eq "fetch fail: exit 1" 1 "$rc"
assert_contains "fetch fail: message" "$out" "failed to fetch"

FAKE_CP_RC=1 run_wrapper "$good_pat"
assert_eq "docker cp fail: exit 1" 1 "$rc"
assert_contains "docker cp fail: message" "$out" "failed to copy"

# ---------- wrapper: payload rc -> build behaviour ----------
run_wrapper "$good_pat"    # payload rc 0 -> build runs
assert_eq "update ok: exit 0" 0 "$rc"
docker_log="$(cat "$DOCKER_LOG")"
assert_contains "update ok: payload ran" "$docker_log" "python3"
assert_contains "update ok: build ran" "$docker_log" "colcon_build"
assert_not_contains "pat never in docker argv" "$docker_log" "$good_pat"
curl_log="$(cat "$CURL_LOG")"
assert_not_contains "pat never in curl argv" "$curl_log" "$good_pat"

FAKE_PAYLOAD_RC=10 run_wrapper "$good_pat"
assert_eq "nothing updated: exit 0" 0 "$rc"
assert_contains "nothing updated: message" "$out" "skipping colcon_build"
docker_log="$(cat "$DOCKER_LOG")"
assert_not_contains "nothing updated: no build" "$docker_log" "colcon_build"

FAKE_PAYLOAD_RC=1 run_wrapper "$good_pat"
assert_eq "payload failed: exit 1" 1 "$rc"
docker_log="$(cat "$DOCKER_LOG")"
assert_not_contains "payload failed: no build" "$docker_log" "colcon_build"

FAKE_PAYLOAD_RC=11 run_wrapper "$good_pat"
assert_eq "manual-rebuild-only workspaces: exit 0" 0 "$rc"
assert_contains "manual-rebuild-only workspaces: message" "$out" "rebuild them manually"
docker_log="$(cat "$DOCKER_LOG")"
assert_not_contains "manual-rebuild-only workspaces: no build" "$docker_log" "colcon_build"

FAKE_BUILD_RC=97 run_wrapper "$good_pat"
assert_eq "helpers missing: exit 97" 97 "$rc"
assert_contains "helpers missing: message" "$out" "helper_bash_functions"

FAKE_BUILD_RC=2 run_wrapper "$good_pat"
assert_eq "build failed: exit 2" 2 "$rc"
assert_contains "build failed: message" "$out" "colcon_build failed"

printf '\n%d passed, %d failed\n' "$pass_count" "$fail_count"
[ "$fail_count" -eq 0 ]
