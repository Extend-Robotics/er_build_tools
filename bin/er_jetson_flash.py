#!/usr/bin/env python3
"""er_jetson_flash — one-command flash pipeline for AGX Orin QA cortexes (JetPack 5.1.2).

Pipeline (subcommand `flash`, the default):
  1. verify   — is the local flash tree present, manifest-clean and num_sectors-patched?
                (restores it from the newest local backup tarball, or guides an
                sdkmanager --cli reinstall, when it is not)
  2. preflight— run bin/jetson-flash-preflight.sh: GO / DO NOT FLASH gate
  3. flash    — sudo ./nvsdkmanager_flash.sh --storage ... --nv-auto-config --username ...
  4. post     — wait for boot, fix the clock, give the board internet via an
                embedded HTTP proxy over the USB link when it has none,
                apt install nvidia-jetpack (pinned 5.1.2-b104), sanity checks

Extra subcommands: verify / restore / make-backup (tarball + hash manifest of the
patched tree, so `verify` can detect a re-extracted or half-deleted tree).

Background: docs/er-jetson-flash.md, docs/jetson-flash-preflight.md.
History: an SDK Manager GUI uninstall deleted the whole patched tree (2026-07-16);
NVIDIA's catalog only lists 5.1.2 with BOTH --show-all-versions --archived-versions.

Python 3.8, stdlib only. Exit codes: 0 = success, 1 = failure, 2 = undetermined/aborted.
"""

import argparse
import glob
import hashlib
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

HOME = os.path.expanduser("~")
DEFAULT_L4T = os.path.join(HOME, "nvidia", "nvidia_sdk",
                           "JetPack_5.1.2_Linux_JETSON_AGX_ORIN_TARGETS", "Linux_for_Tegra")
XML_REL = os.path.join("bootloader", "t186ref", "cfg", "flash_t234_qspi_sdmmc.xml")
STOCK_VAL = 'num_sectors="124321792"'
PATCH_VAL = 'num_sectors="124190720"'

DEFAULT_BACKUP_DIR = os.path.join(HOME, "backups")
BACKUP_GLOB = "JetPack_5.1.2_flash_tree_*.tar.zst"
MANIFEST_SUFFIX = ".manifest.json"
# Small, load-bearing files only (configs + flash scripts) — hashing the full
# 38 GB tree every run would take minutes for no extra signal.
MANIFEST_GLOBS = ("*.conf", "*.conf.common", "flash.sh", "nvsdkmanager_flash.sh",
                  os.path.join("bootloader", "t186ref", "cfg", "*.xml"),
                  os.path.join("tools", "kernel_flash", "*.sh"),
                  os.path.join("tools", "kernel_flash", "*.xml"))

RAW_URL_BASE = ("https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/"
                + os.environ.get("ER_BUILD_TOOLS_BRANCH", "main"))
PREFLIGHT_REL = "bin/jetson-flash-preflight.sh"

# Both flags are required: NVIDIA pruned 5.1.2 from the default AND the plain
# archived catalog server-side; only the combination brings it back.
SDKMANAGER_CMD = ["sdkmanager", "--cli", "--action", "install", "--login-type", "devzone",
                  "--product", "Jetson", "--target-os", "Linux", "--version", "5.1.2",
                  "--show-all-versions", "--archived-versions",
                  "--target", "JETSON_AGX_ORIN_TARGETS", "--license", "accept"]

DEF_USER = "extend"
DEF_PASS = "extend"
DEF_HOST = "192.168.55.1"
USB_PID_RECOVERY = "7023"
USB_PID_BOOTED = "7020"
USB_PID_INITRD = "7035"

SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3"]

BOOT_TIMEOUT_S = 300
SSH_TIMEOUT_S = 240
APT_PROXY_CONF = "/etc/apt/apt.conf.d/99er-usb-proxy"

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


def find_backup(backup_dir):
    """Newest backup tarball (and its manifest path, or None) in backup_dir."""
    tarballs = sorted(glob.glob(os.path.join(backup_dir, BACKUP_GLOB)),
                      key=os.path.getmtime, reverse=True)
    if not tarballs:
        return None, None
    manifest = tarballs[0] + MANIFEST_SUFFIX
    return tarballs[0], manifest if os.path.isfile(manifest) else None


def load_manifest(manifest_path):
    """Load a manifest JSON; None when there is no manifest."""
    if not manifest_path:
        return None
    with open(manifest_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


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
        # The XML legitimately differs from a stock re-extract only via the patch,
        # which is checked above — manifest hashes are taken from the PATCHED tree.
        if problems:
            bad("tree differs from the known-good backup manifest:")
            for problem in problems[:10]:
                print("         {}".format(problem))
            if len(problems) > 10:
                print("         ... and {} more".format(len(problems) - 10))
            return False
        good("all {} manifest files match the known-good backup".format(len(manifest)))
    else:
        warn("no backup manifest available — only the patch state was checked")
    return state in ("patched", "stock")


# ---------------------------------------------------------------- tree restore


def restore_from_tarball(l4t, tarball):
    """Extract the backup tarball over (a moved-aside) tree. True on success."""
    # layout: <sdk_dir>/<jetpack_dir>/Linux_for_Tegra — the tarball contains <jetpack_dir>
    jetpack_dir = os.path.abspath(os.path.join(l4t, os.pardir))
    sdk_dir = os.path.dirname(jetpack_dir)
    if not shutil.which("zstd"):
        bad("zstd is not installed (sudo apt-get install zstd) — cannot extract the backup")
        return False
    needed = os.path.getsize(tarball) * 4  # zstd of this tree compresses ~2.5-4x
    free = shutil.disk_usage(sdk_dir).free
    if free < needed:
        bad("not enough disk space to extract: {:.0f} GB free, ~{:.0f} GB needed".format(
            free / 1e9, needed / 1e9))
        return False
    if os.path.isdir(jetpack_dir):
        aside = "{}.broken.{}".format(jetpack_dir, time.strftime("%Y%m%d-%H%M%S"))
        warn("moving the existing (bad) tree aside to {}".format(aside))
        warn("delete it yourself once the restored tree flashes successfully")
        os.rename(jetpack_dir, aside)
    print("  extracting {} (this takes a few minutes)...".format(os.path.basename(tarball)))
    res = subprocess.run(["tar", "-C", sdk_dir, "-I", "zstd", "-xf", tarball], check=False)
    if res.returncode != 0:
        bad("tar extraction failed (rc {})".format(res.returncode))
        return False
    good("backup tree extracted")
    return True


def guided_sdkmanager():
    """Run the interactive sdkmanager reinstall with exact operator guidance."""
    hdr("== Guided sdkmanager reinstall ==")
    print("""  No local backup tarball found — falling back to NVIDIA SDK Manager.
  This needs YOUR interactive NVIDIA Developer login; everything else is preset.

  Answers to give when the wizard asks anything not already preset:
    - system configuration:      DESELECT 'Host Machine', keep 'Target Hardware'
    - additional SDKs:           None
    - 'flash the module?':       No   (this tool patches the tree, then flashes)
    - 'install SDK components on your Jetson?':  Skip / decline
                                 (done post-flash via apt, pinned to 5.1.2-b104)

  Downloads reuse ~/Downloads/nvidia/sdkm_downloads when present (mostly offline).
""")
    print("  running: {}\n".format(" ".join(SDKMANAGER_CMD)))
    try:
        res = subprocess.run(SDKMANAGER_CMD, check=False)
    except FileNotFoundError:
        bad("sdkmanager is not installed on this machine")
        return False
    return res.returncode == 0


def ensure_tree(l4t, backup_dir, assume_yes):
    """Verify the tree; restore (tarball, else sdkmanager) and re-verify when bad."""
    tarball, manifest_path = find_backup(backup_dir)
    manifest = load_manifest(manifest_path)
    # A stock tree only differs from known-good by the patch — apply it before the
    # manifest comparison so a fresh sdkmanager extract doesn't trigger a restore.
    if xml_patch_state(l4t) == "stock" and not apply_patch(l4t):
        return False
    if verify_tree(l4t, manifest):
        return apply_patch(l4t)

    hdr("== Restore ==")
    if tarball:
        print("  newest backup: {}".format(tarball))
        if not confirm("  Restore the flash tree from this backup?", assume_yes):
            warn("restore declined")
            return False
        if not restore_from_tarball(l4t, tarball):
            return False
    else:
        if not confirm("  Reinstall the flash tree via sdkmanager (interactive login)?", assume_yes):
            warn("reinstall declined")
            return False
        if not guided_sdkmanager():
            bad("sdkmanager did not complete successfully")
            return False
    if xml_patch_state(l4t) == "stock" and not apply_patch(l4t):
        return False
    if not verify_tree(l4t, manifest):
        bad("tree still not healthy after restore")
        return False
    return apply_patch(l4t)


# ---------------------------------------------------------------- preflight


def locate_preflight():
    """Prefer the sibling repo checkout; otherwise fetch from GitHub raw. (path, is_temp)."""
    sibling = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           os.path.basename(PREFLIGHT_REL))
    if os.path.isfile(sibling):
        return sibling, False
    tmp_fd, path = tempfile.mkstemp(prefix="er_preflight.", suffix=".sh")
    os.close(tmp_fd)
    url = "{}/{}?nocache={}".format(RAW_URL_BASE, PREFLIGHT_REL, os.getpid())
    res = subprocess.run(["curl", "-fsSL", "--max-time", "30", url, "-o", path], check=False)
    if res.returncode != 0:
        os.unlink(path)
        return None, False
    return path, True


def run_preflight(l4t):
    """Run the GO/DO-NOT-FLASH preflight; returns its exit code (2 when unavailable)."""
    hdr("== Preflight ==")
    script, is_temp = locate_preflight()
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


def run_flash(l4t, username, password, storage):
    """Run nvsdkmanager_flash.sh under sudo, feeding the preseed password on stdin."""
    hdr("== Flash ==")
    print("  validating sudo (the flash needs root)...")
    if subprocess.run(["sudo", "-v"], check=False).returncode != 0:
        bad("sudo credentials unavailable")
        return False
    cmd = ["sudo", "./nvsdkmanager_flash.sh", "--storage", storage,
           "--nv-auto-config", "--username", username]
    print("  running: {}  (in {})".format(" ".join(cmd), l4t))
    print("  expect ~15-25 min; do NOT unplug the board, even on failure it is recoverable\n")
    # nv_preseed.sh reads the new user's password from stdin (`read -s`), so a
    # pipe makes the whole flash non-interactive. The password never hits argv.
    with subprocess.Popen(cmd, cwd=l4t, stdin=subprocess.PIPE, universal_newlines=True) as proc:
        try:
            proc.stdin.write(password + "\n")
            proc.stdin.flush()
        except BrokenPipeError:
            pass  # flash died before reading the password; the rc check below reports it
        finally:
            proc.stdin.close()
        returncode = proc.wait()
    if returncode != 0:
        bad("flash failed (rc {}) — see {}/initrdlog/ for details".format(returncode, l4t))
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
        method, target, _ = req.split(b"\r\n")[0].decode("latin1").split(" ", 2)
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            upstream = socket.create_connection((host, int(port)), 20)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            url = urlsplit(target)
            upstream = socket.create_connection((url.hostname, url.port or 80), 20)
            path = (url.path or "/") + ("?" + url.query if url.query else "")
            lines = req.split(b"\r\n")
            lines[0] = "{} {} HTTP/1.1".format(method, path).encode()
            upstream.sendall(b"\r\n".join(
                line for line in lines if not line.lower().startswith(b"proxy-connection:")))
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
        self.server, port = start_proxy()
        self.tunnel = subprocess.Popen(
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
        good("USB internet bridge up (host proxy on 127.0.0.1:{})".format(port))
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        remote_run(self.target, self.password, "rm -f {}".format(APT_PROXY_CONF), sudo=True)
        if self.tunnel and self.tunnel.poll() is None:
            self.tunnel.terminate()
        if self.server:
            self.server.close()
        return False


def fix_clock(target, password):
    """Set the Jetson's clock from this machine's UTC (fresh flashes boot in the past)."""
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    res = remote_run(target, password, "date -u -s '{}'".format(now), sudo=True)
    if res.returncode == 0:
        good("Jetson clock set to {} UTC".format(now))
    else:
        warn("could not set the Jetson clock — apt may reject Release files")


def apt_install_jetpack(target, password):
    """apt update + install nvidia-jetpack on the Jetson, streaming output."""
    print("  apt-get update ...")
    res = remote_run(target, password, "apt-get update", sudo=True)
    if res.returncode != 0:
        bad("apt-get update failed:\n{}".format((res.stdout or "").strip()[-2000:]))
        return False
    print("  apt-get install -y nvidia-jetpack  (several GB — this is the long part)...")
    res = remote_run(target, password,
                     "DEBIAN_FRONTEND=noninteractive apt-get install -y nvidia-jetpack",
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


def post_flash(target, password):
    """Boot-wait, clock fix, apt (bridged over USB when offline), sanity checks."""
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
    fix_clock(target, password)

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


# ---------------------------------------------------------------- backup


def make_backup(l4t, backup_dir, manifest_only):
    """Tar + manifest the (patched) tree so verify/restore have a known-good reference."""
    if xml_patch_state(l4t) != "patched":
        bad("refusing to back up an unpatched/unhealthy tree — run 'verify' first")
        return False
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    tarball = os.path.join(backup_dir, "JetPack_5.1.2_flash_tree_patched_{}.tar.zst".format(stamp))

    print("  building manifest ({} file patterns)...".format(len(MANIFEST_GLOBS)))
    manifest = build_manifest(l4t)
    if manifest_only:
        existing, _ = find_backup(backup_dir)
        if not existing:
            bad("--manifest-only needs an existing tarball to attach the manifest to")
            return False
        tarball = existing
    with open(tarball + MANIFEST_SUFFIX, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)
    good("manifest written: {} ({} files)".format(tarball + MANIFEST_SUFFIX, len(manifest)))
    if manifest_only:
        return True

    if not shutil.which("zstd"):
        bad("zstd is not installed (sudo apt-get install zstd)")
        return False
    jetpack_dir = os.path.abspath(os.path.join(l4t, os.pardir))
    print("  creating {} (~38 GB in, expect 10+ min)...".format(tarball))
    res = subprocess.run(
        ["tar", "-C", os.path.dirname(jetpack_dir), "-I", "zstd -T0", "-cf", tarball + ".partial",
         os.path.basename(jetpack_dir)], check=False)
    if res.returncode != 0:
        bad("tar failed (rc {})".format(res.returncode))
        return False
    os.replace(tarball + ".partial", tarball)
    good("backup written: {} ({:.1f} GB)".format(tarball, os.path.getsize(tarball) / 1e9))
    return True


# ---------------------------------------------------------------- CLI


SUBCOMMANDS = ("flash", "verify", "restore", "make-backup")


def build_parser():
    """argparse tree for the four subcommands (flash is the default)."""
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--l4t", default=DEFAULT_L4T, help="Linux_for_Tegra tree (default: %(default)s)")
    shared.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR,
                        help="where backup tarballs live (default: %(default)s)")

    parser = argparse.ArgumentParser(
        prog="er_jetson_flash", parents=[shared],
        description="Check, restore, flash and provision an AGX Orin QA cortex (JetPack 5.1.2).")
    sub = parser.add_subparsers(dest="command")

    flash = sub.add_parser("flash", parents=[shared],
                           help="full pipeline: verify/restore, preflight, flash, post-flash (default)")
    flash.add_argument("--username", default=DEF_USER, help="Jetson user to create (default: %(default)s)")
    flash.add_argument("--password", default=DEF_PASS, help="password for that user (default: %(default)s)")
    flash.add_argument("--storage", default="nvme0n1p1", help="rootfs device (default: %(default)s)")
    flash.add_argument("--yes", action="store_true", help="assume yes on restore prompts (login stays interactive)")
    flash.add_argument("--skip-post", action="store_true", help="stop after the flash itself")

    sub.add_parser("verify", parents=[shared],
                   help="check tree presence, patch and manifest; changes nothing")

    restore = sub.add_parser("restore", parents=[shared],
                             help="restore the tree from backup (or guided sdkmanager) now")
    restore.add_argument("--yes", action="store_true", help="assume yes on prompts")

    backup = sub.add_parser("make-backup", parents=[shared],
                            help="tar + manifest the current patched tree")
    backup.add_argument("--manifest-only", action="store_true",
                        help="regenerate the manifest for the newest existing tarball")
    return parser


def cmd_flash(args):
    """Full pipeline."""
    if not ensure_tree(args.l4t, args.backup_dir, args.yes):
        return EXIT_FAILURE
    preflight_rc = run_preflight(args.l4t)
    if preflight_rc != 0:
        bad("preflight did not report GO (rc {}) — fix that first; NOT flashing".format(preflight_rc))
        return preflight_rc
    if current_usb_pid() != USB_PID_RECOVERY:
        bad("board is not in forced recovery — put it there and re-run (see preflight banner)")
        return EXIT_UNDETERMINED
    if not run_flash(args.l4t, args.username, args.password, args.storage):
        return EXIT_FAILURE
    if args.skip_post:
        good("flash done; post-flash setup skipped (--skip-post)")
        return EXIT_OK
    target = "{}@{}".format(args.username, DEF_HOST)
    if not post_flash(target, args.password):
        return EXIT_FAILURE
    if not sanity_checks(target, args.password):
        return EXIT_FAILURE
    hdr("== DONE — board flashed, provisioned and sane ==")
    return EXIT_OK


def cmd_verify(args):
    """Verify-only entry point."""
    _, manifest_path = find_backup(args.backup_dir)
    manifest = load_manifest(manifest_path)
    healthy = verify_tree(args.l4t, manifest)
    if healthy and xml_patch_state(args.l4t) == "stock":
        warn("run 'er_jetson_flash restore' or apply the patch before flashing a post-PCN module")
        return EXIT_UNDETERMINED
    return EXIT_OK if healthy else EXIT_FAILURE


def main(argv=None):
    """Entry point. Returns the process exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    # `flash` is the default subcommand: `er_jetson_flash --username bob` works.
    if not any(token in SUBCOMMANDS for token in argv) and "-h" not in argv and "--help" not in argv:
        argv.insert(0, "flash")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            return cmd_verify(args)
        if args.command == "restore":
            if ensure_tree(args.l4t, args.backup_dir, args.yes):
                good("tree is healthy and patched")
                return EXIT_OK
            return EXIT_FAILURE
        if args.command == "make-backup":
            return EXIT_OK if make_backup(args.l4t, args.backup_dir, args.manifest_only) else EXIT_FAILURE
        return cmd_flash(args)
    except KeyboardInterrupt:
        print()
        bad("interrupted")
        return EXIT_UNDETERMINED


if __name__ == "__main__":
    sys.exit(main())
