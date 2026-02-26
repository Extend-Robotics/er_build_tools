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
from rich.markdown import Markdown

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
    except FileNotFoundError:
        return 0
    except (json.JSONDecodeError, KeyError) as error:
        console.print(f"[yellow]State file corrupt, resetting attempt count: {error}[/yellow]")
        return 0


def format_elapsed(start_time):
    """Format elapsed time as 'Xm Ys'."""
    elapsed_seconds = int(time.time() - start_time)
    minutes = elapsed_seconds // 60
    seconds = elapsed_seconds % 60
    if minutes > 0:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def format_tool_status(tool_counts, start_time):
    """Format spinner status text showing tool activity summary."""
    if not tool_counts:
        return "[cyan]Working...[/cyan]"
    parts = []
    for name, count in tool_counts.items():
        if count > 1:
            parts.append(f"{name} x{count}")
        else:
            parts.append(name)
    return f"[cyan]{', '.join(parts)}[/cyan] [dim][{format_elapsed(start_time)}][/dim]"


def format_tool_summary(tool_counts):
    """Format a final one-line summary of all tools used."""
    parts = []
    for name, count in tool_counts.items():
        if count > 1:
            parts.append(f"{name} x{count}")
        else:
            parts.append(name)
    return ", ".join(parts)


def flush_text_buffer(text_buffer):
    """Render accumulated text as rich markdown and clear the buffer."""
    if not text_buffer:
        return
    combined = "".join(text_buffer)
    text_buffer.clear()
    if combined.strip():
        console.print(Markdown(combined))


def handle_assistant_event(message, start_time, text_buffer, tool_counts, status):
    """Display content blocks from an assistant message event."""
    for block in message.get("content", []):
        block_type = block.get("type", "")

        if block_type == "text":
            text = block.get("text", "")
            if text:
                text_buffer.append(text)

        elif block_type == "tool_use":
            flush_text_buffer(text_buffer)
            tool_name = block.get("name", "unknown")
            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
            status.update(format_tool_status(tool_counts, start_time))


def handle_event(event, start_time, text_buffer, tool_counts, status):
    """Handle a single stream-json event. Returns session_id if found, else None."""
    session_id = event.get("session_id") or None
    event_type = event.get("type", "")

    if event_type == "assistant":
        message = event.get("message", {})
        handle_assistant_event(message, start_time, text_buffer, tool_counts, status)

    elif event_type == "tool_result":
        flush_text_buffer(text_buffer)
        status.update(format_tool_status(tool_counts, start_time))

    return session_id


def print_session_summary(session_id, start_time, tool_counts):
    """Print the final session summary after stream ends."""
    elapsed = format_elapsed(start_time)
    if tool_counts:
        console.print(f"  [dim]Tools: {format_tool_summary(tool_counts)}[/dim]")
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
        console.print(f"[dim]No stderr log at {CLAUDE_STDERR_LOG}[/dim]")


def main():
    """Read stream-json from stdin, display progress, write state on exit."""
    session_id = None
    attempt_count = read_existing_attempt_count() + 1
    phase = "fixing"
    start_time = time.time()
    text_buffer = []
    tool_counts = {}

    try:
        with console.status("[cyan]Working...[/cyan]", spinner="dots") as status:
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
                        sys.stderr.write(f"  {line}\n")
                        continue

                    event_session_id = handle_event(event, start_time, text_buffer, tool_counts, status)
                    if event_session_id:
                        session_id = event_session_id

        flush_text_buffer(text_buffer)
        phase = "completed"

    except KeyboardInterrupt:
        phase = "interrupted"
    except (IOError, ValueError, UnicodeDecodeError):
        console.print(
            f"\n[red]Display processor error:[/red]\n{traceback.format_exc()}"
        )
        phase = "stuck"

    write_state(session_id, phase, attempt_count)
    print_session_summary(session_id, start_time, tool_counts)


if __name__ == "__main__":
    main()
