#!/usr/bin/env python3
"""Install Claude Code in a container and copy auth/config from host."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from rich.console import Console

from ci_tool.containers import docker_exec, docker_cp_to_container, run_command

console = Console()

LEARNINGS_HOST_DIR = Path.home() / ".ci_tool" / "learnings"
LEARNINGS_CONTAINER_PATH = "/ros_ws/.ci_learnings.md"

CLAUDE_HOME = Path.home() / ".claude"
CI_CONTEXT_DIR = Path(__file__).parent / "ci_context"


def install_node_in_container(container_name):
    """Install Node.js 20 LTS in the container."""
    console.print("[cyan]Installing Node.js 20 in container...[/cyan]")
    docker_exec(container_name, (
        "curl -fsSL https://deb.nodesource.com/setup_20.x | bash - "
        "&& apt-get install -y nodejs"
    ))


def install_claude_in_container(container_name):
    """Install Claude Code via npm in the container."""
    console.print("[cyan]Installing Claude Code in container...[/cyan]")
    docker_exec(container_name, "npm install -g @anthropic-ai/claude-code")


def install_fzf_in_container(container_name):
    """Install fzf in the container (non-critical)."""
    console.print("[cyan]Installing fzf in container...[/cyan]")
    result = docker_exec(container_name, "apt-get update && apt-get install -y fzf", check=False)
    if result.returncode != 0:
        console.print("[yellow]fzf installation failed (non-critical) — continuing[/yellow]")


def install_python_deps_in_container(container_name):
    """Install ci_tool Python dependencies (rich, etc.) in the container."""
    requirements_file = Path(__file__).parent / "requirements.txt"
    if not requirements_file.exists():
        return

    console.print("[cyan]Installing Python dependencies in container...[/cyan]")
    docker_cp_to_container(
        str(requirements_file), container_name, "/tmp/ci_tool_requirements.txt"
    )
    docker_exec(
        container_name,
        "pip install --quiet -r /tmp/ci_tool_requirements.txt",
    )


def copy_claude_credentials(container_name):
    """Copy Claude credentials into the container."""
    credentials_path = CLAUDE_HOME / ".credentials.json"
    if not credentials_path.exists():
        raise RuntimeError(f"Claude credentials not found at {credentials_path}")

    console.print("[cyan]Copying Claude credentials...[/cyan]")
    docker_exec(container_name, "mkdir -p /root/.claude")
    docker_cp_to_container(
        str(credentials_path), container_name, "/root/.claude/.credentials.json"
    )


def copy_claude_config(container_name):
    """Copy CLAUDE.md and modified settings.json into the container."""
    claude_md_path = CLAUDE_HOME / "CLAUDE.md"
    settings_path = CLAUDE_HOME / "settings.json"

    if claude_md_path.exists():
        console.print("[cyan]Copying CLAUDE.md...[/cyan]")
        docker_cp_to_container(str(claude_md_path), container_name, "/root/.claude/CLAUDE.md")

    if settings_path.exists():
        console.print("[cyan]Copying settings.json (modified for dangerous mode)...[/cyan]")
        with open(settings_path, encoding="utf-8") as settings_file:
            settings = json.load(settings_file)

        settings.setdefault("permissions", {})["defaultMode"] = "dangerouslySkipPermissions"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json.dump(settings, tmp, indent=2)
            tmp_path = tmp.name

        try:
            docker_cp_to_container(tmp_path, container_name, "/root/.claude/settings.json")
        finally:
            os.unlink(tmp_path)


def copy_ci_context(container_name):
    """Copy CI-specific CLAUDE.md into the container, replacing the host's global CLAUDE.md."""
    ci_claude_md = CI_CONTEXT_DIR / "CLAUDE.md"
    if not ci_claude_md.exists():
        console.print("[yellow]CI context CLAUDE.md not found, skipping[/yellow]")
        return

    console.print("[cyan]Copying CI context CLAUDE.md...[/cyan]")
    docker_exec(container_name, "mkdir -p /root/.claude")
    docker_cp_to_container(str(ci_claude_md), container_name, "/root/.claude/CLAUDE.md")


def copy_display_script(container_name):
    """Copy the stream-json display processor into the container."""
    display_script = Path(__file__).parent / "display_progress.py"
    if not display_script.exists():
        raise RuntimeError(f"display_progress.py not found at {display_script}")

    console.print("[cyan]Copying ci_fix display script...[/cyan]")
    docker_cp_to_container(
        str(display_script), container_name, "/usr/local/bin/ci_fix_display"
    )
    docker_exec(container_name, "chmod +x /usr/local/bin/ci_fix_display")


RERUN_TESTS_FUNCTION = r'''
rerun_tests() {
    local packages_file="/ros_ws/.ci_packages"
    if [ ! -f "$packages_file" ]; then
        echo "No package list found at $packages_file"
        return 1
    fi
    local packages
    packages=$(tr '\n' ' ' < "$packages_file")
    echo "Rebuilding and testing: ${packages}"
    cd /ros_ws
    colcon build --packages-select ${packages} --cmake-args -DSETUPTOOLS_DEB_LAYOUT=OFF
    source /ros_ws/install/setup.bash
    colcon test --packages-select ${packages}
    for pkg in ${packages}; do
        if ! colcon test-result --test-result-base "build/$pkg/test_results"; then
            python3 - "build/$pkg/test_results" <<'PYEOF'
import sys, xml.etree.ElementTree as ET
from pathlib import Path
for p in sorted(Path(sys.argv[1]).rglob("*.xml")):
    for tc in ET.parse(p).iter("testcase"):
        for f in list(tc.iter("failure")) + list(tc.iter("error")):
            tag = "FAIL" if f.tag == "failure" else "ERROR"
            print(f"\n  {tag}: {tc.get('classname', '')}.{tc.get('name', '')}")
            if f.text:
                lines = f.text.strip().splitlines()
                for l in lines[:20]:
                    print(f"    {l}")
                if len(lines) > 20:
                    print(f"    ... ({len(lines) - 20} more lines)")
PYEOF
        fi
    done
}
'''


RESUME_CLAUDE_FUNCTION = r'''
resume_claude() {
    local state_file="/ros_ws/.ci_fix_state.json"
    if [ ! -f "$state_file" ]; then
        echo "No ci_fix state found. Run ci_fix first."
        return 1
    fi
    local session_id
    session_id=$(python3 -c "import json,sys; print(json.load(open('$state_file'))['session_id'])")
    if [ -z "$session_id" ] || [ "$session_id" = "None" ]; then
        echo "No session_id in state file. Starting fresh Claude session."
        cd /ros_ws && IS_SANDBOX=1 claude --dangerously-skip-permissions
        return
    fi
    echo "Resuming Claude session ${session_id}..."
    cd /ros_ws && IS_SANDBOX=1 claude --dangerously-skip-permissions --resume "$session_id"
}
'''


def inject_resume_function(container_name):
    """Add resume_claude bash function to the container's bashrc."""
    console.print("[cyan]Injecting resume_claude function...[/cyan]")
    marker = "# ci_fix resume_claude"
    check_command = f"grep -q '{marker}' /root/.bashrc"
    already_present = docker_exec(
        container_name, check_command, check=False, quiet=True,
    )
    if already_present.returncode == 0:
        return

    docker_exec(
        container_name,
        f"echo '{marker}' >> /root/.bashrc && cat >> /root/.bashrc << 'RESUME_EOF'\n"
        f"{RESUME_CLAUDE_FUNCTION}\nRESUME_EOF",
        quiet=True,
    )


def save_package_list(container_name):
    """Run colcon list in the container and save package names to /ros_ws/.ci_packages."""
    console.print("[cyan]Saving workspace package list...[/cyan]")
    result = docker_exec(
        container_name,
        "cd /ros_ws && colcon list --names-only > /ros_ws/.ci_packages",
        check=False,
        quiet=True,
    )
    if result.returncode != 0:
        console.print(
            "[yellow]Could not save package list (colcon list failed). "
            "The 'rerun_tests' helper will not work.[/yellow]"
        )


def inject_rerun_tests_function(container_name):
    """Add rerun_tests bash function to the container's bashrc."""
    console.print("[cyan]Injecting rerun_tests function...[/cyan]")
    marker = "# ci_fix rerun_tests"
    check_command = f"grep -q '{marker}' /root/.bashrc"
    already_present = docker_exec(
        container_name, check_command, check=False, quiet=True,
    )
    if already_present.returncode == 0:
        return

    docker_exec(
        container_name,
        f"echo '{marker}' >> /root/.bashrc && cat >> /root/.bashrc << 'RERUN_EOF'\n"
        f"{RERUN_TESTS_FUNCTION}\nRERUN_EOF",
        quiet=True,
    )


def copy_claude_memory(container_name):
    """Copy Claude project memory files into the container."""
    projects_dir = CLAUDE_HOME / "projects"
    if not projects_dir.exists():
        return

    console.print("[cyan]Copying Claude memory files...[/cyan]")
    for project_dir in projects_dir.iterdir():
        memory_dir = project_dir / "memory"
        if not memory_dir.exists():
            continue
        memory_files = [f for f in memory_dir.iterdir() if f.is_file()]
        if not memory_files:
            continue

        container_memory_path = f"/root/.claude/projects/{project_dir.name}/memory"
        docker_exec(container_name, f"mkdir -p {container_memory_path}")
        for memory_file in memory_files:
            docker_cp_to_container(
                str(memory_file),
                container_name,
                f"{container_memory_path}/{memory_file.name}",
            )


def _learnings_host_path(org, repo_name):
    """Return the host path for a repo's learnings file."""
    return LEARNINGS_HOST_DIR / f"{org}_{repo_name}.md"


def copy_learnings_to_container(container_name, org, repo_name):
    """Copy repo-specific learnings file into the container (if it exists)."""
    host_path = _learnings_host_path(org, repo_name)
    if not host_path.exists():
        return
    console.print("[cyan]Copying CI learnings into container...[/cyan]")
    docker_cp_to_container(str(host_path), container_name, LEARNINGS_CONTAINER_PATH)


def copy_learnings_from_container(container_name, org, repo_name):
    """Copy learnings file back from container to host (if Claude updated it)."""
    result = subprocess.run(
        ["docker", "exec", container_name, "test", "-s", LEARNINGS_CONTAINER_PATH],
        check=False,
    )
    if result.returncode != 0:
        return

    host_path = _learnings_host_path(org, repo_name)
    host_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        ["docker", "cp", f"{container_name}:{LEARNINGS_CONTAINER_PATH}", str(host_path)],
        quiet=True,
    )
    console.print(f"[green]Learnings saved to {host_path}[/green]")


def copy_helper_bash_functions(container_name):
    """Copy ~/.helper_bash_functions and source it in bashrc."""
    helper_path = Path.home() / ".helper_bash_functions"
    if not helper_path.exists():
        console.print("[yellow]~/.helper_bash_functions not found, skipping[/yellow]")
        return

    console.print("[cyan]Copying helper bash functions...[/cyan]")
    docker_cp_to_container(str(helper_path), container_name, "/root/.helper_bash_functions")
    docker_exec(
        container_name,
        "grep -q 'source ~/.helper_bash_functions' /root/.bashrc "
        "|| echo 'source ~/.helper_bash_functions' >> /root/.bashrc",
    )


def get_host_git_config(key):
    """Read a value from the host's git config."""
    result = subprocess.run(
        ["git", "config", "--global", key],
        capture_output=True, text=True, check=False,
    )
    value = result.stdout.strip()
    if not value:
        raise RuntimeError(
            f"git config --global {key} is not set on the host. "
            f"Set it with: git config --global {key} 'Your Value'"
        )
    return value


def configure_git_in_container(container_name):
    """Set up git identity, token-based auth, and gh CLI auth in the container."""
    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("ER_SETUP_TOKEN") or ""

    git_user_name = get_host_git_config("user.name")
    git_user_email = get_host_git_config("user.email")

    console.print("[cyan]Configuring git in container...[/cyan]")
    docker_exec(container_name, f'git config --global user.name "{git_user_name}"')
    docker_exec(container_name, f'git config --global user.email "{git_user_email}"')

    if not gh_token:
        console.print(
            "[yellow]No GH_TOKEN or ER_SETUP_TOKEN found — "
            "git auth and gh CLI will not be configured in container[/yellow]"
        )
        return

    docker_exec(
        container_name,
        f'git config --global url."https://{gh_token}@github.com/"'
        f'.insteadOf "https://github.com/"',
        quiet=True,
    )
    install_gh_cli(container_name)


def install_gh_cli(container_name):
    """Install gh CLI in the container.

    Authentication is handled by the GH_TOKEN env var already set on the container.
    """
    console.print("[cyan]Installing gh CLI in container...[/cyan]")
    install_result = docker_exec(container_name, (
        "type gh >/dev/null 2>&1 || ("
        "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg "
        "| dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg "
        "&& chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg "
        '&& echo "deb [arch=$(dpkg --print-architecture) '
        "signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] "
        'https://cli.github.com/packages stable main" '
        "| tee /etc/apt/sources.list.d/github-cli-stable.list > /dev/null "
        "&& apt-get update && apt-get install -y gh)"
    ), check=False)
    if install_result.returncode != 0:
        console.print(
            "[yellow]gh CLI installation failed — "
            "Claude will not be able to interact with GitHub from inside the container[/yellow]"
        )


def is_claude_installed_in_container(container_name):
    """Check if Claude Code is already installed in the container."""
    result = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c", "which claude"],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


def set_sandbox_env(container_name):
    """Set IS_SANDBOX=1 so Claude allows --dangerously-skip-permissions as root."""
    docker_exec(
        container_name,
        "grep -q 'export IS_SANDBOX=1' /root/.bashrc "
        "|| echo 'export IS_SANDBOX=1' >> /root/.bashrc",
        quiet=True,
    )


def setup_claude_in_container(container_name):
    """Full setup: install Claude Code and copy all config into container."""
    console.print("\n[bold cyan]Setting up Claude in container...[/bold cyan]")

    install_node_in_container(container_name)
    install_claude_in_container(container_name)
    install_fzf_in_container(container_name)
    install_python_deps_in_container(container_name)
    copy_claude_credentials(container_name)
    copy_claude_config(container_name)
    copy_ci_context(container_name)
    copy_display_script(container_name)
    inject_resume_function(container_name)
    inject_rerun_tests_function(container_name)
    set_sandbox_env(container_name)
    copy_claude_memory(container_name)
    copy_helper_bash_functions(container_name)
    configure_git_in_container(container_name)

    console.print("[bold green]Claude Code is installed and configured in the container[/bold green]")
