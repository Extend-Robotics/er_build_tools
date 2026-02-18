#!/usr/bin/env python3
"""Interactive CLI menu for CI tool."""
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


def _handle_reproduce(args):
    from ci_tool.ci_reproduce import reproduce_ci
    reproduce_ci(args)


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
