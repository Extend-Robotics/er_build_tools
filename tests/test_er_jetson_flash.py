#!/usr/bin/env python3
"""Self-contained tests for bin/er_jetson_flash.py — no hardware, no network, no sudo.

Everything that talks to the outside world (lsusb, ssh, sdkmanager, tar, the
Jetson itself) is either mocked or exercised against tmpdir fixtures. Run with:
    python3 -m unittest tests.test_er_jetson_flash        (from the repo root)
    python3 tests/test_er_jetson_flash.py
"""

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


class FindBackupTest(unittest.TestCase):
    """find_backup returns the newest tarball and ignores partial/foreign files."""

    def setUp(self):
        self.backup_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.backup_dir)

    def _touch(self, name, mtime):
        path = os.path.join(self.backup_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        os.utime(path, (mtime, mtime))
        return path

    def test_empty_dir(self):
        self.assertEqual(ejf.find_backup(self.backup_dir), (None, None))

    def test_newest_wins_and_partials_ignored(self):
        now = time.time()
        self._touch("JetPack_5.1.2_flash_tree_old.tar.zst", now - 100)
        newest = self._touch("JetPack_5.1.2_flash_tree_new.tar.zst", now)
        self._touch("JetPack_5.1.2_flash_tree_newer.tar.zst.partial", now + 100)
        tarball, manifest = ejf.find_backup(self.backup_dir)
        self.assertEqual(tarball, newest)
        self.assertIsNone(manifest)

    def test_manifest_found_when_present(self):
        tarball = self._touch("JetPack_5.1.2_flash_tree_patched_2026-07-16.tar.zst", time.time())
        with open(tarball + ejf.MANIFEST_SUFFIX, "w", encoding="utf-8") as handle:
            json.dump({}, handle)
        _, manifest = ejf.find_backup(self.backup_dir)
        self.assertEqual(manifest, tarball + ejf.MANIFEST_SUFFIX)


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
        self.backup_dir = os.path.join(self.root, "backups")
        os.makedirs(self.backup_dir)

    def test_stock_tree_gets_patched_in_place(self):
        l4t = make_tree(self.root)
        self.assertTrue(ejf.ensure_tree(l4t, self.backup_dir, assume_yes=False))
        self.assertEqual(ejf.xml_patch_state(l4t), "patched")

    def test_missing_tree_no_backup_declined(self):
        l4t = os.path.join(self.root, "JetPack_5.1.2_Linux_JETSON_AGX_ORIN_TARGETS", "Linux_for_Tegra")
        with mock.patch.object(sys, "stdin", io.StringIO("")):  # EOF -> decline
            self.assertFalse(ejf.ensure_tree(l4t, self.backup_dir, assume_yes=False))

    def test_missing_tree_guided_sdkmanager_invoked_with_yes(self):
        l4t = os.path.join(self.root, "JetPack_5.1.2_Linux_JETSON_AGX_ORIN_TARGETS", "Linux_for_Tegra")
        with mock.patch.object(ejf, "guided_sdkmanager", return_value=False) as guided:
            self.assertFalse(ejf.ensure_tree(l4t, self.backup_dir, assume_yes=True))
        guided.assert_called_once()

    def test_drifted_tree_restored_from_tarball(self):
        l4t = make_tree(self.root)
        ejf.apply_patch(l4t)
        manifest = ejf.build_manifest(l4t)
        tarball = os.path.join(self.backup_dir, "JetPack_5.1.2_flash_tree_patched_x.tar.zst")
        with open(tarball, "w", encoding="utf-8") as handle:
            handle.write("fake tarball")
        with open(tarball + ejf.MANIFEST_SUFFIX, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        # tamper with a manifest-tracked file -> verify fails -> restore path runs
        with open(os.path.join(l4t, "flash.sh"), "a", encoding="utf-8") as handle:
            handle.write("echo tampered\n")

        def fake_restore(l4t_path, tarball_path):
            self.assertEqual(tarball_path, tarball)
            shutil.rmtree(os.path.dirname(os.path.dirname(l4t_path)))
            make_tree(self.root)  # pristine stock tree, as a real extract would give
            return True

        with mock.patch.object(ejf, "restore_from_tarball", side_effect=fake_restore):
            self.assertTrue(ejf.ensure_tree(l4t, self.backup_dir, assume_yes=True))
        self.assertEqual(ejf.xml_patch_state(l4t), "patched")


class CliTest(unittest.TestCase):
    """Argument handling: defaults, default subcommand insertion, verify exit codes."""

    def test_flash_defaults(self):
        args = ejf.build_parser().parse_args(["flash"])
        self.assertEqual(args.username, "extend")
        self.assertEqual(args.password, "extend")
        self.assertEqual(args.storage, "nvme0n1p1")

    def test_default_subcommand_is_flash(self):
        with mock.patch.object(ejf, "cmd_flash", return_value=0) as cmd:
            self.assertEqual(ejf.main(["--username", "bob"]), 0)
        self.assertEqual(cmd.call_args[0][0].username, "bob")

    def test_verify_missing_tree_fails(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        result = ejf.main(["verify", "--l4t", os.path.join(root, "nope"), "--backup-dir", root])
        self.assertEqual(result, ejf.EXIT_FAILURE)

    def test_verify_patched_tree_passes(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        l4t = make_tree(root)
        ejf.apply_patch(l4t)
        result = ejf.main(["verify", "--l4t", l4t, "--backup-dir", root])
        self.assertEqual(result, ejf.EXIT_OK)

    def test_verify_stock_tree_is_undetermined(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        l4t = make_tree(root)
        result = ejf.main(["verify", "--l4t", l4t, "--backup-dir", root])
        self.assertEqual(result, ejf.EXIT_UNDETERMINED)


class MakeBackupTest(unittest.TestCase):
    """make-backup refuses unpatched trees and writes manifests for existing tarballs."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.backup_dir = os.path.join(self.root, "backups")

    def test_refuses_unpatched_tree(self):
        l4t = make_tree(self.root)
        self.assertFalse(ejf.make_backup(l4t, self.backup_dir, manifest_only=False))

    def test_manifest_only_requires_existing_tarball(self):
        l4t = make_tree(self.root)
        ejf.apply_patch(l4t)
        os.makedirs(self.backup_dir)
        self.assertFalse(ejf.make_backup(l4t, self.backup_dir, manifest_only=True))

    def test_manifest_only_writes_manifest(self):
        l4t = make_tree(self.root)
        ejf.apply_patch(l4t)
        os.makedirs(self.backup_dir)
        tarball = os.path.join(self.backup_dir, "JetPack_5.1.2_flash_tree_patched_x.tar.zst")
        with open(tarball, "w", encoding="utf-8") as handle:
            handle.write("fake")
        self.assertTrue(ejf.make_backup(l4t, self.backup_dir, manifest_only=True))
        with open(tarball + ejf.MANIFEST_SUFFIX, encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertIn(ejf.XML_REL, manifest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
