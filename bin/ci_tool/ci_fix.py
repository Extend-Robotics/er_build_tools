#!/usr/bin/env python3
"""Fix CI test failures using Claude Code inside a container."""
from __future__ import annotations

import sys

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
    remove_container,
    start_container,
)
from ci_tool.ci_reproduce import reproduce_ci, extract_repo_url_from_args
from ci_tool.preflight import run_all_preflight_checks, PreflightError

console = Console()

DEFAULT_PROMPT = (
    "You are inside a CI reproduction container at /ros_ws. "
    "Source the ROS workspace: "
    "`source /opt/ros/noetic/setup.bash && source /ros_ws/install/setup.bash`.\n\n"
    "The CI tests have already been run. Your job:\n"
    "1. Examine the test output in /ros_ws/test_output.log to identify failures\n"
    "2. Find and fix the root cause in the source code under /ros_ws/src/\n"
    "3. Rebuild the affected packages\n"
    "4. Re-run the failing tests to verify your fix\n"
    "5. Iterate until all tests pass\n\n"
    "When done, print EXACTLY this format:\n\n"
    "--- SUMMARY ---\n"
    "Problem: <what was wrong>\n"
    "Fix: <what you changed>\n"
    "Assumptions: <any assumptions you made, or 'None'>\n\n"
    "--- COMMIT MESSAGE ---\n"
    "<brief concise commit message, no leading/trailing whitespace>\n"
    "--- END ---"
)


def parse_fix_args(args):
    """Parse fix-specific arguments, separating them from reproduce args."""
    parsed = {
        "prompt": DEFAULT_PROMPT,
        "container_name": DEFAULT_CONTAINER_NAME,
        "reproduce_args": [],
    }

    i = 0
    while i < len(args):
        if args[i] == "--prompt" and i + 1 < len(args):
            parsed["prompt"] = args[i + 1]
            i += 2
        elif args[i] in ("--container-name", "-n") and i + 1 < len(args):
            parsed["container_name"] = args[i + 1]
            i += 2
        else:
            parsed["reproduce_args"].append(args[i])
            i += 1

    return parsed


def fix_ci(args):
    """Main fix workflow: preflight -> ensure container -> install Claude -> run -> drop to shell."""
    parsed = parse_fix_args(args)
    container_name = parsed["container_name"]
    prompt = parsed["prompt"]

    console.print(Panel("[bold cyan]CI Fix with Claude[/bold cyan]", expand=False))

    # Step 0: Preflight checks
    repo_url = extract_repo_url_from_args(parsed["reproduce_args"])
    try:
        run_all_preflight_checks(repo_url=repo_url)
    except PreflightError as error:
        console.print(f"\n[bold red]Preflight failed:[/bold red] {error}")
        sys.exit(1)

    # Step 1: Ensure container exists
    needs_reproduce = False

    if container_exists(container_name):
        if container_is_running(container_name):
            action = inquirer.select(
                message=f"Container '{container_name}' is running. What to do?",
                choices=[
                    {"name": "Use existing container (skip CI reproduction)", "value": "reuse"},
                    {"name": "Remove and recreate from scratch", "value": "recreate"},
                    {"name": "Cancel", "value": "cancel"},
                ],
            ).execute()
        else:
            action = inquirer.select(
                message=f"Container '{container_name}' exists but is stopped.",
                choices=[
                    {"name": "Start and reuse it", "value": "reuse"},
                    {"name": "Remove and recreate from scratch", "value": "recreate"},
                    {"name": "Cancel", "value": "cancel"},
                ],
            ).execute()

        if action == "cancel":
            return
        if action == "recreate":
            remove_container(container_name)
            needs_reproduce = True
        elif action == "reuse":
            if not container_is_running(container_name):
                start_container(container_name)
    else:
        needs_reproduce = True

    if needs_reproduce:
        if not parsed["reproduce_args"]:
            console.print("[red]No container exists and no reproduce args provided.[/red]")
            console.print(
                "Pass repo args, e.g.: ci_tool fix -r https://github.com/... "
                "-b main --only-needed-deps"
            )
            sys.exit(1)
        reproduce_ci(parsed["reproduce_args"], skip_preflight=True)

    # Step 2: Install Claude in container
    setup_claude_in_container(container_name)

    # Step 3: Launch Claude
    console.print("\n[bold cyan]Launching Claude Code...[/bold cyan]")
    console.print("[dim]Claude will attempt to fix CI failures autonomously[/dim]\n")

    escaped_prompt = prompt.replace("'", "'\\''")
    claude_command = f"cd /ros_ws && IS_SANDBOX=1 claude --dangerously-skip-permissions -p '{escaped_prompt}'"
    docker_exec(container_name, claude_command, check=False)

    # Step 4: Drop into interactive shell
    console.print("\n[bold green]Claude has finished.[/bold green]")
    console.print("[cyan]Dropping you into the container shell.[/cyan]")
    console.print("[dim]You can run 'git diff', 'git add', 'git commit' etc.[/dim]")
    console.print("[dim]The repo is at /ros_ws/src/<repo_name>[/dim]\n")

    docker_exec_interactive(container_name)
