# CI Context — Extend Robotics ROS1 Workspace

You are inside a CI reproduction container. The ROS workspace is at `/ros_ws/`.
Source code is under `/ros_ws/src/`. Built packages install to `/ros_ws/install/`.

## Token Efficiency

Use Grep to search log files for relevant errors — never read entire log files.
When examining test output, search for FAILURE, FAILED, ERROR, or assertion messages.
Pipe long command output through `tail -200` or `grep` to avoid dumping huge logs.

Always use the helper functions (`colcon_build`, `colcon_build_no_deps`, `colcon_test_this_package`) instead of raw `colcon` commands — they limit output to the last 50 lines and log full output to `/ros_ws/.colcon_build.log` and `/ros_ws/.colcon_test.log`. If you need more detail, Grep the log files.

## Environment Setup

```bash
source /opt/ros/noetic/setup.bash && source /ros_ws/install/setup.bash
source ~/.helper_bash_functions
```

## Build Commands

```bash
source ~/.helper_bash_functions

# Full workspace build
colcon_build

# Single package (with deps)
colcon_build <package_name>

# Single package (no deps — use after editing only Python in that package)
colcon_build_no_deps <package_name>

# Multiple packages
colcon_build "<pkg1> <pkg2>"
```

Python imports resolve to installed `.pyc` in `/ros_ws/install/`, not source. Always build after editing Python before running tests.

## Testing

```bash
# Run all tests in a package
colcon_test_this_package <package_name>

# Run specific rostest
rostest <package_name> <test_file>.test
```

`colcon_test_this_package` does NOT build dependencies — only builds and tests the named package. If you changed code in other packages, build those first.

When reporting test results, check per-package XML files in `build/<pkg>/test_results/` — `colcon test-result --verbose` without `--test-result-base` aggregates stale results from the entire `build/` directory.

## Linting

```bash
source ~/.helper_bash_functions && cd /ros_ws/src/er_interface/<package_name> && er_python_linters_here
```

Linters must pass including warnings. Don't use `# pylint: disable` unless absolutely necessary. Always lint before committing.

After changing `er_robot_description`, run `rosrun er_interface xacro_lint.py` to validate all assembly XACRO permutations. `er_python_linters_here` does NOT run this.

## Style Guide

- Code must follow KISS, YAGNI, and SOLID principles.
- Self-documenting code with verbose variable names. Comments only for maths or external doc links.
- Fail fast — no fallback behaviour or silent defaults. If something goes wrong, raise a clear exception.
- Never add try/except, None-return, or fallback behaviour to existing functions that currently raise on error.
- Scope changes to what was requested — no cosmetic cleanups, no "while I'm here" changes.
- Do not rename functions, variables, or files unless renaming is the task.
- Keep diffs minimal. Every changed line must serve the requested purpose.
- Do not mention Claude in commit messages or PRs.

## Common CI Failure Patterns

1. **Missing package.xml dependencies**: Code works locally because a dependency is installed system-wide, but CI only installs declared dependencies. Check `<exec_depend>`, `<depend>`, and `<build_depend>` tags match all imports.

2. **Import errors**: If a node crashes with `ModuleNotFoundError`, the package is missing from `package.xml`. Trace the import chain to find which dependency is needed.

3. **Race conditions in launch files**: Use `conditional_delayed_rostool` to wait for topics/params/services before launching dependent nodes. Don't restructure node startup code or add timeouts.

4. **Stale test results**: `colcon test-result --verbose` aggregates stale results from the entire `build/` directory. Always check per-package XML files in `build/<pkg>/test_results/`.

5. **XACRO validation failures**: After changing `er_robot_description`, run `rosrun er_interface xacro_lint.py`. The CI `er_xacro_checks.yml` workflow runs this automatically.

6. **Test tolerance failures**: Check per-joint tolerance overrides in test config YAML files. Some joints (e.g. thumb) have higher variability under IK.

## Architecture Overview

ROS Noetic catkin workspace for multi-robot assemblies:

- **er_robot_description**: URDF/XACRO files for all robots
- **er_robot_config**: Configuration generation (SRDF, controllers, kinematics from Jinja2 templates)
- **er_robot_launch**: Main launch entrypoint for complete robot system
- **er_robot_hand_interface**: Human hand pose projection to robot hands/grippers via IK
- **er_state_validity_checker**: In-process collision checking, joint limits, manipulability via MoveIt
- **er_auto_moveit_config**: MoveIt configuration generation
- **er_utilities_common**: Shared utilities (conditional_delayed_rostool, joint state aggregation)
- **er_moveit_collisions_updater_python**: Automatic MoveIt collision pair exclusion via randomised sampling

Configuration pipeline: Assembly configs → robot configs → Jinja2 templates → URDF/SRDF/controllers. Generated files output to `/tmp/`.

## Learnings

If `/ros_ws/.ci_learnings.md` exists, read it before starting — it contains lessons from
previous CI fix sessions for this repo.

After fixing CI failures, update `/ros_ws/.ci_learnings.md` with any new insights:
- Root causes that were non-obvious
- Patterns that recur (e.g. "this repo often breaks because of X")
- Debugging techniques that saved time
- False leads to avoid next time

Keep it concise. This file persists across sessions.

## Design Principles

- Explicit over implicit. Use named fields with clear values, not absence-of-key or empty-dict semantics.
- Mode-dispatching methods must branch on mode first. No computation before the branch unless genuinely shared.
- After any code change, run existing tests before declaring done. A test passing before and failing after is a regression.
- Before modifying shared helper functions, read the whole file and check all callers.
- When adding fail-fast errors, trace all call paths.
- Check assumptions empirically before asserting them. Don't dismiss failures without data.
