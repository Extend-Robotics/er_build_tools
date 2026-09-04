# setup-container-shell

Prepares the interactive shell inside a cortex image: installs the shared
`.helper_bash_functions` and repairs ROS1 tab completion. Run by the image build,
not from a shell.

## Use it

One line per Dockerfile, placed **after the last `apt`/`rosdep` step**:

    RUN curl -fsSL https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/setup-container-shell.sh | bash

Position matters: `rosdep install` can pull `ros-noetic-rosbash` back in, which
would overwrite the completion patch applied earlier in the file.

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
the second stage walks the filesystem. `rosmsg`, `rossrv`, `rosrun` and `rosed`
are hit the same way.

The fix replaces all 13 occurrences (two variants) with a basename check,
`-not -name '.*'`, which keeps the original intent — hide dotfiles — without
inspecting ancestor directories.

## Behaviour

| Situation | Result |
|-----------|--------|
| rosbash found, 13 filters | Patched |
| rosbash found, already patched | No-op |
| No rosbash under `$ROSBASH_SEARCH_ROOT` | Skipped (image has no ROS1) |
| rosbash found, filter count is not 13 | **Exit 1** — rosbash changed upstream |

An absent rosbash is a legitimate state, so it is skipped; a rosbash whose shape
is not what the patch expects fails the build rather than silently no-op'ing.
Every rosbash under the search root is patched, so multi-distro images are covered.

## Environment

| Variable | Default |
|----------|---------|
| `TARGET_HOME` | `/root` |
| `ROSBASH_SEARCH_ROOT` | `/opt/ros` |
| `HELPER_FUNCTIONS_URL` | `.../refs/heads/main/.helper_bash_functions` |

Overridden by the tests to run against fixtures; image builds use the defaults.

## Note

The underlying cause is the dot in `/cortex/.catkin_ws`. Renaming the workspace
would remove this whole class of bug, since other tooling that skips hidden paths
is silently affected too.
