#!/usr/bin/env python3
"""Interactive CLI menu for CI tool."""
# pylint: disable=import-outside-toplevel
from __future__ import annotations

import sys

from InquirerPy import inquirer
from rich.console import Console
from rich.panel import Panel

console = Console()

MENU_CHOICES = [
    {"name": "Reproduce CI (create container)", "value": "reproduce"},
    {"name": "Fix CI with Claude", "value": "fix"},
    {"name": "Claude session (interactive)", "value": "claude"},
    {"name": "Shell into container", "value": "shell"},
    {"name": "Re-run tests in container", "value": "retest"},
    {"name": "Clean up containers", "value": "clean"},
    {"name": "Exit", "value": "exit"},
]


def main():
    """Entry point - show menu or dispatch subcommand."""
    if len(sys.argv) > 1:
        dispatch_subcommand(sys.argv[1], sys.argv[2:])
        return

    console.print(Panel("[bold cyan]CI Tool[/bold cyan]", expand=False))

    action = inquirer.select(
        message="What would you like to do?",
        choices=MENU_CHOICES,
        default="fix",
    ).execute()

    if action == "exit":
        return

    dispatch_subcommand(action, [])


def dispatch_subcommand(command, args):
    """Route to the appropriate subcommand handler."""
    handlers = {
        "reproduce": _handle_reproduce,
        "fix": _handle_fix,
        "claude": _handle_claude,
        "shell": _handle_shell,
        "retest": _handle_retest,
        "clean": _handle_clean,
    }
    handler = handlers.get(command)
    if handler is None:
        console.print(f"[red]Unknown command: {command}[/red]")
        console.print(f"Available: {', '.join(handlers.keys())}")
        sys.exit(1)
    handler(args)


def _handle_container_collision(container_name):
    """Handle an existing container: recreate, keep, or cancel.

    Returns True if reproduce should proceed, False to skip.
    """
    from ci_tool.containers import (
        container_is_running,
        remove_container,
        start_container,
    )

    action = inquirer.select(
        message=f"Container '{container_name}' already exists. What to do?",
        choices=[
            {"name": "Remove and recreate", "value": "recreate"},
            {"name": "Keep existing (skip creation)", "value": "keep"},
            {"name": "Cancel", "value": "cancel"},
        ],
    ).execute()

    if action == "cancel":
        return False
    if action == "recreate":
        remove_container(container_name)
        return True
    # action == "keep"
    if not container_is_running(container_name):
        start_container(container_name)
    console.print(
        f"[green]Using existing container '{container_name}'[/green]"
    )
    return False


def _handle_reproduce(_args):
    from ci_tool.ci_reproduce import reproduce_ci, prompt_for_reproduce_args
    from ci_tool.containers import DEFAULT_CONTAINER_NAME, container_exists
    from ci_tool.preflight import validate_docker_available, validate_gh_token, PreflightError

    try:
        validate_docker_available()
    except PreflightError as error:
        console.print(f"\n[bold red]Preflight failed:[/bold red] {error}")
        sys.exit(1)

    repo_url, branch, only_needed_deps = prompt_for_reproduce_args()
    container_name = DEFAULT_CONTAINER_NAME

    if container_exists(container_name):
        if not _handle_container_collision(container_name):
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


def _handle_fix(args):
    from ci_tool.ci_fix import fix_ci
    fix_ci(args)


def _handle_claude(args):
    from ci_tool.claude_session import claude_session
    claude_session(args)


def _handle_shell(args):
    from ci_tool.containers import shell_into_container
    shell_into_container(args)


def _handle_retest(args):
    from ci_tool.containers import retest_in_container
    retest_in_container(args)


def _handle_clean(args):
    from ci_tool.containers import clean_containers
    clean_containers(args)
