# CI Tool Rearchitect Design

## Problem

When the CI tool is run without a GitHub Actions URL, it fails to ask for a repo URL and branch, then crashes through a cascade of "No such container" errors. This is symptomatic of deeper architectural issues:

1. **Prompting scattered across modules** — `select_or_create_session` (ci_fix.py), `prompt_for_reproduce_args` (ci_reproduce.py), and `fix_ci` all collect user input at different stages.
2. **No fail-fast after reproduction failure** — The tool continues to container setup even when `reproduce_ci.sh` fails and no container exists.
3. **Absurd fetch chain** — Python curls a public bash wrapper, which curls 3 private scripts, which run bash that does what Python could do directly.
4. **Side-effect mutation** — `select_or_create_session` mutates `parsed["reproduce_args"]` rather than returning clean data.
5. **Dead code and redundant abstractions** — `rename_container`, `parse_fix_args`, `extract_*_from_args` helpers.
6. **Container collision handled in 3 places** — `prompt_for_session_name`, `reproduce_ci`, and `fix_ci` all handle existing containers differently.
7. **Preflight skips repo validation** — When no CI URL is provided, `repo_url` is None and the token's repo-access check is silently skipped.

## Design

### Entry Points

```
setup.sh (bash, one-time setup + hand-off)
  Install helpers → configure GH_TOKEN → install ci_tool → exec ci_tool

ci_tool / ci_fix (Python, primary interface)
  All functionality: reproduce, fix with Claude, shell, clean, retest

reproduce_ci.sh (bash, backward-compatible standalone)
  Standalone CI reproduction without Python. No changes.
```

### Core Flow: `ci_tool fix`

```
gather_session_info()          <- All prompts happen here
  |- Existing containers? -> Resume menu
  '- New session:
      |- CI URL? (optional)
      |- Repo URL + Branch (if no CI URL)
      |- Only needed deps?
      '- Session name

run_all_preflight_checks()     <- Always validates repo_url

reproduce_ci()                 <- Python does Docker orchestration directly
  |- Fetch ci_workspace_setup.sh + ci_repull_and_retest.sh via urllib
  |- Validate deps.repos reachable
  |- docker create (env vars, volume mounts, graphical forwarding)
  |- docker start
  |- docker exec ci_workspace_setup.sh
  '- Guard: raise if container doesn't exist

setup_claude_in_container()    <- Existing, no major changes

run_claude_workflow()           <- Analysis -> Review -> Fix (or custom/resume)

drop_to_shell()                 <- Interactive container shell
```

### Data Structures

`gather_session_info` returns a dict:

```python
# New session:
{
    "mode": "new",
    "container_name": "er_ci_my_branch",
    "repo_url": "https://github.com/Extend-Robotics/er_interface",
    "branch": "my-branch",
    "only_needed_deps": True,
    "ci_run_info": {...} or None,
}

# Resume existing container:
{
    "mode": "resume",
    "container_name": "er_ci_existing",
    "resume_session_id": "abc123" or None,
}
```

### Module Responsibilities

| Module | Responsibility |
|--------|---------------|
| `ci_fix.py` | `gather_session_info()`, `fix_ci()` linear orchestration, Claude prompts/templates |
| `ci_reproduce.py` | Docker orchestration (create/start/exec), fetch container-side scripts, validate deps.repos |
| `preflight.py` | Validate Docker, GH token (with repo access — always), Claude credentials |
| `claude_setup.py` | Install/configure Claude in container (unchanged) |
| `containers.py` | Low-level Docker helpers (exists, running, exec, cp, remove, list) |
| `cli.py` | Menu dispatcher (minor adapter for `_handle_reproduce`) |
| `display_progress.py` | Stream-json display processor (unchanged, runs in container) |
| `claude_session.py` | Interactive Claude session launcher (unchanged) |

### `reproduce_ci` New Interface

```python
def reproduce_ci(
    repo_url: str,
    branch: str,
    container_name: str,
    gh_token: str,
    only_needed_deps: bool = True,
    scripts_branch: str = "main",
    graphical: bool = True,
):
    """Create a CI reproduction container.

    Fetches container-side scripts from er_build_tools_internal,
    creates Docker container with proper env/volumes, runs workspace setup.

    Raises RuntimeError if container doesn't exist after execution.
    """
```

Explicit parameters instead of a string arg list. No interactive prompts.
The CLI `reproduce` subcommand uses a thin adapter that parses CLI args or
calls `prompt_for_reproduce_args()` before calling this function.

### Python Docker Orchestration (replaces bash wrapper)

Currently Python shells out to a bash wrapper that shells out to another
bash script. The new `reproduce_ci` does the Docker orchestration directly:

1. **Fetch container-side scripts** via `urllib` with GH token auth header
   from `er_build_tools_internal` (configurable branch).
2. **Write to `/tmp/er_reproduce_ci/`** — same location the bash wrapper uses.
3. **Validate deps.repos** is reachable (curl-equivalent HTTP HEAD check).
4. **Build `docker create` args** — env vars (GH_TOKEN, REPO_URL, BRANCH, etc.),
   volume mounts (scripts as read-only), network/IPC host, optional graphical
   forwarding (X11, NVIDIA).
5. **`docker create`** + **`docker start`** + **`docker exec bash /tmp/ci_workspace_setup.sh`**
   via `subprocess.run`.
6. **Container guard** — verify `container_exists()` after execution, raise if not.

### Rich UI Throughout

All existing UI stays:
- **InquirerPy** for interactive prompts (select menus, text inputs, confirmations).
- **Rich** for colored console output, panels, status messages.
- **display_progress.py** for Claude stream-json spinner/activity display.

The reproduce step gets improved UI by moving from plain bash output to rich:
- Spinner with elapsed time during long-running docker exec
- Checkmark confirmations for each setup step
- Colored error messages on failure

### Preflight Changes

`run_all_preflight_checks` always requires `repo_url`:
- `gather_session_info` always provides a repo URL (from CI URL extraction or direct prompt)
- For resume mode, preflight is skipped (container already exists)
- The GH token repo-access check always runs for new sessions

### setup.sh Changes

Add ci_tool Python package installation and hand-off:
- After existing setup steps (helpers, GH token, shell integration, Claude check)
- `pip install` ci_tool from er_build_tools
- `exec python3 -m ci_tool` to hand off to the Python tool

### Files Changed

| File | Change | Repo |
|------|--------|------|
| `ci_fix.py` | Major refactor: `gather_session_info`, linear `fix_ci` flow | er_build_tools |
| `ci_reproduce.py` | Rewrite: Python Docker orchestration, explicit params | er_build_tools |
| `preflight.py` | `repo_url` always required for new sessions | er_build_tools |
| `containers.py` | Remove `rename_container` (dead code) | er_build_tools |
| `cli.py` | Adapt `_handle_reproduce` for new `reproduce_ci` interface | er_build_tools |
| `setup.sh` | Add ci_tool install + hand-off to Python | er_build_tools |

### Files Unchanged

| File | Reason |
|------|--------|
| `claude_setup.py` | Works as-is |
| `display_progress.py` | Runs in container, works as-is |
| `claude_session.py` | Works as-is |
| `reproduce_ci.sh` (public wrapper) | Kept for backward compat |
| All `er_build_tools_internal` scripts | No changes needed |
