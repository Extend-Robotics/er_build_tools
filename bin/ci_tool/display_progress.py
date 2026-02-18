#!/usr/bin/env python3
"""Display human-readable progress from Claude Code stream-json output.

Reads newline-delimited JSON from stdin (Claude's --output-format stream-json),
shows a live spinner + assistant text + tool activity via rich, captures the
session_id, and writes a state file on exit.

Designed to run INSIDE a CI container. Requires: rich (from requirements.txt).
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime, timezone

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

STATE_FILE = "/ros_ws/.ci_fix_state.json"

console = Console(stderr=True)


def write_state(session_id, phase, attempt_count=1):
    """Write the ci_fix state file."""
    state = {
        "session_id": session_id,
        "phase": phase,
        "attempt_count": attempt_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with open(STATE_FILE, "w", encoding="utf-8") as state_file:
        json.dump(state, state_file, indent=2)


def read_existing_attempt_count():
    """Read attempt_count from existing state file, or return 0."""
    try:
        with open(STATE_FILE, encoding="utf-8") as state_file:
            return json.load(state_file).get("attempt_count", 0)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return 0


def format_elapsed(start_time):
    """Format elapsed time as 'Xm Ys'."""
    elapsed_seconds = int(time.time() - start_time)
    minutes = elapsed_seconds // 60
    seconds = elapsed_seconds % 60
    if minutes > 0:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def main():
    """Read stream-json from stdin, display progress, write state on exit."""
    session_id = None
    attempt_count = read_existing_attempt_count() + 1
    phase = "fixing"
    start_time = time.time()
    current_activity = "Starting up"

    try:
        with Live(
            Spinner("dots", text=Text(f" {current_activity}...", style="cyan")),
            console=console,
            refresh_per_second=10,
            transient=True,
        ) as live:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if "session_id" in event and event["session_id"]:
                    session_id = event["session_id"]

                inner = event.get("event", event)
                event_type = inner.get("type", "")

                if event_type == "content_block_start":
                    block = inner.get("content_block", {})
                    if block.get("type") == "tool_use":
                        tool_name = block.get("name", "unknown")
                        current_activity = f"Using {tool_name}"
                        live.update(Spinner(
                            "dots",
                            text=Text(
                                f" {current_activity}  "
                                f"[{format_elapsed(start_time)}]",
                                style="cyan",
                            ),
                        ))
                        console.print(
                            f"  [dim]tool:[/dim] [bold]{tool_name}[/bold]"
                        )

                elif event_type == "content_block_delta":
                    delta = inner.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        console.file.write(text)
                        console.file.flush()
                        current_activity = "Thinking"
                        live.update(Spinner(
                            "dots",
                            text=Text(
                                f" {current_activity}  "
                                f"[{format_elapsed(start_time)}]",
                                style="cyan",
                            ),
                        ))

        phase = "completed"

    except KeyboardInterrupt:
        phase = "interrupted"
    except Exception:
        console.print(f"\n[red]Display processor error:[/red]\n{traceback.format_exc()}")
        phase = "stuck"

    write_state(session_id, phase, attempt_count)

    elapsed = format_elapsed(start_time)
    if session_id:
        console.print(
            f"\n[green]Session saved ({session_id}). "
            f"Elapsed: {elapsed}. Use 'resume_claude' to continue.[/green]"
        )
    else:
        console.print(f"\n[yellow]No session ID captured. Elapsed: {elapsed}.[/yellow]")


if __name__ == "__main__":
    main()
