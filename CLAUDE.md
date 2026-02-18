# er_build_tools

Public build tools and CI utilities for Extend Robotics ROS1 repositories. The main component is `ci_tool`, an interactive CLI that reproduces CI failures locally in Docker and uses Claude Code to fix them.

## Project Structure

```
bin/
  ci_tool/              # Main Python package
    __main__.py         # Entry point (auto-installs missing deps)
    cli.py              # Menu dispatch router
    ci_fix.py           # Core workflow: reproduce -> Claude analysis -> fix -> shell
    ci_reproduce.py     # Docker container setup for CI reproduction
    claude_setup.py     # Install Claude + copy credentials/config into container
    claude_session.py   # Interactive Claude session launcher
    containers.py       # Docker lifecycle (create, exec, cp, remove)
    preflight.py        # Auth/setup validation (fail-fast)
    display_progress.py # Stream-json output processor for Claude
    ci_context/
      CLAUDE.md         # CI-specific instructions for Claude inside containers
  setup.sh              # User-facing setup script
  reproduce_ci.sh       # Public wrapper for CI reproduction
.helper_bash_functions  # Sourced by users; provides colcon/rosdep helpers + ci_tool alias
pylintrc                # Pylint config (strict: fail-under=10.0, max-line-length=140)
```

## Code Style

- Python 3.6+, `from __future__ import annotations` in all modules
- snake_case everywhere; PascalCase for classes only
- 4-space indentation, max 140 char line length
- Pylint must pass at 10.0 (`pylintrc` at repo root)
- Use `# pylint: disable=...` pragmas only when essential, with justification
- Interactive prompts via `InquirerPy`; terminal UI via `rich`

## Conventions

- **Fail fast**: `PreflightError` for expected failures, `RuntimeError` for unexpected. No silent defaults, no fallback behaviour.
- **Minimal diffs**: Only change what's requested. No cosmetic cleanups, no "while I'm here" changes.
- **Self-documenting code**: Verbose variable names. Comments only for maths or external doc links.
- **Subprocess calls**: Use `docker_exec()` / `run_command()` from `containers.py`. Pass `check=False` when non-zero is expected; `quiet=True` to suppress echo.
- **State files**: `/ros_ws/.ci_fix_state.json` inside containers (session_id, phase, attempt_count)
- **Learnings persistence**: `~/.ci_tool/learnings/{org}_{repo}.md` on host, `/ros_ws/.ci_learnings.md` in container

## Environment Variables

- `GH_TOKEN` or `ER_SETUP_TOKEN` — GitHub token (checked in preflight)
- `CI_TOOL_SCRIPTS_BRANCH` — branch of er_build_tools_internal to fetch scripts from
- `IS_SANDBOX=1` — injected into all docker exec calls for Claude

## Running

```bash
# From host
source ~/.helper_bash_functions
ci_tool          # interactive menu
ci_fix           # shortcut for ci_tool fix

# Lint
pylint --rcfile=pylintrc bin/ci_tool/
```

## Testing

No unit tests yet. Test manually by running `ci_tool` workflows end-to-end.

## Common Pitfalls

- Container Claude settings must use valid `defaultMode` values: `"acceptEdits"`, `"bypassPermissions"`, `"default"`, `"dontAsk"`, `"plan"`. The old `"dangerouslySkipPermissions"` is invalid.
- `docker_cp_to_container` requires the container to be running.
- Claude inside containers runs with `--dangerously-skip-permissions` flag (separate from the settings.json mode).
