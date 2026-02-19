#!/usr/bin/env python3
"""Fix CI test failures using Claude Code inside a container."""
# pylint: disable=duplicate-code  # shared imports with ci_reproduce.py
from __future__ import annotations

import json
import os
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from InquirerPy import inquirer
from rich.console import Console
from rich.panel import Panel

from ci_tool.claude_setup import (
    copy_ci_context,
    copy_claude_credentials,
    copy_display_script,
    copy_learnings_from_container,
    copy_learnings_to_container,
    inject_colcon_wrappers,
    inject_rerun_tests_function,
    inject_resume_function,
    is_claude_installed_in_container,
    save_package_list,
    seed_claude_state,
    setup_claude_in_container,
)
from ci_tool.ci_reproduce import (
    _parse_repo_url,
    prompt_for_reproduce_args,
    reproduce_ci,
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
    "1. Use Grep to search /ros_ws/test_output.log for FAILURE, FAILED, ERROR, "
    "and assertion messages. Do NOT read the entire file.\n"
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
    "- Fetch CI logs: `gh run view {run_id} --log-failed 2>&1 | tail -200` "
    "(increase if needed, but avoid dumping full logs)\n"
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
        console.print(
            f"[yellow]State file exists but contains invalid JSON: "
            f"{result.stdout[:200]}[/yellow]"
        )
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

    if "github.com/" not in ci_run_url or "/actions/" not in ci_run_url:
        raise ValueError(f"Not a valid GitHub Actions URL: {ci_run_url}")

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
    try:
        with urlopen(request, timeout=15) as response:
            data = json.loads(response.read())
    except HTTPError as error:
        raise RuntimeError(
            f"Failed to fetch run info for {owner_repo} "
            f"run {run_id} (HTTP {error.code})"
        ) from error
    except URLError as error:
        raise RuntimeError(
            f"Cannot reach GitHub API: {error.reason}"
        ) from error

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
        message="Session name (container will be er_ci_<name>):",
        default=default,
        validate=lambda n: len(n.strip()) > 0,
        invalid_message="Session name cannot be empty",
    ).execute().strip()

    container_name = f"er_ci_{sanitize_container_name(name)}"
    console.print(f"  Container name: [cyan]{container_name}[/cyan]")

    if container_exists(container_name):
        console.print(
            f"[red]Container '{container_name}' already exists. "
            f"Choose a different name or clean up first.[/red]"
        )
        sys.exit(1)

    return container_name


def _prompt_resume_session(container_name):
    """Prompt user to resume or start fresh in an existing container.

    Returns a resume dict for gather_session_info().
    """
    if not container_is_running(container_name):
        start_container(container_name)

    resume_session_id = None
    state = read_container_state(container_name)
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
        "container_name": container_name,
        "resume_session_id": resume_session_id,
    }


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
            return _prompt_resume_session(selection)

    # New session: collect all info up front
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
        only_needed_deps = not inquirer.confirm(
            message="Build everything (slower, disable --only-needed-deps)?",
            default=False,
        ).execute()
    else:
        repo_url, branch, only_needed_deps = prompt_for_reproduce_args()

    container_name = prompt_for_session_name(branch)

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
        f"set -o pipefail && "
        f"cd /ros_ws && IS_SANDBOX=1 claude --dangerously-skip-permissions "
        f"-p '{escaped_prompt}' --verbose --output-format stream-json "
        f"2>{CLAUDE_STDERR_LOG} | ci_fix_display"
    )
    result = docker_exec(container_name, claude_command, tty=True, check=False, quiet=True)
    if result.returncode != 0:
        console.print(
            f"[yellow]Claude exited with code {result.returncode} — "
            f"check {CLAUDE_STDERR_LOG} inside the container for details[/yellow]"
        )


def run_claude_resumed(container_name, session_id, prompt):
    """Resume a Claude session with a new prompt, streaming output."""
    escaped_prompt = prompt.replace("'", "'\\''")
    claude_command = (
        f"set -o pipefail && "
        f"cd /ros_ws && IS_SANDBOX=1 claude --dangerously-skip-permissions "
        f"--resume '{session_id}' -p '{escaped_prompt}' "
        f"--verbose --output-format stream-json "
        f"2>{CLAUDE_STDERR_LOG} | ci_fix_display"
    )
    result = docker_exec(container_name, claude_command, tty=True, check=False, quiet=True)
    if result.returncode != 0:
        console.print(
            f"[yellow]Claude exited with code {result.returncode} — "
            f"check {CLAUDE_STDERR_LOG} inside the container for details[/yellow]"
        )


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
        "[green]Claude already installed — refreshing config...[/green]"
    )
    copy_claude_credentials(container_name)
    copy_ci_context(container_name)
    copy_display_script(container_name)
    inject_resume_function(container_name)
    inject_rerun_tests_function(container_name)
    inject_colcon_wrappers(container_name)
    seed_claude_state(container_name)


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
            "— analysis phase...[/bold cyan]"
        )
        console.print(
            "[dim]Claude will analyse failures before "
            "attempting fixes[/dim]\n"
        )
        run_claude_streamed(container_name, analysis_prompt)

        # User review
        console.print()
        try:
            user_feedback = prompt_user_for_feedback()
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted — skipping fix phase.[/yellow]")
            return

        # Fix phase (resume session)
        state = read_container_state(container_name)
        session_id = state.get("session_id") if state else None
        if session_id:
            console.print(
                "\n[bold cyan]Resuming Claude "
                "— fix phase...[/bold cyan]"
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
                "\n[yellow]No session ID from analysis phase — "
                "cannot resume. Dropping to shell.[/yellow]"
            )

    # Show outcome
    state = read_container_state(container_name)
    if state:
        phase = state.get("phase", "unknown")
        session_id = state.get("session_id")
        attempt = state.get("attempt_count", 1)
        console.print(
            f"\n[bold]Claude finished — "
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
        "— rebuild and re-run CI tests locally"
    )
    console.print(
        "  [bold]resume_claude[/bold]  "
        "— resume the Claude session interactively"
    )
    console.print("  [bold]git diff[/bold]        — review changes")
    console.print(
        "  [bold]git add && git commit[/bold] — commit fixes"
    )
    console.print("  [dim]Repo is at /ros_ws/src/<repo_name>[/dim]\n")
    docker_exec_interactive(container_name)


def _read_container_env(container_name, var_name):
    """Read an environment variable from a running container."""
    result = subprocess.run(
        ["docker", "exec", container_name, "printenv", var_name],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _resolve_org_repo(session, container_name):
    """Resolve (org, repo_name) from session info or container env vars."""
    repo_url = session.get("repo_url")
    if repo_url:
        org, repo_name, _ = _parse_repo_url(repo_url)
        return org, repo_name

    org = _read_container_env(container_name, "ORG")
    repo_name = _read_container_env(container_name, "REPO_NAME")
    return org, repo_name


def fix_ci(_args):
    """Main fix workflow: gather -> preflight -> reproduce -> Claude -> shell.

    _args is accepted for backward compat but ignored (interactive only).
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

    # Step 4b: Copy learnings into container
    org, repo_name = _resolve_org_repo(session, container_name)
    if org and repo_name:
        copy_learnings_to_container(container_name, org, repo_name)

    # Step 5: Run Claude
    resume_session_id = session.get("resume_session_id")
    try:
        if resume_session_id:
            console.print(
                "\n[bold cyan]Resuming Claude session...[/bold cyan]"
            )
            console.print(
                "[dim]You are now in an interactive Claude session[/dim]\n"
            )
            result = docker_exec(
                container_name,
                "cd /ros_ws && IS_SANDBOX=1 claude "
                "--dangerously-skip-permissions "
                f'--resume "{resume_session_id}"',
                interactive=True, check=False,
            )
            if result.returncode != 0:
                console.print(
                    f"[yellow]Claude exited with code {result.returncode}[/yellow]"
                )
        else:
            run_claude_workflow(
                container_name, session.get("ci_run_info")
            )
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")

    # Step 6: Save learnings from container back to host
    if org and repo_name:
        copy_learnings_from_container(container_name, org, repo_name)

    # Step 7: Drop to shell
    drop_to_shell(container_name)
