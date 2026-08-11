#!/usr/bin/env python3
"""Per-repo spec-vs-actual table for the colcon workspaces in a cortex image.

For every repo in each workspace, prints:
    local-name | declared branch | as-built SHA | current HEAD | dirty

The declared branch and as-built SHA come from provenance files baked into the
image at build time; the current HEAD and dirty state come from git. A missing
provenance file is reported per-cell as "n/a (<why>)" — images built before the
provenance convention landed (ERD-2141 phase B) simply have fewer files baked.

Provenance files, looked up per workspace root:
  - <workspace>/build_manifest.repos   resolved closure manifest: the exact SHA
                                       every repo was built from (.repos map
                                       format; baked from ERD-2141 phase B on)
  - <workspace>/build_branches.yaml    branch record: the branch each repo was
                                       pinned from, with /cortex/rosinstall_branches.yaml
                                       as the image-wide fallback that pre-phase-B
                                       images bake (wstool-list or .repos map shape)

Repos in a provenance record whose source is not on disk (DELETE_SRC release
images) still get a row, with the git-derived columns marked n/a.

Usage: cortex_ws_info.py [workspace_dir ...]
       (no args: every default workspace that exists on this machine)
"""
import os
import subprocess
import sys

DEFAULT_WORKSPACES = [
    "/cortex/.catkin_ws",  # ros1 workspace (legacy path name; it is a colcon workspace)
    "/cortex/ros2_ws",
    "/cortex/ros2_ros1_bridge_ws",
]
IMAGE_WIDE_BRANCH_RECORD = "/cortex/rosinstall_branches.yaml"
SHA_DISPLAY_LENGTH = 12
COLUMN_HEADERS = ["local-name", "declared branch", "as-built SHA", "current HEAD", "dirty"]


def load_yaml(path):
    try:
        import yaml
    except ImportError:
        sys.exit("ERROR: PyYAML is required to read provenance records (apt install python3-yaml)")
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def entries_from_record(parsed, path):
    """{local-name: version} from either provenance shape.

    Accepts the .repos map shape ({repositories: {name: {version: ...}}}) and
    the wstool-list shape ([{git: {local-name: ..., version: ...}}]) that
    pre-ERD-2162 images bake as their branch record.
    """
    if isinstance(parsed, dict) and isinstance(parsed.get("repositories"), dict):
        return {name: str(spec.get("version", ""))
                for name, spec in parsed["repositories"].items()}
    if isinstance(parsed, list):
        entries = {}
        for item in parsed:
            if not (isinstance(item, dict) and isinstance(item.get("git"), dict)):
                raise ValueError(f"{path}: unrecognised entry {item!r}")
            spec = item["git"]
            entries[str(spec["local-name"])] = str(spec.get("version", ""))
        return entries
    raise ValueError(f"{path}: unrecognised provenance record shape")


def find_branch_record(workspace_dir):
    """(entries, reason): the branch record covering this workspace, else why not."""
    for path in [os.path.join(workspace_dir, "build_branches.yaml"), IMAGE_WIDE_BRANCH_RECORD]:
        if os.path.isfile(path):
            return entries_from_record(load_yaml(path), path), None
    return {}, "no branch record baked"


def find_build_manifest(workspace_dir):
    """(entries, reason): the as-built closure manifest, else why not."""
    path = os.path.join(workspace_dir, "build_manifest.repos")
    if os.path.isfile(path):
        return entries_from_record(load_yaml(path), path), None
    return {}, "no build manifest baked (pre-ERD-2141-phase-B image)"


def repos_on_disk(workspace_dir):
    """{local-name: repo_dir} for every git checkout directly under the
    workspace root or its src/ (unitree_sdk2_python lives outside src/)."""
    repos = {}
    source_dir = os.path.join(workspace_dir, "src")
    for parent, prefix in [(workspace_dir, ""), (source_dir, "src/")]:
        if not os.path.isdir(parent):
            continue
        for name in sorted(os.listdir(parent)):
            repo_dir = os.path.join(parent, name)
            if os.path.isdir(os.path.join(repo_dir, ".git")):
                repos[prefix + name if prefix and name in repos else name] = repo_dir
    return repos


def git_output(repo_dir, *arguments):
    result = subprocess.run(
        ["git", "-C", repo_dir, *arguments],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, text=True)
    return result.stdout.strip()


def head_and_dirty(repo_dir):
    head_sha = git_output(repo_dir, "rev-parse", f"--short={SHA_DISPLAY_LENGTH}", "HEAD")
    branch_probe = subprocess.run(
        ["git", "-C", repo_dir, "symbolic-ref", "-q", "--short", "HEAD"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    head = head_sha if branch_probe.returncode else f"{head_sha} ({branch_probe.stdout.strip()})"
    dirty = "yes" if git_output(repo_dir, "status", "--porcelain") else "no"
    return head, dirty


def not_available(reason):
    return f"n/a ({reason})"


def workspace_rows(workspace_dir):
    branch_entries, branch_reason = find_branch_record(workspace_dir)
    manifest_entries, manifest_reason = find_build_manifest(workspace_dir)
    disk_repos = repos_on_disk(workspace_dir)
    local_names = sorted(set(disk_repos) | set(branch_entries) | set(manifest_entries))
    if not local_names:
        return []

    rows = []
    for name in local_names:
        declared = branch_entries.get(name) or not_available(
            branch_reason or "not in branch record")
        as_built = manifest_entries.get(name) or not_available(
            manifest_reason or "not in build manifest")
        as_built = as_built if as_built.startswith("n/a") else as_built[:SHA_DISPLAY_LENGTH]
        if name in disk_repos:
            head, dirty = head_and_dirty(disk_repos[name])
        else:
            head = dirty = not_available("source not on disk")
        rows.append([name, declared, as_built, head, dirty])
    return rows


def format_table(rows):
    widths = [max(len(row[column]) for row in [COLUMN_HEADERS] + rows)
              for column in range(len(COLUMN_HEADERS))]
    lines = []
    for row in [COLUMN_HEADERS] + rows:
        lines.append(" | ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    lines.insert(1, "-+-".join("-" * width for width in widths))
    return "\n".join(lines)


def main(argv):
    if len(argv) > 1:
        workspaces = argv[1:]
        for workspace_dir in workspaces:
            if not os.path.isdir(workspace_dir):
                sys.exit(f"ERROR: workspace directory not found: {workspace_dir}")
    else:
        workspaces = [path for path in DEFAULT_WORKSPACES if os.path.isdir(path)]
        if not workspaces:
            sys.exit("ERROR: none of the default workspaces exist here: "
                     + ", ".join(DEFAULT_WORKSPACES)
                     + " — pass a workspace directory explicitly")

    for workspace_dir in workspaces:
        print(f"\n{workspace_dir}:")
        rows = workspace_rows(workspace_dir)
        if rows:
            print(format_table(rows))
        else:
            print("  no repos on disk and no provenance records for this workspace")


if __name__ == "__main__":
    main(sys.argv)
