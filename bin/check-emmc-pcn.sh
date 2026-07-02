#!/usr/bin/env bash
# check-emmc-pcn.sh — AGX Orin 64GB: pre- or post-PCN210100 eMMC?
# Run ON a booted Jetson (or in the flash initrd). Exit: 0=pre-PCN, 1=post-PCN, 2=unknown.
set -u
sz=$(cat /sys/block/mmcblk0/size 2>/dev/null || echo 0)
name=$(cat /sys/block/mmcblk0/device/name 2>/dev/null || echo "?")
tnspec=$(grep -oE "TNSPEC [0-9]{4}-[0-9]{3}-[0-9]{4}-[A-Za-z0-9.]*" /etc/nv_boot_control.conf 2>/dev/null | head -1 || true)
echo "eMMC model: ${name}   sectors: ${sz}   ${tnspec:-"(TNSPEC n/a - not booted L4T)"}"
case "${sz}" in
  124190720) echo "VERDICT: POST-PCN (new smaller eMMC). Stock JetPack 5.1.2 flasher FAILS on this module - use the patched tree (see README-LOCAL-PATCH.md) or JetPack 5.1.3+."; exit 1;;
  124321792) echo "VERDICT: PRE-PCN (original eMMC). Stock flasher layout fits."; exit 0;;
  0)         echo "VERDICT: UNKNOWN - no eMMC visible. Not a booted Jetson? For an unbootable board: check from the flash initrd, or grep any flash log for \"Board ID\" (FAB>=501 + SKU 0004/0005 => assume post-PCN)."; exit 2;;
  *)         echo "VERDICT: UNKNOWN - unexpected size ${sz}. Not an AGX Orin 64GB, or a new eMMC variant - investigate before flashing."; exit 2;;
esac
