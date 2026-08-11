# cortex_ws_info

Per-repo spec-vs-actual table for the colcon workspaces in a cortex image:
the declared branch and as-built SHA come from provenance files baked into the
image at build time, the current HEAD and dirty state from live git. Replaces
the spec-vs-actual view `wstool info` used to provide, and covers all three
workspaces — ros1 (`/cortex/.catkin_ws` is a legacy path name for a colcon
workspace), ros2 and the ros1/ros2 bridge. Works inside cortex containers and
on dev hosts.

## Run it

With the helper functions sourced:

    cortex_ws_info                     # every default workspace that exists
    cortex_ws_info /cortex/ros2_ws     # or explicit workspace dir(s)

Default workspaces: `/cortex/.catkin_ws`, `/cortex/ros2_ws`,
`/cortex/ros2_ros1_bridge_ws`.

## Columns

| Column | Source |
|--------|--------|
| `local-name` | union of repos on disk and repos in the provenance records |
| `declared branch` | branch record (lookup below) |
| `as-built SHA` | `<workspace>/build_manifest.repos`, shown as 12 chars |
| `current HEAD` | `git rev-parse` (branch name appended when on one) |
| `dirty` | `git status --porcelain` |

A cell whose source is unavailable prints `n/a (<why>)` rather than guessing:
images built before the provenance convention landed (ERD-2141 phase B) bake
fewer files, and stripped release images (no `src/`) still get a row per
provenance repo with the git-derived columns marked n/a.

## Provenance file lookup, per workspace

1. `<workspace>/build_manifest.repos` — resolved closure manifest: the exact
   SHA every repo was built from (as-built SHA column)
2. `<workspace>/build_branches.yaml` — the branch each repo was pinned from
   (declared branch column)
3. `/cortex/rosinstall_branches.yaml` — image-wide branch-record fallback,
   the only record pre-phase-B images bake

## Example output

    /cortex/ros2_ws:
    local-name   | declared branch | as-built SHA | current HEAD        | dirty
    -------------+-----------------+--------------+---------------------+------
    er_common    | main            | 1a2b3c4d5e6f | 1a2b3c4d5e6f (main) | no
    er_interface | ERD-2100_grasp  | 9f8e7d6c5b4a | 0123456789ab (main) | yes

## Exit behaviour

Exits non-zero with a clear error when an explicitly passed workspace
directory does not exist, none of the default workspaces exist on the machine,
PyYAML is missing, or a provenance record cannot be parsed. Otherwise exits 0,
including when some cells are `n/a`.
