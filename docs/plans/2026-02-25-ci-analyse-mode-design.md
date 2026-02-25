# CI Analyse Mode Design

## Goal

Add an "Analyse CI" mode to ci_tool that diagnoses CI failures. Offers two
sub-modes: a fast remote-only analysis (fetch + filter GH Actions logs, diagnose
with Claude haiku), and a full parallel analysis that also reproduces locally in
Docker.

## User Flow

1. User selects **"Analyse CI"** from main menu (or `ci_tool analyse`)
2. Provides a **GitHub Actions URL** (required)
3. Sub-menu: **"Remote only (fast)"** vs **"Remote + local reproduction"**

### Remote only (fast)

4. Fetches GH Actions logs, filters with Python regex, sends reduced context
   to Claude haiku on host for diagnosis
5. Prints structured report
6. Done — no container, no Docker

### Remote + local reproduction

4. Provides build options (only-needed-deps) and session name
5. Two parallel threads start with a Rich Live split-panel display:
   - **Top panel ("Remote CI Logs"):** Same as remote-only pipeline above
   - **Bottom panel ("Local Reproduction"):** `reproduce_ci()` in Docker,
     then Claude analyses local `test_output.log` inside the container
6. Once both complete, prints combined summary
7. Asks: "Proceed to fix with Claude?" — if yes, transitions into existing
   `run_claude_workflow()` fix phase (container already set up)

## Architecture

### New file: `bin/ci_tool/ci_analyse.py`

Entry point `analyse_ci(args)`:
- Prompts for GH Actions URL (required)
- Sub-menu for analysis depth
- Remote-only: runs remote pipeline, prints report, exits
- Full: runs preflight, launches parallel threads with split display

### Remote Analysis Pipeline

```
gh run view {run_id} --log-failed
        |
        v
  Python regex filter (ci_log_filter.py)
  - Extract ERROR/FAIL/assertion/[FAIL] blocks
  - Keep ~5 lines context around each match
  - Strip ANSI codes, timestamps, build noise
        |
        v
  ~50-200 lines (vs thousands raw)
        |
        v
  claude --model haiku -p "Analyse these CI failures..."
        |
        v
  Structured diagnosis
```

### Local Reproduction Pipeline (full mode only)

```
reproduce_ci() (existing)
  - Create Docker container
  - Clone repo, install deps, build, run tests
        |
        v
  setup_claude_in_container() (existing)
        |
        v
  Claude analysis with ANALYSIS_PROMPT_TEMPLATE (existing)
        |
        v
  Local analysis results
```

### New file: `bin/ci_tool/ci_log_filter.py`

Python regex-based log filter. Extracts failure-relevant lines with surrounding
context, strips ANSI codes and timestamps. Reduces thousands of raw log lines
to ~50-200 lines for Claude haiku.

### New file: `bin/ci_tool/ci_analyse_display.py`

`SplitPanelDisplay` class for the full parallel mode:
- `rich.live.Live` with `rich.layout.Layout` (two rows)
- Thread-safe `append_remote()` / `append_local()` methods
- Auto-refreshes at 4 Hz

### Display Layout (full mode only)

```
+------------------------------------------+
| Remote CI Logs                           |
| Package: my_pkg                          |
| Test: test_something                     |
| Error: AssertionError: expected 5, got 3 |
| Diagnosis: ...                           |
+------------------------------------------+
| Local Reproduction                       |
| [Building workspace...]                  |
| [Running tests...]                       |
| [Analysing failures...]                  |
+------------------------------------------+
```

## Changes to Existing Files

- **`cli.py`**: Add `{"name": "Analyse CI", "value": "analyse"}` to
  `MENU_CHOICES`. Add `"analyse": _handle_analyse` to `dispatch_subcommand`.
  Add `_handle_analyse()` handler. Update `HELP_TEXT`.
- **No changes** to `ci_fix.py`, `ci_reproduce.py`, or other modules.

## Error Handling

- **GH logs unavailable:** Fail fast with clear error message
- **One thread fails (full mode):** Show partial results with warning
- **Claude haiku on host fails:** Fall back to displaying filtered logs raw
- **Container build fails (full mode):** Show error in bottom panel, remote
  analysis still completes

## Fix Transition (full mode only)

After both panels complete:
1. Print combined report
2. Prompt: "Proceed to fix with Claude?"
   - Yes: transition into existing `run_claude_workflow()` with container ready
   - No: offer shell access or exit

## Dependencies

- `rich` (already in requirements.txt) — Live, Layout, Panel, Text
- `gh` CLI (already required) — for `gh run view --log-failed`
- `claude` on host — for haiku analysis of filtered remote logs
- `threading` (stdlib) — parallel execution (full mode only)
