# ci_tool TODO

## Bug Fixes

- [ ] If branch name is empty/blank, default to the repo's default branch instead of requiring input

## Testing

- [ ] Add unit tests for each module, leveraging the clean separation of concerns:
  - **ci_reproduce.py**: `_parse_repo_url` (edge cases: trailing slashes, `.git` suffix, invalid URLs, non-GitHub URLs), `_fetch_github_raw_file` (HTTP errors, timeouts, bad tokens), `prompt_for_reproduce_args` / `prompt_for_repo_and_branch` (input validation)
  - **ci_fix.py**: `extract_run_id_from_url` (valid/invalid/malformed URLs), `extract_info_from_ci_url` (API errors, missing fields, bad URLs), `gather_session_info` (all input combinations: with/without CI URL, new/resume, empty fields)
  - **containers.py**: `sanitize_container_name`, `container_exists`/`container_is_running` (mock docker calls)
  - **preflight.py**: each check in isolation (mock docker/gh/claude)
  - **cli.py**: `dispatch_subcommand` routing, `_handle_container_collision` (all three choices)
- [ ] Input validation / boundary tests: verify weird input combinations at module boundaries (e.g. gather_session_info output dict is always valid input for reproduce_ci, prompt outputs satisfy reproduce_ci preconditions)
- [ ] Integration-style tests: mock Docker/GitHub and run full flows end-to-end (new session, resume session, reproduce-only)
