# er_jetson_flash — one-command AGX Orin QA-cortex flash (JetPack 5.1.2)

`er_jetson_flash` takes a Jetson AGX Orin devkit in forced recovery and a
deployment machine in *any* state, and produces a flashed, provisioned,
sanity-checked QA cortex:

```
er_jetson_flash                      # everything, with extend/extend defaults
er_jetson_flash --username qa --password s3cret
er_jetson_flash --storage internal   # board has no NVMe — put the rootfs on the eMMC
er_jetson_flash verify               # read-only health check of the flash tree
er_jetson_flash restore              # fix/restore the flash tree, don't flash
er_jetson_flash make-manifest        # regenerate the canonical manifest (maintainers)
```

Nothing is tied to a particular machine: a fresh (or mangled) deployment machine
just takes longer the first time, while the tool walks a guided reinstall.

## What the full pipeline does

1. **Verify the flash tree** (`~/nvidia/nvidia_sdk/JetPack_5.1.2_.../Linux_for_Tegra`)
   - the PCN210100 `num_sectors` eMMC patch is applied (applied in-place when the
     tree is healthy-but-stock; see [jetson-flash-preflight.md](jetson-flash-preflight.md)
     for why unpatched flashes of post-PCN modules fail)
   - every config/flash-script hashed in the repo's **canonical manifest**
     ([`bin/er_jetson_flash_manifest.json`](../bin/er_jetson_flash_manifest.json))
     matches. A JetPack 5.1.2 extract is deterministic, so one committed manifest
     serves every machine, and catches silently re-extracted, half-deleted, or
     hand-edited trees. (Hashing all 38 GB would add minutes for no extra signal —
     only the ~117 small load-bearing files are hashed.)
2. **Restore when broken**: the bad tree is moved aside (never deleted), then the
   tree is **rebuilt from the two L4T R35.4.1 tarballs** — the same unpack SDK
   Manager performs (`tar xf` BSP, `sudo ./tools/l4t_flash_prerequisites.sh`,
   `sudo tar xpf` sample rootfs, `sudo ./apply_binaries.sh`), so the result
   matches the canonical manifest. No SDK Manager, so no NVIDIA login and no
   host-OS gate: SDK Manager refuses JetPack 5.1.2 on anything newer than Ubuntu
   20.04, while NVIDIA's own `l4t_flash_prerequisites.sh` in R35.4.1 already
   handles 22.04 hosts. The rebuild runs the same on 20.04 and 22.04.
   - Tarballs come from NVIDIA's public release directory first
     (`developer.download.nvidia.com/embedded/L4T/r35_Release_v4.1/release/`), and
     from the private `Extend-Robotics/er_jetson_archive` release `r35.4.1` when
     that fails (needs `gh auth login` or `$GH_TOKEN`). Every download prints
     `source: nvidia.com` or `source: er_jetson_archive (fallback: <why>)`, and is
     checked against the sha256 pinned in the script whichever source served it.
   - Downloads land in `~/Downloads/nvidia/sdkm_downloads` — the directory and
     file names SDK Manager used, so a machine with its old cache rebuilds offline.
   - Fail-fast before the 2.2 GB download: `curl` and `tar` must be present and the
     target filesystem needs `REBUILD_MIN_FREE_GIB` free. Everything else the flash
     host needs (`qemu-user-static`, `binfmt-support`, `lz4`, `abootimg`, `dtc`, …)
     is installed by NVIDIA's `l4t_flash_prerequisites.sh` as part of the rebuild.
3. **Preflight gate**: runs [`er_jetson_flash_preflight`](jetson-flash-preflight.md);
   anything but GO stops the pipeline.
4. **Flash**: `sudo ./nvsdkmanager_flash.sh [--storage <dev>] --nv-auto-config
   --username <user>` — first-boot user preseeded (no monitor needed); the password
   is fed to the preseeder over stdin, never argv. `--storage` chooses where the
   rootfs goes: the default `nvme0n1p1` puts it on the NVMe SSD (the eMMC then holds
   only a boot partition pointing at it), while `--storage internal` omits the flag so
   the rootfs lands on the on-board eMMC. Use `internal` for boards with **no NVMe
   fitted** — otherwise the flash writes the eMMC boot GPT and then aborts at
   `Could not stat device /dev/nvme0n1`.
5. **Post-flash setup**:
   - waits for the board to boot (USB `0955:7020`) and for ssh
   - fixes the fresh-flash clock skew (apt rejects "future" Release files otherwise)
   - quiets the apt-daily/unattended-upgrades race: the clock jump makes systemd's
     persistent apt-daily timers fire "missed" runs immediately, and their apt-get
     grabs the apt locks (`Could not get lock /var/lib/apt/lists/lock`). The tool
     stops both timers for this boot only (the shipped image stays stock) and runs
     its own apt-get with `-o DPkg::Lock::Timeout=600` to wait out any run that
     already started
   - checks whether the board has internet; when it is USB-only, the tool starts an
     embedded HTTP proxy on the host, reverse-tunnels it over ssh, and points the
     board's apt at it for the duration (config removed afterwards)
   - `apt install nvidia-jetpack` — the r35.4 repo pins the exact 5.1.2-b104
     component set (CUDA 11.4, cuDNN, TensorRT, OpenCV, VPI, container runtime),
     identical to what SDK Manager's GUI would install
   - sanity checks: L4T release is R35.4.1, `nvidia-jetpack` is 5.1.2-b104, and an
     eMMC `dmesg` baseline (post-PCN DG4064 modules have a known CQE quirk on
     stock JP 5.1.2 — a clean baseline now makes later errors attributable)

## The canonical manifest

`bin/er_jetson_flash_manifest.json` is sha256 of every config and flash script in
a known-good patched tree (generated from the tree that flashed the QA cortexes
on 2026-07-16). `verify` compares against it; `make-manifest` regenerates it after
an intentional change (commit the result via PR).

Note the manifest hashes the **patched** tree — a fresh extract gets the
`num_sectors` patch applied in-place first, then matches.

**Delisting insurance**: the rebuild's fallback source is the private
`Extend-Robotics/er_jetson_archive` release `r35.4.1`, holding byte-identical
copies of both tarballs plus `SHA256SUMS`. NVIDIA has already hidden JetPack 5.1.2
from SDK Manager's default catalog; if the public tarballs go too, the tool keeps
working for anyone with access to that repo.

## Why this tool exists

On 2026-07-16 an SDK Manager GUI *Uninstall* deleted the entire patched flash
tree mid-QA-cycle (the GUI offers this next to Install; do not use it on the
deployment machine). Recovery took a rebuilt catalog trick, a re-extract, a
re-patch and a hand-held flash. This tool makes that whole loop one command.

## Requirements

- deployment machine: `python3` (3.8+), `sshpass`, `curl`; for the rebuild path
  also `tar`, `sudo`, and `gh` (or `$GH_TOKEN`) only if the NVIDIA download
  fails. Ubuntu 20.04 or 22.04: the tree build was exercised on 22.04.5;
  NVIDIA's host-OS list for flashing JetPack 5.1.2 stops at 20.04, so a 22.04
  flash is outside their support matrix
- Jetson connected over the flashing USB-C port, in forced recovery for the
  flash itself (the preflight prints how)

## Env overrides

- `ER_BUILD_TOOLS_BRANCH` — branch the preflight script and canonical manifest
  are fetched from when not running from a repo checkout (set automatically by
  the `er_jetson_flash` helper function)
- `--l4t`, `--manifest` — non-default tree / manifest locations
- `--storage` — rootfs location: `nvme` (default, = `nvme0n1p1`) for an NVMe SSD, or
  `internal`/`emmc` for the on-board eMMC on boards with no NVMe fitted; also accepts a
  raw device such as `sda1`. `internal` omits `--storage` from the underlying flash.
