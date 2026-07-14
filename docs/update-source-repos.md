# update-source-repos

Pulls the latest source code for every git repo in `/cortex/.catkin_ws/src`
inside the running `er_robot` container, then rebuilds with `colcon_build`.
For QA testing on Jetsons with a **source-code** image — stripped images (no
`src/`, only `install/`) are rejected with a clear error.

## Run it

On the Jetson **host** (not inside the container), with the helper functions
sourced:

    er_update_source_repos <github-pat>

or with the token in the environment:

    export GITHUB_PAT=ghp_...
    er_update_source_repos

The PAT needs read access to the Extend-Robotics repos — classic (`ghp_...`,
`repo` scope) or fine-grained (`github_pat_...`, Contents: read). The token is
verified against the GitHub API before anything runs, travels only via the
environment (never argv/`ps`, remote URLs, or git config), and is discarded
when the util exits. Prefer the `GITHUB_PAT` env-var form if you don't want
the token in your shell history. Public third-party repos in `src/` work too —
they never trigger the credential path.

## What it does per repo

| Repo state | Action |
|------------|--------|
| On a branch, behind origin | fast-forward to origin |
| Already up to date | nothing |
| Uncommitted tracked changes (repo or submodule) | **skipped** with a warning — local edits are never touched |
| Local commits diverged from origin | **skipped** with a warning — `--ff-only`, never rewrites |
| Update would overwrite an untracked file | **skipped** with a warning — move the file aside first |
| Branch missing on origin | **skipped** with a warning |
| No `origin` remote | **skipped** with a warning |
| Detached HEAD, tip of exactly one origin branch | that branch is checked out and pulled |
| Detached HEAD, commit on exactly one branch | that branch is checked out and pulled |
| Detached HEAD, commit on several branches | interactive picker (with a skip option) |
| Detached HEAD, commit on no remote branch | **skipped** with a warning |
| Has submodules | synced + updated to the superproject's recorded commits |

Untracked files never block an update — unless the incoming update would
overwrite one, in which case the repo is skipped with a warning. A stale
submodule pointer left by an interrupted previous run self-heals on the next
run. A summary table at the end lists every repo as updated / up-to-date /
skipped (with reason) / failed.

## After updating

If at least one repo updated, `colcon_build` (from `.helper_bash_functions`
inside the container) runs automatically. If nothing updated, the build is
skipped.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success — updated + built, or nothing to do |
| 97 | `.helper_bash_functions` / `colcon_build` missing inside the container |
| other | the failing step's code (bad/rejected PAT, container down, pull or build failure) |
