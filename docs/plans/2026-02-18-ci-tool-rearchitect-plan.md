# CI Tool Rearchitect Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the missing repo/branch prompt bug and rearchitect ci_tool so prompting is consolidated, Docker orchestration is done in Python (not bash), and the tool fails fast on errors.

**Architecture:** All user prompts happen up-front in `gather_session_info()`. `reproduce_ci()` takes explicit params and does Docker orchestration directly in Python (bypassing the bash wrapper chain). A container existence guard prevents cascading failures.

**Tech Stack:** Python 3.8+, InquirerPy, Rich, Docker CLI via subprocess, urllib for GitHub API

---

### Task 1: Remove dead code from containers.py

**Files:**
- Modify: `bin/ci_tool/containers.py:80-83` (remove `rename_container`)

**Step 1: Remove `rename_container` function**

Delete the `rename_container` function at line 80-83 of `containers.py`:

```python
# DELETE this entire function:
def rename_container(old_name, new_name):
    """Rename a Docker container."""
    run_command(["docker", "rename", old_name, new_name])
```

**Step 2: Verify no references remain**

Run: `cd /cortex/er_build_tools && grep -r "rename_container" bin/ci_tool/`
Expected: No matches

**Step 3: Lint**

Run: `source ~/.helper_bash_functions && cd /cortex/.catkin_ws/src/er_build_tools/bin/ci_tool && python3 -m pylint containers.py --disable=all --enable=E`
Expected: No errors (warnings OK if pre-existing)

**Step 4: Commit**

```bash
git add bin/ci_tool/containers.py
git commit -m "remove unused rename_container from containers.py"
```

---

### Task 2: Rewrite ci_reproduce.py — Python Docker orchestration

**Files:**
- Modify: `bin/ci_tool/ci_reproduce.py` (full rewrite)

**Step 1: Write the new ci_reproduce.py**

Replace the entire file with:

```python
#!/usr/bin/env python3
"""Reproduce CI locally by creating a Docker container."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rich.console import Console
from rich.panel import Panel

from ci_tool.containers import container_exists

console = Console()

DEFAULT_SCRIPTS_BRANCH = "main"
SCRIPTS_CACHE_DIR = "/tmp/er_reproduce_ci"
INTERNAL_REPO = "Extend-Robotics/er_build_tools_internal"
DEFAULT_DOCKER_IMAGE = (
    "rostooling/setup-ros-docker:ubuntu-focal-ros-noetic-desktop-latest"
)

CONTAINER_SIDE_SCRIPTS = [
    "ci_workspace_setup.sh",
    "ci_repull_and_retest.sh",
]


def fetch_internal_script(script_name, gh_token, scripts_branch):
    """Fetch a script from er_build_tools_internal and save to cache dir."""
    url = (
        f"https://raw.githubusercontent.com/{INTERNAL_REPO}"
        f"/refs/heads/{scripts_branch}/bin/{script_name}"
    )
    request = Request(url, headers={"Authorization": f"token {gh_token}"})
    try:
        with urlopen(request, timeout=30) as response:
            content = response.read()
    except HTTPError as error:
        raise RuntimeError(
            f"Failed to fetch {script_name} from {INTERNAL_REPO} "
            f"(branch: {scripts_branch}): HTTP {error.code}"
        ) from error

    cache_dir = Path(SCRIPTS_CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    script_path = cache_dir / script_name
    script_path.write_bytes(content)
    script_path.chmod(0o755)
    return str(script_path)


def fetch_container_side_scripts(gh_token, scripts_branch):
    """Fetch all container-side scripts from er_build_tools_internal."""
    console.print(
        f"[cyan]Fetching CI scripts from {INTERNAL_REPO} "
        f"(branch: {scripts_branch})...[/cyan]"
    )
    script_paths = {}
    for script_name in CONTAINER_SIDE_SCRIPTS:
        path = fetch_internal_script(script_name, gh_token, scripts_branch)
        console.print(f"  [green]\u2713[/green] {script_name}")
        script_paths[script_name] = path
    return script_paths


def validate_deps_repos_reachable(
    repo_url, branch, gh_token, deps_file="deps.repos"
):
    """Validate that deps.repos is reachable at the given branch."""
    repo_path = parse_repo_path(repo_url)
    branch_for_raw = branch or "main"
    deps_url = (
        f"https://raw.githubusercontent.com/{repo_path}"
        f"/{branch_for_raw}/{deps_file}"
    )
    console.print(f"[cyan]Validating {deps_file} is reachable...[/cyan]")
    request = Request(
        deps_url, method="HEAD", headers={"Authorization": f"token {gh_token}"}
    )
    try:
        with urlopen(request, timeout=10):
            pass
    except HTTPError as error:
        raise RuntimeError(
            f"Could not reach {deps_file} at {deps_url} (HTTP {error.code}). "
            f"Check that branch '{branch_for_raw}' exists and "
            f"{deps_file} is present."
        ) from error
    console.print(
        f"  [green]\u2713[/green] {deps_file} reachable "
        f"at branch {branch_for_raw}"
    )


def parse_repo_path(repo_url):
    """Extract 'org/repo' from a GitHub URL."""
    repo_url_clean = repo_url.rstrip("/").removesuffix(".git")
    return repo_url_clean.split("github.com/")[1]


def parse_repo_parts(repo_url):
    """Extract org, repo_name, and cleaned URL from a GitHub URL."""
    repo_url_clean = repo_url.rstrip("/").removesuffix(".git")
    repo_path = repo_url_clean.split("github.com/")[1]
    org, repo_name = repo_path.split("/", 1)
    return org, repo_name, repo_url_clean


def build_docker_create_command(
    container_name,
    script_paths,
    gh_token,
    repo_url_clean,
    repo_name,
    org,
    branch,
    only_needed_deps,
    graphical,
):
    """Build the full docker create command with all args."""
    docker_args = [
        "docker", "create",
        "--name", container_name,
        "--network=host",
        "--ipc=host",
        "-v", f"{script_paths['ci_workspace_setup.sh']}:/tmp/ci_workspace_setup.sh:ro",
        "-v", f"{script_paths['ci_repull_and_retest.sh']}:/tmp/ci_repull_and_retest.sh:ro",
        "-e", f"GH_TOKEN={gh_token}",
        "-e", f"REPO_URL={repo_url_clean}",
        "-e", f"REPO_NAME={repo_name}",
        "-e", f"ORG={org}",
        "-e", "DEPS_FILE=deps.repos",
        "-e", f"BRANCH={branch}",
        "-e", f"ONLY_NEEDED_DEPS={'true' if only_needed_deps else 'false'}",
        "-e", "SKIP_TESTS=false",
        "-e", "ADDITIONAL_COMMAND=",
    ]

    if graphical:
        display = os.environ.get("DISPLAY", "")
        if display:
            console.print("[cyan]Enabling graphical forwarding...[/cyan]")
            subprocess.run(
                ["xhost", "+local:"], check=False, capture_output=True
            )
            docker_args.extend([
                "--runtime", "nvidia",
                "--gpus", "all",
                "--privileged",
                "--security-opt", "seccomp=unconfined",
                "-v", "/tmp/.X11-unix:/tmp/.X11-unix:rw",
                "-e", f"DISPLAY={display}",
                "-e", "QT_X11_NO_MITSHM=1",
                "-e", "NVIDIA_DRIVER_CAPABILITIES=all",
                "-e", "NVIDIA_VISIBLE_DEVICES=all",
            ])

    docker_args.extend([DEFAULT_DOCKER_IMAGE, "sleep", "infinity"])
    return docker_args


def reproduce_ci(
    repo_url,
    branch,
    container_name,
    gh_token,
    only_needed_deps=True,
    scripts_branch=DEFAULT_SCRIPTS_BRANCH,
    graphical=True,
):
    """Create a CI reproduction container.

    Fetches container-side scripts from er_build_tools_internal,
    creates Docker container with proper env/volumes, runs workspace setup.

    Raises RuntimeError if container doesn't exist after execution.
    """
    console.print(Panel("[bold]Reproducing CI Locally[/bold]", expand=False))

    script_paths = fetch_container_side_scripts(gh_token, scripts_branch)
    validate_deps_repos_reachable(repo_url, branch, gh_token)

    org, repo_name, repo_url_clean = parse_repo_parts(repo_url)
    console.print(f"  Organization: {org}")
    console.print(f"  Repository:   {repo_name}")

    create_command = build_docker_create_command(
        container_name, script_paths, gh_token, repo_url_clean,
        repo_name, org, branch, only_needed_deps, graphical,
    )

    console.print(f"\n[cyan]Creating container '{container_name}'...[/cyan]")
    subprocess.run(create_command, check=True)
    console.print(f"  [green]\u2713[/green] Container created")

    subprocess.run(["docker", "start", container_name], check=True)
    console.print(f"  [green]\u2713[/green] Container started")

    console.print("\n[cyan]Running CI workspace setup...[/cyan]")
    workspace_setup_exit_code = 0
    try:
        result = subprocess.run(
            ["docker", "exec", container_name,
             "bash", "/tmp/ci_workspace_setup.sh"],
            check=False,
        )
        workspace_setup_exit_code = result.returncode
    except KeyboardInterrupt:
        console.print(
            "\n[yellow]Interrupted \u2014 continuing with whatever test "
            "output was captured[/yellow]"
        )

    if workspace_setup_exit_code != 0:
        console.print(
            f"\n[yellow]CI workspace setup exited with code "
            f"{workspace_setup_exit_code} "
            f"(expected \u2014 tests likely failed)[/yellow]"
        )

    if not container_exists(container_name):
        raise RuntimeError(
            f"Container '{container_name}' was not created. "
            "Check the output above for errors."
        )
    console.print(
        f"\n[green]\u2713 Container '{container_name}' is ready[/green]"
    )


def prompt_for_reproduce_args():
    """Interactively ask user for reproduce arguments.

    Used by the CLI 'reproduce' subcommand only.
    Returns (repo_url, branch, only_needed_deps).
    """
    from InquirerPy import inquirer

    repo_url = inquirer.text(
        message="Repository URL:",
        validate=lambda url: url.startswith("https://github.com/"),
        invalid_message="Must be a GitHub URL (https://github.com/...)",
    ).execute()

    branch = inquirer.text(
        message="Branch name:",
        validate=lambda b: len(b.strip()) > 0,
        invalid_message="Branch name cannot be empty",
    ).execute()

    only_needed_deps = not inquirer.confirm(
        message="Build everything (slower, disable --only-needed-deps)?",
        default=False,
    ).execute()

    return repo_url, branch, only_needed_deps
```

**Step 2: Lint**

Run: `source ~/.helper_bash_functions && cd /cortex/.catkin_ws/src/er_build_tools/bin/ci_tool && python3 -m pylint ci_reproduce.py --disable=all --enable=E`
Expected: No errors

**Step 3: Commit**

```bash
git add bin/ci_tool/ci_reproduce.py
git commit -m "rewrite ci_reproduce.py with Python Docker orchestration

Replaces the bash wrapper fetch chain with direct Python Docker
orchestration. reproduce_ci() now takes explicit params, fetches
container-side scripts via urllib, and raises on failure."
```

---

### Task 3: Refactor ci_fix.py — consolidated prompting and linear flow

**Files:**
- Modify: `bin/ci_tool/ci_fix.py` (major refactor)

**Step 1: Write the new ci_fix.py**

Replace the entire file. Key changes:
- New `gather_session_info()` consolidates all up-front prompts
- `fix_ci()` is a linear sequence with no scattered prompts
- Removed: `parse_fix_args`, `select_or_create_session`, the args-based path
- Uses new `reproduce_ci` interface with explicit params

```python
#!/usr/bin/env python3
"""Fix CI test failures using Claude Code inside a container."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from urllib.request import Request, urlopen

from InquirerPy import inquirer
from rich.console import Console
from rich.panel import Panel

from ci_tool.claude_setup import (
    copy_ci_context,
    copy_claude_credentials,
    copy_display_script,
    inject_rerun_tests_function,
    inject_resume_function,
    is_claude_installed_in_container,
    save_package_list,
    setup_claude_in_container,
)
from ci_tool.containers import (
    container_exists,
    container_is_running,
    docker_exec,
    docker_exec_interactive,
    list_ci_containers,
    remove_container,
    sanitize_container_name,
    start_container,
)
from ci_tool.ci_reproduce import reproduce_ci
from ci_tool.preflight import run_all_preflight_checks, PreflightError

console = Console()

SUMMARY_FORMAT = (
    "When done, print EXACTLY this format:\n\n"
    "--- SUMMARY ---\n"
    "Problem: <what was wrong>\n"
    "Fix: <what you changed>\n"
    "Assumptions: <any assumptions you made, or 'None'>\n\n"
    "--- COMMIT MESSAGE ---\n"
    "<brief concise commit message, no leading/trailing whitespace>\n"
    "--- END ---"
)

ROS_SOURCE_PREAMBLE = (
    "You are inside a CI reproduction container at /ros_ws. "
    "Source the ROS workspace: "
    "`source /opt/ros/noetic/setup.bash && source /ros_ws/install/setup.bash`."
    "\n\n"
)

ANALYSIS_PROMPT_TEMPLATE = (
    ROS_SOURCE_PREAMBLE
    + "The CI tests have already been run. Analyse the failures:\n"
    "1. Examine the test output in /ros_ws/test_output.log\n"
    "2. For each failing test, report:\n"
    "   - Package and test name\n"
    "   - The error/assertion message\n"
    "   - Your hypothesis for the root cause\n"
    "3. Suggest a fix strategy for each failure\n\n"
    "Do NOT make any code changes. Only analyse and report.\n"
    "{extra_context}"
)

CI_COMPARE_EXTRA_CONTEXT_TEMPLATE = (
    "\nAlso investigate the CI run: {ci_run_url}\n"
    "- Verify local and CI are on the same commit:\n"
    "  - Local: check HEAD in the repo under /ros_ws/src/\n"
    "  - CI: `gh api repos/{owner_repo}/actions/runs/{run_id}"
    " --jq '.head_sha'`\n"
    "  - If they differ, determine whether the missing/extra commits "
    "explain the failure\n"
    "- Fetch CI logs: `gh run view {run_id} --log-failed` "
    "(use `--log` for full output if needed)\n"
    "- Compare CI failures with local test results\n"
)

FIX_PROMPT_TEMPLATE = (
    "The user has reviewed your analysis. Their feedback:\n"
    "{user_feedback}\n\n"
    "Now fix the CI failures based on this understanding.\n"
    "Rebuild the affected packages and re-run the failing tests to verify.\n"
    "Iterate until all tests pass.\n\n"
    + SUMMARY_FORMAT
)

FIX_MODE_CHOICES = [
    {"name": "Fix CI failures (from test_output.log)", "value": "fix_from_log"},
    {"name": "Compare with GitHub Actions CI run", "value": "compare_ci_run"},
    {"name": "Custom prompt", "value": "custom"},
]

CLAUDE_STDERR_LOG = "/ros_ws/.claude_stderr.log"


def read_container_state(container_name):
    """Read the ci_fix state file from a container. Returns dict or None."""
    result = subprocess.run(
        ["docker", "exec", container_name,
         "cat", "/ros_ws/.ci_fix_state.json"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def extract_run_id_from_url(ci_run_url):
    """Extract the numeric run ID from a GitHub Actions URL.

    Handles URLs like:
      https://github.com/org/repo/actions/runs/12345678901
      https://github.com/org/repo/actions/runs/12345678901/job/98765
    """
    parts = ci_run_url.rstrip("/").split("/runs/")
    if len(parts) < 2:
        raise ValueError(f"Cannot extract run ID from URL: {ci_run_url}")
    run_id = parts[1].split("/")[0]
    if not run_id.isdigit():
        raise ValueError(f"Run ID is not numeric: {run_id}")
    return run_id


def extract_info_from_ci_url(ci_run_url):
    """Extract repo URL, branch, and run ID from a GitHub Actions URL."""
    run_id = extract_run_id_from_url(ci_run_url)

    owner_repo = ci_run_url.split("github.com/")[1].split("/actions/")[0]
    repo_url = f"https://github.com/{owner_repo}"

    token = os.environ.get("GH_TOKEN") or os.environ.get("ER_SETUP_TOKEN")
    if not token:
        raise ValueError(
            "No GitHub token found (GH_TOKEN or ER_SETUP_TOKEN)"
        )

    api_url = (
        f"https://api.github.com/repos/{owner_repo}/actions/runs/{run_id}"
    )
    request = Request(api_url, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    })
    with urlopen(request) as response:
        data = json.loads(response.read())

    return {
        "repo_url": repo_url,
        "owner_repo": owner_repo,
        "branch": data["head_branch"],
        "run_id": run_id,
        "ci_run_url": ci_run_url,
    }


def prompt_for_session_name(branch_hint=None):
    """Ask user for a session name. Returns full container name (er_ci_<name>).

    Exits if the container already exists.
    """
    default = sanitize_container_name(branch_hint) if branch_hint else ""
    name = inquirer.text(
        message="Session name (used for container naming):",
        default=default,
        validate=lambda n: len(n.strip()) > 0,
        invalid_message="Session name cannot be empty",
    ).execute().strip()

    container_name = f"er_ci_{sanitize_container_name(name)}"

    if container_exists(container_name):
        console.print(
            f"[red]Container '{container_name}' already exists. "
            f"Choose a different name or clean up first.[/red]"
        )
        sys.exit(1)

    return container_name


def gather_session_info():
    """Collect all session information up front via interactive prompts.

    Returns a dict with 'mode' key:
      - mode='new': container_name, repo_url, branch, only_needed_deps,
                     ci_run_info (or None)
      - mode='resume': container_name, resume_session_id (or None)
    """
    existing = list_ci_containers()

    if existing:
        choices = [{"name": "Start new session", "value": "_new"}]
        for container in existing:
            choices.append({
                "name": (
                    f"Resume '{container['name']}' ({container['status']})"
                ),
                "value": container["name"],
            })

        selection = inquirer.select(
            message="Select a session:",
            choices=choices,
        ).execute()

        if selection != "_new":
            if not container_is_running(selection):
                start_container(selection)

            resume_session_id = None
            state = read_container_state(selection)
            if state and state.get("session_id"):
                session_id = state["session_id"]
                phase = state.get("phase", "unknown")
                attempt = state.get("attempt_count", 0)
                console.print(
                    f"  [dim]Previous session: {phase} "
                    f"(attempt {attempt}, id: {session_id})[/dim]"
                )

                resume_choice = inquirer.select(
                    message=(
                        "Resume previous Claude session or start fresh?"
                    ),
                    choices=[
                        {
                            "name": f"Resume session ({phase})",
                            "value": "resume",
                        },
                        {
                            "name": "Start fresh fix attempt",
                            "value": "fresh",
                        },
                    ],
                ).execute()

                if resume_choice == "resume":
                    resume_session_id = session_id

            return {
                "mode": "resume",
                "container_name": selection,
                "resume_session_id": resume_session_id,
            }

    # New session: collect all info
    ci_run_info = None
    ci_run_url = inquirer.text(
        message="GitHub Actions run URL (leave blank to skip):",
        default="",
    ).execute().strip()

    if ci_run_url:
        ci_run_info = extract_info_from_ci_url(ci_run_url)
        repo_url = ci_run_info["repo_url"]
        branch = ci_run_info["branch"]
        console.print(f"  [green]Repo:[/green] {repo_url}")
        console.print(f"  [green]Branch:[/green] {branch}")
        console.print(f"  [green]Run ID:[/green] {ci_run_info['run_id']}")
    else:
        repo_url = inquirer.text(
            message="Repository URL:",
            validate=lambda url: url.startswith("https://github.com/"),
            invalid_message="Must be a GitHub URL (https://github.com/...)",
        ).execute()

        branch = inquirer.text(
            message="Branch name:",
            validate=lambda b: len(b.strip()) > 0,
            invalid_message="Branch name cannot be empty",
        ).execute()

    only_needed_deps = not inquirer.confirm(
        message="Build everything (slower, disable --only-needed-deps)?",
        default=False,
    ).execute()

    container_name = prompt_for_session_name(branch if branch else None)

    return {
        "mode": "new",
        "container_name": container_name,
        "repo_url": repo_url,
        "branch": branch,
        "only_needed_deps": only_needed_deps,
        "ci_run_info": ci_run_info,
    }


def select_fix_mode():
    """Let the user choose how Claude should fix CI failures.

    Returns (ci_run_info_or_none, custom_prompt_or_none).
    """
    mode = inquirer.select(
        message="How should Claude fix CI?",
        choices=FIX_MODE_CHOICES,
        default="fix_from_log",
    ).execute()

    if mode == "fix_from_log":
        return None, None

    if mode == "compare_ci_run":
        ci_run_url = inquirer.text(
            message="GitHub Actions run URL:",
            validate=lambda url: "/runs/" in url,
            invalid_message=(
                "URL must contain /runs/ "
                "(e.g. https://github.com/org/repo/actions/runs/12345)"
            ),
        ).execute()
        return extract_info_from_ci_url(ci_run_url), None

    custom_prompt = inquirer.text(
        message="Enter your custom prompt for Claude:"
    ).execute()
    return None, custom_prompt


def build_analysis_prompt(ci_run_info):
    """Build the analysis prompt, optionally including CI compare context."""
    if ci_run_info:
        extra_context = CI_COMPARE_EXTRA_CONTEXT_TEMPLATE.format(
            **ci_run_info
        )
    else:
        extra_context = ""
    return ANALYSIS_PROMPT_TEMPLATE.format(extra_context=extra_context)


def run_claude_streamed(container_name, prompt):
    """Run Claude non-interactively with stream-json output."""
    escaped_prompt = prompt.replace("'", "'\\''")
    claude_command = (
        f"cd /ros_ws && IS_SANDBOX=1 claude --dangerously-skip-permissions "
        f"-p '{escaped_prompt}' --verbose --output-format stream-json "
        f"2>{CLAUDE_STDERR_LOG} | ci_fix_display"
    )
    docker_exec(container_name, claude_command, check=False)


def run_claude_resumed(container_name, session_id, prompt):
    """Resume a Claude session with a new prompt, streaming output."""
    escaped_prompt = prompt.replace("'", "'\\''")
    claude_command = (
        f"cd /ros_ws && IS_SANDBOX=1 claude --dangerously-skip-permissions "
        f"--resume '{session_id}' -p '{escaped_prompt}' "
        f"--verbose --output-format stream-json "
        f"2>{CLAUDE_STDERR_LOG} | ci_fix_display"
    )
    docker_exec(container_name, claude_command, check=False)


def prompt_user_for_feedback():
    """Ask user to review Claude's analysis and provide corrections."""
    feedback = inquirer.text(
        message=(
            "Review the analysis above. "
            "Provide corrections or context (Enter to accept as-is):"
        ),
        default="",
    ).execute().strip()
    if not feedback:
        return "Analysis looks correct, proceed with fixing."
    return feedback


def refresh_claude_config(container_name):
    """Refresh Claude config in an existing container."""
    console.print(
        "[green]Claude already installed \u2014 refreshing config...[/green]"
    )
    copy_claude_credentials(container_name)
    copy_ci_context(container_name)
    copy_display_script(container_name)
    inject_resume_function(container_name)
    inject_rerun_tests_function(container_name)


def run_claude_workflow(container_name, ci_run_info):
    """Run the Claude analysis -> feedback -> fix workflow."""
    if ci_run_info:
        custom_prompt = None
    else:
        ci_run_info, custom_prompt = select_fix_mode()

    if custom_prompt:
        console.print(
            "\n[bold cyan]Launching Claude Code (custom prompt)...[/bold cyan]"
        )
        run_claude_streamed(container_name, custom_prompt)
    else:
        # Analysis phase
        analysis_prompt = build_analysis_prompt(ci_run_info)
        console.print(
            "\n[bold cyan]Launching Claude Code "
            "\u2014 analysis phase...[/bold cyan]"
        )
        console.print(
            "[dim]Claude will analyse failures before "
            "attempting fixes[/dim]\n"
        )
        run_claude_streamed(container_name, analysis_prompt)

        # User review
        console.print()
        user_feedback = prompt_user_for_feedback()

        # Fix phase (resume session)
        state = read_container_state(container_name)
        session_id = state["session_id"] if state else None
        if session_id:
            console.print(
                "\n[bold cyan]Resuming Claude "
                "\u2014 fix phase...[/bold cyan]"
            )
            console.print(
                "[dim]Claude will now fix the failures[/dim]\n"
            )
            fix_prompt = FIX_PROMPT_TEMPLATE.format(
                user_feedback=user_feedback
            )
            run_claude_resumed(container_name, session_id, fix_prompt)
        else:
            console.print(
                "\n[yellow]No session ID from analysis phase \u2014 "
                "cannot resume. Dropping to shell.[/yellow]"
            )

    # Show outcome
    state = read_container_state(container_name)
    if state:
        phase = state.get("phase", "unknown")
        session_id = state.get("session_id")
        attempt = state.get("attempt_count", 1)
        console.print(
            f"\n[bold]Claude finished \u2014 "
            f"phase: {phase}, attempt: {attempt}[/bold]"
        )
        if session_id:
            console.print(f"[dim]Session ID: {session_id}[/dim]")
    else:
        console.print(
            "\n[yellow]Could not read state file from container[/yellow]"
        )


def drop_to_shell(container_name):
    """Drop user into an interactive container shell."""
    console.print("\n[bold green]Dropping into container shell.[/bold green]")
    console.print("[cyan]Useful commands:[/cyan]")
    console.print(
        "  [bold]rerun_tests[/bold]    "
        "\u2014 rebuild and re-run CI tests locally"
    )
    console.print(
        "  [bold]resume_claude[/bold]  "
        "\u2014 resume the Claude session interactively"
    )
    console.print("  [bold]git diff[/bold]        \u2014 review changes")
    console.print(
        "  [bold]git add && git commit[/bold] \u2014 commit fixes"
    )
    console.print("  [dim]Repo is at /ros_ws/src/<repo_name>[/dim]\n")
    docker_exec_interactive(container_name)


def fix_ci(args):
    """Main fix workflow: gather -> preflight -> reproduce -> Claude -> shell.

    Args are accepted for backward compat but ignored (interactive only).
    """
    console.print(
        Panel("[bold cyan]CI Fix with Claude[/bold cyan]", expand=False)
    )

    # Step 1: Gather all session info up front
    session = gather_session_info()
    container_name = session["container_name"]

    if session["mode"] == "new":
        # Step 2: Preflight checks
        try:
            gh_token = run_all_preflight_checks(
                repo_url=session["repo_url"]
            )
        except PreflightError as error:
            console.print(
                f"\n[bold red]Preflight failed:[/bold red] {error}"
            )
            sys.exit(1)

        # Step 3: Reproduce CI in container
        if container_exists(container_name):
            remove_container(container_name)
        reproduce_ci(
            repo_url=session["repo_url"],
            branch=session["branch"],
            container_name=container_name,
            gh_token=gh_token,
            only_needed_deps=session["only_needed_deps"],
        )
        save_package_list(container_name)

    # Step 4: Setup Claude in container
    if is_claude_installed_in_container(container_name):
        refresh_claude_config(container_name)
    else:
        setup_claude_in_container(container_name)

    # Step 5: Run Claude
    resume_session_id = session.get("resume_session_id")
    if resume_session_id:
        console.print(
            "\n[bold cyan]Resuming Claude session...[/bold cyan]"
        )
        console.print(
            "[dim]You are now in an interactive Claude session[/dim]\n"
        )
        docker_exec(
            container_name,
            "cd /ros_ws && IS_SANDBOX=1 claude "
            "--dangerously-skip-permissions "
            f'--resume "{resume_session_id}"',
            interactive=True, check=False,
        )
    else:
        run_claude_workflow(
            container_name, session.get("ci_run_info")
        )

    # Step 6: Drop to shell
    drop_to_shell(container_name)
```

**Step 2: Lint**

Run: `source ~/.helper_bash_functions && cd /cortex/.catkin_ws/src/er_build_tools/bin/ci_tool && python3 -m pylint ci_fix.py --disable=all --enable=E`
Expected: No errors

**Step 3: Commit**

```bash
git add bin/ci_tool/ci_fix.py
git commit -m "refactor ci_fix.py: consolidated prompting and linear flow

gather_session_info() collects all user input up front. fix_ci() is
now a linear sequence: gather -> preflight -> reproduce -> claude -> shell.
Fixes the missing repo/branch prompt when CI URL is left blank."
```

---

### Task 4: Adapt cli.py for new reproduce_ci interface

**Files:**
- Modify: `bin/ci_tool/cli.py`

**Step 1: Update `_handle_reproduce`**

The `reproduce` CLI subcommand needs a thin adapter since `reproduce_ci` now takes explicit params. Replace `_handle_reproduce` (and add necessary imports):

In `cli.py`, replace the `_handle_reproduce` function (lines 62-64):

```python
def _handle_reproduce(args):
    import os
    from ci_tool.ci_reproduce import (
        reproduce_ci,
        prompt_for_reproduce_args,
        DEFAULT_SCRIPTS_BRANCH,
    )
    from ci_tool.containers import (
        DEFAULT_CONTAINER_NAME,
        container_exists,
        container_is_running,
        remove_container,
    )
    from ci_tool.preflight import (
        validate_docker_available,
        validate_gh_token,
        PreflightError,
    )

    try:
        validate_docker_available()
    except PreflightError as error:
        console.print(f"\n[bold red]Preflight failed:[/bold red] {error}")
        sys.exit(1)

    repo_url, branch, only_needed_deps = prompt_for_reproduce_args()
    container_name = DEFAULT_CONTAINER_NAME

    if container_exists(container_name):
        from InquirerPy import inquirer
        action = inquirer.select(
            message=f"Container '{container_name}' already exists. What to do?",
            choices=[
                {"name": "Remove and recreate", "value": "recreate"},
                {"name": "Keep existing (skip creation)", "value": "keep"},
                {"name": "Cancel", "value": "cancel"},
            ],
        ).execute()

        if action == "cancel":
            return
        if action == "recreate":
            remove_container(container_name)
        if action == "keep":
            if not container_is_running(container_name):
                import subprocess
                subprocess.run(
                    ["docker", "start", container_name], check=True
                )
            console.print(
                f"[green]Using existing container "
                f"'{container_name}'[/green]"
            )
            return

    try:
        gh_token = validate_gh_token(repo_url=repo_url)
    except PreflightError as error:
        console.print(f"\n[bold red]Preflight failed:[/bold red] {error}")
        sys.exit(1)

    reproduce_ci(
        repo_url=repo_url,
        branch=branch,
        container_name=container_name,
        gh_token=gh_token,
        only_needed_deps=only_needed_deps,
    )
```

**Step 2: Lint**

Run: `source ~/.helper_bash_functions && cd /cortex/.catkin_ws/src/er_build_tools/bin/ci_tool && python3 -m pylint cli.py --disable=all --enable=E`
Expected: No errors

**Step 3: Commit**

```bash
git add bin/ci_tool/cli.py
git commit -m "adapt cli.py reproduce handler for new reproduce_ci interface"
```

---

### Task 5: Update setup.sh — install ci_tool and hand-off

**Files:**
- Modify: `bin/setup.sh`

**Step 1: Add ci_tool install step and hand-off**

After the existing Step 4 (Claude Code authentication) and before the "Done" section, add a new step 5 that installs the ci_tool Python package. Then change the "Done" section to exec into ci_tool.

After line 118 (`fi` closing the Claude auth block), add:

```bash
# --- Step 5: Install ci_tool ---

echo ""
echo -e "${Bold}[5/5] Installing ci_tool...${Color_Off}"

CI_TOOL_DIR="${HOME}/.ci_tool"
CI_TOOL_URL="${BASE_URL}/bin/ci_tool"

if [ -d "${CI_TOOL_DIR}/ci_tool" ]; then
    echo -e "  ${Green}ci_tool already installed at ${CI_TOOL_DIR}${Color_Off}"
    echo -e "  ${Cyan}Updating...${Color_Off}"
fi

mkdir -p "${CI_TOOL_DIR}"

# Download ci_tool package files
CI_TOOL_FILES=(
    "__init__.py"
    "__main__.py"
    "cli.py"
    "ci_fix.py"
    "ci_reproduce.py"
    "claude_setup.py"
    "claude_session.py"
    "containers.py"
    "preflight.py"
    "display_progress.py"
    "requirements.txt"
)

mkdir -p "${CI_TOOL_DIR}/ci_tool/ci_context"
for file in "${CI_TOOL_FILES[@]}"; do
    curl -fsSL "${CI_TOOL_URL}/${file}" -o "${CI_TOOL_DIR}/ci_tool/${file}" || {
        echo -e "  ${Red}Failed to download ${file}${Color_Off}"
        exit 1
    }
done

# Download CI context CLAUDE.md
curl -fsSL "${CI_TOOL_URL}/ci_context/CLAUDE.md" \
    -o "${CI_TOOL_DIR}/ci_tool/ci_context/CLAUDE.md" 2>/dev/null || true

# Install Python dependencies
pip3 install --user --quiet -r "${CI_TOOL_DIR}/ci_tool/requirements.txt" 2>/dev/null || {
    echo -e "  ${Yellow}Some dependencies may not have installed. ci_tool will retry on first run.${Color_Off}"
}

echo -e "  ${Green}ci_tool installed at ${CI_TOOL_DIR}${Color_Off}"
```

Then update the "Done" section to hand off:

```bash
# --- Done ---

echo ""
echo -e "${Bold}${Green}Setup complete!${Color_Off}"
echo ""
echo -e "  Reload your shell or run:"
echo -e "    ${Bold}source ~/.helper_bash_functions${Color_Off}"
echo ""

# Source helper functions so GH_TOKEN is available for ci_tool
source "${HELPER_PATH}" 2>/dev/null || true

echo -e "  ${Bold}${Cyan}Launching ci_tool...${Color_Off}"
echo ""
exec python3 "${CI_TOOL_DIR}/ci_tool/__main__.py"
```

**Step 2: Update step numbering**

Change step header counts from `[1/4]`, `[2/4]`, `[3/4]`, `[4/4]` to `[1/5]`, `[2/5]`, `[3/5]`, `[4/5]`.

**Step 3: Commit**

```bash
git add bin/setup.sh
git commit -m "setup.sh: install ci_tool and hand off after setup"
```

---

### Task 6: Lint all changed files

**Files:**
- All modified Python files

**Step 1: Run linters on all changed files**

Run: `source ~/.helper_bash_functions && cd /cortex/.catkin_ws/src/er_build_tools/bin/ci_tool && er_python_linters_here`
Expected: No errors or warnings. Fix any that appear.

**Step 2: Commit lint fixes if any**

```bash
git add -A bin/ci_tool/
git commit -m "fix lint issues from rearchitect"
```

---

### Task 7: Manual integration test

**Step 1: Test new session without CI URL**

Run: `python3 -m ci_tool`
Select: "Fix CI with Claude"
Leave CI URL blank.
Expected: Prompted for "Repository URL:" and "Branch name:" (the core bug fix).
Enter valid repo/branch. Verify preflight runs with repo validation, container is created, workspace setup runs.

**Step 2: Test new session with CI URL**

Run: `python3 -m ci_tool`
Select: "Fix CI with Claude"
Enter a valid GitHub Actions URL.
Expected: Repo/branch auto-extracted, not prompted again. Preflight validates repo. Full flow works.

**Step 3: Test resume existing session**

Run: `python3 -m ci_tool` (with existing container from previous test)
Select existing container from resume menu.
Expected: Container starts, Claude resume/fresh choice shown, flow continues without reproduction step.

**Step 4: Test standalone reproduce**

Run: `python3 -m ci_tool`
Select: "Reproduce CI (create container)"
Expected: Prompted for repo, branch, only-needed-deps. Container created via Python Docker orchestration (no bash wrapper chain).

**Step 5: Test failure guard**

Intentionally provide a bad repo URL (e.g. `https://github.com/fake/nonexistent`).
Expected: Preflight fails with clear error about repo access. Tool exits cleanly, no cascade of "No such container" errors.
