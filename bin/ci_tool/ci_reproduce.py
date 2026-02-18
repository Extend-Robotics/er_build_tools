#!/usr/bin/env python3
"""Reproduce CI locally by creating a Docker container with Python Docker orchestration."""
from __future__ import annotations

import os
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from InquirerPy import inquirer
from rich.console import Console
from rich.panel import Panel

from ci_tool.containers import (
    DEFAULT_CONTAINER_NAME,
    container_exists,
    container_is_running,
    remove_container,
    run_command,
    start_container,
)
from ci_tool.preflight import validate_docker_available, validate_gh_token, PreflightError

console = Console()

DEFAULT_DOCKER_IMAGE = "rostooling/setup-ros-docker:ubuntu-focal-ros-noetic-desktop-latest"
DEFAULT_SCRIPTS_BRANCH = "ERD-1633_reproduce_ci_locally"
INTERNAL_REPO = "Extend-Robotics/er_build_tools_internal"
CONTAINER_SETUP_SCRIPT_PATH = "/tmp/ci_workspace_setup.sh"
CONTAINER_RETEST_SCRIPT_PATH = "/tmp/ci_repull_and_retest.sh"


def _parse_repo_url(repo_url):
    """Extract org and repo name from a GitHub URL.

    Returns (org, repo_name, clean_url) tuple.
    """
    clean_url = repo_url.rstrip("/").removesuffix(".git")
    repo_path = clean_url.removeprefix("https://github.com/")
    parts = repo_path.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"Cannot parse org/repo from URL: {repo_url}")
    return parts[0], parts[1], clean_url


def _fetch_github_raw_file(repo, file_path, branch, gh_token):
    """Fetch a file from a GitHub repo via raw.githubusercontent.com.

    Returns the file content as a string.
    Raises RuntimeError if the file cannot be fetched.
    """
    url = f"https://raw.githubusercontent.com/{repo}/refs/heads/{branch}/{file_path}"
    request = Request(url, headers={"Authorization": f"token {gh_token}"})
    try:
        with urlopen(request, timeout=15) as response:
            return response.read().decode()
    except HTTPError as error:
        raise RuntimeError(
            f"Failed to fetch {file_path} from {repo} branch '{branch}' "
            f"(HTTP {error.code}). Check the branch exists and your token has access."
        ) from error
    except URLError as error:
        raise RuntimeError(
            f"Cannot reach GitHub to fetch {file_path}: {error.reason}"
        ) from error


def _validate_deps_repos_reachable(org, repo_name, branch, gh_token, deps_file="deps.repos"):
    """Validate that deps.repos is reachable at the target branch before creating the container."""
    branch_for_raw = branch or "main"
    deps_url = (
        f"https://raw.githubusercontent.com/{org}/{repo_name}/"
        f"{branch_for_raw}/{deps_file}"
    )
    console.print(f"  Validating {deps_file} is reachable at: [dim]{deps_url}[/dim]")
    request = Request(deps_url, headers={"Authorization": f"token {gh_token}"})
    try:
        with urlopen(request, timeout=10) as response:
            http_code = response.getcode()
    except HTTPError as error:
        http_code = error.code
        hints = [
            f"Could not reach {deps_file} (HTTP {http_code})",
            f"Check that '{org}/{repo_name}' exists and your token has access",
            f"Check that '{deps_file}' exists at ref '{branch_for_raw}'",
        ]
        if branch:
            hints.insert(1, f"Branch/commit '{branch}' may not exist in '{org}/{repo_name}'")
        raise RuntimeError("\n  ".join(hints)) from error
    except URLError as error:
        raise RuntimeError(f"Cannot reach GitHub to validate {deps_file}: {error.reason}") from error

    console.print(f"  [green]\u2713[/green] Validation passed (HTTP {http_code})")


def _fetch_internal_scripts(gh_token, scripts_branch):
    """Fetch ci_workspace_setup.sh and ci_repull_and_retest.sh from er_build_tools_internal.

    Returns (setup_script_path, retest_script_path) as temporary file paths on the host.
    """
    console.print(f"  Fetching CI scripts from [cyan]{INTERNAL_REPO}[/cyan] branch [cyan]{scripts_branch}[/cyan]")

    setup_content = _fetch_github_raw_file(
        INTERNAL_REPO, "bin/ci_workspace_setup.sh", scripts_branch, gh_token,
    )
    retest_content = _fetch_github_raw_file(
        INTERNAL_REPO, "bin/ci_repull_and_retest.sh", scripts_branch, gh_token,
    )

    script_dir = tempfile.mkdtemp(prefix="ci_reproduce_scripts_")
    setup_script_host_path = os.path.join(script_dir, "ci_workspace_setup.sh")
    retest_script_host_path = os.path.join(script_dir, "ci_repull_and_retest.sh")

    with open(setup_script_host_path, "w", encoding="utf-8") as script_file:
        script_file.write(setup_content)
    os.chmod(setup_script_host_path, 0o755)

    with open(retest_script_host_path, "w", encoding="utf-8") as script_file:
        script_file.write(retest_content)
    os.chmod(retest_script_host_path, 0o755)

    console.print("  [green]\u2713[/green] Scripts fetched and saved to temp directory")
    return setup_script_host_path, retest_script_host_path


def _build_graphical_docker_args():
    """Build Docker args for X11/NVIDIA graphical forwarding.

    Raises RuntimeError if DISPLAY is not set.
    """
    display = os.environ.get("DISPLAY")
    if not display:
        raise RuntimeError(
            "Graphical mode requires DISPLAY to be set (X11 forwarding). "
            "Set DISPLAY or pass graphical=False."
        )

    console.print("  Enabling graphical forwarding (X11 + NVIDIA)...")
    subprocess.run(["xhost", "+local:"], check=False, capture_output=True)

    return [
        "--runtime", "nvidia",
        "--gpus", "all",
        "--privileged",
        "--security-opt", "seccomp=unconfined",
        "-v", "/tmp/.X11-unix:/tmp/.X11-unix:rw",
        "-e", f"DISPLAY={display}",
        "-e", "QT_X11_NO_MITSHM=1",
        "-e", "NVIDIA_DRIVER_CAPABILITIES=all",
        "-e", "NVIDIA_VISIBLE_DEVICES=all",
    ]


def _docker_create_and_start(
    container_name,
    docker_image,
    env_vars,
    volume_mounts,
    graphical_args,
):
    """Create and start a Docker container with the given configuration."""
    create_command = ["docker", "create", "--name", container_name]
    create_command.extend(["--network=host", "--ipc=host"])

    for volume_mount in volume_mounts:
        create_command.extend(["-v", volume_mount])

    for env_key, env_value in env_vars.items():
        create_command.extend(["-e", f"{env_key}={env_value}"])

    create_command.extend(graphical_args)
    create_command.extend([docker_image, "sleep", "infinity"])

    console.print(f"\n  Creating container [cyan]'{container_name}'[/cyan]...")
    run_command(create_command, quiet=True)
    console.print(f"  [green]\u2713[/green] Container '{container_name}' created")

    console.print(f"  Starting container [cyan]'{container_name}'[/cyan]...")
    run_command(["docker", "start", container_name], quiet=True)
    console.print(f"  [green]\u2713[/green] Container '{container_name}' started")


def _docker_exec_workspace_setup(container_name):
    """Run ci_workspace_setup.sh inside the container.

    Handles KeyboardInterrupt gracefully by letting the container keep running.
    """
    console.print("\n  Running CI workspace setup inside container...")
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "bash", CONTAINER_SETUP_SCRIPT_PATH],
            check=False,
        )
    except KeyboardInterrupt:
        console.print(
            "\n[yellow]Interrupted during workspace setup "
            "-- container is still running with partial setup[/yellow]"
        )
        return

    if result.returncode != 0:
        console.print(
            f"\n[yellow]Workspace setup exited with code {result.returncode} "
            f"(expected if tests failed)[/yellow]"
        )


def prompt_for_reproduce_args():
    """Interactively ask user for the required reproduce arguments.

    Returns (repo_url, branch, only_needed_deps) tuple.
    """
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

    only_needed_deps = not build_everything
    return repo_url, branch, only_needed_deps


def reproduce_ci(
    repo_url,
    branch,
    container_name=DEFAULT_CONTAINER_NAME,
    gh_token=None,
    only_needed_deps=True,
    scripts_branch=DEFAULT_SCRIPTS_BRANCH,
    graphical=True,
    skip_preflight=False,
):
    """Create a CI reproduction container using direct Docker orchestration.

    Fetches container-side scripts from er_build_tools_internal, validates
    deps.repos is reachable, creates and starts a Docker container, then
    runs workspace setup inside it.
    """
    if not skip_preflight:
        try:
            validate_docker_available()
            validate_gh_token(repo_url=repo_url)
        except PreflightError as error:
            raise RuntimeError(f"Preflight failed: {error}") from error

    if gh_token is None:
        gh_token = os.environ.get("GH_TOKEN") or os.environ.get("ER_SETUP_TOKEN") or ""
    if not gh_token:
        raise RuntimeError(
            "No GitHub token found. Set GH_TOKEN or ER_SETUP_TOKEN environment variable."
        )

    console.print(Panel("[bold cyan]Reproduce CI[/bold cyan]", expand=False))

    # Handle existing container
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
                start_container(container_name)
            console.print(f"[green]\u2713 Using existing container '{container_name}'[/green]")
            return

    # Parse repo URL
    org, repo_name, clean_repo_url = _parse_repo_url(repo_url)
    console.print(f"  Organization: [cyan]{org}[/cyan]")
    console.print(f"  Repository:   [cyan]{repo_name}[/cyan]")

    # Validate deps.repos is reachable before doing anything expensive
    _validate_deps_repos_reachable(org, repo_name, branch, gh_token)

    # Fetch internal scripts
    setup_script_host_path, retest_script_host_path = _fetch_internal_scripts(
        gh_token, scripts_branch,
    )

    # Build graphical args
    graphical_docker_args = []
    if graphical:
        try:
            graphical_docker_args = _build_graphical_docker_args()
        except RuntimeError:
            console.print(
                "  [yellow]DISPLAY not set -- skipping graphical forwarding[/yellow]"
            )

    # Environment variables for the container-side scripts
    container_env_vars = {
        "GH_TOKEN": gh_token,
        "REPO_URL": clean_repo_url,
        "REPO_NAME": repo_name,
        "ORG": org,
        "DEPS_FILE": "deps.repos",
        "BRANCH": branch or "",
        "ONLY_NEEDED_DEPS": "true" if only_needed_deps else "false",
        "SKIP_TESTS": "false",
        "ADDITIONAL_COMMAND": "",
    }

    # Volume mounts (scripts mounted read-only into the container)
    volume_mounts = [
        f"{setup_script_host_path}:{CONTAINER_SETUP_SCRIPT_PATH}:ro",
        f"{retest_script_host_path}:{CONTAINER_RETEST_SCRIPT_PATH}:ro",
    ]

    # Create and start container
    _docker_create_and_start(
        container_name,
        DEFAULT_DOCKER_IMAGE,
        container_env_vars,
        volume_mounts,
        graphical_docker_args,
    )

    # Run workspace setup
    _docker_exec_workspace_setup(container_name)

    # Container existence guard
    if not container_exists(container_name):
        raise RuntimeError(
            f"Container '{container_name}' does not exist after execution. "
            "Docker create or start may have failed silently."
        )

    console.print(f"\n[green]\u2713 Container '{container_name}' is ready[/green]")
