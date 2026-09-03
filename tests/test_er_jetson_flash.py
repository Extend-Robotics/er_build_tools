#!/usr/bin/env python3
# SKIP_CHECK — this is a PYTHON file. check_bash.yml greps for the bash-shebang
# literal, which the make_tree fixture below writes into its fake flash.sh; the
# marker stops CI from trying to source this file as bash.
"""Self-contained tests for bin/er_jetson_flash.py — no hardware, no network, no sudo.

Everything that talks to the outside world (lsusb, ssh, curl, sudo, tar, the
Jetson itself) is either mocked or exercised against tmpdir fixtures. Run with:
    python3 -m unittest tests.test_er_jetson_flash        (from the repo root)
    python3 tests/test_er_jetson_flash.py
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "bin"))
import er_jetson_flash as ejf  # noqa: E402  (path bootstrap must run first)

FAKE_XML_STOCK = '<device type="sdmmc_user">\n <num_sectors> {} </num_sectors>\n'.format(
    ejf.STOCK_VAL)


def manifest_fixture(l4t):
    """Write a matching canonical-manifest fixture; returns its path."""
    path = os.path.join(os.path.dirname(os.path.dirname(l4t)), "manifest.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(ejf.build_manifest(l4t), handle)
    return path


def make_tree(root, xml_content=FAKE_XML_STOCK):
    """Create a minimal Linux_for_Tegra fixture under root; returns the l4t path."""
    l4t = os.path.join(root, "JetPack_5.1.2_Linux_JETSON_AGX_ORIN_TARGETS", "Linux_for_Tegra")
    xml_dir = os.path.join(l4t, "bootloader", "t186ref", "cfg")
    os.makedirs(xml_dir)
    with open(os.path.join(xml_dir, "flash_t234_qspi_sdmmc.xml"), "w", encoding="utf-8") as handle:
        handle.write(xml_content)
    with open(os.path.join(l4t, "jetson-agx-orin-devkit.conf"), "w", encoding="utf-8") as handle:
        handle.write("EMMC_CFG=flash_t234_qspi_sdmmc.xml\n")
    with open(os.path.join(l4t, "flash.sh"), "w", encoding="utf-8") as handle:
        handle.write("#!/bin/bash\n")
    return l4t


class ClassifyPatchTest(unittest.TestCase):
    """classify_patch / xml_patch_state cover all four content states + missing."""

    def test_stock(self):
        self.assertEqual(ejf.classify_patch("a {} b".format(ejf.STOCK_VAL)), "stock")

    def test_patched(self):
        self.assertEqual(ejf.classify_patch("a {} b".format(ejf.PATCH_VAL)), "patched")

    def test_half_edited(self):
        both = ejf.STOCK_VAL + ejf.PATCH_VAL
        self.assertEqual(ejf.classify_patch(both), "half")

    def test_weird(self):
        self.assertEqual(ejf.classify_patch('num_sectors="42"'), "weird")

    def test_missing_tree(self):
        self.assertEqual(ejf.xml_patch_state("/nonexistent/l4t"), "missing")


class ApplyPatchTest(unittest.TestCase):
    """apply_patch patches once, keeps a pristine .orig, and refuses odd states."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.l4t = make_tree(self.root)
        self.xml = os.path.join(self.l4t, ejf.XML_REL)

    def test_patches_and_keeps_orig(self):
        self.assertTrue(ejf.apply_patch(self.l4t))
        self.assertEqual(ejf.xml_patch_state(self.l4t), "patched")
        with open(self.xml + ".orig", encoding="utf-8") as handle:
            self.assertIn(ejf.STOCK_VAL, handle.read())

    def test_idempotent(self):
        self.assertTrue(ejf.apply_patch(self.l4t))
        with open(self.xml, encoding="utf-8") as handle:
            first = handle.read()
        self.assertTrue(ejf.apply_patch(self.l4t))
        with open(self.xml, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), first)

    def test_never_clobbers_existing_orig(self):
        with open(self.xml + ".orig", "w", encoding="utf-8") as handle:
            handle.write("pristine sentinel")
        self.assertTrue(ejf.apply_patch(self.l4t))
        with open(self.xml + ".orig", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "pristine sentinel")

    def test_refuses_half_edited(self):
        with open(self.xml, "w", encoding="utf-8") as handle:
            handle.write(ejf.STOCK_VAL + "\n" + ejf.PATCH_VAL)
        self.assertFalse(ejf.apply_patch(self.l4t))


class ManifestTest(unittest.TestCase):
    """build_manifest picks the load-bearing files; compare_manifest spots drift."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.l4t = make_tree(self.root)

    def test_roundtrip_clean(self):
        manifest = ejf.build_manifest(self.l4t)
        self.assertIn("jetson-agx-orin-devkit.conf", manifest)
        self.assertIn(ejf.XML_REL, manifest)
        self.assertEqual(ejf.compare_manifest(self.l4t, manifest), [])

    def test_detects_modified_file(self):
        manifest = ejf.build_manifest(self.l4t)
        with open(os.path.join(self.l4t, "flash.sh"), "a", encoding="utf-8") as handle:
            handle.write("echo tampered\n")
        problems = ejf.compare_manifest(self.l4t, manifest)
        self.assertEqual(problems, ["flash.sh: content differs"])

    def test_detects_missing_file(self):
        manifest = ejf.build_manifest(self.l4t)
        os.unlink(os.path.join(self.l4t, "flash.sh"))
        self.assertEqual(ejf.compare_manifest(self.l4t, manifest), ["flash.sh: missing"])


class CanonicalManifestTest(unittest.TestCase):
    """load_canonical_manifest: override path wins; sibling repo file is found."""

    def test_override_path(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        path = os.path.join(root, "m.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"a": "1"}, handle)
        self.assertEqual(ejf.load_canonical_manifest(path), {"a": "1"})

    def test_sibling_repo_file_is_used(self):
        # The repo checkout ships the canonical manifest next to the script.
        sibling = os.path.join(os.path.dirname(os.path.abspath(ejf.__file__)),
                               os.path.basename(ejf.MANIFEST_REL))
        self.assertTrue(os.path.isfile(sibling),
                        "canonical manifest missing from the repo: {}".format(ejf.MANIFEST_REL))
        manifest = ejf.load_canonical_manifest()
        self.assertIsInstance(manifest, dict)
        self.assertIn(ejf.XML_REL, manifest)


class ParseUsbPidTest(unittest.TestCase):
    """parse_usb_pid pulls the NVIDIA product id out of lsusb output."""

    def test_recovery(self):
        text = "Bus 003 Device 021: ID 0955:7023 NVIDIA Corp. APX\n"
        self.assertEqual(ejf.parse_usb_pid(text), "7023")

    def test_booted_amongst_other_devices(self):
        text = ("Bus 001 Device 002: ID 8087:0026 Intel Corp.\n"
                "Bus 003 Device 023: ID 0955:7020 NVIDIA Corp. L4T\n")
        self.assertEqual(ejf.parse_usb_pid(text), "7020")

    def test_no_nvidia(self):
        self.assertIsNone(ejf.parse_usb_pid("Bus 001 Device 002: ID 8087:0026 Intel Corp.\n"))


class AptProxyConfTest(unittest.TestCase):
    """apt proxy config carries the tunnelled port for both http and https."""

    def test_both_schemes_use_port(self):
        conf = ejf.apt_proxy_conf(12345)
        self.assertIn('Acquire::http::Proxy "http://127.0.0.1:12345";', conf)
        self.assertIn('Acquire::https::Proxy "http://127.0.0.1:12345";', conf)


class QuietAptDailyTest(unittest.TestCase):
    """fix_clock's jump fires the Persistent apt-daily timers; their apt-get takes
    /var/lib/apt/lists/lock, which apt-get update cannot wait for. Stopping the
    timers alone is not enough — a service already started keeps running — so both
    the timers and the services are stopped, before the clock jump."""

    def test_stops_timers_and_services(self):
        commands = []

        def fake_remote_run(_target, _password, command, **_kwargs):
            commands.append(command)
            return mock.Mock(returncode=0, stdout="")

        with mock.patch.object(ejf, "remote_run", side_effect=fake_remote_run):
            ejf.quiet_apt_daily("192.0.2.1", "pw")
        self.assertEqual(len(commands), 1)
        for unit in ("apt-daily.timer", "apt-daily-upgrade.timer", "apt-daily.service", "apt-daily-upgrade.service"):
            self.assertIn(unit, commands[0])
        self.assertTrue(commands[0].startswith("systemctl stop "))

    def test_failure_warns_but_does_not_raise(self):
        out = io.StringIO()
        with mock.patch.object(ejf, "remote_run", return_value=mock.Mock(returncode=1, stdout="nope")), \
                contextlib.redirect_stdout(out):
            ejf.quiet_apt_daily("192.0.2.1", "pw")
        self.assertIn("WARN", out.getvalue())


class PostFlashOrderTest(unittest.TestCase):
    """post_flash must quiet apt-daily BEFORE fix_clock — the clock jump is what fires the timers."""

    def test_apt_daily_quieted_before_clock_jump(self):
        order = []
        with mock.patch.object(ejf, "wait_for", return_value=True), \
                mock.patch.object(ejf, "remote_run", return_value=mock.Mock(returncode=0, stdout="SSH_OK")), \
                mock.patch.object(ejf, "quiet_apt_daily", side_effect=lambda *_: order.append("quiet")), \
                mock.patch.object(ejf, "fix_clock", side_effect=lambda *_: order.append("clock")), \
                mock.patch.object(ejf, "jetson_has_internet", return_value=True), \
                mock.patch.object(ejf, "apt_install_jetpack", side_effect=lambda *_: order.append("apt") or True), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(ejf.post_flash("extend@192.0.2.1", "pw"))
        self.assertEqual(order, ["quiet", "clock", "apt"])


class AptInstallJetpackTest(unittest.TestCase):
    """apt_install_jetpack: update then install, both waiting on the dpkg lock; no unit juggling here."""

    def test_update_then_install_waiting_on_dpkg_lock(self):
        commands = []

        def fake_remote_run(_target, _password, command, **_kwargs):
            commands.append(command)
            return mock.Mock(returncode=0, stdout="")

        with mock.patch.object(ejf, "remote_run", side_effect=fake_remote_run):
            self.assertTrue(ejf.apt_install_jetpack("192.0.2.1", "pw"))

        self.assertEqual(len(commands), 2)
        self.assertNotIn("systemctl", " ".join(commands))
        for cmd in commands:
            self.assertIn("-o DPkg::Lock::Timeout={}".format(ejf.APT_LOCK_WAIT_S), cmd)
        self.assertTrue(commands[0].endswith(" update"))
        self.assertIn("install -y nvidia-jetpack", commands[1])


class ProxySmokeTest(unittest.TestCase):
    """start_proxy binds an ephemeral localhost port and accepts connections."""

    def test_listens_and_accepts(self):
        server, port = ejf.start_proxy()
        try:
            self.assertGreater(port, 0)
            with socket.create_connection(("127.0.0.1", port), timeout=5):
                pass
        finally:
            server.close()


class EnsureTreeTest(unittest.TestCase):
    """ensure_tree end-to-end against fixtures: patch-in-place and restore paths."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def test_stock_tree_gets_patched_in_place(self):
        l4t = make_tree(self.root)
        self.assertTrue(ejf.ensure_tree(l4t, None, assume_yes=False))
        self.assertEqual(ejf.xml_patch_state(l4t), "patched")

    def test_missing_tree_reinstall_declined(self):
        l4t = os.path.join(self.root, "JetPack_5.1.2_Linux_JETSON_AGX_ORIN_TARGETS", "Linux_for_Tegra")
        with mock.patch.object(sys, "stdin", io.StringIO("")):  # EOF -> decline
            self.assertFalse(ejf.ensure_tree(l4t, None, assume_yes=False))

    def test_missing_tree_rebuild_invoked_with_yes(self):
        l4t = os.path.join(self.root, "JetPack_5.1.2_Linux_JETSON_AGX_ORIN_TARGETS", "Linux_for_Tegra")
        with mock.patch.object(ejf, "rebuild_tree", return_value=False) as rebuild:
            self.assertFalse(ejf.ensure_tree(l4t, None, assume_yes=True))
        rebuild.assert_called_once_with(l4t)

    def test_drifted_tree_moved_aside_and_reinstalled(self):
        l4t = make_tree(self.root)
        ejf.apply_patch(l4t)
        manifest = ejf.build_manifest(l4t)
        # tamper with a manifest-tracked file -> verify fails -> restore path runs
        with open(os.path.join(l4t, "flash.sh"), "a", encoding="utf-8") as handle:
            handle.write("echo tampered\n")

        def fake_rebuild(_l4t):
            # the bad tree must already be out of the way, like a real rebuild sees
            self.assertFalse(os.path.isdir(l4t))
            make_tree(self.root)  # pristine stock tree, as a real extract would give
            return True

        with mock.patch.object(ejf, "rebuild_tree", side_effect=fake_rebuild):
            self.assertTrue(ejf.ensure_tree(l4t, manifest, assume_yes=True))
        self.assertEqual(ejf.xml_patch_state(l4t), "patched")
        aside = [name for name in os.listdir(self.root) if ".broken." in name]
        self.assertEqual(len(aside), 1)


class CliTest(unittest.TestCase):
    """Argument handling: defaults, default subcommand insertion, verify exit codes."""

    def test_flash_defaults(self):
        args = ejf.build_parser().parse_args(["flash"])
        self.assertEqual(args.username, "extend")
        # password defaults to None on argv — resolved later from
        # $ER_JETSON_PASSWORD (preferred: argv is visible in ps) or an interactive prompt
        self.assertIsNone(args.password)
        self.assertFalse(hasattr(ejf, "DEF_PASS"), "no default password may live in a public repo")
        self.assertEqual(args.storage, "nvme0n1p1")

    def test_default_subcommand_is_flash(self):
        with mock.patch.object(ejf, "cmd_flash", return_value=0) as cmd:
            self.assertEqual(ejf.main(["--username", "bob"]), 0)
        self.assertEqual(cmd.call_args[0][0].username, "bob")

    def test_option_value_colliding_with_subcommand_name(self):
        # regression: an option VALUE equal to a subcommand name must not
        # suppress the default-subcommand insertion (--l4t flash used to crash)
        with mock.patch.object(ejf, "cmd_flash", return_value=0) as cmd:
            self.assertEqual(ejf.main(["--l4t", "flash"]), 0)
        args = cmd.call_args[0][0]
        self.assertEqual(args.command, "flash")
        self.assertEqual(args.l4t, "flash")

    def test_explicit_subcommand_not_shadowed(self):
        self.assertEqual(ejf.default_subcommand(["verify", "--l4t", "x"]),
                         ["verify", "--l4t", "x"])
        self.assertEqual(ejf.default_subcommand(["--l4t", "x", "verify"]),
                         ["--l4t", "x", "verify"])
        self.assertEqual(ejf.default_subcommand(["--yes"]), ["flash", "--yes"])

    def test_verify_missing_tree_fails(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        empty = os.path.join(root, "empty.json")
        with open(empty, "w", encoding="utf-8") as handle:
            json.dump({}, handle)
        result = ejf.main(["verify", "--l4t", os.path.join(root, "nope"), "--manifest", empty])
        self.assertEqual(result, ejf.EXIT_FAILURE)

    def test_verify_patched_tree_passes(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        l4t = make_tree(root)
        ejf.apply_patch(l4t)
        result = ejf.main(["verify", "--l4t", l4t, "--manifest", manifest_fixture(l4t)])
        self.assertEqual(result, ejf.EXIT_OK)

    def test_verify_stock_tree_is_undetermined(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        l4t = make_tree(root)
        result = ejf.main(["verify", "--l4t", l4t, "--manifest", manifest_fixture(l4t)])
        self.assertEqual(result, ejf.EXIT_UNDETERMINED)

    def test_verify_stock_tree_with_patched_manifest_is_undetermined(self):
        # regression: the canonical manifest hashes the PATCHED tree, so a stock
        # tree used to be misreported as manifest drift (exit 1) instead of the
        # designed "stock, fixable in-place" exit 2
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        l4t = make_tree(root)
        ejf.apply_patch(l4t)
        manifest = manifest_fixture(l4t)  # manifest of the PATCHED tree
        with open(os.path.join(l4t, ejf.XML_REL), "w", encoding="utf-8") as handle:
            handle.write(FAKE_XML_STOCK)  # tree back to stock, like a fresh extract
        result = ejf.main(["verify", "--l4t", l4t, "--manifest", manifest])
        self.assertEqual(result, ejf.EXIT_UNDETERMINED)

    def test_verify_drifted_tree_fails(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        l4t = make_tree(root)
        ejf.apply_patch(l4t)
        manifest = manifest_fixture(l4t)
        with open(os.path.join(l4t, "flash.sh"), "a", encoding="utf-8") as handle:
            handle.write("echo drift\n")
        result = ejf.main(["verify", "--l4t", l4t, "--manifest", manifest])
        self.assertEqual(result, ejf.EXIT_FAILURE)


class MakeManifestTest(unittest.TestCase):
    """make-manifest refuses unpatched trees and writes a loadable manifest."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.output = os.path.join(self.root, "out.json")

    def test_refuses_unpatched_tree(self):
        l4t = make_tree(self.root)
        self.assertFalse(ejf.make_manifest(l4t, self.output))

    def test_writes_loadable_manifest(self):
        l4t = make_tree(self.root)
        ejf.apply_patch(l4t)
        self.assertTrue(ejf.make_manifest(l4t, self.output))
        manifest = ejf.load_canonical_manifest(self.output)
        self.assertIn(ejf.XML_REL, manifest)
        self.assertEqual(ejf.compare_manifest(l4t, manifest), [])

    def test_refuses_empty_tree_location(self):
        l4t = make_tree(self.root)
        ejf.apply_patch(l4t)
        with mock.patch.object(ejf, "MANIFEST_GLOBS", ("*.nomatch",)):
            self.assertFalse(ejf.make_manifest(l4t, self.output))


class StorageSelectionTest(unittest.TestCase):
    """--storage aliases resolve correctly; flash_command omits --storage for eMMC."""

    def test_nvme_alias_and_passthrough(self):
        self.assertEqual(ejf.resolve_storage("nvme"), "nvme0n1p1")
        self.assertEqual(ejf.resolve_storage("nvme0n1p1"), "nvme0n1p1")  # the default passes through
        self.assertEqual(ejf.resolve_storage("sda1"), "sda1")            # unknown device passes through

    def test_internal_aliases_mean_no_storage_flag(self):
        # None is the signal to omit --storage, which is how nvsdkmanager_flash.sh
        # is told to put the rootfs on the internal eMMC.
        self.assertIsNone(ejf.resolve_storage("internal"))
        self.assertIsNone(ejf.resolve_storage("emmc"))
        self.assertIsNone(ejf.resolve_storage("  INTERNAL "))  # trimmed + case-insensitive

    def test_flash_command_external_includes_storage(self):
        self.assertEqual(
            ejf.flash_command("qa", "nvme0n1p1"),
            ["sudo", "./nvsdkmanager_flash.sh", "--storage", "nvme0n1p1",
             "--nv-auto-config", "--username", "qa"])

    def test_flash_command_internal_omits_storage(self):
        cmd = ejf.flash_command("qa", None)
        self.assertEqual(
            cmd, ["sudo", "./nvsdkmanager_flash.sh", "--nv-auto-config", "--username", "qa"])
        self.assertNotIn("--storage", cmd)

    def test_cmd_flash_passes_resolved_storage_to_run_flash(self):
        # end-to-end wiring: `--storage internal` must reach run_flash as None (eMMC),
        # with every hardware/tree gate stubbed out.
        args = ejf.build_parser().parse_args(["flash", "--storage", "internal", "--skip-post"])
        with mock.patch.dict(os.environ, {"ER_JETSON_PASSWORD": "pw"}), \
                mock.patch.object(ejf, "check_host_tools", return_value=True), \
                mock.patch.object(ejf, "load_canonical_manifest", return_value=None), \
                mock.patch.object(ejf, "ensure_tree", return_value=True), \
                mock.patch.object(ejf, "run_preflight", return_value=ejf.EXIT_OK), \
                mock.patch.object(ejf, "current_usb_pid", return_value=ejf.USB_PID_RECOVERY), \
                mock.patch.object(ejf, "run_flash", return_value=True) as run_flash:
            self.assertEqual(ejf.cmd_flash(args), ejf.EXIT_OK)
        # run_flash(l4t, username, password, storage) — storage is the 4th positional arg
        self.assertIsNone(run_flash.call_args[0][3])


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


class PostFlashSubcommandTest(unittest.TestCase):
    """`post-flash`: re-run only the post-flash provisioning on an already flashed, booted board."""

    def test_parses_with_the_flash_credentials_options(self):
        args = ejf.build_parser().parse_args(["post-flash", "--username", "qa", "--password", "s3cret"])
        self.assertEqual(args.command, "post-flash")
        self.assertEqual((args.username, args.password), ("qa", "s3cret"))

    def test_runs_post_flash_then_sanity_checks_against_the_usb_host(self):
        args = ejf.build_parser().parse_args(["post-flash", "--username", "qa"])
        with mock.patch.dict(os.environ, {"ER_JETSON_PASSWORD": "from-env"}), \
                mock.patch.object(ejf, "check_host_tools", return_value=True), \
                mock.patch.object(ejf, "post_flash", return_value=True) as post, \
                mock.patch.object(ejf, "sanity_checks", return_value=True) as sanity, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ejf.cmd_post_flash(args), ejf.EXIT_OK)
        post.assert_called_once_with("qa@" + ejf.DEF_HOST, "from-env")
        sanity.assert_called_once_with("qa@" + ejf.DEF_HOST, "from-env")

    def test_no_password_anywhere_fails_before_touching_the_board(self):
        args = ejf.build_parser().parse_args(["post-flash"])
        env = {key: value for key, value in os.environ.items() if key != "ER_JETSON_PASSWORD"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: False)), \
                mock.patch.object(ejf, "check_host_tools", return_value=True), \
                mock.patch.object(ejf, "post_flash") as post, \
                contextlib.redirect_stdout(out):
            self.assertEqual(ejf.cmd_post_flash(args), ejf.EXIT_FAILURE)
        post.assert_not_called()
        self.assertIn("ER_JETSON_PASSWORD", out.getvalue())

    def test_post_flash_failure_skips_sanity_and_fails(self):
        args = ejf.build_parser().parse_args(["post-flash", "--password", "pw"])
        with mock.patch.object(ejf, "check_host_tools", return_value=True), \
                mock.patch.object(ejf, "post_flash", return_value=False), \
                mock.patch.object(ejf, "sanity_checks") as sanity, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ejf.cmd_post_flash(args), ejf.EXIT_FAILURE)
        sanity.assert_not_called()

    def test_main_dispatches_post_flash(self):
        with mock.patch.object(ejf, "cmd_post_flash", return_value=ejf.EXIT_OK) as cmd:
            self.assertEqual(ejf.main(["post-flash"]), ejf.EXIT_OK)
        cmd.assert_called_once()


class LineBufferedOutputTest(unittest.TestCase):
    """Status lines must not lag behind the ssh children's output when stdout is a pipe (tee, logs)."""

    def test_main_switches_stdout_to_line_buffering(self):
        fake_stdout = mock.Mock()
        with mock.patch.object(sys, "stdout", fake_stdout), \
                mock.patch.object(ejf, "cmd_verify", return_value=ejf.EXIT_OK):
            self.assertEqual(ejf.main(["verify"]), ejf.EXIT_OK)
        fake_stdout.reconfigure.assert_called_once_with(line_buffering=True)


def post_flash_args(*argv):
    return ejf.build_parser().parse_args(["post-flash"] + list(argv))


def env_without_password():
    env = {key: value for key, value in os.environ.items() if key != "ER_JETSON_PASSWORD"}
    return mock.patch.dict(os.environ, env, clear=True)


class ResolvePasswordTest(unittest.TestCase):
    """The Jetson password comes from --password, else $ER_JETSON_PASSWORD, else a prompt — never a default."""

    def test_argv_wins_over_env(self):
        with mock.patch.dict(os.environ, {"ER_JETSON_PASSWORD": "from-env"}):
            self.assertEqual(ejf.resolve_password(post_flash_args("--password", "from-argv")), "from-argv")

    def test_env_when_argv_absent(self):
        with mock.patch.dict(os.environ, {"ER_JETSON_PASSWORD": "from-env"}):
            self.assertEqual(ejf.resolve_password(post_flash_args()), "from-env")

    def test_interactive_prompt_when_neither_and_stdin_is_a_tty(self):
        with env_without_password(), mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: True)), \
                mock.patch.object(ejf.getpass, "getpass", return_value="typed") as prompt:
            self.assertEqual(ejf.resolve_password(post_flash_args("--username", "qa")), "typed")
        self.assertIn("qa", prompt.call_args[0][0])

    def test_none_when_neither_and_no_tty(self):
        with env_without_password(), mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: False)), \
                mock.patch.object(ejf.getpass, "getpass") as prompt:
            self.assertIsNone(ejf.resolve_password(post_flash_args()))
        prompt.assert_not_called()


class TopLevelHelpTest(unittest.TestCase):
    """`er_jetson_flash -h` must show the subcommand list, not the flash subparser's help."""

    def test_help_flags_are_not_routed_to_the_default_subcommand(self):
        self.assertEqual(ejf.default_subcommand(["-h"]), ["-h"])
        self.assertEqual(ejf.default_subcommand(["--help"]), ["--help"])

    def test_top_level_help_lists_post_flash(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            ejf.main(["-h"])
        self.assertIn("post-flash", out.getvalue())


class SshCommandTest(unittest.TestCase):
    """remote_run merges ssh's stderr into stdout, so ssh must not chat: with a throwaway
    known_hosts file every connection would otherwise print 'Warning: Permanently added ...',
    which the sanity checks then read as the command's output."""

    def test_ssh_is_silenced_to_errors_only(self):
        argv = ejf.ssh_cmd("qa@192.0.2.1")
        self.assertIn("LogLevel=ERROR", argv)
        self.assertEqual(argv[:3], ["sshpass", "-e", "ssh"])
        self.assertEqual(argv[-1], "qa@192.0.2.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
