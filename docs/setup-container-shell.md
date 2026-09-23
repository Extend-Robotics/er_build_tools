# setup-container-shell

Prepares the interactive shell: installs the shared `.helper_bash_functions`
and repairs rosbash for a workspace under a dot directory. Run by image builds,
and by hand on dev hosts (see the [README](../README.md#initial-setup)).

## Use it

One call per Dockerfile, placed **after the last `apt`/`rosdep` step**:

    ARG ER_BUILD_TOOLS_BRANCH="main"
    ARG SETUP_CONTAINER_SHELL_URL="https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/${ER_BUILD_TOOLS_BRANCH}/bin/setup-container-shell.sh"
    RUN curl -fsSL "${SETUP_CONTAINER_SHELL_URL}" | ER_BUILD_TOOLS_BRANCH="${ER_BUILD_TOOLS_BRANCH}" bash

Passing the branch through to the script keeps the script and the
`.helper_bash_functions` it fetches on the same branch, so
`--build-arg ER_BUILD_TOOLS_BRANCH=my-branch` tests both together.

Position matters: `rosdep install` can pull `ros-noetic-rosbash` back in, which
would overwrite the rosbash patch applied earlier in the file.

## What it does

**Helper functions.** Fetches `.helper_bash_functions` to `$TARGET_HOME` and adds
a `source` line to `.bashrc`, skipping the append if it is already there.

**ROS1 tab completion.** rosbash filters completion candidates against the whole
path:

    ! -regex ".*/[.][^./].*"

A workspace under a dot directory — `/cortex/.catkin_ws` — matches that pattern,
so every candidate beneath it is excluded. The symptom is partial completion:

    roslaunch wuji_control_ros <TAB>     # completes the package name
    roslaunch wuji_control_ros wuji_<TAB> # offers nothing

The package name still completes because that stage uses the package index; only
the second stage walks the filesystem. Completion for `rosrun`, `roscat`, `rosed`,
`roscp`, `rosmsg` and `rossrv` is hit the same way.

**roscat, rosed, roscp and rosmv.** These look the file up through rosbash's
`_roscmd`, which carries an unquoted variant of the same filter:

    ! -regex .*/[.].*

so even with completion repaired, the command cannot find the file:

    roscat wuji_control_ros wuji_driver.launch  # That file does not exist in that package.

`rosrun` and `roslaunch` find files without this filter; only their completion
is affected.

**The fix.** The 13 completion filters become a basename check,
`-not -name '.*'`, which hides dotfiles without inspecting ancestor directories.
`_roscmd` picks the file that is printed, edited, copied or moved, so there the
filter becomes a prune instead, which also skips hidden directories inside the
package (`.git/`, `.pytest_cache/`, `.claude/worktrees/`) as upstream intended:

    find ... -name '.*' -prune -o -name $2 -type f ... -print

Completion can still list names from hidden directories inside a package. Only
bash's `rosbash` is patched; `rosfish`, `roszsh` and `rostcsh` are not.

## Behaviour

| Situation | Result |
|-----------|--------|
| rosbash found, unpatched | Patched |
| rosbash patched by an earlier version (completion filters only) | `_roscmd` filter patched |
| rosbash found, already patched | No-op |
| No rosbash under `$ROSBASH_SEARCH_ROOT` | Skipped (image has no ROS1) |
| rosbash found, filters do not add up to 13 completion + 1 `_roscmd` | **Exit 1**, file untouched: rosbash changed upstream or was edited |

An absent rosbash is a legitimate state, so it is skipped; a rosbash whose shape
is not what the patch expects fails the build rather than silently no-op'ing.
Every rosbash under the search root is patched, so multi-distro images are covered.

## Elevation

The rosbash patch rewrites files under `/opt/ros`. Where they are writable —
which is the case running as root, as image builds do — nothing is elevated.
Where they are not, typically ROS installed via apt and a non-root user,
**only the `sed` runs under sudo**, prompting at that point. The script itself is never
run with sudo: doing so would leave a root-owned `.helper_bash_functions` in
the user's home.

Without a usable sudo credential and no terminal to prompt on, sudo fails
immediately and the script exits non-zero with the file untouched — it does not
hang.

## Environment

| Variable | Default |
|----------|---------|
| `TARGET_HOME` | `/root` |
| `ROSBASH_SEARCH_ROOT` | `/opt/ros` |
| `ER_BUILD_TOOLS_BRANCH` | `main` |
| `HELPER_FUNCTIONS_URL` | `.../refs/heads/${ER_BUILD_TOOLS_BRANCH}/.helper_bash_functions` |

Overridden by the tests to run against fixtures; image builds use the defaults.

## Note

The underlying cause is the dot in `/cortex/.catkin_ws`. Renaming the workspace
would remove this whole class of bug, since other tooling that skips hidden paths
is silently affected too.
