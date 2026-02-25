# CI Analyse Mode Design

## Goal

Add an "Analyse CI" mode to ci_tool that fetches and analyses GitHub Actions
failure logs in parallel with local Docker reproduction, displaying both in a
split Rich Live Layout, then offering to transition into fix mode.

## User Flow

1. User selects **"Analyse CI"** from main menu (or `ci_tool analyse`)
2. Provides a **GitHub Actions URL** (required)
3. Provides build options (only-needed-deps) and session name
4. Two parallel threads start with a Rich Live split-panel display:
   - **Top panel ("Remote CI Logs"):** Fetches GH Actions logs, filters them
     with Python regex, sends reduced context to Claude haiku for diagnosis
   - **Bottom panel ("Local Reproduction"):** Runs `reproduce_ci()` in Docker,
     then analyses local `test_output.log` with Claude inside the container
5. Once both complete, prints a combined summary
6. Asks: "Proceed to fix with Claude?" — if yes, transitions into existing
   `run_claude_workflow()` fix phase (container already set up)

## Architecture

### New file: `bin/ci_tool/ci_analyse.py`

Entry point `analyse_ci(args)`:
- Prompts for GH Actions URL (required for this mode)
- Prompts for build options + session name
- Runs preflight checks
- Launches `ParallelAnalyser`

### Remote Analysis Pipeline (top panel)

```
gh run view {run_id} --log-failed
        |
        v
  Python regex filter
  - Extract ERROR/FAIL/assertion/[FAIL] blocks
  - Keep ~5 lines context around each match
  - Strip ANSI codes, timestamps, build noise
  - Deduplicate (same error in summary + detail)
        |
        v
  ~50-200 lines (vs thousands raw)
        |
        v
  claude --model haiku -p "Analyse these CI failures..."
        |
        v
  Structured diagnosis displayed in top panel
```

### Local Reproduction Pipeline (bottom panel)

```
reproduce_ci() (existing)
  - Create Docker container
  - Clone repo, install deps, build, run tests
        |
        v
  setup_claude_in_container() (existing)
        |
        v
  run_claude_streamed() with ANALYSIS_PROMPT_TEMPLATE (existing)
        |
        v
  Analysis displayed in bottom panel
```

### ParallelAnalyser class

- Uses `threading.Thread` for both pipelines
- `rich.live.Live` with `rich.layout.Layout` (two rows)
- Each panel wraps a `rich.text.Text` that gets appended to from its thread
- Thread-safe updates via Lock
- Both threads capture their results for the combined summary

### Display Layout

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

- **GH logs unavailable:** Fail fast in top panel, local reproduction continues
- **One thread fails:** Show partial results with warning about failed side
- **Claude on host fails:** Fall back to displaying filtered logs raw
- **Container build fails:** Show build error in bottom panel, remote completes

## Fix Transition

After both panels complete:
1. Print combined report (findings from both remote and local analysis)
2. Prompt: "Proceed to fix with Claude?"
   - Yes: transition into existing `run_claude_workflow()` with container ready
   - No: offer shell access or exit

## Dependencies

- `rich` (already in requirements.txt) — Live, Layout, Panel, Text
- `gh` CLI (already required) — for `gh run view --log-failed`
- `claude` on host — for haiku analysis of filtered remote logs
- `threading` (stdlib) — parallel execution
