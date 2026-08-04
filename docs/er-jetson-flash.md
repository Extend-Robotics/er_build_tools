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
2. **Restore when broken**: the bad tree is moved aside (never deleted), then a
   **guided sdkmanager reinstall** — the tool prints the exact answers to give and
   runs `sdkmanager --cli --action install ... --show-all-versions
   --archived-versions`. Both catalog flags are required — NVIDIA's server-side
   catalog hides JetPack 5.1.2 without them. You supply the interactive login.
   Downloads reuse `~/Downloads/nvidia/sdkm_downloads` when present.
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

**Delisting insurance**: everything here depends on NVIDIA keeping JetPack 5.1.2
reachable in the archived catalog (they already pruned it from the default view).
Cheap mitigation, outside this tool: archive `Jetson_Linux_R35.4.1_aarch64.tbz2` +
`Tegra_Linux_Sample-Root-Filesystem_R35.4.1_aarch64.tbz2` (~2.2 GB, already in
`~/Downloads/nvidia/sdkm_downloads`) somewhere central — a tree can be rebuilt
from those by hand without SDK Manager.

## Why this tool exists

On 2026-07-16 an SDK Manager GUI *Uninstall* deleted the entire patched flash
tree mid-QA-cycle (the GUI offers this next to Install; do not use it on the
deployment machine). Recovery took a rebuilt catalog trick, a re-extract, a
re-patch and a hand-held flash. This tool makes that whole loop one command.

## Requirements

- deployment machine: `python3` (3.8+), `sshpass`, `curl`; `sdkmanager` only for
  the restore path
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
