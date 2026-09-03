#!/usr/bin/env python3
# SKIP_CHECK — this is a PYTHON file; check_bash.yml greps for the bash-shebang
# literal that the fixtures below write, and the marker stops it sourcing this.
"""Tests for the flash-tree rebuild half of bin/er_jetson_flash.py — tarball
sources and checksums, host prerequisites, the unpack sequence and its progress
bar, and the pinned raw.githubusercontent URL. No hardware, no network, no sudo.
Run with:
    python3 tests/test_er_jetson_flash_rebuild.py
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "bin"))
import er_jetson_flash as ejf  # noqa: E402  (path bootstrap must run first)


class TarballChecksumTest(unittest.TestCase):
    """tarball_is_valid: a cached tarball counts only when present AND its sha256 matches."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.path = os.path.join(self.root, "blob.tbz2")
        with open(self.path, "wb") as handle:
            handle.write(b"l4t bits")
        self.expected = ejf.sha256_file(self.path)

    def test_matching_file_is_valid(self):
        self.assertTrue(ejf.tarball_is_valid(self.path, self.expected))

    def test_wrong_hash_is_invalid(self):
        self.assertFalse(ejf.tarball_is_valid(self.path, "0" * 64))

    def test_missing_file_is_invalid(self):
        self.assertFalse(ejf.tarball_is_valid(os.path.join(self.root, "absent.tbz2"), self.expected))


class ObtainTarballTest(unittest.TestCase):
    """obtain_tarball: cache first, then each source in order, sha256-verified whatever the source."""

    PAYLOAD = b"pretend this is a tbz2"

    def setUp(self):
        self.cache_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cache_dir)
        self.tarball = ejf.L4tTarball("Jetson_Linux_R35.4.1_aarch64.tbz2", hashlib.sha256(self.PAYLOAD).hexdigest(), 4096)
        self.dest = os.path.join(self.cache_dir, self.tarball.name)
        self.calls = []

    def fetcher(self, label, payload=None, reason=None):
        """A fake source: records the call, writes payload (or fails with reason)."""
        def fetch(tarball, dest_path):
            self.calls.append(label)
            self.assertEqual(tarball, self.tarball)
            if reason is not None:
                return reason
            with open(dest_path, "wb") as handle:
                handle.write(payload)
            return None
        return (label, fetch)

    def obtain(self, sources):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = ejf.obtain_tarball(self.tarball, self.cache_dir, sources)
        return result, out.getvalue()

    def test_valid_cached_file_is_reused_without_fetching(self):
        with open(self.dest, "wb") as handle:
            handle.write(self.PAYLOAD)
        result, out = self.obtain([self.fetcher("nvidia.com", self.PAYLOAD)])
        self.assertTrue(result)
        self.assertEqual(self.calls, [])
        self.assertIn("cached", out)

    def test_primary_source_used_when_it_works(self):
        result, out = self.obtain([self.fetcher("nvidia.com", self.PAYLOAD),
                                   self.fetcher("er_jetson_archive", self.PAYLOAD)])
        self.assertTrue(result)
        self.assertEqual(self.calls, ["nvidia.com"])
        self.assertIn("source: nvidia.com", out)
        self.assertNotIn("fallback", out)
        self.assertTrue(ejf.tarball_is_valid(self.dest, self.tarball.sha256))

    def test_fallback_source_used_and_announced_when_primary_fails(self):
        result, out = self.obtain([self.fetcher("nvidia.com", reason="HTTP 404"),
                                   self.fetcher("er_jetson_archive", self.PAYLOAD)])
        self.assertTrue(result)
        self.assertEqual(self.calls, ["nvidia.com", "er_jetson_archive"])
        self.assertIn("source: er_jetson_archive (fallback: nvidia.com: HTTP 404)", out)

    def test_checksum_mismatch_counts_as_source_failure(self):
        result, out = self.obtain([self.fetcher("nvidia.com", b"truncated"),
                                   self.fetcher("er_jetson_archive", self.PAYLOAD)])
        self.assertTrue(result)
        self.assertEqual(self.calls, ["nvidia.com", "er_jetson_archive"])
        self.assertIn("sha256 mismatch", out)
        self.assertTrue(ejf.tarball_is_valid(self.dest, self.tarball.sha256))

    def test_all_sources_failing_leaves_no_file_behind(self):
        result, out = self.obtain([self.fetcher("nvidia.com", reason="HTTP 404"),
                                   self.fetcher("er_jetson_archive", reason="no GitHub token")])
        self.assertFalse(result)
        self.assertFalse(os.path.exists(self.dest))
        self.assertIn("HTTP 404", out)
        self.assertIn("no GitHub token", out)

    def test_corrupt_cached_file_is_replaced(self):
        with open(self.dest, "wb") as handle:
            handle.write(b"half a download")
        result, _ = self.obtain([self.fetcher("nvidia.com", self.PAYLOAD)])
        self.assertTrue(result)
        self.assertEqual(self.calls, ["nvidia.com"])
        self.assertTrue(ejf.tarball_is_valid(self.dest, self.tarball.sha256))


class FakeRun:
    """subprocess.run stand-in: records argv, answers from a per-argv-prefix table."""

    def __init__(self, responses):
        self.responses = responses  # {first argv token or (token, marker): (rc, stdout)}
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        for key, (returncode, stdout) in self.responses.items():
            marker = key if isinstance(key, str) else key[1]
            if argv[0] == (key if isinstance(key, str) else key[0]) and any(marker in tok for tok in argv):
                return mock.Mock(returncode=returncode, stdout=stdout)
        raise AssertionError("unexpected command: {}".format(argv))


class FetchFromNvidiaTest(unittest.TestCase):
    """fetch_from_nvidia: one curl to the public L4T release URL, rc mapped to a reason."""

    TARBALL = ejf.L4tTarball("Jetson_Linux_R35.4.1_aarch64.tbz2", "0" * 64, 4096)

    def test_downloads_from_the_public_release_url(self):
        run = FakeRun({("curl", "curl"): (0, "")})
        with mock.patch.object(ejf.subprocess, "run", run):
            self.assertIsNone(ejf.fetch_from_nvidia(self.TARBALL, "/cache/x.tbz2"))
        self.assertEqual(len(run.calls), 1)
        argv = run.calls[0]
        self.assertEqual(argv[0], "curl")
        self.assertIn(ejf.NVIDIA_L4T_URL_BASE + "/" + self.TARBALL.name, argv)
        self.assertEqual(argv[argv.index("-o") + 1], "/cache/x.tbz2")

    def test_curl_failure_becomes_a_reason(self):
        run = FakeRun({("curl", "curl"): (22, "")})
        with mock.patch.object(ejf.subprocess, "run", run):
            reason = ejf.fetch_from_nvidia(self.TARBALL, "/cache/x.tbz2")
        self.assertIn("curl rc 22", reason)


class FetchFromArchiveTest(unittest.TestCase):
    """fetch_from_archive: GitHub token -> release JSON -> asset id -> octet-stream download."""

    TARBALL = ejf.L4tTarball("Jetson_Linux_R35.4.1_aarch64.tbz2", "0" * 64, 4096)
    RELEASE_JSON = json.dumps({"assets": [
        {"id": 479622079, "name": "Jetson_Linux_R35.4.1_aarch64.tbz2"},
        {"id": 479622051, "name": "SHA256SUMS"}]})

    def test_downloads_the_named_asset_with_the_token(self):
        run = FakeRun({("curl", "releases/tags"): (0, self.RELEASE_JSON),
                       ("curl", "releases/assets/479622079"): (0, "")})
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ghp_test"}), \
                mock.patch.object(ejf.subprocess, "run", run):
            self.assertIsNone(ejf.fetch_from_archive(self.TARBALL, "/cache/x.tbz2"))
        self.assertEqual(len(run.calls), 2)
        lookup, download = run.calls[0], run.calls[1]
        self.assertIn("https://api.github.com/repos/{}/releases/tags/{}".format(
            ejf.ARCHIVE_REPO, ejf.ARCHIVE_TAG), lookup)
        self.assertIn("Authorization: Bearer ghp_test", download)
        self.assertIn("Accept: application/octet-stream", download)
        self.assertIn("https://api.github.com/repos/{}/releases/assets/479622079".format(ejf.ARCHIVE_REPO), download)
        self.assertEqual(download[download.index("-o") + 1], "/cache/x.tbz2")

    def test_token_comes_from_gh_when_env_is_unset(self):
        run = FakeRun({("gh", "token"): (0, "gho_from_gh\n"),
                       ("curl", "releases/tags"): (0, self.RELEASE_JSON),
                       ("curl", "releases/assets/479622079"): (0, "")})
        env = {key: value for key, value in os.environ.items() if key != "GH_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(ejf.subprocess, "run", run):
            self.assertIsNone(ejf.fetch_from_archive(self.TARBALL, "/cache/x.tbz2"))
        self.assertIn("Authorization: Bearer gho_from_gh", run.calls[-1])

    def test_no_token_anywhere_is_a_reason_not_a_download(self):
        run = FakeRun({})
        env = {key: value for key, value in os.environ.items() if key != "GH_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(ejf.subprocess, "run", run), \
                mock.patch.object(ejf.shutil, "which", return_value=None):
            reason = ejf.fetch_from_archive(self.TARBALL, "/cache/x.tbz2")
        self.assertIn("GH_TOKEN", reason)
        self.assertEqual(run.calls, [])

    def test_asset_missing_from_release_is_a_reason(self):
        run = FakeRun({("curl", "releases/tags"): (0, json.dumps({"assets": []}))})
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ghp_test"}), mock.patch.object(ejf.subprocess, "run", run):
            reason = ejf.fetch_from_archive(self.TARBALL, "/cache/x.tbz2")
        self.assertIn(self.TARBALL.name, reason)
        self.assertIn(ejf.ARCHIVE_TAG, reason)


class RebuildPrerequisitesTest(unittest.TestCase):
    """check_rebuild_prerequisites: host tools and free disk, checked before any download."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.jetpack_dir = os.path.join(self.root, "not-yet-created", "JetPack_5.1.2_Linux_JETSON_AGX_ORIN_TARGETS")

    def run_check(self, missing_tools=(), free_gib=1000):
        fake_usage = mock.Mock(free=free_gib * (1 << 30))
        out = io.StringIO()
        with mock.patch.object(ejf.shutil, "which", side_effect=lambda tool: None if tool in missing_tools else "/usr/bin/" + tool), \
                mock.patch.object(ejf.shutil, "disk_usage", return_value=fake_usage) as disk_usage, \
                contextlib.redirect_stdout(out):
            result = ejf.check_rebuild_prerequisites(self.jetpack_dir)
        return result, out.getvalue(), disk_usage

    def test_all_present_passes(self):
        result, _, _ = self.run_check()
        self.assertTrue(result)

    def test_missing_tool_fails_with_the_apt_line(self):
        result, out, _ = self.run_check(missing_tools=("tar",))
        self.assertFalse(result)
        self.assertIn("tar", out)
        self.assertIn("apt-get install", out)

    def test_low_disk_fails_and_names_the_threshold(self):
        result, out, _ = self.run_check(free_gib=ejf.REBUILD_MIN_FREE_GIB - 1)
        self.assertFalse(result)
        self.assertIn("{} GiB".format(ejf.REBUILD_MIN_FREE_GIB), out)

    def test_disk_is_measured_on_the_nearest_existing_ancestor(self):
        _, _, disk_usage = self.run_check()
        disk_usage.assert_called_once_with(self.root)


class RecordingRunner:
    """subprocess.run stand-in for the build steps: records (argv, cwd); fails on a chosen token."""

    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.calls = []

    def __call__(self, argv, cwd=None, **kwargs):
        self.calls.append((argv, cwd))
        failed = self.fail_on is not None and any(self.fail_on in tok for tok in argv)
        return mock.Mock(returncode=1 if failed else 0)


def pinned_to_fixture_payloads():
    """L4T_BSP/L4T_ROOTFS re-pinned to the RebuildTreeTest fixture payloads' hashes."""
    return mock.patch.multiple(
        ejf,
        L4T_BSP=ejf.L4tTarball(ejf.L4T_BSP.name, hashlib.sha256(b"bsp").hexdigest(), ejf.L4T_BSP.unpacked_bytes),
        L4T_ROOTFS=ejf.L4tTarball(ejf.L4T_ROOTFS.name, hashlib.sha256(b"rootfs").hexdigest(), ejf.L4T_ROOTFS.unpacked_bytes))


class RebuildTreeTest(unittest.TestCase):
    """rebuild_tree: prerequisites -> both tarballs -> sudo -> extract/apply steps in order, stop on first failure."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.cache_dir = os.path.join(self.root, "cache")
        self.jetpack_dir = os.path.join(self.root, "JetPack_5.1.2_Linux_JETSON_AGX_ORIN_TARGETS")
        self.l4t = os.path.join(self.jetpack_dir, "Linux_for_Tegra")
        self.payloads = {ejf.L4T_BSP.name: b"bsp", ejf.L4T_ROOTFS.name: b"rootfs"}
        self.prereqs = mock.patch.object(ejf, "check_rebuild_prerequisites", return_value=True)
        self.prereqs.start()
        self.addCleanup(self.prereqs.stop)

    def sources(self, fail=False):
        def fetch(tarball, dest_path):
            if fail:
                return "offline"
            with open(dest_path, "wb") as handle:
                handle.write(self.payloads[tarball.name])
            return None
        return (("test-source", fetch),)

    def rebuild(self, runner, fail_fetch=False):
        with pinned_to_fixture_payloads(), contextlib.redirect_stdout(io.StringIO()):
            return ejf.rebuild_tree(self.l4t, cache_dir=self.cache_dir, sources=self.sources(fail_fetch), runner=runner)

    def test_steps_run_in_order_from_the_right_directories(self):
        runner = RecordingRunner()
        self.assertTrue(self.rebuild(runner))
        bsp = os.path.join(self.cache_dir, ejf.L4T_BSP.name)
        rootfs = os.path.join(self.cache_dir, ejf.L4T_ROOTFS.name)
        # NVIDIA's prerequisites script installs qemu-user-static/binfmt-support,
        # which apply_binaries.sh needs — so it runs first
        self.assertEqual(runner.calls, [
            (["sudo", "-v"], None),
            (["tar", "xf", bsp, "-C", self.jetpack_dir], None),
            (["sudo", "./tools/l4t_flash_prerequisites.sh"], self.l4t),
            (["sudo", "tar", "xpf", rootfs, "-C", os.path.join(self.l4t, "rootfs")], None),
            (["sudo", "./apply_binaries.sh"], self.l4t),
        ])
        self.assertTrue(os.path.isdir(self.jetpack_dir))

    def test_tar_steps_report_progress_against_the_pinned_unpacked_size(self):
        runner = RecordingRunner()
        with mock.patch.object(ejf, "UnpackProgress") as progress:
            self.assertTrue(self.rebuild(runner))
        self.assertEqual(progress.call_args_list, [
            mock.call("unpacking the BSP", self.jetpack_dir, ejf.L4T_BSP.unpacked_bytes),
            mock.call("unpacking the sample rootfs", self.jetpack_dir, ejf.L4T_ROOTFS.unpacked_bytes),
        ])

    def test_failing_step_stops_the_sequence(self):
        runner = RecordingRunner(fail_on="apply_binaries")
        self.assertFalse(self.rebuild(runner))
        self.assertEqual(runner.calls[-1][0], ["sudo", "./apply_binaries.sh"])

    def test_unobtainable_tarball_runs_nothing(self):
        runner = RecordingRunner()
        self.assertFalse(self.rebuild(runner, fail_fetch=True))
        self.assertEqual(runner.calls, [])

    def test_failed_prerequisites_fetch_nothing(self):
        self.prereqs.stop()
        runner = RecordingRunner()
        with mock.patch.object(ejf, "check_rebuild_prerequisites", return_value=False):
            self.assertFalse(self.rebuild(runner))
        self.assertEqual(runner.calls, [])
        self.assertFalse(os.path.exists(self.cache_dir))
        self.prereqs.start()


class FormatProgressTest(unittest.TestCase):
    """format_progress: a bar, a capped percentage, MiB counts and m:ss elapsed."""

    def test_midway(self):
        line = ejf.format_progress("unpacking the BSP", written=512 * (1 << 20), expected=1024 * (1 << 20), elapsed_s=65)
        self.assertIn("unpacking the BSP", line)
        self.assertIn(" 50%", line)
        self.assertIn("512 / 1024 MiB", line)
        self.assertIn("1:05", line)
        bar_cells = line[line.index("[") + 1:line.index("]")]
        self.assertEqual(len(bar_cells), ejf.PROGRESS_BAR_WIDTH)
        self.assertEqual(bar_cells.count("#"), ejf.PROGRESS_BAR_WIDTH // 2)

    def test_overshoot_is_capped_at_full(self):
        line = ejf.format_progress("x", written=1300 * (1 << 20), expected=1000 * (1 << 20), elapsed_s=3)
        self.assertIn("100%", line)
        self.assertIn("#" * ejf.PROGRESS_BAR_WIDTH, line)

    def test_nothing_written_yet(self):
        line = ejf.format_progress("x", written=0, expected=1000 * (1 << 20), elapsed_s=0)
        self.assertIn("  0%", line)
        self.assertIn("0:00", line)
        self.assertNotIn("#", line)


def run_with_unpack_progress(free_readings, isatty):
    """Run a short sleep inside UnpackProgress with disk_usage fed from free_readings; returns stdout."""
    readings = iter(free_readings)
    usage = mock.Mock(side_effect=lambda _path: mock.Mock(free=next(readings)))
    out = io.StringIO()
    out.isatty = lambda: isatty
    with mock.patch.object(ejf.shutil, "disk_usage", usage), contextlib.redirect_stdout(out), \
            ejf.UnpackProgress("unpacking the BSP", "/some/dir", expected=100 * (1 << 20), interval_s=0.01):
        time.sleep(0.08)
    return out.getvalue()


class UnpackProgressTest(unittest.TestCase):
    """UnpackProgress: reports growth of the target filesystem while the wrapped step runs."""

    def test_tty_output_updates_in_place_and_ends_on_a_fresh_line(self):
        gib = 1 << 30
        out = run_with_unpack_progress([10 * gib] + [10 * gib - 50 * (1 << 20)] * 50, isatty=True)
        self.assertIn("\r", out)
        self.assertIn(" 50%", out)
        self.assertTrue(out.endswith("\n"))

    def test_non_tty_output_is_plain_lines(self):
        gib = 1 << 30
        out = run_with_unpack_progress([10 * gib] + [10 * gib - 25 * (1 << 20)] * 50, isatty=False)
        self.assertNotIn("\r", out)
        self.assertIn(" 25%", out)


class RawUrlBaseTest(unittest.TestCase):
    """Sibling files are fetched from the commit the helper pinned (ER_BUILD_TOOLS_REF),
    else the branch ref; raw.githubusercontent serves commit URLs immediately but
    caches refs/heads/<branch> for minutes and ignores query strings."""

    def test_pinned_commit_wins(self):
        with mock.patch.dict(os.environ, {"ER_BUILD_TOOLS_REF": "0123456789abcdef0123456789abcdef01234567",
                                          "ER_BUILD_TOOLS_BRANCH": "some_branch"}):
            self.assertEqual(ejf.raw_url_base(),
                             "https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/"
                             "0123456789abcdef0123456789abcdef01234567")

    def test_branch_ref_without_a_pin(self):
        env = {key: value for key, value in os.environ.items() if key != "ER_BUILD_TOOLS_REF"}
        env["ER_BUILD_TOOLS_BRANCH"] = "some_branch"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(ejf.raw_url_base(),
                             "https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/some_branch")

    def test_main_when_nothing_is_set(self):
        env = {key: value for key, value in os.environ.items()
               if key not in ("ER_BUILD_TOOLS_REF", "ER_BUILD_TOOLS_BRANCH")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(ejf.raw_url_base().endswith("/refs/heads/main"))

    def test_fetch_url_has_no_nocache_query(self):
        recorded = []

        def fake_run(argv, **_kwargs):
            recorded.append(argv)
            with open(argv[argv.index("-o") + 1], "w", encoding="utf-8") as handle:
                handle.write("{}")
            return mock.Mock(returncode=0)

        with mock.patch.dict(os.environ, {"ER_BUILD_TOOLS_REF": "0123456789abcdef0123456789abcdef01234567"}), \
                mock.patch.object(ejf.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(ejf.os.path, "isfile", return_value=False):
            path, is_temp = ejf.locate_repo_file(ejf.MANIFEST_REL, ".json")
        self.assertTrue(is_temp)
        os.unlink(path)
        url = [tok for tok in recorded[0] if tok.startswith("http")][0]
        self.assertEqual(url, "https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/"
                              "0123456789abcdef0123456789abcdef01234567/" + ejf.MANIFEST_REL)


if __name__ == "__main__":
    unittest.main()
