#!/usr/bin/env python3
"""Preflight validation - fail fast on auth issues before any docker operations."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from rich.console import Console
from rich.panel import Panel

console = Console()

CLAUDE_HOME = Path.home() / ".claude"


class PreflightError(RuntimeError):
    """Raised when a preflight check fails."""


def _check_pass(message):
    console.print(f"  [green]\u2713[/green] {message}")


def _check_fail(message):
    console.print(f"  [red]\u2717[/red] {message}")


def _check_warn(message):
    console.print(f"  [yellow]![/yellow] {message}")


def _github_api_get(endpoint, gh_token):
    """Make an authenticated GET request to the GitHub API."""
    url = f"https://api.github.com{endpoint}"
    request = Request(url, headers={
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
    })
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode())


def validate_gh_token(repo_url=None):
    """Validate GitHub token exists, is valid, and has repo access."""
    console.print("\n[bold]Checking GitHub token...[/bold]")

    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("ER_SETUP_TOKEN") or ""
    if not gh_token:
        _check_fail("GH_TOKEN or ER_SETUP_TOKEN not set")
        raise PreflightError(
            "No GitHub token found. Set GH_TOKEN or ER_SETUP_TOKEN environment variable."
        )
    _check_pass("Token environment variable found")

    try:
        user_data = _github_api_get("/user", gh_token)
        username = user_data.get("login", "unknown")
        _check_pass(f"Token is valid (authenticated as: {username})")
    except HTTPError as error:
        _check_fail(f"Token validation failed (HTTP {error.code})")
        if error.code == 401:
            raise PreflightError("GitHub token is invalid or expired.") from error
        raise PreflightError(f"GitHub API error: HTTP {error.code}") from error
    except URLError as error:
        _check_fail(f"Cannot reach GitHub API: {error.reason}")
        raise PreflightError(f"Cannot reach GitHub API: {error.reason}") from error

    if repo_url:
        repo_url_clean = repo_url.rstrip("/")
        if repo_url_clean.endswith(".git"):
            repo_url_clean = repo_url_clean[:-4]
        repo_path = repo_url_clean.split("github.com/")[-1]
        try:
            _github_api_get(f"/repos/{repo_path}", gh_token)
            _check_pass(f"Token has access to {repo_path}")
        except HTTPError as error:
            _check_fail(f"Cannot access {repo_path} (HTTP {error.code})")
            if error.code == 404:
                raise PreflightError(
                    f"Token does not have access to {repo_path}. "
                    "Check the token has 'repo' scope and org access."
                ) from error
            raise PreflightError(
                f"GitHub API error for {repo_path}: HTTP {error.code}"
            ) from error

    return gh_token


def validate_claude_credentials():
    """Validate Claude credentials file exists and is structurally valid."""
    console.print("\n[bold]Checking Claude credentials...[/bold]")

    credentials_path = CLAUDE_HOME / ".credentials.json"
    if not credentials_path.exists():
        _check_fail(f"Credentials file not found: {credentials_path}")
        raise PreflightError(
            f"Claude credentials not found at {credentials_path}. "
            "Run 'claude' to authenticate first."
        )
    _check_pass("Credentials file exists")

    try:
        with open(credentials_path, encoding="utf-8") as credentials_file:
            credentials = json.load(credentials_file)
    except json.JSONDecodeError as error:
        _check_fail("Credentials file is not valid JSON")
        raise PreflightError(f"Invalid JSON in {credentials_path}") from error
    _check_pass("Credentials file is valid JSON")

    oauth_data = credentials.get("claudeAiOauth", {})
    access_token = oauth_data.get("accessToken", "")
    if not access_token:
        _check_fail("No accessToken found in credentials")
        raise PreflightError(
            "Claude credentials missing accessToken. Re-run 'claude' to authenticate."
        )
    _check_pass("Access token present")

    expires_at_ms = oauth_data.get("expiresAt", 0)
    now_ms = int(time.time() * 1000)
    if expires_at_ms and expires_at_ms < now_ms:
        refresh_token = oauth_data.get("refreshToken", "")
        if refresh_token:
            _check_warn("Access token expired, but refresh token exists (Claude may auto-refresh)")
        else:
            _check_fail("Access token expired and no refresh token")
            raise PreflightError(
                "Claude access token has expired. Re-run 'claude' to re-authenticate."
            )
    else:
        remaining_hours = (expires_at_ms - now_ms) / (1000 * 60 * 60)
        _check_pass(f"Access token valid ({remaining_hours:.0f}h remaining)")


def validate_claude_auth_works():
    """Run a minimal Claude prompt to verify auth actually works."""
    console.print("\n[bold]Testing Claude authentication...[/bold]")

    try:
        result = subprocess.run(
            ["claude", "-p", "say ok", "--max-turns", "1"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            _check_pass("Claude auth verified (test prompt succeeded)")
        else:
            stderr_preview = result.stderr.strip()[:200] if result.stderr else "no stderr"
            _check_fail(f"Claude test prompt failed (exit {result.returncode}): {stderr_preview}")
            raise PreflightError(
                f"Claude auth test failed. Exit code: {result.returncode}. "
                f"stderr: {stderr_preview}"
            )
    except FileNotFoundError as error:
        _check_fail("'claude' command not found on host")
        raise PreflightError(
            "'claude' is not installed on the host. Install with: npm install -g @anthropic-ai/claude-code"
        ) from error
    except subprocess.TimeoutExpired:
        _check_warn("Claude test prompt timed out (30s) - proceeding anyway")


def validate_docker_available():
    """Check that docker is available and running."""
    console.print("\n[bold]Checking Docker...[/bold]")

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            _check_pass("Docker is available and running")
        else:
            _check_fail("Docker is not running or not accessible")
            raise PreflightError(
                "Docker is not running. Start Docker and try again."
            )
    except FileNotFoundError as error:
        _check_fail("'docker' command not found")
        raise PreflightError("Docker is not installed.") from error


def run_all_preflight_checks(repo_url=None):
    """Run all preflight checks. Raises PreflightError on first failure."""
    console.print(Panel("[bold]Preflight Checks[/bold]", expand=False))

    validate_docker_available()
    gh_token = validate_gh_token(repo_url=repo_url)
    validate_claude_credentials()
    validate_claude_auth_works()

    console.print("\n[bold green]All preflight checks passed![/bold green]\n")
    return gh_token
