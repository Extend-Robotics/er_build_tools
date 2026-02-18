#!/usr/bin/env python3
"""Install Claude Code in a container and copy auth/config from host."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from rich.console import Console

from ci_tool.containers import docker_exec, docker_cp_to_container

console = Console()

CLAUDE_HOME = Path.home() / ".claude"


def install_node_in_container(container_name):
    """Install Node.js 20 LTS in the container."""
    console.print("[cyan]Installing Node.js 20 in container...[/cyan]")
    docker_exec(container_name, (
        "curl -fsSL https://deb.nodesource.com/setup_20.x | bash - "
        "&& apt-get install -y nodejs"
    ))


def install_claude_in_container(container_name):
    """Install Claude Code via npm in the container."""
    console.print("[cyan]Installing Claude Code in container...[/cyan]")
    docker_exec(container_name, "npm install -g @anthropic-ai/claude-code")


def install_fzf_in_container(container_name):
    """Install fzf in the container."""
    console.print("[cyan]Installing fzf in container...[/cyan]")
    docker_exec(container_name, "apt-get update && apt-get install -y fzf", check=False)


def copy_claude_credentials(container_name):
    """Copy Claude credentials into the container."""
    credentials_path = CLAUDE_HOME / ".credentials.json"
    if not credentials_path.exists():
        raise RuntimeError(f"Claude credentials not found at {credentials_path}")

    console.print("[cyan]Copying Claude credentials...[/cyan]")
    docker_exec(container_name, "mkdir -p /root/.claude")
    docker_cp_to_container(
        str(credentials_path), container_name, "/root/.claude/.credentials.json"
    )


def copy_claude_config(container_name):
    """Copy CLAUDE.md and modified settings.json into the container."""
    claude_md_path = CLAUDE_HOME / "CLAUDE.md"
    settings_path = CLAUDE_HOME / "settings.json"

    if claude_md_path.exists():
        console.print("[cyan]Copying CLAUDE.md...[/cyan]")
        docker_cp_to_container(str(claude_md_path), container_name, "/root/.claude/CLAUDE.md")

    if settings_path.exists():
        console.print("[cyan]Copying settings.json (modified for dangerous mode)...[/cyan]")
        with open(settings_path, encoding="utf-8") as settings_file:
            settings = json.load(settings_file)

        settings.setdefault("permissions", {})["defaultMode"] = "dangerouslySkipPermissions"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json.dump(settings, tmp, indent=2)
            tmp_path = tmp.name

        try:
            docker_cp_to_container(tmp_path, container_name, "/root/.claude/settings.json")
        finally:
            os.unlink(tmp_path)


def copy_claude_memory(container_name):
    """Copy Claude project memory files into the container."""
    projects_dir = CLAUDE_HOME / "projects"
    if not projects_dir.exists():
        return

    console.print("[cyan]Copying Claude memory files...[/cyan]")
    for project_dir in projects_dir.iterdir():
        memory_dir = project_dir / "memory"
        if not memory_dir.exists():
            continue
        memory_files = [f for f in memory_dir.iterdir() if f.is_file()]
        if not memory_files:
            continue

        container_memory_path = f"/root/.claude/projects/{project_dir.name}/memory"
        docker_exec(container_name, f"mkdir -p {container_memory_path}")
        for memory_file in memory_files:
            docker_cp_to_container(
                str(memory_file),
                container_name,
                f"{container_memory_path}/{memory_file.name}",
            )


def copy_helper_bash_functions(container_name):
    """Copy ~/.helper_bash_functions and source it in bashrc."""
    helper_path = Path.home() / ".helper_bash_functions"
    if not helper_path.exists():
        console.print("[yellow]~/.helper_bash_functions not found, skipping[/yellow]")
        return

    console.print("[cyan]Copying helper bash functions...[/cyan]")
    docker_cp_to_container(str(helper_path), container_name, "/root/.helper_bash_functions")
    docker_exec(
        container_name,
        "grep -q 'source ~/.helper_bash_functions' /root/.bashrc "
        "|| echo 'source ~/.helper_bash_functions' >> /root/.bashrc",
    )


def get_host_git_config(key):
    """Read a value from the host's git config."""
    result = subprocess.run(
        ["git", "config", "--global", key],
        capture_output=True, text=True, check=False,
    )
    value = result.stdout.strip()
    if not value:
        raise RuntimeError(
            f"git config --global {key} is not set on the host. "
            f"Set it with: git config --global {key} 'Your Value'"
        )
    return value


def configure_git_in_container(container_name):
    """Set up git identity, token-based auth, and gh CLI auth in the container."""
    gh_token = os.environ.get("GH_TOKEN", "")

    git_user_name = get_host_git_config("user.name")
    git_user_email = get_host_git_config("user.email")

    console.print("[cyan]Configuring git in container...[/cyan]")
    docker_exec(container_name, f'git config --global user.name "{git_user_name}"')
    docker_exec(container_name, f'git config --global user.email "{git_user_email}"')

    if gh_token:
        docker_exec(
            container_name,
            f'git config --global url."https://{gh_token}@github.com/"'
            f'.insteadOf "https://github.com/"',
            quiet=True,
        )
        install_and_auth_gh_cli(container_name, gh_token)


def install_and_auth_gh_cli(container_name, gh_token):
    """Install gh CLI and authenticate with the provided token."""
    console.print("[cyan]Installing gh CLI in container...[/cyan]")
    docker_exec(container_name, (
        "type gh >/dev/null 2>&1 || ("
        "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg "
        "| dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg "
        "&& chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg "
        '&& echo "deb [arch=$(dpkg --print-architecture) '
        "signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] "
        'https://cli.github.com/packages stable main" '
        "| tee /etc/apt/sources.list.d/github-cli-stable.list > /dev/null "
        "&& apt-get update && apt-get install -y gh)"
    ), check=False)
    console.print("[cyan]Authenticating gh CLI...[/cyan]")
    docker_exec(
        container_name,
        f'echo "{gh_token}" | gh auth login --with-token',
        check=False,
        quiet=True,
    )


def setup_claude_in_container(container_name):
    """Full setup: install Claude Code and copy all config into container."""
    console.print("\n[bold cyan]Setting up Claude in container...[/bold cyan]")

    install_node_in_container(container_name)
    install_claude_in_container(container_name)
    install_fzf_in_container(container_name)
    copy_claude_credentials(container_name)
    copy_claude_config(container_name)
    copy_claude_memory(container_name)
    copy_helper_bash_functions(container_name)
    configure_git_in_container(container_name)

    console.print("[bold green]Claude Code is installed and configured in the container[/bold green]")
