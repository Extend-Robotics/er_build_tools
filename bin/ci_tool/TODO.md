# ci_tool TODO

## Features

- [ ] **Analyse CI mode**: Implement the plan in `docs/plans/2026-02-25-ci-analyse-mode-plan.md`. New "Analyse CI" menu item with two sub-modes: "Remote only (fast)" fetches GH Actions logs, filters with regex, diagnoses with Claude haiku on host — no Docker. "Remote + local reproduction" runs both in parallel with a Rich Live split-panel display, then offers to transition into fix mode. New files: `ci_analyse.py`, `ci_analyse_display.py`, `ci_log_filter.py`.
- [x] ~~**Parallel CI analysis during local reproduction**~~: Superseded by the Analyse CI mode above.

## UX

- [ ] **Simplify the main menu**: Too many top-level options (reproduce, fix, claude, shell, retest, clean, exit). Several overlap — e.g. "Reproduce CI" is already a step within "Fix CI with Claude", and "Claude session" / "Shell into container" / "Re-run tests" are all post-reproduce actions on an existing container. Consolidate into fewer choices and push the rest into sub-menus or contextual prompts.

## Bug Fixes

- [ ] If branch name is empty/blank, default to the repo's default branch instead of requiring input
- [ ] In "Reproduce CI (create container)" mode, extract the branch name from the GitHub Actions URL (like `extract_info_from_ci_url` already does in "Fix CI with Claude" mode) instead of requiring the user to enter it manually

## Done
- [x] ~~Render markdown in terminal~~ — display_progress.py now buffers text between tool calls and renders via `rich.markdown.Markdown` (tables, headers, code blocks, bold/italic)
- [x] ~~Empty workspace after reproduce~~ — `_docker_exec_workspace_setup()` distinguishes setup failures from test failures; `wstool scrape` fixed in internal repo
- [x] ~~Silent failures~~ — 21 issues audited and fixed across all modules
- [x] ~~resume_claude auth~~ — `IS_SANDBOX=1` passed via `docker exec -e` on all calls (`.bashrc` not sourced by non-interactive shells)
- [x] ~~gh CLI auth warning~~ — removed redundant `gh auth login` (GH_TOKEN env var handles auth)
- [x] ~~Token efficiency~~ — prompts updated to use grep instead of reading full logs
- [x] ~~Persistent learnings~~ — `~/.ci_tool/learnings/{org}_{repo}.md` persists between sessions

## Testing

- [ ] Add unit tests for each module, leveraging the clean separation of concerns:
  - **ci_reproduce.py**: `_parse_repo_url` (edge cases: trailing slashes, `.git` suffix, invalid URLs, non-GitHub URLs), `_fetch_github_raw_file` (HTTP errors, timeouts, bad tokens), `prompt_for_reproduce_args` / `prompt_for_repo_and_branch` (input validation)
  - **ci_fix.py**: `extract_run_id_from_url` (valid/invalid/malformed URLs), `extract_info_from_ci_url` (API errors, missing fields, bad URLs), `gather_session_info` (all input combinations: with/without CI URL, new/resume, empty fields)
  - **containers.py**: `sanitize_container_name`, `container_exists`/`container_is_running` (mock docker calls)
  - **preflight.py**: each check in isolation (mock docker/gh/claude)
  - **cli.py**: `dispatch_subcommand` routing, `_handle_container_collision` (all three choices)
- [ ] Input validation / boundary tests: verify weird input combinations at module boundaries (e.g. gather_session_info output dict is always valid input for reproduce_ci, prompt outputs satisfy reproduce_ci preconditions)
- [ ] Integration-style tests: mock Docker/GitHub and run full flows end-to-end (new session, resume session, reproduce-only)
