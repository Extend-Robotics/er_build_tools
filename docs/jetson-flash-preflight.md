# jetson-flash-preflight

GO / DO NOT FLASH check to run on the flashing host **before** flashing an
AGX Orin 64GB with JetPack 5.1.2.

PCN210100 moved the AGX Orin 64GB to a slightly smaller eMMC (124,190,720
sectors vs 124,321,792). The stock JetPack 5.1.2 flash layout no longer fits a
post-PCN module and the flash aborts partway; the fix is a one-line
`num_sectors` patch to
`Linux_for_Tegra/bootloader/t186ref/cfg/flash_t234_qspi_sdmmc.xml`. This
script tells you whether the connected module needs that patch and whether
your flash tree has it — before you find out the hard way.

## Run it (curl-bash, no install)

    bash <(curl -Ls https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/jetson-flash-preflight.sh)

or, with `.helper_bash_functions` installed:

    er_jetson_flash_preflight

The one-liner is for eyeball runs only: if the network fails, `bash <(curl -Ls …)`
executes nothing and exits **0**, which reads as GO. Anything automated must use
`er_jetson_flash_preflight` — it verifies the fetch before running and returns
non-zero when the fetch fails. Helper fetches are also cache-busted, so every
run executes the current branch tip (raw.githubusercontent otherwise serves a
CDN copy for up to ~5 minutes after a push).

## What it checks

1. **Connected Jetson (USB)** — finds the NVIDIA USB device and classifies
   the module pre/post-PCN by state:

   | USB state (0955:PID) | How the module is classified |
   |----------------------|------------------------------|
   | flash initrd (`7035`) | companion check over the USB link (root/root), falling back to a direct eMMC sector read |
   | booted L4T (`7020`) | companion check over ssh — key auth, `JETSON_PASS`/`JETSON_SSH`, or an interactive prompt |
   | forced recovery (`7023`) | module EEPROM via RCM (`nvautoflash.sh --print_boardid`, needs sudo, ~15s): BOARDID 3701 + FAB ≥ 501 + SKU 0004/0005 ⇒ post-PCN |
   | other `0955:` PID | not applicable — not a state this check covers |
   | none | undetermined — connect the flashing USB-C port |

2. **Host flash tree** — whether the `num_sectors` patch is applied to the
   flash XML (severity depends on what step 1 found).

3. **Verdict** — GO / DO NOT FLASH, with the reapply command printed when the
   patch is missing.

## Companion script

[`bin/check-emmc-pcn.sh`](../bin/check-emmc-pcn.sh) runs *on* the Jetson
(piped over ssh) and reports the eMMC verdict — exit 0=pre-PCN, 1=post-PCN,
2=unknown. The preflight looks for it at `$COMPANION` (default
`~/check-emmc-pcn.sh`) and fetches it from this repo automatically when
absent. It also works standalone on any booted Jetson:

    bash <(curl -Ls https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/check-emmc-pcn.sh)

## Env overrides

| Variable | Default | Meaning |
|----------|---------|---------|
| `L4T` | `~/nvidia/nvidia_sdk/JetPack_5.1.2_Linux_JETSON_AGX_ORIN_TARGETS/Linux_for_Tegra` | flash tree to check |
| `COMPANION` | `~/check-emmc-pcn.sh` | local companion path (auto-fetched when absent) |
| `JETSON_SSH` | `extend@192.168.55.1` | ssh target for the booted-L4T check |
| `JETSON_PASS` | *(unset)* | ssh password, enables the non-interactive booted-L4T check |
| `ER_BUILD_TOOLS_BRANCH` | `main` | branch the companion is fetched from (honoured for direct/curl-bash runs; `er_jetson_flash_preflight` deliberately pins it to the helper's own branch for coherence) |
| `NO_COLOR` | *(unset)* | disable colours |

## Verdicts and exit codes

| Module × tree | Verdict | Exit |
|---------------|---------|------|
| post-PCN × patched | GO | 0 |
| post-PCN × anything else | DO NOT FLASH | 1 |
| pre-PCN × patched or stock | GO | 0 |
| pre-PCN × missing/unrecognized XML | resolve first | 2 |
| undetermined / not-applicable | resolve first | 2 |

`er_jetson_flash_preflight` also returns 1 when the script fetch itself fails —
fail-closed for `&&` gating (indistinguishable from DO NOT FLASH only if you
`case` on `$?`).

When the connected Jetson is in any state other than forced recovery (or absent),
a large orange banner above the verdict reminds you that `flash.sh` cannot start
yet and how to enter recovery. It is purely visual — the exit code still reports
the module × tree verdict.

Automation can gate on the exit code (`L4T` exported in your shell, same
default as the script's):

    er_jetson_flash_preflight && cd "$L4T" && sudo ./flash.sh jetson-agx-orin-devkit external
    # swap 'external' for 'internal' to put the rootfs on the eMMC (boards with no NVMe fitted)
