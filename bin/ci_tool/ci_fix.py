#!/usr/bin/env python3
"""Fix CI test failures using Claude Code inside a container."""
from __future__ import annotations

import json
import os
import sys
from urllib.request import Request, urlopen

from InquirerPy import inquirer
from rich.console import Console
from rich.panel import Panel

from ci_tool.claude_setup import setup_claude_in_container
from ci_tool.containers import (
    DEFAULT_CONTAINER_NAME,
    container_exists,
    container_is_running,
    docker_exec,
    docker_exec_interactive,
    list_ci_containers,
    remove_container,
    rename_container,
    sanitize_container_name,
    start_container,
)
from ci_tool.ci_reproduce import (
    reproduce_ci,
    extract_branch_from_args,
    extract_repo_url_from_args,
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
    "`source /opt/ros/noetic/setup.bash && source /ros_ws/install/setup.bash`.\n\n"
)

FIX_FROM_LOG_PROMPT = (
    ROS_SOURCE_PREAMBLE
    + "The CI tests have already been run. Your job:\n"
    "1. Examine the test output in /ros_ws/test_output.log to identify failures\n"
    "2. Find and fix the root cause in the source code under /ros_ws/src/\n"
    "3. Rebuild the affected packages\n"
    "4. Re-run the failing tests to verify your fix\n"
    "5. Iterate until all tests pass\n\n"
    + SUMMARY_FORMAT
)

CI_RUN_COMPARE_PROMPT_TEMPLATE = (
    ROS_SOURCE_PREAMBLE
    + "Investigate CI failure: {ci_run_url}\n\n"
    "1. Verify local and CI are on the same commit:\n"
    "   - Local: check HEAD in the repo under /ros_ws/src/\n"
    "   - CI: `gh api repos/{owner_repo}/actions/runs/{run_id} --jq '.head_sha'`\n"
    "   - If they differ, determine whether the missing/extra commits explain the failure\n\n"
    "2. Fetch CI logs: `gh run view {run_id} --log-failed` "
    "(use `--log` for full output if needed)\n\n"
    "3. Run the same tests locally and compare:\n"
    "   - Both fail identically: fix the underlying bug\n"
    "   - CI fails but local passes: investigate environment differences "
    "(timing, deps, config)\n"
    "   - Local fails but CI passes: check for local setup issues\n\n"
    "4. Fix the root cause and re-run tests to verify\n\n"
    + SUMMARY_FORMAT
)

FIX_MODE_CHOICES = [
    {"name": "Fix CI failures (from test_output.log)", "value": "fix_from_log"},
    {"name": "Compare with GitHub Actions CI run", "value": "compare_ci_run"},
    {"name": "Custom prompt", "value": "custom"},
]


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
    """Extract repo URL, branch, and run ID from a GitHub Actions run URL via the API."""
    run_id = extract_run_id_from_url(ci_run_url)

    owner_repo = ci_run_url.split("github.com/")[1].split("/actions/")[0]
    repo_url = f"https://github.com/{owner_repo}"

    token = os.environ.get("GH_TOKEN") or os.environ.get("ER_SETUP_TOKEN")
    if not token:
        raise ValueError("No GitHub token found (GH_TOKEN or ER_SETUP_TOKEN)")

    api_url = f"https://api.github.com/repos/{owner_repo}/actions/runs/{run_id}"
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


def select_fix_mode():
    """Let the user choose how Claude should fix CI failures."""
    mode = inquirer.select(
        message="How should Claude fix CI?",
        choices=FIX_MODE_CHOICES,
        default="fix_from_log",
    ).execute()

    if mode == "fix_from_log":
        return FIX_FROM_LOG_PROMPT

    if mode == "compare_ci_run":
        ci_run_url = inquirer.text(
            message="GitHub Actions run URL:",
            validate=lambda url: "/runs/" in url,
            invalid_message="URL must contain /runs/ (e.g. https://github.com/org/repo/actions/runs/12345)",
        ).execute()
        ci_run_info = extract_info_from_ci_url(ci_run_url)
        return CI_RUN_COMPARE_PROMPT_TEMPLATE.format(**ci_run_info)

    return inquirer.text(message="Enter your custom prompt for Claude:").execute()


def parse_fix_args(args):
    """Parse fix-specific arguments, separating them from reproduce args."""
    parsed = {
        "container_name": DEFAULT_CONTAINER_NAME,
        "reproduce_args": [],
    }

    i = 0
    while i < len(args):
        if args[i] in ("--container-name", "-n") and i + 1 < len(args):
            parsed["container_name"] = args[i + 1]
            i += 2
        else:
            parsed["reproduce_args"].append(args[i])
            i += 1

    return parsed


def prompt_for_session_name(branch_hint=None):
    """Ask the user for a session name. Returns full container name (er_ci_<name>)."""
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


def select_or_create_session(parsed):
    """Let user resume an existing session or start a new one.

    Returns (container_name, ci_run_info, needs_reproduce).
    Mutates parsed["reproduce_args"] if CI URL is provided.
    """
    existing = list_ci_containers()

    if existing:
        choices = [{"name": "Start new session", "value": "_new"}]
        for container in existing:
            choices.append({
                "name": f"Resume '{container['name']}' ({container['status']})",
                "value": container["name"],
            })

        selection = inquirer.select(
            message="Select a session:",
            choices=choices,
        ).execute()

        if selection != "_new":
            if not container_is_running(selection):
                start_container(selection)
            return selection, None, False

    # New session: ask for optional CI URL
    ci_run_info = None
    ci_run_url = inquirer.text(
        message="GitHub Actions run URL (leave blank to skip):",
        default="",
    ).execute().strip()

    if ci_run_url:
        ci_run_info = extract_info_from_ci_url(ci_run_url)
        console.print(f"  [green]Repo:[/green] {ci_run_info['repo_url']}")
        console.print(f"  [green]Branch:[/green] {ci_run_info['branch']}")
        console.print(f"  [green]Run ID:[/green] {ci_run_info['run_id']}")
        parsed["reproduce_args"] = [
            "-r", ci_run_info["repo_url"],
            "-b", ci_run_info["branch"],
            "--only-needed-deps",
        ]

    branch_hint = ci_run_info["branch"] if ci_run_info else None
    container_name = prompt_for_session_name(branch_hint)
    return container_name, ci_run_info, True


def fix_ci(args):
    """Main fix workflow: session select -> preflight -> reproduce -> Claude -> shell."""
    parsed = parse_fix_args(args)

    console.print(Panel("[bold cyan]CI Fix with Claude[/bold cyan]", expand=False))

    # Step 0: Session selection
    ci_run_info = None
    if parsed["reproduce_args"]:
        branch_hint = extract_branch_from_args(parsed["reproduce_args"])
        container_name = prompt_for_session_name(branch_hint)
        needs_reproduce = True
    else:
        container_name, ci_run_info, needs_reproduce = select_or_create_session(parsed)

    # Step 1: Preflight checks
    repo_url = extract_repo_url_from_args(parsed["reproduce_args"])
    try:
        run_all_preflight_checks(repo_url=repo_url)
    except PreflightError as error:
        console.print(f"\n[bold red]Preflight failed:[/bold red] {error}")
        sys.exit(1)

    # Step 2: Reproduce CI in container
    if needs_reproduce:
        if container_exists(DEFAULT_CONTAINER_NAME):
            remove_container(DEFAULT_CONTAINER_NAME)
        reproduce_ci(parsed["reproduce_args"], skip_preflight=True)
        if container_name != DEFAULT_CONTAINER_NAME:
            rename_container(DEFAULT_CONTAINER_NAME, container_name)

    # Step 3: Install Claude in container
    setup_claude_in_container(container_name)

    # Step 4: Select fix mode and launch Claude
    if ci_run_info:
        prompt = CI_RUN_COMPARE_PROMPT_TEMPLATE.format(**ci_run_info)
    else:
        prompt = select_fix_mode()

    console.print("\n[bold cyan]Launching Claude Code...[/bold cyan]")
    console.print("[dim]Claude will attempt to fix CI failures autonomously[/dim]\n")

    escaped_prompt = prompt.replace("'", "'\\''")
    claude_command = (
        f"cd /ros_ws && IS_SANDBOX=1 claude --dangerously-skip-permissions "
        f"-p '{escaped_prompt}'"
    )
    docker_exec(container_name, claude_command, check=False)

    # Step 5: Drop into interactive shell
    console.print("\n[bold green]Claude has finished.[/bold green]")
    console.print("[cyan]Dropping you into the container shell.[/cyan]")
    console.print("[dim]You can run 'git diff', 'git add', 'git commit' etc.[/dim]")
    console.print("[dim]The repo is at /ros_ws/src/<repo_name>[/dim]\n")

    docker_exec_interactive(container_name)
