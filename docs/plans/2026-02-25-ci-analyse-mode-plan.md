# CI Analyse Mode Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an "Analyse CI" menu item with two sub-modes: a fast remote-only analysis (fetch + filter GH Actions logs + Claude haiku diagnosis), and a full parallel mode that also reproduces locally in Docker with a split Rich Live display.

**Architecture:** New `ci_analyse.py` module. Sub-menu after URL input: "Remote only (fast)" skips Docker entirely, just fetches/filters/diagnoses. "Remote + local reproduction" runs both pipelines in parallel with `SplitPanelDisplay`. Shared helpers for fetching/filtering/diagnosing reused by both paths. Existing modules untouched.

**Tech Stack:** Python 3.6+, Rich (Live, Layout, Panel, Text), threading, subprocess, `gh` CLI, `claude` CLI on host

---

### Task 1: Add log filtering module `ci_log_filter.py`

Pre-processes raw GH Actions logs to extract only failure-relevant lines, reducing tokens before sending to Claude.

**Files:**
- Create: `bin/ci_tool/ci_log_filter.py`

**Step 1: Create the log filter module**

```python
#!/usr/bin/env python3
"""Filter CI logs to extract failure-relevant lines."""
from __future__ import annotations

import re

# Patterns that indicate a failure or error in ROS/colcon CI output
FAILURE_PATTERNS = [
    re.compile(r'(?i)\bFAILURE\b'),
    re.compile(r'(?i)\bFAILED\b'),
    re.compile(r'(?i)\bERROR\b'),
    re.compile(r'(?i)\b(?:Assertion|Assert)Error\b'),
    re.compile(r'(?i)\bassert\b.*(?:!=|==|is not|not in)'),
    re.compile(r'\[FAIL\]'),
    re.compile(r'\[ERROR\]'),
    re.compile(r'(?i)ERRORS?:?\s*\d+'),
    re.compile(r'(?i)failures?:?\s*\d+'),
    re.compile(r'(?i)Traceback \(most recent call last\)'),
    re.compile(r'(?i)raise\s+\w+Error'),
    re.compile(r'(?i)E\s+\w+Error:'),
    re.compile(r'---\s*\>\s*'),
]

ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*m')

CONTEXT_LINES_BEFORE = 3
CONTEXT_LINES_AFTER = 5
MAX_OUTPUT_LINES = 300


def strip_ansi(text):
    """Remove ANSI escape codes from text."""
    return ANSI_ESCAPE.sub('', text)


def strip_gh_log_timestamps(line):
    """Strip GitHub Actions log timestamp prefixes like '2024-01-15T10:30:00.1234567Z '."""
    return re.sub(r'^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*', '', line)


def filter_ci_logs(raw_logs):
    """Extract failure-relevant lines from raw CI logs with surrounding context.

    Returns a string of filtered lines ready for analysis, or empty string if
    no failure patterns found.
    """
    lines = raw_logs.split('\n')
    cleaned_lines = [strip_gh_log_timestamps(strip_ansi(line)) for line in lines]

    matching_line_indices = set()
    for line_index, line in enumerate(cleaned_lines):
        for pattern in FAILURE_PATTERNS:
            if pattern.search(line):
                matching_line_indices.add(line_index)
                break

    if not matching_line_indices:
        return ""

    included_line_indices = set()
    for match_index in sorted(matching_line_indices):
        context_start = max(0, match_index - CONTEXT_LINES_BEFORE)
        context_end = min(len(cleaned_lines), match_index + CONTEXT_LINES_AFTER + 1)
        for context_index in range(context_start, context_end):
            included_line_indices.add(context_index)

    result_lines = []
    previous_index = -2
    for line_index in sorted(included_line_indices):
        if line_index > previous_index + 1:
            result_lines.append("---")
        result_lines.append(cleaned_lines[line_index])
        previous_index = line_index

    if len(result_lines) > MAX_OUTPUT_LINES:
        result_lines = result_lines[:MAX_OUTPUT_LINES]
        result_lines.append(f"\n... (truncated at {MAX_OUTPUT_LINES} lines)")

    return '\n'.join(result_lines)
```

**Step 2: Verify pylint passes**

Run: `pylint --rcfile=pylintrc bin/ci_tool/ci_log_filter.py`
Expected: Score 10.0/10

**Step 3: Commit**

```bash
git add bin/ci_tool/ci_log_filter.py
git commit -m "Add CI log filter module for pre-processing GH Actions logs"
```

---

### Task 2: Add split-panel display class `ci_analyse_display.py`

Thread-safe Rich Live Layout with two panels. Only used by the "full" parallel mode.

**Files:**
- Create: `bin/ci_tool/ci_analyse_display.py`

**Step 1: Create the display module**

```python
#!/usr/bin/env python3
"""Split-panel Rich Live display for parallel CI analysis."""
from __future__ import annotations

import threading

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


class SplitPanelDisplay:
    """Thread-safe split-panel display using Rich Live Layout.

    Top panel: remote CI log analysis
    Bottom panel: local reproduction progress
    """

    def __init__(self):
        self._console = Console()
        self._lock = threading.Lock()
        self._remote_lines = []
        self._local_lines = []
        self._remote_status = "Waiting..."
        self._local_status = "Waiting..."
        self._live = None

    def _build_panel_content(self, lines, status):
        """Build panel content from accumulated lines and current status."""
        if not lines:
            return Text(status, style="dim")
        text = Text()
        for line in lines[-30:]:
            text.append(line + "\n")
        text.append(f"\n[{status}]", style="dim")
        return text

    def _build_layout(self):
        """Build the full layout with both panels."""
        layout = Layout()
        with self._lock:
            remote_content = self._build_panel_content(
                self._remote_lines, self._remote_status
            )
            local_content = self._build_panel_content(
                self._local_lines, self._local_status
            )
        layout.split_column(
            Layout(
                Panel(
                    remote_content,
                    title="Remote CI Logs",
                    border_style="cyan",
                ),
                name="remote",
            ),
            Layout(
                Panel(
                    local_content,
                    title="Local Reproduction",
                    border_style="green",
                ),
                name="local",
            ),
        )
        return layout

    def append_remote(self, line):
        """Append a line to the remote panel (thread-safe)."""
        with self._lock:
            self._remote_lines.append(line)

    def append_local(self, line):
        """Append a line to the local panel (thread-safe)."""
        with self._lock:
            self._local_lines.append(line)

    def set_remote_status(self, status):
        """Update the remote panel status text (thread-safe)."""
        with self._lock:
            self._remote_status = status

    def set_local_status(self, status):
        """Update the local panel status text (thread-safe)."""
        with self._lock:
            self._local_status = status

    def start(self):
        """Start the live display. Returns the Live context for use with 'with'."""
        self._live = Live(
            self._build_layout(),
            console=self._console,
            refresh_per_second=4,
        )
        return self._live

    def refresh(self):
        """Refresh the display with current state."""
        if self._live:
            self._live.update(self._build_layout())

    def get_remote_lines(self):
        """Return a copy of all remote lines."""
        with self._lock:
            return list(self._remote_lines)

    def get_local_lines(self):
        """Return a copy of all local lines."""
        with self._lock:
            return list(self._local_lines)
```

**Step 2: Verify pylint passes**

Run: `pylint --rcfile=pylintrc bin/ci_tool/ci_analyse_display.py`
Expected: Score 10.0/10

**Step 3: Commit**

```bash
git add bin/ci_tool/ci_analyse_display.py
git commit -m "Add split-panel display for parallel CI analysis"
```

---

### Task 3: Add main analyse module `ci_analyse.py`

Core module with two code paths: remote-only (fast) and full parallel. Shared helpers for fetching, filtering, and diagnosing are used by both.

**Files:**
- Create: `bin/ci_tool/ci_analyse.py`

**Step 1: Create the analyse module**

```python
#!/usr/bin/env python3
"""Analyse CI failures from remote logs, optionally with local reproduction."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

from InquirerPy import inquirer
from rich.console import Console
from rich.panel import Panel

from ci_tool.ci_analyse_display import SplitPanelDisplay
from ci_tool.ci_fix import (
    build_analysis_prompt,
    drop_to_shell,
    extract_info_from_ci_url,
    prompt_for_session_name,
    refresh_claude_config,
    run_claude_workflow,
)
from ci_tool.ci_log_filter import filter_ci_logs
from ci_tool.ci_reproduce import _parse_repo_url, reproduce_ci
from ci_tool.claude_setup import (
    copy_learnings_to_container,
    is_claude_installed_in_container,
    save_package_list,
    setup_claude_in_container,
)
from ci_tool.containers import container_exists, remove_container
from ci_tool.preflight import run_all_preflight_checks, PreflightError

console = Console()

REMOTE_ANALYSIS_PROMPT = (
    "You are analysing CI failure logs from GitHub Actions. "
    "The logs have been pre-filtered to show only failure-relevant lines.\n\n"
    "For each failure, report:\n"
    "- Package and test name\n"
    "- The error/assertion message\n"
    "- Your hypothesis for the root cause\n"
    "- Suggested fix strategy\n\n"
    "Be concise. Here are the filtered CI logs:\n\n{filtered_logs}"
)

ANALYSE_DEPTH_CHOICES = [
    {"name": "Remote only (fast — no Docker)", "value": "remote_only"},
    {"name": "Remote + local reproduction (parallel)", "value": "full"},
]


def _prompt_for_ci_url():
    """Ask user for the GitHub Actions run URL (required)."""
    ci_run_url = inquirer.text(
        message="GitHub Actions run URL:",
        validate=lambda url: "/runs/" in url,
        invalid_message=(
            "URL must contain /runs/ "
            "(e.g. https://github.com/org/repo/actions/runs/12345)"
        ),
    ).execute().strip()

    ci_run_info = extract_info_from_ci_url(ci_run_url)
    console.print(f"  [green]Repo:[/green] {ci_run_info['repo_url']}")
    console.print(f"  [green]Branch:[/green] {ci_run_info['branch']}")
    console.print(f"  [green]Run ID:[/green] {ci_run_info['run_id']}")
    return ci_run_info


def _prompt_for_analyse_depth():
    """Ask user whether to do remote-only or full parallel analysis."""
    return inquirer.select(
        message="Analysis depth:",
        choices=ANALYSE_DEPTH_CHOICES,
        default="remote_only",
    ).execute()


# ---------------------------------------------------------------------------
# Shared helpers (used by both remote-only and full mode)
# ---------------------------------------------------------------------------

def _fetch_failed_logs(run_id, owner_repo):
    """Fetch failed job logs from GitHub Actions via gh CLI."""
    result = subprocess.run(
        ["gh", "run", "view", run_id, "--log-failed",
         "--repo", owner_repo],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to fetch CI logs (exit {result.returncode}): "
            f"{result.stderr.strip()[:300]}"
        )
    if not result.stdout.strip():
        raise RuntimeError("GH Actions returned empty log output")
    return result.stdout


def _run_claude_haiku_on_host(prompt):
    """Run Claude haiku on the host to analyse filtered logs."""
    escaped_prompt = prompt.replace("'", "'\\''")
    result = subprocess.run(
        ["claude", "--model", "haiku", "-p", escaped_prompt,
         "--max-turns", "1"],
        capture_output=True, text=True, check=False, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Claude haiku failed (exit {result.returncode}): "
            f"{result.stderr.strip()[:300]}"
        )
    return result.stdout.strip()


def _fetch_filter_and_diagnose(ci_run_info):
    """Fetch GH Actions logs, filter, and diagnose with Claude haiku.

    Returns (filtered_logs, diagnosis) tuple.
    diagnosis is None if Claude haiku fails (filtered_logs still returned).
    """
    run_id = ci_run_info["run_id"]
    owner_repo = ci_run_info["owner_repo"]

    console.print("[cyan]Fetching CI logs...[/cyan]")
    raw_logs = _fetch_failed_logs(run_id, owner_repo)
    console.print(f"  Fetched {len(raw_logs)} chars of log output")

    console.print("[cyan]Filtering logs...[/cyan]")
    filtered_logs = filter_ci_logs(raw_logs)
    if not filtered_logs:
        console.print("[yellow]No failure patterns found in CI logs.[/yellow]")
        return "", None

    filtered_line_count = len(filtered_logs.split('\n'))
    console.print(f"  Filtered to {filtered_line_count} lines")

    console.print("[cyan]Analysing with Claude haiku...[/cyan]")
    try:
        analysis_prompt = REMOTE_ANALYSIS_PROMPT.format(
            filtered_logs=filtered_logs
        )
        diagnosis = _run_claude_haiku_on_host(analysis_prompt)
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        console.print(
            f"[yellow]Claude haiku failed: {error}[/yellow]\n"
            "[yellow]Showing filtered logs instead.[/yellow]"
        )
        diagnosis = None

    return filtered_logs, diagnosis


# ---------------------------------------------------------------------------
# Remote-only mode
# ---------------------------------------------------------------------------

def _run_remote_only(ci_run_info):
    """Fast remote-only analysis: fetch, filter, diagnose, print report."""
    filtered_logs, diagnosis = _fetch_filter_and_diagnose(ci_run_info)

    console.print()
    console.print(Panel("[bold cyan]Remote CI Analysis[/bold cyan]", expand=False))

    if diagnosis:
        console.print(diagnosis)
    elif filtered_logs:
        console.print(filtered_logs)
    else:
        console.print("[yellow]No failures found in CI logs.[/yellow]")


# ---------------------------------------------------------------------------
# Full parallel mode
# ---------------------------------------------------------------------------

def _gather_full_mode_session_info(ci_run_info):
    """Collect extra session info needed for local reproduction."""
    only_needed_deps = not inquirer.confirm(
        message="Build everything (slower, disable --only-needed-deps)?",
        default=False,
    ).execute()

    container_name = prompt_for_session_name(ci_run_info["branch"])

    return {
        "ci_run_info": ci_run_info,
        "repo_url": ci_run_info["repo_url"],
        "branch": ci_run_info["branch"],
        "only_needed_deps": only_needed_deps,
        "container_name": container_name,
    }


def _remote_analysis_thread(ci_run_info, display):
    """Thread: fetch GH Actions logs, filter, analyse with Claude haiku."""
    run_id = ci_run_info["run_id"]
    owner_repo = ci_run_info["owner_repo"]

    try:
        display.set_remote_status("Fetching CI logs...")
        display.refresh()
        raw_logs = _fetch_failed_logs(run_id, owner_repo)
        display.append_remote(f"Fetched {len(raw_logs)} chars of log output")

        display.set_remote_status("Filtering logs...")
        display.refresh()
        filtered_logs = filter_ci_logs(raw_logs)
        if not filtered_logs:
            display.append_remote("No failure patterns found in CI logs")
            display.set_remote_status("Done (no failures found)")
            display.refresh()
            return

        filtered_line_count = len(filtered_logs.split('\n'))
        display.append_remote(f"Filtered to {filtered_line_count} lines")

        display.set_remote_status("Analysing with Claude haiku...")
        display.refresh()
        analysis_prompt = REMOTE_ANALYSIS_PROMPT.format(
            filtered_logs=filtered_logs
        )
        diagnosis = _run_claude_haiku_on_host(analysis_prompt)
        for line in diagnosis.split('\n'):
            display.append_remote(line)

        display.set_remote_status("Done")
        display.refresh()

    except (RuntimeError, subprocess.TimeoutExpired) as error:
        display.append_remote(f"ERROR: {error}")
        display.set_remote_status("Failed")
        display.refresh()


def _local_reproduction_thread(session, gh_token, display):
    """Thread: reproduce CI locally in Docker, then analyse."""
    container_name = session["container_name"]

    try:
        display.set_local_status("Creating container & running CI...")
        display.refresh()

        if container_exists(container_name):
            remove_container(container_name)
        reproduce_ci(
            repo_url=session["repo_url"],
            branch=session["branch"],
            container_name=container_name,
            gh_token=gh_token,
            only_needed_deps=session["only_needed_deps"],
        )
        save_package_list(container_name)
        display.append_local("Container ready, build and tests complete")

        display.set_local_status("Setting up Claude in container...")
        display.refresh()
        if is_claude_installed_in_container(container_name):
            refresh_claude_config(container_name)
        else:
            setup_claude_in_container(container_name)

        org, repo_name, _ = _parse_repo_url(session["repo_url"])
        if org and repo_name:
            copy_learnings_to_container(container_name, org, repo_name)

        display.set_local_status("Analysing local test failures with Claude...")
        display.refresh()
        analysis_prompt = build_analysis_prompt(session["ci_run_info"])
        _run_container_analysis(container_name, analysis_prompt, display)

        display.set_local_status("Done")
        display.refresh()

    except (RuntimeError, KeyboardInterrupt) as error:
        display.append_local(f"ERROR: {error}")
        display.set_local_status("Failed")
        display.refresh()


def _run_container_analysis(container_name, prompt, display):
    """Run Claude analysis inside container, capturing output to local panel."""
    escaped_prompt = prompt.replace("'", "'\\''")
    claude_command = (
        f"cd /ros_ws && IS_SANDBOX=1 claude --dangerously-skip-permissions "
        f"-p '{escaped_prompt}' --max-turns 10 --output-format stream-json "
        f"2>/ros_ws/.claude_stderr.log"
    )
    process = subprocess.Popen(
        ["docker", "exec", "-e", "IS_SANDBOX=1", container_name,
         "bash", "-c", claude_command],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    for raw_line in process.stdout:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
            _handle_stream_event(event, display)
        except json.JSONDecodeError:
            pass
    process.wait()


def _handle_stream_event(event, display):
    """Handle a Claude stream-json event, appending text to the local panel."""
    if event.get("type") != "assistant":
        return
    for block in event.get("message", {}).get("content", []):
        if block.get("type") == "text":
            text = block.get("text", "")
            if text.strip():
                for text_line in text.split('\n'):
                    display.append_local(text_line)
                display.refresh()


def _print_combined_report(display):
    """Print the combined analysis report after both threads complete."""
    console.print("\n")
    console.print(Panel("[bold cyan]Combined Analysis Report[/bold cyan]", expand=False))

    remote_lines = display.get_remote_lines()
    local_lines = display.get_local_lines()

    if remote_lines:
        console.print("\n[bold]Remote CI Analysis:[/bold]")
        for line in remote_lines:
            console.print(f"  {line}")

    if local_lines:
        console.print("\n[bold]Local Reproduction Analysis:[/bold]")
        for line in local_lines:
            console.print(f"  {line}")

    if not remote_lines and not local_lines:
        console.print("[yellow]No analysis results from either source.[/yellow]")


def _offer_fix_transition(container_name, ci_run_info):
    """Ask user if they want to proceed to fix mode."""
    proceed = inquirer.confirm(
        message="Proceed to fix with Claude?",
        default=True,
    ).execute()

    if proceed:
        console.print("\n[bold cyan]Transitioning to fix mode...[/bold cyan]")
        run_claude_workflow(container_name, ci_run_info)
        drop_to_shell(container_name)
    else:
        shell_choice = inquirer.confirm(
            message="Drop into container shell?",
            default=True,
        ).execute()
        if shell_choice:
            drop_to_shell(container_name)


def _run_full_parallel(ci_run_info, gh_token):
    """Full parallel analysis with split display: remote + local."""
    session = _gather_full_mode_session_info(ci_run_info)
    display = SplitPanelDisplay()

    remote_thread = threading.Thread(
        target=_remote_analysis_thread,
        args=(ci_run_info, display),
        daemon=True,
    )
    local_thread = threading.Thread(
        target=_local_reproduction_thread,
        args=(session, gh_token, display),
        daemon=True,
    )

    try:
        with display.start() as _live:
            remote_thread.start()
            local_thread.start()
            while remote_thread.is_alive() or local_thread.is_alive():
                display.refresh()
                time.sleep(0.25)
            display.refresh()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        return

    _print_combined_report(display)
    _offer_fix_transition(session["container_name"], ci_run_info)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def analyse_ci(_args):
    """Main analyse workflow: URL -> depth choice -> run analysis -> report."""
    console.print(
        Panel("[bold cyan]Analyse CI[/bold cyan]", expand=False)
    )

    ci_run_info = _prompt_for_ci_url()
    analyse_depth = _prompt_for_analyse_depth()

    if analyse_depth == "remote_only":
        _run_remote_only(ci_run_info)
        return

    # Full mode needs preflight checks (Docker, token, Claude credentials)
    try:
        gh_token = run_all_preflight_checks(
            repo_url=ci_run_info["repo_url"]
        )
    except PreflightError as error:
        console.print(
            f"\n[bold red]Preflight failed:[/bold red] {error}"
        )
        sys.exit(1)

    _run_full_parallel(ci_run_info, gh_token)
```

**Step 2: Verify pylint passes**

Run: `pylint --rcfile=pylintrc bin/ci_tool/ci_analyse.py`
Expected: Score 10.0/10

**Step 3: Commit**

```bash
git add bin/ci_tool/ci_analyse.py
git commit -m "Add CI analysis module with remote-only and full parallel modes"
```

---

### Task 4: Wire up the menu in `cli.py`

Add the "Analyse CI" menu item and dispatcher entry.

**Files:**
- Modify: `bin/ci_tool/cli.py:14-22` (MENU_CHOICES)
- Modify: `bin/ci_tool/cli.py:24-40` (HELP_TEXT)
- Modify: `bin/ci_tool/cli.py:73-80` (dispatch_subcommand handlers)
- Add: `_handle_analyse()` function

**Step 1: Add "Analyse CI" to MENU_CHOICES**

In `bin/ci_tool/cli.py`, insert after "Reproduce CI" (line 15):

```python
MENU_CHOICES = [
    {"name": "Reproduce CI (create container)", "value": "reproduce"},
    {"name": "Analyse CI", "value": "analyse"},
    {"name": "Fix CI with Claude", "value": "fix"},
    {"name": "Claude session (interactive)", "value": "claude"},
    {"name": "Shell into container", "value": "shell"},
    {"name": "Re-run tests in container", "value": "retest"},
    {"name": "Clean up containers", "value": "clean"},
    {"name": "Exit", "value": "exit"},
]
```

**Step 2: Update HELP_TEXT**

Add `analyse` to the commands list:

```
Commands:
  analyse      Analyse CI failures (remote-only or with local reproduction)
  fix          Fix CI failures with Claude
  reproduce    Reproduce CI environment in Docker
  claude       Interactive Claude session in container
  shell        Shell into an existing CI container
  retest       Re-run tests in a CI container
  clean        Remove CI containers
```

**Step 3: Add handler to dispatch_subcommand**

Add `"analyse": _handle_analyse` to the handlers dict:

```python
handlers = {
    "reproduce": _handle_reproduce,
    "analyse": _handle_analyse,
    "fix": _handle_fix,
    "claude": _handle_claude,
    "shell": _handle_shell,
    "retest": _handle_retest,
    "clean": _handle_clean,
}
```

**Step 4: Add _handle_analyse function**

Add alongside the other handler functions:

```python
def _handle_analyse(args):
    from ci_tool.ci_analyse import analyse_ci
    analyse_ci(args)
```

**Step 5: Verify pylint passes**

Run: `pylint --rcfile=pylintrc bin/ci_tool/cli.py`
Expected: Score 10.0/10

**Step 6: Commit**

```bash
git add bin/ci_tool/cli.py
git commit -m "Add 'Analyse CI' to main menu and command dispatch"
```

---

### Task 5: Manual end-to-end test

No unit tests yet — test the full workflow manually.

**Step 1: Verify the module loads**

Run: `cd /cortex/er_build_tools && python3 -c "from ci_tool.ci_analyse import analyse_ci; print('OK')"`
Expected: `OK`

**Step 2: Verify menu shows new option**

Run: `cd /cortex/er_build_tools && python3 -m ci_tool --help`
Expected: Output includes `analyse` in the commands list

**Step 3: Run full pylint on the package**

Run: `pylint --rcfile=pylintrc bin/ci_tool/`
Expected: Score 10.0/10

**Step 4: Test remote-only mode with a real GH Actions URL (if available)**

Run: `cd /cortex/er_build_tools && python3 -m ci_tool analyse`
- Select a GH Actions URL
- Choose "Remote only (fast)"
- Expected: fetches logs, filters, sends to Claude haiku, prints diagnosis

**Step 5: Commit any fixes needed**

```bash
git add -u
git commit -m "Fix issues found during manual testing of analyse mode"
```

---

### Task 6: Update CLAUDE.md project docs

**Files:**
- Modify: `CLAUDE.md` — add `analyse` to Running section and new files to Project Structure

**Step 1: Add to Running section**

```
ci_tool          # interactive menu
ci_tool analyse  # analyse CI failures (remote-only or with local reproduction)
ci_fix           # shortcut for ci_tool fix
```

**Step 2: Add to Project Structure**

Under `bin/ci_tool/`, add:

```
    ci_analyse.py         # CI analysis: remote-only or parallel with local reproduction
    ci_analyse_display.py # Split-panel Rich Live display for parallel analysis
    ci_log_filter.py      # Pre-filter GH Actions logs to reduce token usage
```

**Step 3: Verify pylint still passes**

Run: `pylint --rcfile=pylintrc bin/ci_tool/`
Expected: Score 10.0/10

**Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "Document new 'analyse' command in CLAUDE.md"
```
