#!/usr/bin/env python3
"""Docker container lifecycle management."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

from rich.console import Console

console = Console()

DEFAULT_CONTAINER_NAME = "er_ci_reproduced_testing_env"


def require_docker():
    """Fail fast if docker is not available."""
    if not shutil.which("docker"):
        console.print("[red]Error: 'docker' command not found. Is Docker installed?[/red]")
        sys.exit(1)


def run_command(command, capture_output=False, check=True, quiet=False):
    """Run a shell command, raising on failure."""
    if not quiet:
        console.print(f"[dim]$ {' '.join(command)}[/dim]")
    return subprocess.run(command, capture_output=capture_output, check=check, text=True)


def container_exists(container_name=DEFAULT_CONTAINER_NAME):
    """Check if a container exists (running or stopped)."""
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{container_name}$", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=False,
    )
    return container_name in result.stdout.strip()


def container_is_running(container_name=DEFAULT_CONTAINER_NAME):
    """Check if a container is currently running."""
    result = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{container_name}$", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=False,
    )
    return container_name in result.stdout.strip()


def start_container(container_name=DEFAULT_CONTAINER_NAME):
    """Start a stopped container."""
    run_command(["docker", "start", container_name])


def remove_container(container_name=DEFAULT_CONTAINER_NAME):
    """Force remove a container."""
    run_command(["docker", "rm", "-f", container_name])
    console.print(f"[green]Container '{container_name}' removed[/green]")


def list_ci_containers():
    """List all CI containers (er_ci_* prefix) with their status."""
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=er_ci_",
         "--format", "{{.Names}}\t{{.Status}}"],
        capture_output=True, text=True, check=False,
    )
    if not result.stdout.strip():
        return []
    containers = []
    for line in result.stdout.strip().split("\n"):
        parts = line.split("\t")
        containers.append({
            "name": parts[0],
            "status": parts[1] if len(parts) > 1 else "unknown",
        })
    return containers


def rename_container(old_name, new_name):
    """Rename a Docker container."""
    run_command(["docker", "rename", old_name, new_name])


def sanitize_container_name(name):
    """Replace characters invalid for Docker container names with underscores."""
    return re.sub(r'[^a-zA-Z0-9_.-]', '_', name)


def docker_exec(container_name, command, interactive=False, check=True, quiet=False):
    """Run a command inside a container."""
    docker_command = ["docker", "exec"]
    if interactive:
        docker_command.extend(["-it"])
    docker_command.extend([container_name, "bash", "-c", command])
    return run_command(docker_command, check=check, quiet=quiet)


def docker_exec_interactive(container_name=DEFAULT_CONTAINER_NAME):
    """Drop user into an interactive shell inside the container."""
    console.print(f"\n[bold cyan]Entering container '{container_name}'...[/bold cyan]")
    console.print("[dim]Type 'exit' to leave the container[/dim]\n")
    os.execvp("docker", ["docker", "exec", "-it", container_name, "bash"])


def docker_cp_to_container(host_path, container_name, container_path):
    """Copy a file/directory from host into container."""
    run_command(["docker", "cp", host_path, f"{container_name}:{container_path}"])


def shell_into_container(args):
    """Shell subcommand handler."""
    require_docker()
    container_name = args[0] if args else DEFAULT_CONTAINER_NAME
    if not container_exists(container_name):
        console.print(f"[red]Container '{container_name}' does not exist[/red]")
        sys.exit(1)
    if not container_is_running(container_name):
        console.print(f"[yellow]Container '{container_name}' is stopped, starting...[/yellow]")
        start_container(container_name)
    docker_exec_interactive(container_name)


def retest_in_container(args):
    """Re-run tests subcommand handler."""
    require_docker()
    container_name = args[0] if args else DEFAULT_CONTAINER_NAME
    if not container_is_running(container_name):
        console.print(f"[red]Container '{container_name}' is not running[/red]")
        sys.exit(1)
    console.print(f"[cyan]Re-running tests in '{container_name}'...[/cyan]")
    docker_exec(container_name, "bash /tmp/ci_repull_and_retest.sh")


def clean_containers(_args):
    """Clean up CI containers."""
    require_docker()
    from InquirerPy import inquirer

    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=er_ci_", "--format", "{{.Names}}\t{{.Status}}"],
        capture_output=True, text=True, check=False,
    )
    if not result.stdout.strip():
        console.print("[green]No CI containers found[/green]")
        return

    console.print("[bold]CI containers:[/bold]")
    containers = []
    for line in result.stdout.strip().split("\n"):
        parts = line.split("\t")
        name = parts[0]
        status = parts[1] if len(parts) > 1 else "unknown"
        containers.append({"name": f"{name} ({status})", "value": name})
        console.print(f"  {name}: {status}")

    selected = inquirer.checkbox(
        message="Select containers to remove:",
        choices=containers,
    ).execute()

    for name in selected:
        remove_container(name)
