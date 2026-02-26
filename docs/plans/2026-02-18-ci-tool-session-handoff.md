# CI Tool Rearchitect — Session Handoff

**Branch:** `ERD-1633_reproduce_ci_locally_tool` in `/cortex/er_build_tools`
**Date:** 2026-02-18

## What Was Done

Full rearchitect of the `bin/ci_tool` Python package:

1. **Removed dead code** — `rename_container` from containers.py
2. **Rewrote ci_reproduce.py** — Python Docker orchestration replaces bash wrapper chain. `reproduce_ci()` is a pure function (no interactive prompts). Fetches scripts from `er_build_tools_internal` via urllib, creates Docker containers directly.
3. **Refactored ci_fix.py** — `gather_session_info()` consolidates all prompting up front. Fixes the original bug where skipping the CI URL didn't prompt for repo/branch.
4. **Adapted cli.py** — `_handle_container_collision()` extracted. Container collision handling is the caller's responsibility, not `reproduce_ci`'s.
5. **Updated setup.sh** — Installs ci_tool and hands off to Python.
6. **Fixed display_progress.py** — Rewrote event handler to match Claude Code's actual stream-json format (`{"type":"assistant","message":{"content":[...]}}` not Anthropic API format). Added `force_terminal=True`.
7. **Fixed claude_setup.py** — Added `IS_SANDBOX=1` to `resume_claude` function.
8. **All files lint clean** — pylint 10.00/10 across all changed modules.

## Outstanding Issues to Debug on Test Machine

### 1. Empty workspace after reproduce (HIGH PRIORITY)

The `ci_workspace_setup.sh` runs inside the container but the workspace ends up empty (`/ros_ws/src/` has nothing). Need to:

```bash
# Check env vars were passed correctly to the container
docker exec er_ci_main env | grep -E 'REPO_URL|REPO_NAME|ORG|BRANCH|GH_TOKEN|DEPS_FILE'

# Check if the setup script is mounted
docker exec er_ci_main ls -la /tmp/ci_workspace_setup.sh

# Re-run setup manually to see errors
docker exec er_ci_main bash /tmp/ci_workspace_setup.sh
```

The `_docker_exec_workspace_setup()` in ci_reproduce.py treats all non-zero exit codes as "expected if tests failed" but doesn't distinguish setup failures from test failures.

### 2. Display progress — no spinners

The display now shows text and tool names (format fix worked) but has no animated spinners. Rich's `Live` display was removed because it swallowed all output in docker exec. The `-t` flag is now passed to docker exec. Could try re-adding a spinner now that `-t` is set, or use a simpler periodic timer approach.

### 3. resume_claude auth (just pushed fix)

Added `IS_SANDBOX=1` to the `resume_claude` bash function. Without it, Claude shows the login screen instead of resuming. Needs testing.

### 4. CDN caching

`ci_tool()` in `.helper_bash_functions` fetches Python files from `raw.githubusercontent.com` which caches aggressively (sometimes minutes). For rapid iteration, either:
- Copy files directly: `cp /cortex/er_build_tools/bin/ci_tool/*.py ~/.ci_tool/ci_tool/`
- Or run locally: `cd /cortex/er_build_tools/bin && python3 -m ci_tool`

### 5. Test repo

Use `https://github.com/Extend-Robotics/er_ci_test_fixture` for integration testing (noted in TODO.md).

## Key Files

- `bin/ci_tool/ci_reproduce.py` — Docker orchestration, script fetching, prompting
- `bin/ci_tool/ci_fix.py` — Claude workflow, session management, prompting
- `bin/ci_tool/cli.py` — Menu routing, container collision handling
- `bin/ci_tool/containers.py` — Low-level Docker helpers
- `bin/ci_tool/display_progress.py` — Stream-json event display
- `bin/ci_tool/claude_setup.py` — Claude installation and config in containers
- `bin/ci_tool/TODO.md` — Future work items
- `.helper_bash_functions` — Bash wrapper that fetches and runs ci_tool

## Key Design Decisions

- `reproduce_ci()` is pure — callers handle container collisions and preflight
- `gather_session_info()` collects ALL user input before any work starts
- `prompt_for_repo_and_branch()` is shared between ci_fix.py and ci_reproduce.py
- `DEFAULT_SCRIPTS_BRANCH` reads `CI_TOOL_SCRIPTS_BRANCH` env var, defaults to `ERD-1633_reproduce_ci_locally` (change to `main` when internal scripts are merged)
- Graphical mode fails fast if DISPLAY not set (CLAUDE.md: no fallback behavior)
- `force_terminal=True` on Rich Console + `docker exec -t` for display

## CLAUDE.md Rules

- KISS, YAGNI, SOLID
- Self-documenting variable names, minimal comments
- Fail fast — no fallback behaviour or silent defaults
- Linters must pass (pylint 10.00/10)
- ROS1 Noetic, Python 3.8 compatibility required
