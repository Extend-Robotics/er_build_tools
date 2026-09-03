#!/usr/bin/env python3
"""er_jetson_flash — one-command flash pipeline for AGX Orin QA cortexes (JetPack 5.1.2).

Pipeline (subcommand `flash`, the default):
  1. verify   — is the local flash tree present, manifest-clean and num_sectors-patched?
                (when not, the tree is rebuilt from NVIDIA's two public L4T R35.4.1
                tarballs, with the private er_jetson_archive release as the fallback
                source — no SDK Manager, so no NVIDIA login and no host-OS gate;
                nothing is tied to any particular machine)
  2. preflight— run bin/jetson-flash-preflight.sh: GO / DO NOT FLASH gate
  3. flash    — sudo ./nvsdkmanager_flash.sh [--storage <dev>] --nv-auto-config --username ...
                (--storage picks the rootfs device; omitted => rootfs on the internal eMMC)
  4. post     — wait for boot, fix the clock, give the board internet via an
                embedded HTTP proxy over the USB link when it has none,
                apt install nvidia-jetpack (pinned 5.1.2-b104), sanity checks

Verification compares the tree's configs/flash scripts against a CANONICAL manifest
committed in this repo (bin/er_jetson_flash_manifest.json) — a JetPack 5.1.2 extract
is deterministic, so one manifest serves every machine and catches re-extracted,
half-deleted or hand-edited trees. Extra subcommands: verify / restore /
post-flash (stage 4 alone, on a board that is already flashed and booted) /
make-manifest (regenerate the canonical manifest after an intentional change).

Background: docs/er-jetson-flash.md, docs/jetson-flash-preflight.md.
History: an SDK Manager GUI uninstall deleted the whole patched tree (2026-07-16),
and SDK Manager refuses to install JetPack 5.1.2 on anything newer than Ubuntu 20.04.

Python 3.8, stdlib only. Exit codes: 0 = success, 1 = failure, 2 = undetermined/aborted.
"""
# Single file by design: the er_jetson_flash shell helper fetches and runs this
# module on its own, so it cannot be split to satisfy max-module-lines.
# pylint: disable=too-many-lines

import argparse
import collections
import getpass
import glob
import hashlib
import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

# unpacked_bytes: apparent size of the extracted archive, the denominator of the
# unpack progress bar (the tarballs are hash-pinned, so it is a constant).
L4tTarball = collections.namedtuple("L4tTarball", "name sha256 unpacked_bytes")

HOME = os.path.expanduser("~")
DEFAULT_L4T = os.path.join(HOME, "nvidia", "nvidia_sdk",
                           "JetPack_5.1.2_Linux_JETSON_AGX_ORIN_TARGETS", "Linux_for_Tegra")
XML_REL = os.path.join("bootloader", "t186ref", "cfg", "flash_t234_qspi_sdmmc.xml")
STOCK_VAL = 'num_sectors="124321792"'
PATCH_VAL = 'num_sectors="124190720"'

# Small, load-bearing files only (configs + flash scripts) — hashing the full
# 38 GB tree every run would take minutes for no extra signal.
MANIFEST_GLOBS = ("*.conf", "*.conf.common", "flash.sh", "nvsdkmanager_flash.sh",
                  os.path.join("bootloader", "t186ref", "cfg", "*.xml"),
                  os.path.join("tools", "kernel_flash", "*.sh"),
                  os.path.join("tools", "kernel_flash", "*.xml"))

RAW_REPO_URL = "https://raw.githubusercontent.com/Extend-Robotics/er_build_tools"


def raw_url_base():
    """Where sibling repo files are fetched from: the commit the helper pinned
    (ER_BUILD_TOOLS_REF), else the branch ref. raw.githubusercontent serves commit
    URLs immediately but caches refs/heads/<branch> for minutes, query string or not."""
    ref = os.environ.get("ER_BUILD_TOOLS_REF") or "refs/heads/" + os.environ.get("ER_BUILD_TOOLS_BRANCH", "main")
    return "{}/{}".format(RAW_REPO_URL, ref)


PREFLIGHT_REL = "bin/jetson-flash-preflight.sh"
MANIFEST_REL = "bin/er_jetson_flash_manifest.json"

# JetPack 5.1.2 = L4T R35.4.1. The flash tree is rebuilt from NVIDIA's two public
# release tarballs (no SDK Manager, so no NVIDIA login and no host-OS gate); the
# private er_jetson_archive release holds byte-identical copies as delisting insurance.
NVIDIA_L4T_URL_BASE = "https://developer.download.nvidia.com/embedded/L4T/r35_Release_v4.1/release"
ARCHIVE_REPO = "Extend-Robotics/er_jetson_archive"
ARCHIVE_TAG = "r35.4.1"
GITHUB_API = "https://api.github.com"
L4T_BSP = L4tTarball("Jetson_Linux_R35.4.1_aarch64.tbz2",
                     "72b75a0c7fa3bf6ef41ae06634bb67c38a92682155d1206026dbee4a6b9a016f", 851352350)
L4T_ROOTFS = L4tTarball("Tegra_Linux_Sample-Root-Filesystem_R35.4.1_aarch64.tbz2",
                        "27656df9aa7d0171905d8da18197cb2bc5225e12bcd8a9abda3922b5eba94ccd", 4355714973)
# Same directory and file names SDK Manager used, so a machine with its old
# download cache rebuilds offline.
TARBALL_CACHE_DIR = os.path.join(HOME, "Downloads", "nvidia", "sdkm_downloads")
# Checked before the 2.2 GB download. Everything else the host needs (qemu,
# binfmt, lz4, abootimg, dtc, ...) is installed by NVIDIA's l4t_flash_prerequisites.sh.
REBUILD_HOST_TOOLS = (("curl", "curl"), ("tar", "tar"))
# Measured: ~7 GiB for the built tree plus 2.2 GiB of tarballs when not cached.
REBUILD_MIN_FREE_GIB = 12

DEF_USER = "extend"
DEF_HOST = "192.168.55.1"
# NVIDIA (vendor 0955) USB product ids: 7023 = AGX Orin in forced recovery (RCM,
# the only state flash.sh can start from), 7020 = booted L4T in USB device mode.
# (7035, the mid-flash initrd, is handled by the bash preflight, not needed here.)
USB_PID_RECOVERY = "7023"
USB_PID_BOOTED = "7020"

# LogLevel=ERROR: with a throwaway known_hosts file ssh would otherwise print
# "Warning: Permanently added ..." on every connection, and remote_run merges
# stderr into the output the sanity checks parse.
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3"]

BOOT_TIMEOUT_S = 300
SSH_TIMEOUT_S = 240
APT_PROXY_CONF = "/etc/apt/apt.conf.d/99er-usb-proxy"
# fix_clock() jumps the fresh flash's clock forward by years, which makes
# systemd's Persistent=true apt-daily timers fire their "missed" runs at once —
# their apt-get then holds /var/lib/apt/lists/lock, which `apt-get update`
# cannot wait for (DPkg::Lock::Timeout only covers the dpkg frontend lock). So
# the timers AND their services are stopped before the clock jump; the dpkg lock
# wait below still guards the install against anything else holding dpkg.
APT_DAILY_UNITS = "apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service"
APT_LOCK_WAIT_S = 600

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
RED = "\033[0;31m" if USE_COLOR else ""
GREEN = "\033[0;32m" if USE_COLOR else ""
YELLOW = "\033[0;33m" if USE_COLOR else ""
BOLD = "\033[1m" if USE_COLOR else ""
OFF = "\033[0m" if USE_COLOR else ""

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_UNDETERMINED = 2


def good(msg):
    """Print a green [OK] line."""
    print("  {}[OK]{}   {}".format(GREEN, OFF, msg))


def warn(msg):
    """Print a yellow [WARN] line."""
    print("  {}[WARN]{} {}".format(YELLOW, OFF, msg))


def bad(msg):
    """Print a red [FAIL] line."""
    print("  {}[FAIL]{} {}".format(RED, OFF, msg))


def hdr(msg):
    """Print a bold section header."""
    print("{}{}{}".format(BOLD, msg, OFF))


def confirm(prompt, assume_yes):
    """Ask a y/N question; --yes answers yes without prompting; EOF answers no."""
    if assume_yes:
        print("{} [auto-yes]".format(prompt))
        return True
    try:
        return input("{} [y/N] ".format(prompt)).strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---------------------------------------------------------------- tree verify


def classify_patch(xml_content):
    """Classify a flash XML's num_sectors state: patched / stock / half / weird."""
    has_stock = STOCK_VAL in xml_content
    has_patch = PATCH_VAL in xml_content
    if has_stock and has_patch:
        return "half"
    if has_patch:
        return "patched"
    if has_stock:
        return "stock"
    return "weird"


def xml_patch_state(l4t):
    """Return the patch state of the tree's flash XML, or 'missing'."""
    xml = os.path.join(l4t, XML_REL)
    if not os.path.isfile(xml):
        return "missing"
    with open(xml, "r", encoding="utf-8") as handle:
        return classify_patch(handle.read())


def apply_patch(l4t):
    """Idempotently apply the num_sectors patch (keeping a .orig). True on success."""
    xml = os.path.join(l4t, XML_REL)
    state = xml_patch_state(l4t)
    if state == "patched":
        return True
    if state != "stock":
        bad("cannot patch {}: state is '{}' — refusing to guess".format(xml, state))
        return False
    if not os.path.exists(xml + ".orig"):
        shutil.copy2(xml, xml + ".orig")
    with open(xml, "r", encoding="utf-8") as handle:
        content = handle.read()
    with open(xml, "w", encoding="utf-8") as handle:
        handle.write(content.replace(STOCK_VAL, PATCH_VAL))
    good("num_sectors patch applied ({} -> {})".format(STOCK_VAL, PATCH_VAL))
    return True


def sha256_file(path):
    """Streaming sha256 of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tarball_is_valid(path, expected_sha256):
    """True when the file exists and its sha256 matches the pinned value."""
    return os.path.isfile(path) and sha256_file(path) == expected_sha256


def fetch_from_nvidia(tarball, dest_path):
    """Download from NVIDIA's public L4T release directory. None on success, else a reason."""
    url = "{}/{}".format(NVIDIA_L4T_URL_BASE, tarball.name)
    res = subprocess.run(["curl", "-fSL", "--retry", "3", url, "-o", dest_path], check=False)
    return None if res.returncode == 0 else "curl rc {} fetching {}".format(res.returncode, url)


def github_token():
    """$GH_TOKEN, else the gh CLI's stored token, else None."""
    token = os.environ.get("GH_TOKEN")
    if token:
        return token
    if not shutil.which("gh"):
        return None
    res = subprocess.run(["gh", "auth", "token"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         universal_newlines=True, check=False)
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def fetch_from_archive(tarball, dest_path):
    """Download the same file from the private er_jetson_archive release. None on success, else a reason.

    Private-repo assets are only reachable via the API asset endpoint with
    Accept: application/octet-stream; plain curl because the deployment machines'
    gh 2.4 `release download` cannot target a directory or overwrite.
    """
    token = github_token()
    if not token:
        return "no GitHub token (set GH_TOKEN or run `gh auth login`) for {}".format(ARCHIVE_REPO)
    auth_header = "Authorization: Bearer {}".format(token)
    release_url = "{}/repos/{}/releases/tags/{}".format(GITHUB_API, ARCHIVE_REPO, ARCHIVE_TAG)
    lookup = subprocess.run(["curl", "-fsSL", "-H", auth_header, release_url],
                            stdout=subprocess.PIPE, universal_newlines=True, check=False)
    if lookup.returncode != 0:
        return "release lookup failed (curl rc {}) for {}".format(lookup.returncode, release_url)
    try:
        assets = json.loads(lookup.stdout).get("assets", [])
    except ValueError:
        return "release lookup returned non-JSON for {}".format(release_url)
    asset_ids = [asset["id"] for asset in assets if asset.get("name") == tarball.name]
    if not asset_ids:
        return "asset {} not in release {} of {}".format(tarball.name, ARCHIVE_TAG, ARCHIVE_REPO)
    asset_url = "{}/repos/{}/releases/assets/{}".format(GITHUB_API, ARCHIVE_REPO, asset_ids[0])
    res = subprocess.run(["curl", "-fSL", "--retry", "3", "-H", auth_header,
                          "-H", "Accept: application/octet-stream", asset_url, "-o", dest_path], check=False)
    return None if res.returncode == 0 else "curl rc {} fetching {}".format(res.returncode, asset_url)


TARBALL_SOURCES = (("nvidia.com", fetch_from_nvidia), ("er_jetson_archive", fetch_from_archive))


def obtain_tarball(tarball, cache_dir, sources):
    """Get cache_dir/<name> with the pinned sha256, trying each (label, fetch) source in turn.

    fetch(tarball, dest_path) returns None on success or a reason string. A cached
    file is reused only when its sha256 matches; a downloaded file is kept only when
    it does. Prints which source served it so the operator can tell nvidia.com
    from the private archive fallback.
    """
    dest_path = os.path.join(cache_dir, tarball.name)
    if tarball_is_valid(dest_path, tarball.sha256):
        good("{}: cached copy sha256-verified ({})".format(tarball.name, cache_dir))
        return True
    if os.path.exists(dest_path):
        warn("{}: cached copy fails sha256 — re-downloading".format(tarball.name))
        os.unlink(dest_path)
    os.makedirs(cache_dir, exist_ok=True)
    previous_failure = None
    for label, fetch in sources:
        fallback_note = " (fallback: {})".format(previous_failure) if previous_failure else ""
        print("  downloading {} — source: {}{}".format(tarball.name, label, fallback_note))
        reason = fetch(tarball, dest_path)
        if reason is None and not tarball_is_valid(dest_path, tarball.sha256):
            reason = "sha256 mismatch"
        if reason is None:
            good("{}: sha256 verified (source: {})".format(tarball.name, label))
            return True
        warn("{}: {}".format(label, reason))
        if os.path.exists(dest_path):
            os.unlink(dest_path)
        previous_failure = "{}: {}".format(label, reason)
    bad("{}: no source could provide it".format(tarball.name))
    return False


def build_manifest(l4t):
    """Hash the load-bearing files of the tree: {relative path: sha256}."""
    manifest = {}
    for pattern in MANIFEST_GLOBS:
        for path in sorted(glob.glob(os.path.join(l4t, pattern))):
            if os.path.isfile(path):
                manifest[os.path.relpath(path, l4t)] = sha256_file(path)
    return manifest


def compare_manifest(l4t, manifest):
    """Return a list of 'rel_path: problem' strings; empty means the tree matches."""
    problems = []
    for rel_path, expected in sorted(manifest.items()):
        path = os.path.join(l4t, rel_path)
        if not os.path.isfile(path):
            problems.append("{}: missing".format(rel_path))
        elif sha256_file(path) != expected:
            problems.append("{}: content differs".format(rel_path))
    return problems


def locate_repo_file(rel_path, suffix):
    """Find a repo file next to this script, else fetch from GitHub raw. (path, is_temp).

    The sibling shortcut only applies when this script actually runs from a repo
    checkout (parent dir has the repo's .helper_bash_functions). When fetched to
    ${TMPDIR} by the er_jetson_flash wrapper, a same-named file in the
    world-writable temp dir must NOT shadow the fetch pinned to the same commit.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    in_repo_checkout = os.path.isfile(os.path.join(script_dir, os.pardir, ".helper_bash_functions"))
    sibling = os.path.join(script_dir, os.path.basename(rel_path))
    if in_repo_checkout and os.path.isfile(sibling):
        return sibling, False
    tmp_fd, path = tempfile.mkstemp(prefix="er_jetson_flash.", suffix=suffix)
    os.close(tmp_fd)
    url = "{}/{}".format(raw_url_base(), rel_path)
    res = subprocess.run(["curl", "-fsSL", "--max-time", "30", url, "-o", path], check=False)
    if res.returncode != 0 or os.path.getsize(path) == 0:
        os.unlink(path)
        return None, False
    return path, True


def load_canonical_manifest(override_path=None):
    """The repo's canonical manifest as a dict, or None when unobtainable."""
    if override_path:
        with open(override_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    path, is_temp = locate_repo_file(MANIFEST_REL, ".json")
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except ValueError:
        return None
    finally:
        if is_temp:
            os.unlink(path)


def verify_tree(l4t, manifest):
    """Check tree presence, patch state and (when a manifest exists) content hashes."""
    hdr("== Flash tree: {} ==".format(l4t))
    state = xml_patch_state(l4t)
    if state == "missing":
        bad("flash config not found — tree deleted, moved, or never installed")
        return False
    if state == "stock":
        warn("tree present but num_sectors patch NOT applied (fixable in-place)")
    elif state != "patched":
        bad("flash XML in unexpected state '{}' — half-edited tree?".format(state))
        return False
    else:
        good("num_sectors patch is applied")
    if manifest:
        problems = compare_manifest(l4t, manifest)
        # The canonical manifest hashes the PATCHED tree, so on a stock (not yet
        # patched) tree the flash XML differing is EXPECTED, not drift — the patch
        # state is already reported above and `verify` maps stock to exit 2.
        if state == "stock":
            problems = [p for p in problems if p != "{}: content differs".format(XML_REL)]
        if problems:
            bad("tree differs from the canonical manifest:")
            for problem in problems[:10]:
                print("         {}".format(problem))
            if len(problems) > 10:
                print("         ... and {} more".format(len(problems) - 10))
            return False
        good("all {} manifest files match the canonical manifest".format(len(manifest)))
    else:
        warn("canonical manifest unavailable — only the patch state was checked")
    return state in ("patched", "stock")


# ---------------------------------------------------------------- tree rebuild


def nearest_existing_ancestor(path):
    """The deepest existing directory on path's chain (for disk_usage before mkdir)."""
    path = os.path.abspath(path)
    while not os.path.isdir(path):
        path = os.path.dirname(path)
    return path


def check_rebuild_prerequisites(jetpack_dir):
    """Host tools and free disk for a tree rebuild; prints what is missing."""
    missing_packages = [package for tool, package in REBUILD_HOST_TOOLS if not shutil.which(tool)]
    missing_tools = [tool for tool, _ in REBUILD_HOST_TOOLS if not shutil.which(tool)]
    if missing_tools:
        bad("missing host tools for the tree rebuild: {} — sudo apt-get install {}".format(
            ", ".join(missing_tools), " ".join(missing_packages)))
        return False
    free_gib = shutil.disk_usage(nearest_existing_ancestor(jetpack_dir)).free / float(1 << 30)
    if free_gib < REBUILD_MIN_FREE_GIB:
        bad("only {:.1f} GiB free under {} — the rebuild needs {} GiB".format(
            free_gib, jetpack_dir, REBUILD_MIN_FREE_GIB))
        return False
    return True


PROGRESS_BAR_WIDTH = 30


def format_progress(description, written, expected, elapsed_s):
    """One progress line: bar, percent (capped), MiB written/expected, m:ss elapsed."""
    fraction = min(written / float(expected), 1.0) if expected > 0 else 0.0
    filled = int(round(fraction * PROGRESS_BAR_WIDTH))
    minutes, seconds = divmod(int(elapsed_s), 60)
    return "  {}: [{}{}] {:3d}%  {} / {} MiB  {}:{:02d}".format(
        description, "#" * filled, " " * (PROGRESS_BAR_WIDTH - filled), int(fraction * 100),
        written >> 20, expected >> 20, minutes, seconds)


class UnpackProgress:
    """Context manager: while the body runs, report how much the filesystem under
    measure_dir has grown against the tarball's known unpacked size.

    Free-space delta rather than walking the tree: cheap, and independent of the
    tar version (tar 1.30 on the 20.04 machines lacks the newer checkpoint formats).
    A tty gets an in-place line; anything else gets a plain line per interval.
    """

    def __init__(self, description, measure_dir, expected, interval_s=None):
        self.description = description
        self.measure_dir = measure_dir
        self.expected = expected
        self.is_tty = sys.stdout.isatty()
        self.interval_s = interval_s if interval_s is not None else (1.0 if self.is_tty else 15.0)
        self.free_at_start = None
        self.started_at = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._report_until_stopped, daemon=True)

    def _written(self):
        return max(self.free_at_start - shutil.disk_usage(self.measure_dir).free, 0)

    def _line(self):
        return format_progress(self.description, self._written(), self.expected, time.monotonic() - self.started_at)

    def _report_until_stopped(self):
        while not self.stop.wait(self.interval_s):
            if self.is_tty:
                sys.stdout.write("\r" + self._line())
            else:
                sys.stdout.write(self._line() + "\n")
            sys.stdout.flush()

    def __enter__(self):
        self.free_at_start = shutil.disk_usage(self.measure_dir).free
        self.started_at = time.monotonic()
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop.set()
        self.thread.join()
        sys.stdout.write(("\r" if self.is_tty else "") + self._line() + "\n")
        sys.stdout.flush()
        return False


def rebuild_steps(jetpack_dir, l4t, bsp_path, rootfs_path):
    """The NVIDIA-documented manual unpack, as (description, argv, cwd, unpacked_bytes) —
    the same sequence SDK Manager performs, which is why the result matches the
    manifest. unpacked_bytes drives a progress bar for the silent tar steps and is
    None for the scripts, which print their own output. The prerequisites script
    provides the qemu binfmt that apply_binaries.sh needs."""
    return [
        ("unpacking the BSP", ["tar", "xf", bsp_path, "-C", jetpack_dir], None, L4T_BSP.unpacked_bytes),
        ("installing the flash host prerequisites", ["sudo", "./tools/l4t_flash_prerequisites.sh"], l4t, None),
        ("unpacking the sample rootfs", ["sudo", "tar", "xpf", rootfs_path, "-C", os.path.join(l4t, "rootfs")],
         None, L4T_ROOTFS.unpacked_bytes),
        ("applying the NVIDIA binaries to the rootfs", ["sudo", "./apply_binaries.sh"], l4t, None),
    ]


def rebuild_tree(l4t, cache_dir=TARBALL_CACHE_DIR, sources=TARBALL_SOURCES, runner=subprocess.run):
    """Build a fresh JetPack 5.1.2 flash tree at l4t from the L4T R35.4.1 tarballs."""
    hdr("== Rebuilding the flash tree from the L4T R35.4.1 tarballs ==")
    jetpack_dir = os.path.abspath(os.path.join(l4t, os.pardir))
    if not check_rebuild_prerequisites(jetpack_dir):
        return False
    for tarball in (L4T_BSP, L4T_ROOTFS):
        if not obtain_tarball(tarball, cache_dir, sources):
            return False
    print("  validating sudo (host prerequisites, rootfs extraction and apply_binaries.sh need root)...")
    if runner(["sudo", "-v"], check=False).returncode != 0:
        bad("sudo credentials unavailable")
        return False
    os.makedirs(jetpack_dir, exist_ok=True)
    bsp_path = os.path.join(cache_dir, L4T_BSP.name)
    rootfs_path = os.path.join(cache_dir, L4T_ROOTFS.name)
    for description, argv, cwd, unpacked_bytes in rebuild_steps(jetpack_dir, l4t, bsp_path, rootfs_path):
        print("  {}: {}{}".format(description, " ".join(argv), "  (in {})".format(cwd) if cwd else ""))
        if unpacked_bytes is None:
            returncode = runner(argv, cwd=cwd, check=False).returncode
        else:
            with UnpackProgress(description, jetpack_dir, unpacked_bytes):
                returncode = runner(argv, cwd=cwd, check=False).returncode
        if returncode != 0:
            bad("{} failed".format(description))
            return False
    good("tree built at {}".format(l4t))
    return True


def move_tree_aside(l4t):
    """Rename a bad-but-present tree out of the way so the reinstall starts clean."""
    jetpack_dir = os.path.abspath(os.path.join(l4t, os.pardir))
    if not os.path.isdir(jetpack_dir):
        return
    aside = "{}.broken.{}".format(jetpack_dir, time.strftime("%Y%m%d-%H%M%S"))
    warn("moving the existing (bad) tree aside to {}".format(aside))
    warn("delete it yourself once the restored tree flashes successfully")
    os.rename(jetpack_dir, aside)


def ensure_tree(l4t, manifest, assume_yes):
    """Verify the tree; rebuild from the L4T tarballs and re-verify when bad."""
    # A stock tree only differs from known-good by the patch — apply it before the
    # manifest comparison so a fresh extract doesn't trigger a restore.
    if xml_patch_state(l4t) == "stock" and not apply_patch(l4t):
        return False
    if verify_tree(l4t, manifest):
        return apply_patch(l4t)

    hdr("== Restore ==")
    if not confirm("  Rebuild the flash tree from the L4T R35.4.1 tarballs "
                   "(~2.2 GB download unless cached, needs sudo)?", assume_yes):
        warn("rebuild declined")
        return False
    move_tree_aside(l4t)
    if not rebuild_tree(l4t):
        bad("tree rebuild did not complete")
        return False
    if xml_patch_state(l4t) == "stock" and not apply_patch(l4t):
        return False
    if not verify_tree(l4t, manifest):
        bad("tree still not healthy after restore")
        return False
    return apply_patch(l4t)


# ---------------------------------------------------------------- preflight


def run_preflight(l4t):
    """Run the GO/DO-NOT-FLASH preflight; returns its exit code (2 when unavailable)."""
    hdr("== Preflight ==")
    script, is_temp = locate_repo_file(PREFLIGHT_REL, ".sh")
    if not script:
        bad("could not obtain jetson-flash-preflight.sh — refusing to report GO")
        return EXIT_UNDETERMINED
    try:
        env = dict(os.environ, L4T=l4t)
        return subprocess.run(["bash", script], env=env, check=False).returncode
    finally:
        if is_temp:
            os.unlink(script)


# ---------------------------------------------------------------- flash


def parse_usb_pid(lsusb_text):
    """First NVIDIA (0955:xxxx) product id in lsusb output, or None."""
    for line in lsusb_text.splitlines():
        if "ID 0955:" in line:
            return line.split("ID 0955:", 1)[1][:4].lower()
    return None


def current_usb_pid():
    """NVIDIA USB product id currently on the bus, or None."""
    res = subprocess.run(["lsusb"], stdout=subprocess.PIPE, universal_newlines=True, check=False)
    return parse_usb_pid(res.stdout) if res.returncode == 0 else None


# nvsdkmanager_flash.sh puts the rootfs on the internal eMMC when given NO
# --storage, and on an EXTERNAL device (the eMMC then keeps only a boot partition
# pointing at it) when given one. So an eMMC-rootfs flash means passing no
# --storage at all — 'internal'/'emmc' therefore resolve to None, not a device
# name. Boards with no NVMe fitted must flash internal, or the run writes the
# eMMC boot GPT and then aborts at "Could not stat device /dev/nvme0n1".
STORAGE_INTERNAL = None
STORAGE_ALIASES = {"nvme": "nvme0n1p1", "internal": STORAGE_INTERNAL, "emmc": STORAGE_INTERNAL}


def resolve_storage(value):
    """Map a --storage value onto what nvsdkmanager_flash.sh wants.

    Returns a device string (external rootfs) or None (internal eMMC — no
    --storage flag). Aliases: nvme -> nvme0n1p1, internal/emmc -> None. Any other
    value (a raw device such as nvme0n1p1 or sda1) passes through unchanged.
    """
    key = value.strip().lower()
    return STORAGE_ALIASES[key] if key in STORAGE_ALIASES else value


def flash_command(username, storage):
    """argv for the nvsdkmanager_flash.sh run. storage=None omits --storage so the
    rootfs lands on the internal eMMC; a device name puts the rootfs there instead."""
    cmd = ["sudo", "./nvsdkmanager_flash.sh"]
    if storage is not None:
        cmd += ["--storage", storage]
    return cmd + ["--nv-auto-config", "--username", username]


def run_flash(l4t, username, password, storage):
    """Run nvsdkmanager_flash.sh under sudo, feeding the preseed password on stdin.

    storage is None for an internal-eMMC rootfs (no --storage) or a device name
    (e.g. nvme0n1p1) for an external rootfs.
    """
    hdr("== Flash ==")
    on_nvme = storage is not None and storage.startswith("nvme")
    print("  rootfs target: {}".format("internal eMMC" if storage is None else storage))
    if on_nvme:
        warn("rootfs goes on {} — the board MUST have that NVMe fitted; if it has none, "
             "Ctrl-C now and re-run with --storage internal".format(storage))
    print("  validating sudo (the flash needs root)...")
    if subprocess.run(["sudo", "-v"], check=False).returncode != 0:
        bad("sudo credentials unavailable")
        return False
    cmd = flash_command(username, storage)
    print("  running: {}  (in {})".format(" ".join(cmd), l4t))
    print("  expect ~15-25 min; do NOT unplug the board, even on failure it is recoverable\n")
    # nv_preseed.sh reads the new user's password from stdin (`read -s`), so a
    # pipe makes the whole flash non-interactive. The password never hits argv.
    with subprocess.Popen(cmd, cwd=l4t, stdin=subprocess.PIPE, universal_newlines=True) as proc:
        try:
            proc.stdin.write(password + "\n")
            proc.stdin.flush()
            proc.stdin.close()
        except BrokenPipeError:
            pass  # flash died before reading the password; the rc check below reports it
            # (close() is inside the guard too: it re-flushes buffered bytes and
            # raises the same BrokenPipeError when the child is already gone)
        returncode = proc.wait()
    if returncode != 0:
        bad("flash failed (rc {}) — see {}/initrdlog/ for details".format(returncode, l4t))
        if on_nvme:
            warn("no NVMe in this board? that aborts exactly here — re-run with "
                 "--storage internal to put the rootfs on the eMMC")
        return False
    good("flash reported success")
    return True


# ---------------------------------------------------------------- post-flash


def ssh_cmd(target):
    """Base argv for password ssh to the Jetson (password comes via $SSHPASS)."""
    return ["sshpass", "-e", "ssh"] + SSH_OPTS + [target]


def remote_run(target, password, command, sudo=False, input_text=None, capture=True):
    """Run a command on the Jetson; sudo commands get the password on stdin (never argv)."""
    if sudo:
        command = "sudo -S -p '' " + command
        input_text = password + "\n" + (input_text or "")
    return subprocess.run(
        ssh_cmd(target) + [command],
        env=dict(os.environ, SSHPASS=password),
        input=input_text,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        universal_newlines=True,
        check=False,
    )


def wait_for(description, predicate, timeout_s, interval_s=5):
    """Poll predicate() until true or timeout; prints progress dots."""
    print("  waiting for {} (up to {}s)...".format(description, timeout_s), end="", flush=True)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            print(" up")
            return True
        print(".", end="", flush=True)
        time.sleep(interval_s)
    print(" TIMEOUT")
    return False


def port_open(host, port, timeout=3):
    """True when a TCP connect to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def jetson_has_internet(target, password):
    """True when the Jetson can reach the NVIDIA apt repo by itself."""
    res = remote_run(target, password,
                     "curl -fsI --max-time 10 https://repo.download.nvidia.com/jetson/common/dists/r35.4/InRelease")
    return res.returncode == 0


# --- embedded HTTP proxy (GET + CONNECT), served over a reverse ssh tunnel, so
# --- a USB-only Jetson can reach apt repos through this machine. Stdlib only.


def _proxy_pipe(sock_a, sock_b):
    """Shovel bytes between two sockets until either side closes."""
    socks = [sock_a, sock_b]
    try:
        while True:
            readable, _, exceptional = select.select(socks, [], socks, 60)
            if exceptional or not readable:
                return
            for sock in readable:
                data = sock.recv(65536)
                if not data:
                    return
                (sock_b if sock is sock_a else sock_a).sendall(data)
    except OSError:
        pass


def _proxy_handle(client):
    """Serve one proxied request: CONNECT tunnels, absolute-URI requests forward."""
    upstream = None
    try:
        client.settimeout(30)
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = client.recv(65536)
            if not chunk:
                return
            req += chunk
        head, _, body = req.partition(b"\r\n\r\n")
        method, target, _ = head.split(b"\r\n")[0].decode("latin1").split(" ", 2)
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            upstream = socket.create_connection((host, int(port)), 20)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if body:  # bytes pipelined after the CONNECT header (e.g. an eager TLS hello)
                upstream.sendall(body)
        else:
            url = urlsplit(target)
            upstream = socket.create_connection((url.hostname, url.port or 80), 20)
            path = (url.path or "/") + ("?" + url.query if url.query else "")
            lines = head.split(b"\r\n")
            lines[0] = "{} {} HTTP/1.1".format(method, path).encode()
            # Only this first request line is rewritten; later keep-alive requests
            # would be shoveled raw (absolute-form, possibly for another host), so
            # force one-request-per-connection — the client just reconnects.
            lines = [line for line in lines
                     if not line.lower().startswith((b"proxy-connection:", b"connection:"))]
            lines.append(b"Connection: close")
            upstream.sendall(b"\r\n".join(lines) + b"\r\n\r\n" + body)
        client.settimeout(None)
        _proxy_pipe(client, upstream)
    except OSError:
        pass
    finally:
        for sock in (client, upstream):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def start_proxy():
    """Start the proxy on an ephemeral localhost port; returns (server_socket, port)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(50)
    port = server.getsockname()[1]

    def _serve():
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return  # server socket closed — shutdown
            threading.Thread(target=_proxy_handle, args=(client,), daemon=True).start()

    threading.Thread(target=_serve, daemon=True).start()
    return server, port


def apt_proxy_conf(port):
    """apt.conf.d contents pointing apt at the tunnelled proxy."""
    return ('Acquire::http::Proxy "http://127.0.0.1:{0}";\n'
            'Acquire::https::Proxy "http://127.0.0.1:{0}";\n').format(port)


class UsbInternet:
    """Context manager: proxy + reverse ssh tunnel + apt proxy config on the Jetson."""

    def __init__(self, target, password):
        self.target = target
        self.password = password
        self.server = None
        self.tunnel = None

    def __enter__(self):
        # Any failure below must tear down what already started: __exit__ never
        # runs when __enter__ raises, and an orphaned `ssh -N -R` would outlive
        # the tool and block the port on the Jetson for every later run.
        try:
            self.server, port = start_proxy()
            self.tunnel = subprocess.Popen(  # pylint: disable=consider-using-with
                ["sshpass", "-e", "ssh", "-N", "-o", "ExitOnForwardFailure=yes"] + SSH_OPTS
                + ["-R", "{0}:127.0.0.1:{0}".format(port), self.target],
                env=dict(os.environ, SSHPASS=self.password))
            time.sleep(2)
            if self.tunnel.poll() is not None:
                raise RuntimeError("reverse ssh tunnel failed to start")
            res = remote_run(self.target, self.password,
                             "tee {} >/dev/null".format(APT_PROXY_CONF),
                             sudo=True, input_text=apt_proxy_conf(port))
            if res.returncode != 0:
                raise RuntimeError("could not write {} on the Jetson".format(APT_PROXY_CONF))
        except BaseException:
            self._teardown(remove_conf=False)
            raise
        good("USB internet bridge up (host proxy on 127.0.0.1:{})".format(port))
        return self

    def _teardown(self, remove_conf):
        """Best-effort cleanup of the apt conf, the tunnel, and the proxy socket."""
        if remove_conf:
            remote_run(self.target, self.password, "rm -f {}".format(APT_PROXY_CONF), sudo=True)
        if self.tunnel and self.tunnel.poll() is None:
            self.tunnel.terminate()
        if self.server:
            self.server.close()

    def __exit__(self, exc_type, exc_value, traceback):
        self._teardown(remove_conf=True)
        return False


def fix_clock(target, password):
    """Set the Jetson's clock from this machine's UTC (fresh flashes boot in the past)."""
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    res = remote_run(target, password, "date -u -s '{}'".format(now), sudo=True)
    if res.returncode == 0:
        good("Jetson clock set to {} UTC".format(now))
    else:
        warn("could not set the Jetson clock — apt may reject Release files")


def quiet_apt_daily(target, password):
    """Stop the apt-daily timers and any run they already started, for this boot only."""
    res = remote_run(target, password, "systemctl stop " + APT_DAILY_UNITS, sudo=True)
    if res.returncode != 0:
        warn("could not stop the apt-daily units — apt-get update may collide with them:\n{}".format(
            (res.stdout or "").strip()[-500:]))


def apt_install_jetpack(target, password):
    """apt update + install nvidia-jetpack on the Jetson, streaming output."""
    apt_get = "apt-get -o DPkg::Lock::Timeout={}".format(APT_LOCK_WAIT_S)
    print("  apt-get update ...")
    res = remote_run(target, password, apt_get + " update", sudo=True)
    if res.returncode != 0:
        bad("apt-get update failed:\n{}".format((res.stdout or "").strip()[-2000:]))
        return False
    print("  apt-get install -y nvidia-jetpack  (several GB — this is the long part)...")
    res = remote_run(target, password,
                     "DEBIAN_FRONTEND=noninteractive " + apt_get + " install -y nvidia-jetpack",
                     sudo=True, capture=False)
    if res.returncode != 0:
        bad("nvidia-jetpack install failed (rc {})".format(res.returncode))
        return False
    good("nvidia-jetpack installed")
    return True


def sanity_checks(target, password):
    """Post-install checks: L4T release, jetpack version, eMMC error baseline."""
    hdr("== Sanity checks ==")
    all_good = True

    res = remote_run(target, password, "cat /etc/nv_tegra_release")
    release = (res.stdout or "").strip()
    if "R35" in release and "REVISION: 4.1" in release:
        good("L4T release: R35.4.1 (JetPack 5.1.2)")
    else:
        bad("unexpected L4T release: {}".format(release or "<unreadable>"))
        all_good = False

    res = remote_run(target, password, "dpkg-query -W -f '${Version}' nvidia-jetpack")
    version = (res.stdout or "").strip()
    if version == "5.1.2-b104":
        good("nvidia-jetpack 5.1.2-b104")
    else:
        bad("nvidia-jetpack version is '{}' (expected 5.1.2-b104)".format(version or "<absent>"))
        all_good = False

    res = remote_run(target, password, "dmesg | grep -i mmc0", sudo=True)
    mmc_lines = [line for line in (res.stdout or "").splitlines() if "password" not in line.lower()]
    mmc_bad = [line for line in mmc_lines
               if any(token in line.lower() for token in ("error", "timeout", "failed"))]
    if mmc_bad:
        warn("eMMC (mmc0) reports errors — the DG4064 CQE quirk; watch this under QA load:")
        for line in mmc_bad[-5:]:
            print("         {}".format(line.strip()))
    elif mmc_lines:
        good("eMMC (mmc0) baseline clean ({} dmesg lines, no errors)".format(len(mmc_lines)))
    else:
        warn("could not read mmc0 dmesg baseline")
    return all_good


def set_hostname(target, password, hostname):
    """Set the board's hostname (hostnamectl + the 127.0.1.1 line in /etc/hosts) and read it back."""
    hosts_line = "127.0.1.1\t{}".format(hostname)
    command = ("hostnamectl set-hostname {h} && "
               "if grep -q '^127\\.0\\.1\\.1' /etc/hosts; then sed -i 's/^127\\.0\\.1\\.1.*/{line}/' /etc/hosts; "
               "else printf '%s\\n' '{line}' >> /etc/hosts; fi").format(h=hostname, line=hosts_line)
    res = remote_run(target, password, command, sudo=True)
    if res.returncode != 0:
        bad("setting hostname failed:\n{}".format((res.stdout or "").strip()[-500:]))
        return False
    actual = (remote_run(target, password, "hostname").stdout or "").strip()
    if actual != hostname:
        bad("hostname reads back as '{}' after setting '{}'".format(actual, hostname))
        return False
    good("hostname set to {}".format(hostname))
    return True


def post_flash(target, password, hostname=None):
    """Boot-wait, clock fix, hostname, apt (bridged over USB when offline)."""
    hdr("== Post-flash setup ==")
    host = target.split("@", 1)[1]
    if not wait_for("board to boot (USB device mode)",
                    lambda: current_usb_pid() == USB_PID_BOOTED, BOOT_TIMEOUT_S):
        bad("board did not reach booted USB device mode — check it manually")
        return False
    if not wait_for("ssh on {}".format(host), lambda: port_open(host, 22), SSH_TIMEOUT_S):
        bad("ssh never came up on {}".format(host))
        return False
    res = remote_run(target, password, "echo SSH_OK")
    if "SSH_OK" not in (res.stdout or ""):
        bad("ssh login as {} failed — wrong username/password?".format(target))
        return False
    good("ssh login works")
    quiet_apt_daily(target, password)
    fix_clock(target, password)
    if hostname and not set_hostname(target, password, hostname):
        return False

    if jetson_has_internet(target, password):
        good("Jetson has its own internet access")
        return apt_install_jetpack(target, password)
    warn("Jetson has no internet — bridging over the USB link (plug in Ethernet for a permanent fix)")
    try:
        with UsbInternet(target, password):
            return apt_install_jetpack(target, password)
    except RuntimeError as exc:
        bad(str(exc))
        return False


# ---------------------------------------------------------------- manifest maintenance


def make_manifest(l4t, output):
    """Regenerate the canonical manifest from a healthy patched tree (maintainers only)."""
    if xml_patch_state(l4t) != "patched":
        bad("refusing to snapshot an unpatched/unhealthy tree — run 'verify' first")
        return False
    manifest = build_manifest(l4t)
    if not manifest:
        bad("no manifest files found under {} — wrong --l4t?".format(l4t))
        return False
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)
        handle.write("\n")
    good("manifest written: {} ({} files)".format(output, len(manifest)))
    print("  commit this as {} — every machine verifies against it".format(MANIFEST_REL))
    return True


# ---------------------------------------------------------------- CLI


def dns_label(value):
    """argparse type for --hostname: one DNS label (RFC 1123), the only form a bare
    hostname can safely take."""
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", value):
        raise argparse.ArgumentTypeError(
            "'{}' is not a valid hostname: 1-63 lowercase letters, digits or hyphens, "
            "not starting or ending with a hyphen".format(value))
    return value


def build_parser():
    """argparse tree for the subcommands (flash is the default)."""
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--l4t", default=DEFAULT_L4T, help="Linux_for_Tegra tree (default: %(default)s)")
    shared.add_argument("--manifest", default=None,
                        help="canonical manifest path (default: the repo's {})".format(MANIFEST_REL))

    parser = argparse.ArgumentParser(
        prog="er_jetson_flash", parents=[shared],
        description="Check, restore, flash and provision an AGX Orin QA cortex (JetPack 5.1.2).")
    sub = parser.add_subparsers(dest="command")

    credentials = argparse.ArgumentParser(add_help=False)
    credentials.add_argument("--username", default=DEF_USER, help="Jetson user (default: %(default)s)")
    credentials.add_argument("--password", default=None,
                             help="password for that user (default: $ER_JETSON_PASSWORD, else prompted). "
                                  "Prefer the env var or the prompt — argv is visible in ps")
    credentials.add_argument("--hostname", default=None, type=dns_label,
                             help="machine name to give the board during post-flash, e.g. qa-cortex-3 "
                                  "(default: leave NVIDIA's)")

    flash = sub.add_parser("flash", parents=[shared, credentials],
                           help="full pipeline: verify/restore, preflight, flash, post-flash (default)")
    flash.add_argument("--storage", default="nvme0n1p1",
                       help="rootfs location: 'nvme' (=nvme0n1p1, the default) for an NVMe SSD, "
                            "'internal' (or 'emmc') for the on-board eMMC on boards with no NVMe, "
                            "or a raw device such as sda1 (default: %(default)s)")
    flash.add_argument("--yes", action="store_true", help="assume yes on restore prompts (sudo may still ask)")
    flash.add_argument("--skip-post", action="store_true", help="stop after the flash itself")

    sub.add_parser("post-flash", parents=[shared, credentials],
                   help="only the post-flash provisioning (clock, apt, nvidia-jetpack, sanity checks) "
                        "on a board that is already flashed and booted — the recovery path when a "
                        "flash run failed after the flash itself")

    sub.add_parser("verify", parents=[shared],
                   help="check tree presence, patch and manifest; changes nothing")

    restore = sub.add_parser("restore", parents=[shared],
                             help="verify and repair/reinstall the flash tree now, don't flash")
    restore.add_argument("--yes", action="store_true", help="assume yes on prompts")

    make = sub.add_parser("make-manifest", parents=[shared],
                          help="regenerate the canonical manifest from the local patched tree")
    make.add_argument("--output", default="er_jetson_flash_manifest.json",
                      help="where to write it (default: %(default)s in the current directory)")
    return parser


def check_host_tools(skip_post):
    """Fail fast on missing host tools instead of after a 20-minute flash."""
    needed = ["curl", "lsusb"] + ([] if skip_post else ["sshpass"])
    missing = [tool for tool in needed if not shutil.which(tool)]
    if missing:
        bad("missing host tools: {} — install them first (sshpass/curl via apt, lsusb is usbutils)".format(
            ", ".join(missing)))
        return False
    return True


def resolve_password(args):
    """--password, else $ER_JETSON_PASSWORD, else an interactive prompt; None when none is possible.

    Deliberately no default: this repo is public."""
    password = args.password or os.environ.get("ER_JETSON_PASSWORD")
    if password:
        return password
    if not sys.stdin.isatty():
        return None
    if args.command == "flash":
        print("  The flash creates Jetson user '{}' (change with --username) and needs the password "
              "that will be set for it; the post-flash steps then ssh in with the same password.".format(args.username))
        prompt = "  Password to set for new user '{}': ".format(args.username)
    else:
        print("  Post-flash steps ssh into {} as existing user '{}' (change with --username).".format(
            DEF_HOST, args.username))
        prompt = "  Current ssh password of '{}': ".format(args.username)
    print("  (non-interactive: export ER_JETSON_PASSWORD)")
    try:
        return getpass.getpass(prompt) or None
    except EOFError:
        print()
        return None


NO_PASSWORD_MSG = "no Jetson password: set ER_JETSON_PASSWORD, pass --password, or run interactively"


def provision(target, password, hostname):
    """Post-flash setup and sanity checks; the pipeline's final stage."""
    if not post_flash(target, password, hostname=hostname):
        return EXIT_FAILURE
    if not sanity_checks(target, password):
        return EXIT_FAILURE
    hdr("== DONE — board flashed, provisioned and sane ==")
    return EXIT_OK


def cmd_post_flash(args):
    """Post-flash provisioning only, for a board that is already flashed and booted."""
    if not check_host_tools(skip_post=False):
        return EXIT_FAILURE
    password = resolve_password(args)
    if not password:
        bad(NO_PASSWORD_MSG)
        return EXIT_FAILURE
    return provision("{}@{}".format(args.username, DEF_HOST), password, args.hostname)


def cmd_flash(args):
    """Full pipeline."""
    if not check_host_tools(args.skip_post):
        return EXIT_FAILURE
    password = resolve_password(args)
    if not password:
        bad(NO_PASSWORD_MSG)
        return EXIT_FAILURE
    if not ensure_tree(args.l4t, load_canonical_manifest(args.manifest), args.yes):
        return EXIT_FAILURE
    preflight_rc = run_preflight(args.l4t)
    if preflight_rc != 0:
        bad("preflight did not report GO (rc {}) — fix that first; NOT flashing".format(preflight_rc))
        return preflight_rc
    if current_usb_pid() != USB_PID_RECOVERY:
        bad("board is not in forced recovery — put it there and re-run (see preflight banner)")
        return EXIT_UNDETERMINED
    if not run_flash(args.l4t, args.username, password, resolve_storage(args.storage)):
        return EXIT_FAILURE
    if args.skip_post:
        good("flash done; post-flash setup skipped (--skip-post)")
        return EXIT_OK
    return provision("{}@{}".format(args.username, DEF_HOST), password, args.hostname)


def cmd_verify(args):
    """Verify-only entry point."""
    healthy = verify_tree(args.l4t, load_canonical_manifest(args.manifest))
    if healthy and xml_patch_state(args.l4t) == "stock":
        warn("run 'er_jetson_flash restore' or apply the patch before flashing a post-PCN module")
        return EXIT_UNDETERMINED
    return EXIT_OK if healthy else EXIT_FAILURE


# Options that consume the next argv token — needed to tell a subcommand token
# apart from an option VALUE that happens to collide with one (e.g. --l4t flash).
VALUE_OPTS = frozenset(("--l4t", "--manifest", "--username", "--password", "--storage", "--output"))


def default_subcommand(argv):
    """Prepend 'flash' when argv carries no subcommand (option values don't count).
    A help request stays top-level so the subcommand list is what gets shown."""
    if "-h" in argv or "--help" in argv:
        return argv
    expecting_value = False
    for token in argv:
        if expecting_value:
            expecting_value = False
            continue
        if token.startswith("-"):
            expecting_value = token in VALUE_OPTS  # --opt=value consumes nothing
            continue
        return argv  # first bare token is the subcommand; argparse validates it
    return ["flash"] + argv


def main(argv=None):
    """Entry point. Returns the process exit code."""
    # Status lines must interleave correctly with the ssh children's output when
    # stdout is a pipe (tee, log files) — a block-buffered stdout prints them late.
    # (Only real TextIOWrappers have reconfigure; a redirected stdout is left alone.)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    argv = default_subcommand(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            return cmd_verify(args)
        if args.command == "restore":
            if ensure_tree(args.l4t, load_canonical_manifest(args.manifest), args.yes):
                good("tree is healthy and patched")
                return EXIT_OK
            return EXIT_FAILURE
        if args.command == "make-manifest":
            return EXIT_OK if make_manifest(args.l4t, args.output) else EXIT_FAILURE
        if args.command == "post-flash":
            return cmd_post_flash(args)
        return cmd_flash(args)
    except KeyboardInterrupt:
        print()
        bad("interrupted")
        return EXIT_UNDETERMINED


if __name__ == "__main__":
    sys.exit(main())
