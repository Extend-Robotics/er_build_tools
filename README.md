# er_build_tools

Public build tools and utilities for Extend Robotics repositories.

## Quick Setup

Install helper bash functions, set your GitHub token, and authenticate Claude:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/setup.sh)
```

This installs `~/.helper_bash_functions` which provides build helpers, git aliases, and `ci_tool`.

## ci_tool: Fix CI Failures with Claude

`ci_tool` is an interactive CLI that reproduces CI failures locally in Docker and uses Claude Code to fix them.

### Prerequisites

- **Docker** installed and running
- **Claude Code** installed and authenticated: `npm install -g @anthropic-ai/claude-code && claude`
- **GitHub token** with `repo` scope: [create one](https://github.com/settings/tokens)

### Usage

```bash
source ~/.helper_bash_functions
ci_tool
```

This opens an interactive menu. You can also run subcommands directly — see `ci_tool --help`:

```
$ ci_tool --help
ci_tool — Fix CI failures with Claude

Usage: ci_tool [command]

Commands:
  fix          Fix CI failures with Claude
  reproduce    Reproduce CI environment in Docker
  claude       Interactive Claude session in container
  shell        Shell into an existing CI container
  retest       Re-run tests in a CI container
  clean        Remove CI containers

Shortcuts:
  ci_fix       Alias for 'ci_tool fix'

Run without arguments for interactive menu.
```

### Fix Workflow

1. Run `ci_fix`
2. Create a new session or reuse an existing container
3. Optionally paste a GitHub Actions URL to target a specific failure
4. ci_tool reproduces the CI environment in Docker
5. Claude analyses the test output and applies fixes
6. You're dropped into a shell to review changes, commit, and push

### Manual Setup

If you prefer not to use the setup script:

1. Download helper functions:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/.helper_bash_functions \
     -o ~/.helper_bash_functions
   ```
2. Add your GitHub token to the top of `~/.helper_bash_functions`:
   ```bash
   export GH_TOKEN="ghp_your_token_here"
   ```
3. Source in your shell:
   ```bash
   echo 'source ~/.helper_bash_functions' >> ~/.bashrc
   source ~/.helper_bash_functions
   ```
4. Install and authenticate Claude Code:
   ```bash
   npm install -g @anthropic-ai/claude-code
   claude
   ```

---

## reproduce_ci.sh: Reproduce CI Locally

When CI fails, debugging requires pushing commits and waiting for results. This script reproduces the exact CI environment locally in a persistent Docker container, so you can debug interactively.

It creates a Docker container using the same image as CI, clones your repo and its dependencies, builds everything, and optionally runs tests, mirroring the steps in `setup_and_build_ros_ws.yml`.

### Quick Start

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/reproduce_ci.sh) \
  --gh-token "$GH_TOKEN" \
  --repo https://github.com/Extend-Robotics/er_interface \
  --only-needed-deps
```

### Requirements

- Docker installed and running
- A GitHub token (`--gh-token`) with access to Extend-Robotics private repos

### Options

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--gh-token` | `-t` | *required* | GitHub token with access to private repos |
| `--repo` | `-r` | *required* | Repository URL to test |
| `--branch` | `-b` | `main` | Branch or commit SHA to test |
| `--only-needed-deps` | | off | Only build deps needed by the repo under test (faster) |
| `--skip-tests` | | off | Skip running colcon tests |
| `--image` | `-i` | `rostooling/setup-ros-docker:ubuntu-focal-ros-noetic-desktop-latest` | Docker image |
| `--container-name` | `-n` | `er_ci_reproduced_testing_env` | Docker container name |
| `--deps-file` | `-d` | `deps.repos` | Path to deps file in the repo |
| `--graphical` | `-g` | `true` | Enable X11/NVIDIA forwarding |
| `--additional-command` | `-c` | | Extra command to run after build/test |
| `--scripts-branch` | | `main` | Branch of `er_build_tools_internal` to fetch scripts from |

### Examples

Test a specific branch with all deps:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/reproduce_ci.sh) \
  --gh-token "$GH_TOKEN" \
  --repo https://github.com/Extend-Robotics/er_interface \
  --branch my-feature-branch
```

Build only, skip tests, no graphical forwarding:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/reproduce_ci.sh) \
  --gh-token "$GH_TOKEN" \
  --repo https://github.com/Extend-Robotics/er_interface \
  --only-needed-deps \
  --skip-tests \
  --graphical false
```

Test with feature branches of both `er_build_tools` and `er_build_tools_internal` (useful when developing the CI scripts themselves):

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/my-feature-branch-in-er-build-tools/bin/reproduce_ci.sh) \
  --gh-token "$GH_TOKEN" \
  --repo https://github.com/Extend-Robotics/er_interface \
  --scripts-branch my-feature-branch-in-er-build-tools-internal \
  --only-needed-deps
```

Run xacro lint after build (like er_interface CI does):

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/reproduce_ci.sh) \
  --gh-token "$GH_TOKEN" \
  --repo https://github.com/Extend-Robotics/er_interface \
  --only-needed-deps \
  --additional-command "python3 ros_ws/src/er_interface/er_interface/src/er_interface/xacro_lint.py"
```

### After the Script Completes

The container stays running. You can enter it to debug interactively:

```bash
docker exec -it er_ci_reproduced_testing_env bash
```

The workspace is at `/ros_ws` inside the container.

To clean up:

```bash
docker rm -f er_ci_reproduced_testing_env
```

### Troubleshooting

**Container already exists**: Remove it first: `docker rm -f er_ci_reproduced_testing_env`

**404 when fetching scripts**: Check that your `--gh-token` has access to `er_build_tools_internal`, and that the `--scripts-branch` exists.

**`DISPLAY` error with graphical forwarding**: Either set `DISPLAY` (e.g. via X11 forwarding) or pass `--graphical false`.
