#!/usr/bin/env python3
"""Self-contained tests for bin/cortex_ws_info.py — tmpdir git repos, no network.

Run with:
    python3 -m unittest tests.test_cortex_ws_info        (from the repo root)
    python3 tests/test_cortex_ws_info.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "bin"))
import cortex_ws_info as cwi  # noqa: E402  (path bootstrap must run first)

WSTOOL_SHAPE_RECORD = """\
- git:
    local-name: repo_a
    uri: https://github.com/Extend-Robotics/repo_a
    version: main
- git:
    local-name: repo_gone
    uri: https://github.com/Extend-Robotics/repo_gone
    version: feature/xyz
"""

REPOS_MAP_SHAPE_RECORD = """\
repositories:
  repo_a:
    type: git
    url: https://github.com/Extend-Robotics/repo_a
    version: 0123456789abcdef0123456789abcdef01234567
"""


def make_git_repo(parent_dir, name):
    repo_dir = os.path.join(parent_dir, name)
    os.makedirs(repo_dir)
    subprocess.run(["git", "init", "--quiet", repo_dir], check=True)
    with open(os.path.join(repo_dir, "file.txt"), "w", encoding="utf-8") as handle:
        handle.write("content\n")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", repo_dir, "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", repo_dir, "commit", "--quiet", "-m", "initial"],
                   check=True, env=env)
    return repo_dir


class EntriesFromRecordTest(unittest.TestCase):
    def test_wstool_list_shape(self):
        import yaml
        entries = cwi.entries_from_record(yaml.safe_load(WSTOOL_SHAPE_RECORD), "record")
        self.assertEqual(entries, {"repo_a": "main", "repo_gone": "feature/xyz"})

    def test_repos_map_shape(self):
        import yaml
        entries = cwi.entries_from_record(yaml.safe_load(REPOS_MAP_SHAPE_RECORD), "record")
        self.assertEqual(entries, {"repo_a": "0123456789abcdef0123456789abcdef01234567"})

    def test_unrecognised_shape_raises(self):
        with self.assertRaises(ValueError):
            cwi.entries_from_record("just a string", "record")
        with self.assertRaises(ValueError):
            cwi.entries_from_record([{"hg": {"local-name": "x"}}], "record")


class WorkspaceRowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.workspace = os.path.join(self.tmp, "ws")
        os.makedirs(os.path.join(self.workspace, "src"))
        self.no_fallback = mock.patch.object(
            cwi, "IMAGE_WIDE_BRANCH_RECORD",
            os.path.join(self.tmp, "nonexistent.yaml"))
        self.no_fallback.start()
        self.addCleanup(self.no_fallback.stop)

    def write_branch_record(self, text):
        with open(os.path.join(self.workspace, "build_branches.yaml"), "w",
                  encoding="utf-8") as handle:
            handle.write(text)

    def rows_by_name(self):
        return {row[0]: row for row in cwi.workspace_rows(self.workspace)}

    def test_no_provenance_files_gives_explicit_reasons(self):
        make_git_repo(os.path.join(self.workspace, "src"), "repo_a")
        row = self.rows_by_name()["repo_a"]
        self.assertEqual(row[1], "n/a (no branch record baked)")
        self.assertEqual(row[2], "n/a (no build manifest baked (pre-ERD-2141-phase-B image))")
        self.assertNotIn("n/a", row[3])
        self.assertEqual(row[4], "no")

    def test_declared_branch_read_from_workspace_record(self):
        make_git_repo(os.path.join(self.workspace, "src"), "repo_a")
        self.write_branch_record(WSTOOL_SHAPE_RECORD)
        self.assertEqual(self.rows_by_name()["repo_a"][1], "main")

    def test_record_entry_without_source_on_disk_still_gets_a_row(self):
        self.write_branch_record(WSTOOL_SHAPE_RECORD)
        row = self.rows_by_name()["repo_gone"]
        self.assertEqual(row[1], "feature/xyz")
        self.assertEqual(row[3], "n/a (source not on disk)")
        self.assertEqual(row[4], "n/a (source not on disk)")

    def test_dirty_repo_reported(self):
        repo_dir = make_git_repo(os.path.join(self.workspace, "src"), "repo_a")
        with open(os.path.join(repo_dir, "file.txt"), "a", encoding="utf-8") as handle:
            handle.write("local edit\n")
        self.assertEqual(self.rows_by_name()["repo_a"][4], "yes")

    def test_as_built_sha_from_build_manifest(self):
        make_git_repo(os.path.join(self.workspace, "src"), "repo_a")
        with open(os.path.join(self.workspace, "build_manifest.repos"), "w",
                  encoding="utf-8") as handle:
            handle.write(REPOS_MAP_SHAPE_RECORD)
        self.assertEqual(self.rows_by_name()["repo_a"][2],
                         "0123456789abcdef0123456789abcdef01234567"[:cwi.SHA_DISPLAY_LENGTH])

    def test_repo_outside_src_is_found(self):
        make_git_repo(self.workspace, "unitree_sdk2_python")
        self.assertIn("unitree_sdk2_python", self.rows_by_name())


class MainTest(unittest.TestCase):
    def test_explicit_missing_workspace_fails(self):
        with self.assertRaises(SystemExit):
            cwi.main(["cortex_ws_info.py", "/nonexistent/workspace"])

    def test_no_default_workspace_fails(self):
        with mock.patch.object(cwi, "DEFAULT_WORKSPACES", ["/nonexistent/ws"]):
            with self.assertRaises(SystemExit):
                cwi.main(["cortex_ws_info.py"])


if __name__ == "__main__":
    unittest.main()
