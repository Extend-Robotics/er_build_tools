#!/usr/bin/env python3
"""Reproduce CI locally by creating a Docker container."""
from __future__ import annotations

import os
import subprocess
import sys

from InquirerPy import inquirer
from rich.console import Console

from ci_tool.containers import (
    DEFAULT_CONTAINER_NAME,
    container_exists,
    container_is_running,
    remove_container,
)
from ci_tool.preflight import validate_docker_available, validate_gh_token, PreflightError

console = Console()

DEFAULT_SCRIPTS_BRANCH = "ERD-1633_reproduce_ci_locally"


def extract_repo_url_from_args(args):
    """Extract --repo/-r value from args list, or return None."""
    for i, arg in enumerate(args):
        if arg in ("--repo", "-r") and i + 1 < len(args):
            return args[i + 1]
    return None


def extract_branch_from_args(args):
    """Extract --branch/-b value from args list, or return None."""
    for i, arg in enumerate(args):
        if arg in ("--branch", "-b") and i + 1 < len(args):
            return args[i + 1]
    return None


def prompt_for_reproduce_args():
    """Interactively ask user for the required reproduce arguments."""
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

    build_everything = inquirer.confirm(
        message="Build everything (slower, disable --only-needed-deps)?",
        default=False,
    ).execute()

    args = ["-r", repo_url, "-b", branch]
    if not build_everything:
        args.append("--only-needed-deps")
    return args


def reproduce_ci(args, skip_preflight=False):
    """Create a CI reproduction container."""
    if not skip_preflight:
        try:
            validate_docker_available()
            repo_url = extract_repo_url_from_args(args)
            validate_gh_token(repo_url=repo_url)
        except PreflightError as error:
            console.print(f"\n[bold red]Preflight failed:[/bold red] {error}")
            sys.exit(1)

    container_name = DEFAULT_CONTAINER_NAME

    if container_exists(container_name):
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
                subprocess.run(["docker", "start", container_name], check=True)
            console.print(f"[green]Using existing container '{container_name}'[/green]")
            return

    if not args:
        args = prompt_for_reproduce_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("ER_SETUP_TOKEN") or ""
    if not token:
        console.print("[red]No GitHub token found. Set GH_TOKEN or ER_SETUP_TOKEN.[/red]")
        sys.exit(1)
    scripts_branch = DEFAULT_SCRIPTS_BRANCH

    filtered_args = []
    i = 0
    while i < len(args):
        if args[i] in ("--scripts-branch", "--scripts_branch"):
            scripts_branch = args[i + 1]
            i += 2
        else:
            filtered_args.append(args[i])
            i += 1

    wrapper_url = (
        f"https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/"
        f"refs/heads/{scripts_branch}/bin/reproduce_ci.sh"
    )

    console.print(f"[cyan]Fetching CI scripts from branch: {scripts_branch}[/cyan]")

    full_args = [
        "--gh-token", token,
        "--scripts-branch", scripts_branch,
    ] + filtered_args

    fetch_result = subprocess.run(
        ["curl", "-fSL", wrapper_url],
        capture_output=True, text=True, check=True,
    )

    result = subprocess.run(
        ["bash", "-c", fetch_result.stdout + '\n"$@"', "--"] + full_args,
        check=False,
    )

    if result.returncode != 0:
        console.print(
            f"\n[yellow]CI reproduction exited with code {result.returncode} "
            f"(expected — tests likely failed)[/yellow]"
        )
    console.print(f"\n[green]Container '{container_name}' is ready[/green]")
