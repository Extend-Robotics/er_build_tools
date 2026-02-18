#!/usr/bin/env python3
"""Launch an interactive Claude Code session inside a CI container."""
from __future__ import annotations

import os
import sys

from rich.console import Console

from ci_tool.claude_setup import (
    copy_ci_context,
    copy_claude_credentials,
    is_claude_installed_in_container,
    setup_claude_in_container,
)
from ci_tool.containers import (
    container_exists,
    container_is_running,
    list_ci_containers,
    require_docker,
    start_container,
)

console = Console()


def select_container(args):
    """Select a running CI container from args or interactive prompt."""
    if args:
        return args[0]

    existing = list_ci_containers()
    if not existing:
        console.print("[red]No CI containers found. Run 'Reproduce CI' first.[/red]")
        sys.exit(1)

    from InquirerPy import inquirer
    choices = []
    for container in existing:
        choices.append({
            "name": f"{container['name']} ({container['status']})",
            "value": container["name"],
        })

    return inquirer.select(
        message="Select a container:",
        choices=choices,
    ).execute()


def claude_session(args):
    """Launch an interactive Claude session in a CI container."""
    require_docker()
    container_name = select_container(args)

    if not container_exists(container_name):
        console.print(f"[red]Container '{container_name}' does not exist[/red]")
        sys.exit(1)

    if not container_is_running(container_name):
        console.print(f"[yellow]Starting container '{container_name}'...[/yellow]")
        start_container(container_name)

    if not is_claude_installed_in_container(container_name):
        console.print("[cyan]Claude not installed — running full setup...[/cyan]")
        setup_claude_in_container(container_name)
    else:
        copy_claude_credentials(container_name)
        copy_ci_context(container_name)

    console.print(f"\n[bold cyan]Starting Claude session in '{container_name}'...[/bold cyan]")
    console.print("[dim]Type /exit or Ctrl+C to leave Claude[/dim]\n")

    os.execvp("docker", [
        "docker", "exec", "-it", container_name,
        "bash", "-c",
        "source ~/.bashrc && cd /ros_ws && claude --dangerously-skip-permissions",
    ])
