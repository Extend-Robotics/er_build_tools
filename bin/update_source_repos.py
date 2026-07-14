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


def current_branch(repo, askpass):
    """Return the checked-out branch name, or None when HEAD is detached."""
    res = git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], askpass)
    return res.stdout.strip() if res.returncode == 0 else None


def short_rev(repo, ref, askpass):
    """Return the abbreviated hash of ref, or None."""
    res = git(repo, ["rev-parse", "--short", ref], askpass)
    return res.stdout.strip() if res.returncode == 0 else None


def ref_exists(repo, ref, askpass):
    """True when the fully-qualified ref exists."""
    return git(repo, ["show-ref", "--verify", "--quiet", ref], askpass).returncode == 0


def remote_branches(repo, askpass, extra_args):
    """List origin branch names (without the origin/ prefix) matching extra_args filters."""
    args = ["for-each-ref", "refs/remotes/origin", "--format=%(refname:short)"] + extra_args
    res = git(repo, args, askpass)
    if res.returncode != 0:
        return []
    names = []
    for line in res.stdout.splitlines():
        name = line.strip()
        if not name or name.endswith("/HEAD") or not name.startswith("origin/"):
            continue
        names.append(name[len("origin/"):])
    return names


def pick_branch(repo_name, candidates):
    """Interactively ask the QA tester which branch to check out; None means skip."""
    warn("{}: can't tell which branch this detached HEAD belongs to.".format(repo_name))
    info("  The commit is on these remote branches:")
    for idx, branch in enumerate(candidates, 1):
        info("  {}) {}".format(idx, branch))
    info("  s) skip this repo")
    while True:
        try:
            choice = input("Choose a branch to check out [1-{} or s]: ".format(len(candidates)))
        except EOFError:
            warn("no answer — skipping this repo")
            return None
        choice = choice.strip().lower()
        if choice == "s":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            return candidates[int(choice) - 1]
        info("Invalid choice.")


def resolve_detached(path, name, askpass):
    """Work out which branch a detached HEAD belongs to; None when it stays detached."""
    tips = remote_branches(path, askpass, ["--points-at", "HEAD"])
    if len(tips) == 1:
        info("  detached HEAD is the tip of origin/{} — checking it out".format(tips[0]))
        return tips[0]
    candidates = tips or remote_branches(path, askpass, ["--contains", "HEAD"])
    if not candidates:
        warn("  detached HEAD commit is not on any remote branch — leaving untouched")
        return None
    if len(candidates) == 1:
        info("  detached HEAD commit is only on origin/{} — checking it out".format(candidates[0]))
        return candidates[0]
    return pick_branch(name, candidates)


def checkout_branch(path, branch, askpass):
    """Check out `branch`, creating a tracking branch when needed. Returns the git result."""
    if ref_exists(path, "refs/heads/{}".format(branch), askpass):
        return git(path, ["checkout", branch], askpass)
    return git(path, ["checkout", "--track", "origin/{}".format(branch)], askpass)


def update_submodules(path, askpass):
    """Sync + init/update submodules to the superproject's recorded commits."""
    if not os.path.exists(os.path.join(path, ".gitmodules")):
        return None
    for args in (["submodule", "sync", "--recursive"],
                 ["submodule", "update", "--init", "--recursive"]):
        res = git(path, args, askpass)
        if res.returncode != 0:
            return "submodule update failed: {}".format(res.stdout.strip())
    return None


def update_repo(path, name, askpass):
    """Fetch and fast-forward one repo. Returns (status, detail)."""
    res = git(path, ["status", "--porcelain", "--untracked-files=no"], askpass)
    if res.returncode != 0:
        return "failed", "git status failed: {}".format(res.stdout.strip())
    if res.stdout.strip():
        return "skipped", "uncommitted local changes — commit or stash them first"

    res = git(path, ["fetch", "--prune", "origin"], askpass)
    if res.returncode != 0:
        return "failed", "fetch failed: {}".format(res.stdout.strip())

    old = short_rev(path, "HEAD", askpass)
    branch = current_branch(path, askpass)
    if branch is None:
        branch = resolve_detached(path, name, askpass)
        if branch is None:
            return "skipped", "detached HEAD left untouched"
        res = checkout_branch(path, branch, askpass)
        if res.returncode != 0:
            return "failed", "checkout of {} failed: {}".format(branch, res.stdout.strip())
    elif not ref_exists(path, "refs/remotes/origin/{}".format(branch), askpass):
        return "skipped", "no origin/{} on the remote".format(branch)

    res = git(path, ["merge", "--ff-only", "origin/{}".format(branch)], askpass)
    if res.returncode != 0:
        return "skipped", "{} has diverged from origin/{} — not rewriting local commits".format(branch, branch)

    if git(path, ["rev-parse", "--abbrev-ref", "@{u}"], askpass).returncode != 0:
        git(path, ["branch", "--set-upstream-to", "origin/{}".format(branch)], askpass)

    sub_error = update_submodules(path, askpass)
    if sub_error:
        return "failed", sub_error

    new = short_rev(path, "HEAD", askpass)
    if new != old:
        return "updated", "{} -> {} ({})".format(old, new, branch)
    return "up-to-date", "{} ({})".format(new, branch)


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
