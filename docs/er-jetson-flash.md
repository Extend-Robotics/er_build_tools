# er_jetson_flash — one-command AGX Orin QA-cortex flash (JetPack 5.1.2)

`er_jetson_flash` takes a Jetson AGX Orin devkit in forced recovery and a
deployment machine in *any* state, and produces a flashed, provisioned,
sanity-checked QA cortex:

```
er_jetson_flash                      # everything, with extend/extend defaults
er_jetson_flash --username qa --password s3cret
er_jetson_flash verify               # read-only health check of the flash tree
er_jetson_flash restore              # fix/restore the flash tree, don't flash
er_jetson_flash make-backup          # snapshot the current healthy tree
```

## What the full pipeline does

1. **Verify the flash tree** (`~/nvidia/nvidia_sdk/JetPack_5.1.2_.../Linux_for_Tegra`)
   - the PCN210100 `num_sectors` eMMC patch is applied (applied in-place when the
     tree is healthy-but-stock; see [jetson-flash-preflight.md](jetson-flash-preflight.md)
     for why unpatched flashes of post-PCN modules fail)
   - every config/script hashed in the newest backup's **manifest** matches —
     this catches silently re-extracted, half-deleted, or hand-edited trees
2. **Restore when broken**, in order of preference:
   - untar the newest `~/backups/JetPack_5.1.2_flash_tree_*.tar.zst` (fast, offline,
     no NVIDIA account). The bad tree is moved aside, never deleted.
   - else a **guided sdkmanager reinstall**: the tool prints the exact answers to
     give, then runs `sdkmanager --cli --action install ... --show-all-versions
     --archived-versions`. Both catalog flags are required — NVIDIA's server-side
     catalog hides JetPack 5.1.2 without them. You supply the interactive login.
3. **Preflight gate**: runs [`er_jetson_flash_preflight`](jetson-flash-preflight.md);
   anything but GO stops the pipeline.
4. **Flash**: `sudo ./nvsdkmanager_flash.sh --storage nvme0n1p1 --nv-auto-config
   --username <user>` — rootfs on NVMe, first-boot user preseeded (no monitor needed).
   The password is fed to the preseeder over stdin, never argv.
5. **Post-flash setup**:
   - waits for the board to boot (USB `0955:7020`) and for ssh
   - fixes the fresh-flash clock skew (apt rejects "future" Release files otherwise)
   - checks whether the board has internet; when it is USB-only, the tool starts an
     embedded HTTP proxy on the host, reverse-tunnels it over ssh, and points the
     board's apt at it for the duration (config removed afterwards)
   - `apt install nvidia-jetpack` — the r35.4 repo pins the exact 5.1.2-b104
     component set (CUDA 11.4, cuDNN, TensorRT, OpenCV, VPI, container runtime),
     identical to what SDK Manager's GUI would install
   - sanity checks: L4T release is R35.4.1, `nvidia-jetpack` is 5.1.2-b104, and an
     eMMC `dmesg` baseline (post-PCN DG4064 modules have a known CQE quirk on
     stock JP 5.1.2 — a clean baseline now makes later errors attributable)

## Backups

`er_jetson_flash make-backup` writes two things to `~/backups/`:

- `JetPack_5.1.2_flash_tree_patched_<date>.tar.zst` — the whole patched JetPack
  directory (~15 GB compressed; needs `zstd`)
- `...tar.zst.manifest.json` — sha256 of every config/flash-script in the tree,
  used by `verify` to detect drift cheaply (hashing all 38 GB every run would not
  be worth the signal)

`make-backup --manifest-only` regenerates the manifest for the newest existing
tarball, e.g. after an intentional tree change you consider good.

Keep a copy of the tarball off the deployment machine. It is the only restore
path that does not depend on NVIDIA keeping 5.1.2 in their archived catalog.

## Why this tool exists

On 2026-07-16 an SDK Manager GUI *Uninstall* deleted the entire patched flash
tree mid-QA-cycle (the GUI offers this next to Install; do not use it on the
deployment machine). Recovery took a rebuilt catalog trick, a re-extract, a
re-patch and a hand-held flash. This tool makes that whole loop one command,
and the backup tarball makes the worst case boring.

## Requirements

- deployment machine: `python3` (3.8+), `sshpass`, `zstd`, `curl`; `sdkmanager`
  only for the no-backup restore path
- Jetson connected over the flashing USB-C port, in forced recovery for the
  flash itself (the preflight prints how)

## Env overrides

- `ER_BUILD_TOOLS_BRANCH` — branch the preflight companion scripts are fetched
  from when not running from a repo checkout (set automatically by the
  `er_jetson_flash` helper function)
- `--l4t`, `--backup-dir` — non-default tree / backup locations
