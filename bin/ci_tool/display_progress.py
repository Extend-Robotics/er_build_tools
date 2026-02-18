#!/usr/bin/env python3
"""Display human-readable progress from Claude Code stream-json output.

Reads newline-delimited JSON from stdin (Claude Code's --output-format stream-json),
shows assistant text + tool activity via rich, captures the session_id, and
writes a state file on exit.

Claude Code stream-json event types:
  {"type":"system","subtype":"init","session_id":"..."}
  {"type":"assistant","message":{"content":[{"type":"text","text":"..."},
                                            {"type":"tool_use","name":"..."}]},
   "session_id":"..."}
  {"type":"tool_result","tool_use_id":"...","content":"...","session_id":"..."}
  {"type":"result","subtype":"success","result":"...","session_id":"..."}

Designed to run INSIDE a CI container via docker exec.
Requires: rich (from requirements.txt).
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime, timezone

from rich.console import Console

STATE_FILE = "/ros_ws/.ci_fix_state.json"
CLAUDE_STDERR_LOG = "/ros_ws/.claude_stderr.log"
EVENT_DEBUG_LOG = "/ros_ws/.ci_fix_events.jsonl"

# force_terminal=True is required because docker exec may not allocate a PTY,
# which would cause Rich to suppress all ANSI output.
console = Console(stderr=True, force_terminal=True)


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


def handle_assistant_event(message, start_time):
    """Display content blocks from an assistant message event."""
    for block in message.get("content", []):
        block_type = block.get("type", "")

        if block_type == "text":
            text = block.get("text", "")
            if text:
                sys.stderr.write(text)
                sys.stderr.flush()

        elif block_type == "tool_use":
            tool_name = block.get("name", "unknown")
            console.print(
                f"\n  [dim]tool:[/dim] [bold]{tool_name}[/bold] "
                f"[dim][{format_elapsed(start_time)}][/dim]"
            )


def handle_event(event, start_time):
    """Handle a single stream-json event. Returns session_id if found, else None."""
    session_id = event.get("session_id") or None
    event_type = event.get("type", "")

    if event_type == "assistant":
        message = event.get("message", {})
        handle_assistant_event(message, start_time)

    elif event_type == "tool_result":
        console.print(
            f"  [dim]done[/dim] [{format_elapsed(start_time)}]"
        )

    return session_id


def print_session_summary(session_id, start_time):
    """Print the final session summary after stream ends."""
    elapsed = format_elapsed(start_time)
    if session_id:
        console.print(
            f"\n[green]Session saved ({session_id}). "
            f"Elapsed: {elapsed}. Use 'resume_claude' to continue.[/green]"
        )
        return

    console.print(f"\n[yellow]No session ID captured. Elapsed: {elapsed}.[/yellow]")
    try:
        with open(CLAUDE_STDERR_LOG, encoding="utf-8") as stderr_log:
            stderr_content = stderr_log.read().strip()
        if stderr_content:
            console.print("[yellow]Claude stderr output:[/yellow]")
            console.print(stderr_content)
    except FileNotFoundError:
        pass


def main():
    """Read stream-json from stdin, display progress, write state on exit."""
    session_id = None
    attempt_count = read_existing_attempt_count() + 1
    phase = "fixing"
    start_time = time.time()

    try:
        console.print("[cyan]  Working...[/cyan]")

        with open(EVENT_DEBUG_LOG, "w", encoding="utf-8") as debug_log:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue

                debug_log.write(line + "\n")
                debug_log.flush()

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_session_id = handle_event(event, start_time)
                if event_session_id:
                    session_id = event_session_id

        phase = "completed"

    except KeyboardInterrupt:
        phase = "interrupted"
    except Exception:  # pylint: disable=broad-except
        console.print(
            f"\n[red]Display processor error:[/red]\n{traceback.format_exc()}"
        )
        phase = "stuck"

    write_state(session_id, phase, attempt_count)
    print_session_summary(session_id, start_time)


if __name__ == "__main__":
    main()
