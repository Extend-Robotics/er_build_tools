#!/usr/bin/env python3
"""Container-side payload for er_update_source_repos.

Updates every git repo under <workspace>/src: fetches, resolves detached
HEADs to a branch (interactively when ambiguous), fast-forwards, and updates
submodules. Never touches dirty or diverged repos.

Auth: a GIT_ASKPASS helper feeds the PAT from $GITHUB_PAT so the token never
appears in argv, remote URLs, or git config.

Exit codes: 0 = at least one repo updated, 10 = success but nothing updated,
1 = hard failure.
"""

import os
import stat
import subprocess
import sys
import tempfile

WORKSPACE = os.environ.get("ER_CATKIN_WS", "/cortex/.catkin_ws")

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
RED = "\033[0;31m" if USE_COLOR else ""
GREEN = "\033[0;32m" if USE_COLOR else ""
YELLOW = "\033[0;33m" if USE_COLOR else ""
OFF = "\033[0m" if USE_COLOR else ""

EXIT_UPDATED = 0
EXIT_NOTHING_TO_DO = 10
EXIT_FAILURE = 1

ASKPASS_SCRIPT = """#!/bin/sh
case "$1" in
    [Uu]sername*) echo x-access-token ;;
    *) echo "${GITHUB_PAT:-}" ;;
esac
"""


def error(msg):
    """Print a red error line."""
    print("{}ERROR: {}{}".format(RED, msg, OFF))


def warn(msg):
    """Print a yellow warning line."""
    print("{}WARNING: {}{}".format(YELLOW, msg, OFF))


def good(msg):
    """Print a green line."""
    print("{}{}{}".format(GREEN, msg, OFF))


def info(msg):
    """Print a plain line."""
    print(msg)


def make_askpass():
    """Write a GIT_ASKPASS helper that answers from $GITHUB_PAT; return its path."""
    fd, path = tempfile.mkstemp(prefix="er_askpass.", suffix=".sh")
    with os.fdopen(fd, "w") as handle:
        handle.write(ASKPASS_SCRIPT)
    os.chmod(path, stat.S_IRWXU)
    return path


def git(repo, args, askpass):
    """Run git in `repo` with prompts disabled; return the CompletedProcess."""
    env = dict(os.environ, GIT_ASKPASS=askpass, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(
        ["git", "-C", repo] + args,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        check=False,
    )


def discover_repos(src_dir):
    """Return sorted paths of git repos under src_dir, without descending into them."""
    repos = []
    for root, dirs, _ in os.walk(src_dir):
        if ".git" in dirs or os.path.isfile(os.path.join(root, ".git")):
            repos.append(root)
            dirs[:] = []  # a repo manages its own subtree (incl. submodules)
            continue
        dirs.sort()
    return sorted(repos)


def update_repo(path, name, askpass):  # pylint: disable=unused-argument
    """Fetch and fast-forward one repo. Returns (status, detail). Real logic in Task 2."""
    return "up-to-date", "not implemented yet"


STATUS_PRINTER = {"updated": good, "up-to-date": info, "skipped": warn, "failed": error}


def print_summary(results):
    """Print the end-of-run summary table."""
    print("\n" + "=" * 60)
    print("Summary:")
    for name, status, detail in results:
        STATUS_PRINTER[status]("  {:<11} {}  ({})".format(status, name, detail))


def main():
    """Entry point. Returns the process exit code."""
    src = os.path.join(WORKSPACE, "src")
    install = os.path.join(WORKSPACE, "install")
    if not os.path.isdir(src):
        if os.path.isdir(install):
            error("this is not a source code container, the util will not work here")
        else:
            error("unexpected workspace layout: neither src/ nor install/ found under {}".format(WORKSPACE))
        return EXIT_FAILURE

    repos = discover_repos(src)
    if not repos:
        warn("no git repositories found under {}".format(src))
        return EXIT_NOTHING_TO_DO

    if not os.environ.get("GITHUB_PAT"):
        warn("GITHUB_PAT is not set; private repos will fail to fetch")

    askpass = make_askpass()
    results = []
    try:
        for path in repos:
            name = os.path.relpath(path, src)
            info("\n=== {} ===".format(name))
            status, detail = update_repo(path, name, askpass)
            STATUS_PRINTER[status]("  {}: {}".format(status, detail))
            results.append((name, status, detail))
    finally:
        os.unlink(askpass)

    print_summary(results)
    if any(status == "failed" for _, status, _ in results):
        return EXIT_FAILURE
    if any(status == "updated" for _, status, _ in results):
        return EXIT_UPDATED
    return EXIT_NOTHING_TO_DO


if __name__ == "__main__":
    sys.exit(main())
