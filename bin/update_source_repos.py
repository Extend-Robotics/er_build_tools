#!/usr/bin/env python3
"""Container-side payload for er_update_source_repos.

Updates every git repo in each cortex source workspace — both under
<workspace>/src and sitting directly at the workspace root (pip-installed -e
checkouts such as unitree_sdk2_python live beside src/, not under it).
Fetches, resolves detached HEADs to a branch, fast-forwards, and updates
submodules. Never touches dirty or diverged repos.

CI-built images check every repo out at an exact SHA, so detached HEADs are
the norm. They are resolved from the branch record baked at image build time:
per-workspace <workspace>/build_branches.yaml, with /cortex/rosinstall_branches.yaml
as the image-wide fallback. Repos the record does not cover fall back to
guessing the branch from the remote refs, interactively when ambiguous.

Auth: a GIT_ASKPASS helper feeds the PAT from $GITHUB_PAT so the token never
appears in argv, remote URLs, or git config.

Env overrides: ER_WORKSPACES (colon-separated workspace list),
ER_CATKIN_WS (legacy: run on this single ros1 workspace only),
ER_IMAGE_WIDE_BRANCH_RECORD (tests: relocate the image-wide branch record).

Exit codes: 0 = the ros1 workspace updated (the caller should rebuild it),
11 = only workspaces with no automated rebuild updated, 10 = success but
nothing updated, 1 = hard failure.
"""

import os
import stat
import subprocess
import sys
import tempfile

DEFAULT_WORKSPACES = [
    "/cortex/.catkin_ws",  # ros1 workspace (legacy path name; it is a colcon workspace)
    "/cortex/ros2_ws",
    "/cortex/ros2_ros1_bridge_ws",
]
IMAGE_WIDE_BRANCH_RECORD = os.environ.get("ER_IMAGE_WIDE_BRANCH_RECORD", "/cortex/rosinstall_branches.yaml")

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
RED = "\033[0;31m" if USE_COLOR else ""
GREEN = "\033[0;32m" if USE_COLOR else ""
YELLOW = "\033[0;33m" if USE_COLOR else ""
OFF = "\033[0m" if USE_COLOR else ""

EXIT_UPDATED = 0
EXIT_FAILURE = 1
EXIT_NOTHING_TO_DO = 10
EXIT_UPDATED_MANUAL_REBUILD_ONLY = 11

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
    tmp_fd, path = tempfile.mkstemp(prefix="er_askpass.", suffix=".sh")
    with os.fdopen(tmp_fd, "w") as handle:
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


def determine_workspaces():
    """Resolve the workspace list; None (after printing an error) on a bad override.

    Precedence: ER_WORKSPACES (colon-separated) > ER_CATKIN_WS (legacy single
    ros1 workspace override) > the default workspaces that exist on this image.
    """
    colon_separated_override = os.environ.get("ER_WORKSPACES")
    if colon_separated_override:
        workspaces = [entry for entry in colon_separated_override.split(":") if entry]
        if not workspaces:
            error("ER_WORKSPACES is set but names no workspaces")
            return None
        missing = [workspace for workspace in workspaces if not os.path.isdir(workspace)]
        if missing:
            error("ER_WORKSPACES names workspaces not found on disk: {}".format(", ".join(missing)))
            return None
        return workspaces
    legacy_ros1_override = os.environ.get("ER_CATKIN_WS")
    if legacy_ros1_override:
        return [legacy_ros1_override]
    workspaces = [workspace for workspace in DEFAULT_WORKSPACES if os.path.isdir(workspace)]
    if not workspaces:
        error("none of the default workspaces exist here: {}".format(", ".join(DEFAULT_WORKSPACES)))
        return None
    return workspaces


def is_ros1_workspace(workspace):
    """True for the ros1 workspace — the only one the host wrapper rebuilds automatically.

    /cortex/.catkin_ws is the ros1 workspace's legacy path name (it is a colcon
    workspace); ER_CATKIN_WS has always pointed the tool at that workspace.
    """
    if workspace == os.environ.get("ER_CATKIN_WS"):
        return True
    return os.path.basename(os.path.normpath(workspace)) == ".catkin_ws"


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


def discover_root_repos(workspace):
    """Return git repos sitting directly at the workspace root, skipping src/."""
    if not os.path.isdir(workspace):
        return []
    repos = []
    for entry in sorted(os.listdir(workspace)):
        if entry == "src":
            continue
        candidate = os.path.join(workspace, entry)
        git_marker = os.path.join(candidate, ".git")
        if os.path.isdir(git_marker) or os.path.isfile(git_marker):
            repos.append(candidate)
    return repos


def collect_repos(workspace):
    """Return ([(repo_path, display_name)], problem) for one workspace.

    `problem` is an error string when the workspace layout shows this container
    has no source to update (e.g. a release image whose src/ was deleted).
    """
    src = os.path.join(workspace, "src")
    root_repos = discover_root_repos(workspace)
    src_repos = discover_repos(src) if os.path.isdir(src) else []
    if not os.path.isdir(src) and not root_repos:
        if os.path.isdir(os.path.join(workspace, "install")):
            return None, "this is not a source code container, the util will not work here"
        return None, "unexpected workspace layout: neither src/ nor install/ found under {}".format(workspace)
    named = [(path, os.path.relpath(path, workspace)) for path in root_repos]
    named += [(path, os.path.relpath(path, src)) for path in src_repos]
    return named, None


def branch_entries_from_record(parsed, record_path):
    """{local-name: branch} from either baked branch record shape.

    Accepts the .repos map shape ({repositories: {name: {version: ...}}}) and
    the wstool-list shape ([{git: {local-name: ..., version: ...}}]) that
    pre-ERD-2162 images bake as their branch record.
    """
    if isinstance(parsed, dict) and isinstance(parsed.get("repositories"), dict):
        entries = {}
        for local_name, spec in parsed["repositories"].items():
            if not isinstance(spec, dict):
                raise ValueError("{}: unrecognised entry for {!r}".format(record_path, local_name))
            entries[str(local_name)] = str(spec.get("version", ""))
        return entries
    if isinstance(parsed, list):
        entries = {}
        for item in parsed:
            if not (isinstance(item, dict) and isinstance(item.get("git"), dict)):
                raise ValueError("{}: unrecognised entry {!r}".format(record_path, item))
            git_spec = item["git"]
            entries[str(git_spec["local-name"])] = str(git_spec.get("version", ""))
        return entries
    raise ValueError("{}: unrecognised branch record shape".format(record_path))


def load_branch_record(workspace):
    """{local-name: branch} from the branch record baked into the image.

    Returns {} — after warning loudly, never crashing — when no record exists,
    PyYAML is unavailable, or the record is unreadable: this tool must still be
    usable on old QA images that predate the record convention.
    """
    candidate_paths = [os.path.join(workspace, "build_branches.yaml"), IMAGE_WIDE_BRANCH_RECORD]
    record_path = next((path for path in candidate_paths if os.path.isfile(path)), None)
    if record_path is None:
        return {}
    try:
        import yaml
    except ImportError:
        warn("PyYAML is unavailable — cannot read branch record {}; falling back to branch guessing".format(record_path))
        return {}
    try:
        with open(record_path, encoding="utf-8") as handle:
            parsed = yaml.safe_load(handle)
        return branch_entries_from_record(parsed, record_path)
    except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        error("branch record {} is unreadable: {}".format(record_path, exc))
        warn("falling back to branch guessing for workspace {}".format(workspace))
        return {}


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


def is_ancestor(repo, ancestor_ref, descendant_ref, askpass):
    """True when ancestor_ref is an ancestor of descendant_ref."""
    return git(repo, ["merge-base", "--is-ancestor", ancestor_ref, descendant_ref], askpass).returncode == 0


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
        sys.stdout.flush()  # menu must be visible even when stdout is piped/block-buffered
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


def branch_recorded_for_repo(name, branch_record):
    """The branch the record pins this repo to, or None; keyed by local-name."""
    return branch_record.get(name) or branch_record.get(os.path.basename(name))


def resolve_detached(path, name, askpass, branch_record):
    """Work out which branch a detached HEAD belongs to; None when it stays detached.

    The baked branch record is authoritative when it names a branch that still
    exists on origin and contains the detached commit; otherwise fall back to
    guessing from the remote refs.
    """
    recorded_branch = branch_recorded_for_repo(name, branch_record)
    if recorded_branch:
        if not ref_exists(path, "refs/remotes/origin/{}".format(recorded_branch), askpass):
            warn("  branch record names {} but origin has no such branch — guessing instead".format(recorded_branch))
        elif not is_ancestor(path, "HEAD", "origin/{}".format(recorded_branch), askpass):
            warn("  detached HEAD has commits that origin/{} does not — guessing instead".format(recorded_branch))
        else:
            info("  branch record: this repo was built from {} — checking it out".format(recorded_branch))
            return recorded_branch
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


def preflight_problem(path, askpass):
    """Return (status, detail) when the repo must not be updated, else None."""
    # Submodule state is excluded from the dirty gate: a stale submodule pointer
    # (e.g. a previous run that died between the superproject ff and the
    # submodule update) must self-heal, not skip forever as "dirty".
    res = git(path, ["status", "--porcelain", "--untracked-files=no", "--ignore-submodules=all"], askpass)
    if res.returncode != 0:
        return "failed", "git status failed: {}".format(res.stdout.strip())
    if res.stdout.strip():
        return "skipped", "uncommitted local changes — commit or stash them first"
    if os.path.exists(os.path.join(path, ".gitmodules")):
        res = git(path, ["submodule", "foreach", "--quiet", "--recursive",
                         "git status --porcelain --untracked-files=no"], askpass)
        if res.stdout.strip():
            return "skipped", "uncommitted local changes inside a submodule — commit or stash them first"
    if git(path, ["remote", "get-url", "origin"], askpass).returncode != 0:
        return "skipped", "no 'origin' remote configured"
    return None


def update_repo(path, name, askpass, branch_record):
    """Fetch and fast-forward one repo. Returns (status, detail)."""
    problem = preflight_problem(path, askpass)
    if problem:
        return problem

    res = git(path, ["fetch", "--prune", "origin"], askpass)
    if res.returncode != 0:
        return "failed", "fetch failed: {}".format(res.stdout.strip())

    old = short_rev(path, "HEAD", askpass)
    branch = current_branch(path, askpass)
    if branch is None:
        branch = resolve_detached(path, name, askpass, branch_record)
        if branch is None:
            return "skipped", "detached HEAD left untouched"
        res = checkout_branch(path, branch, askpass)
        if res.returncode != 0:
            return "failed", "checkout of {} failed: {}".format(branch, res.stdout.strip())
    elif not ref_exists(path, "refs/remotes/origin/{}".format(branch), askpass):
        return "skipped", "no origin/{} on the remote".format(branch)

    res = git(path, ["merge", "--ff-only", "origin/{}".format(branch)], askpass)
    if res.returncode != 0:
        if "untracked working tree files" in res.stdout:
            return "skipped", "an untracked file would be overwritten by the update — move it aside first"
        return "skipped", "{b} has diverged from origin/{b} — not rewriting local commits".format(b=branch)

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


def print_summary(results, show_workspace):
    """Print the end-of-run summary table."""
    print("\n" + "=" * 60)
    print("Summary:")
    for workspace, name, status, detail in results:
        label = "{}: {}".format(workspace, name) if show_workspace else name
        STATUS_PRINTER[status]("  {:<11} {}  ({})".format(status, label, detail))


def warn_manual_rebuilds(results):
    """Warn for updated workspaces that the host wrapper does not rebuild."""
    workspaces_needing_manual_rebuild = sorted(
        {workspace for workspace, _, status, _ in results
         if status == "updated" and not is_ros1_workspace(workspace)})
    for workspace in workspaces_needing_manual_rebuild:
        warn("workspace {} was updated — rebuild it manually "
             "(only the ros1 workspace is rebuilt automatically)".format(workspace))


def overall_exit_code(results):
    """Map the per-repo results to the process exit code."""
    if any(status == "failed" for _, _, status, _ in results):
        return EXIT_FAILURE
    if any(status == "updated" and is_ros1_workspace(workspace)
           for workspace, _, status, _ in results):
        return EXIT_UPDATED
    if any(status == "updated" for _, _, status, _ in results):
        return EXIT_UPDATED_MANUAL_REBUILD_ONLY
    return EXIT_NOTHING_TO_DO


def main():
    """Entry point. Returns the process exit code."""
    workspaces = determine_workspaces()
    if workspaces is None:
        return EXIT_FAILURE

    workspace_repos = []
    for workspace in workspaces:
        repos, problem = collect_repos(workspace)
        if problem:
            error(problem)
            return EXIT_FAILURE
        workspace_repos.append((workspace, repos))

    if not any(repos for _, repos in workspace_repos):
        warn("no git repositories found under {}".format(", ".join(workspaces)))
        return EXIT_NOTHING_TO_DO

    if not os.environ.get("GITHUB_PAT"):
        warn("GITHUB_PAT is not set; private repos will fail to fetch")

    askpass = make_askpass()
    results = []
    show_workspace = len(workspaces) > 1
    try:
        for workspace, repos in workspace_repos:
            if show_workspace:
                info("\n===== workspace {} =====".format(workspace))
            branch_record = load_branch_record(workspace)
            for path, name in repos:
                info("\n=== {} ===".format(name))
                status, detail = update_repo(path, name, askpass, branch_record)
                STATUS_PRINTER[status]("  {}: {}".format(status, detail))
                results.append((workspace, name, status, detail))
    finally:
        os.unlink(askpass)

    print_summary(results, show_workspace)
    warn_manual_rebuilds(results)
    return overall_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
