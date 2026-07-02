#!/usr/bin/env bash
# jetson-flash-preflight.sh — pre-flash checks for the AGX Orin 64GB eMMC patch (PCN210100)
# SKIP_CHECK — straight-line script that probes hardware (lsusb/ssh/sudo); check_bash.yml must never source-execute it.
#
# Order of operations:
#   1. Detect the connected Jetson (forced recovery / flash initrd / booted / absent)
#      and determine whether its module is pre- or post-PCN.
#   2. Check the local JetPack 5.1.2 flash tree — severity depends on what step 1 found.
#   3. Verdict: GO / DO NOT FLASH.  Exit: 0 GO, 1 DO NOT FLASH, 2 undetermined.
#
# Background: docs/jetson-flash-preflight.md (in this repo); on the flashing
#             host also Linux_for_Tegra/README-LOCAL-PATCH.md
# Env overrides: L4T, COMPANION, JETSON_SSH (user@host), JETSON_PASS, NO_COLOR,
#                ER_BUILD_TOOLS_BRANCH (branch the companion is fetched from when absent)
#
# Detection method per state:
#   - flash initrd : run companion check over the USB link, root/root (definitive)
#   - booted L4T   : run companion check over ssh — key, env vars, or interactive prompt
#   - forced recov.: no OS running, query module EEPROM via RCM (FAB/SKU — NVIDIA's own gate)

set -u

L4T="${L4T:-$HOME/nvidia/nvidia_sdk/JetPack_5.1.2_Linux_JETSON_AGX_ORIN_TARGETS/Linux_for_Tegra}"
COMPANION="${COMPANION:-$HOME/check-emmc-pcn.sh}"
XML="$L4T/bootloader/t186ref/cfg/flash_t234_qspi_sdmmc.xml"
STOCK_VAL='num_sectors="124321792"'
PATCH_VAL='num_sectors="124190720"'
POST_PCN_SECTORS=124190720
PRE_PCN_SECTORS=124321792
INITRD_ADDR="fc00:1:1::2"
DEF_USER="extend"
DEF_HOST="192.168.55.1"
# ServerAlive*: ConnectTimeout only bounds the TCP connect; these bound an
# established-but-dead session so `preflight && flash` automation can't hang.
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5
          -o ServerAliveInterval=5 -o ServerAliveCountMax=3)
REAPPLY_CMD="cp -n '$XML' '$XML.orig' && sed -i 's/$STOCK_VAL/$PATCH_VAL/' '$XML'"
RAW_URL_BASE="https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/${ER_BUILD_TOOLS_BRANCH:-main}"
COMPANION_TMP=""
trap 'rm -f "$COMPANION_TMP"' EXIT

# Colors: only when stdout is a terminal, and honouring NO_COLOR
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_GRN=$'\033[32m'; C_YLW=$'\033[33m'; C_RED=$'\033[31m'; C_BLD=$'\033[1m'; C_OFF=$'\033[0m'
    C_ORG=$'\033[1;38;5;208m'   # bold orange (256-color) for the not-in-recovery banner
else
    C_GRN=""; C_YLW=""; C_RED=""; C_BLD=""; C_OFF=""; C_ORG=""
fi
ok()   { echo "  ${C_GRN}[OK]${C_OFF}   $*"; }
warn() { echo "  ${C_YLW}[WARN]${C_OFF} $*"; }
bad()  { echo "  ${C_RED}[FAIL]${C_OFF} $*"; }
hdr()  { echo "${C_BLD}$*${C_OFF}"; }

# Single predicate for "the companion can actually be piped to a remote shell":
# -s matters because an empty file (e.g. a failed wget -qO) runs as rc 0, which
# the verdict map would read as pre-PCN.
companion_ok() { [ -f "$COMPANION" ] && [ -r "$COMPANION" ] && [ -s "$COMPANION" ]; }

require_sshpass() {
    command -v sshpass >/dev/null && return 0
    warn "sshpass is not installed (sudo apt-get install sshpass) — cannot use password ssh auth"
    return 1
}

# flash.sh can only start from forced recovery (RCM); any other detected state
# means "not flashable yet" — make that impossible to miss. Purely visual: the
# exit code still reports the module × tree verdict.
warn_not_in_recovery() {        # $1 = detected NVIDIA pid ("" = none)
    [ "$1" = "7023" ] && return 0
    local state="Jetson is NOT in FORCED RECOVERY mode"
    [ -z "$1" ] && state="NO Jetson detected — flashing needs one in FORCED RECOVERY mode"
    echo
    echo "  ${C_ORG}██████████████████████████████████████████████████████████████████${C_OFF}"
    echo "  ${C_ORG}██  $state${C_OFF}"
    echo "  ${C_ORG}██  flash.sh can only start from forced recovery (RCM). To get there:${C_OFF}"
    echo "  ${C_ORG}██    - from booted L4T:    sudo reboot forced-recovery${C_OFF}"
    echo "  ${C_ORG}██    - by hand (AGX Orin): hold FORCE RECOVERY (middle button),${C_OFF}"
    echo "  ${C_ORG}██      press+release RESET, then release FORCE RECOVERY${C_OFF}"
    echo "  ${C_ORG}██████████████████████████████████████████████████████████████████${C_OFF}"
    echo
}

read_sectors_verdict() {        # $1 = sector count
    case "$1" in
        "$POST_PCN_SECTORS") ok "eMMC reports $1 sectors -> POST-PCN module (patch REQUIRED on JP5.1.2)"; module_state="post-pcn";;
        "$PRE_PCN_SECTORS")  ok "eMMC reports $1 sectors -> PRE-PCN module (patch not needed; harmless if applied)"; module_state="pre-pcn";;
        *)                   warn "unexpected eMMC sector count '$1' — new eMMC variant or not an AGX Orin 64GB? Investigate."; module_state="undetermined";;
    esac
}

# Pipe the companion (check-emmc-pcn.sh) into a remote shell; map its exit code
# (0=pre-PCN, 1=post-PCN, 2=unknown) onto module_state. Returns 1 on transport failure.
companion_via() {               # $@ = ssh command opening a shell on the target
    local out rc
    # A missing/empty companion must be a transport failure, not a verdict: the
    # failed stdin redirect would otherwise exit 1 (read as post-PCN), and an
    # empty file runs as rc 0 (read as pre-PCN).
    companion_ok || return 1
    out=$("$@" 'bash -s' < "$COMPANION" 2>/dev/null); rc=$?
    # shellcheck disable=SC2001  # multiline indent; the ${var//} form is unreadable here
    [ -n "$out" ] && echo "$out" | sed 's/^/         /'
    # Trust rc as a verdict only when the companion's own VERDICT line corroborates
    # it — a remote crash, wrapper exit, or truncated stream must not fabricate a
    # module classification.
    case "$rc" in
        0) echo "$out" | grep -q "VERDICT: PRE-PCN"  || return 1; module_state="pre-pcn"  ;;
        1) echo "$out" | grep -q "VERDICT: POST-PCN" || return 1; module_state="post-pcn" ;;
        2) echo "$out" | grep -q "VERDICT: UNKNOWN"  || return 1; module_state="undetermined" ;;
        *) return 1 ;;          # ssh/shell failure, not a verdict
    esac
    return 0
}

# The companion lives in this repo; a fresh host won't have it at $COMPANION.
ensure_companion() {
    companion_ok && return 0
    COMPANION_TMP=$(mktemp) || { warn "mktemp failed — cannot fetch companion"; return 1; }
    if curl -fsSL --max-time 30 "$RAW_URL_BASE/bin/check-emmc-pcn.sh" -o "$COMPANION_TMP"; then
        COMPANION="$COMPANION_TMP"
        return 0
    fi
    warn "companion not at \$COMPANION and fetching $RAW_URL_BASE/bin/check-emmc-pcn.sh failed"
    rm -f "$COMPANION_TMP"; COMPANION_TMP=""
    return 1
}

# ---------- 1. connected jetson ----------
hdr "== 1. Connected Jetson (USB) =="
usbline=$(lsusb | grep -m1 "ID 0955:" || true)
pid=""
[ -n "$usbline" ] && pid=$(echo "$usbline" | sed -E 's/.*ID 0955:([0-9a-fA-F]{4}).*/\1/')
module_state="undetermined"     # post-pcn | pre-pcn | undetermined | not-applicable

case "$pid" in
    7035)
        ok "state: FLASH INITRD ($usbline)"
        if require_sshpass; then
            ensure_companion
            if ! companion_via sshpass -p root ssh "${SSH_OPTS[@]}" "root@$INITRD_ADDR"; then
                # companion missing or initrd lacks bash — fall back to reading the sector count directly
                sectors=$(sshpass -p root ssh "${SSH_OPTS[@]}" "root@$INITRD_ADDR" 'cat /sys/block/mmcblk0/size' 2>/dev/null)
                fallback_rc=$?
                if [ -n "$sectors" ]; then
                    read_sectors_verdict "$sectors"
                elif [ "$fallback_rc" -eq 255 ]; then
                    warn "could not reach initrd at $INITRD_ADDR — link down?"
                else
                    warn "initrd reachable but eMMC size not readable (rc $fallback_rc) — investigate before flashing"
                fi
            fi
        fi
        ;;
    7020)
        ok "state: BOOTED L4T, USB device mode ($usbline)"
        target="${JETSON_SSH:-$DEF_USER@$DEF_HOST}"
        booted_ok=0
        if ensure_companion; then
            echo "         running companion check via ssh $target ..."
            if [ -n "${JETSON_PASS:-}" ]; then
                require_sshpass && SSHPASS="$JETSON_PASS" companion_via sshpass -e ssh "${SSH_OPTS[@]}" "$target" && booted_ok=1
            else
                companion_via ssh -o BatchMode=yes "${SSH_OPTS[@]}" "$target" && booted_ok=1
            fi
        else
            warn "companion unavailable — cannot classify over ssh (fix the fetch or place check-emmc-pcn.sh at \$COMPANION)"
        fi
        # Interactive fallback: prompt for credentials when running in a terminal,
        # and only when there is a companion to run (prompting is pointless otherwise;
        # skipped when non-interactive, so automation never hangs here).
        if [ "$booted_ok" != 1 ] && [ -t 0 ] && companion_ok && require_sshpass; then
            echo "         key/env auth failed — enter Jetson ssh credentials."
            echo "         (press Enter to accept the [default: ...] value; blank password gives up)"
            for _try in 1 2 3; do
                read -r -p "         ssh user     [default: $DEF_USER]: " ju || break
                ju="${ju:-$DEF_USER}"
                read -r -p "         ssh host     [default: $DEF_HOST]: " jh || break
                jh="${jh:-$DEF_HOST}"
                read -r -s -p "         ssh password (no default, blank = give up): " jp; echo
                [ -z "$jp" ] && break
                if SSHPASS="$jp" companion_via sshpass -e ssh "${SSH_OPTS[@]}" "$ju@$jh"; then
                    booted_ok=1; break
                fi
                warn "attempt $_try failed (wrong credentials or unreachable)"
            done
        fi
        if [ "$booted_ok" != 1 ]; then
            warn "could not check the booted Jetson over ssh. Options:"
            warn "  re-run with JETSON_PASS='<password>' [JETSON_SSH=<user@host>]     # non-interactive"
            warn "  ...or run check-emmc-pcn.sh on the Jetson yourself (curl one-liner in docs/jetson-flash-preflight.md), or use forced recovery"
        fi
        ;;
    7023)
        ok "state: FORCED RECOVERY, AGX Orin ($usbline)"
        echo "         querying module EEPROM via RCM (needs sudo, ~15s)..."
        # shellcheck disable=SC2015  # || true intentionally swallows any failure; parsed below
        rcm_out=$(cd "$L4T" && timeout 90 sudo ./nvautoflash.sh --print_boardid 2>&1 || true)
        boardline=$(echo "$rcm_out" | grep -oE 'Board ID\([0-9]+\) version\([0-9]+\) sku\([0-9]+\) revision\([^)]*\)' | head -1)
        if [ -n "$boardline" ]; then
            boardid=$(echo "$boardline" | sed -E 's/Board ID\(([0-9]+)\).*/\1/')
            fab=$(echo "$boardline"     | sed -E 's/.*version\(([0-9]+)\).*/\1/')
            sku=$(echo "$boardline"     | sed -E 's/.*sku\(([0-9]+)\).*/\1/')
            ok "EEPROM: $boardline"
            # SKU compared numerically (base-10 forced) so 0004 and 4 both match — zero-padding varies between tools/logs.
            if [ "$boardid" = "3701" ] && { [ "$((10#$sku))" -eq 4 ] || [ "$((10#$sku))" -eq 5 ]; } && [ "$fab" -ge 501 ]; then
                ok "BOARDID 3701, FAB $fab >= 501, SKU $sku -> POST-PCN module (patch REQUIRED on JP5.1.2)"
                module_state="post-pcn"
            elif [ "$boardid" = "3701" ]; then
                ok "AGX Orin, FAB $fab / SKU $sku outside PCN scope -> PRE-PCN (patch not needed; harmless if applied)"
                module_state="pre-pcn"
            else
                warn "BOARDID $boardid is not an AGX Orin module — this patch does not apply"
                module_state="not-applicable"
            fi
        else
            warn "could not parse EEPROM info; last lines of RCM query:"
            echo "$rcm_out" | tail -5 | sed 's/^/         /'
        fi
        ;;
    "")
        warn "no NVIDIA USB device found — is the Jetson connected via the flashing USB-C port?"
        ;;
    *)
        warn "NVIDIA device 0955:$pid — not an AGX Orin in a state this script recognizes ($usbline)"
        module_state="not-applicable"
        ;;
esac

# ---------- 2. host flash tree (severity depends on the module found above) ----------
hdr "== 2. Host flash tree =="
tree_state="unknown"
if [ ! -f "$XML" ]; then
    bad "flash config not found: $XML (tree moved or re-extracted?)"
elif [ ! -r "$XML" ]; then
    bad "flash config not readable: $XML (root-owned tree? fix permissions and re-run)"
elif grep -qF "$PATCH_VAL" "$XML" && grep -qF "$STOCK_VAL" "$XML"; then
    bad "BOTH stock and patched num_sectors present in $XML — half-edited config, investigate before flashing"
elif grep -qF "$PATCH_VAL" "$XML"; then
    ok "num_sectors patch is applied"
    tree_state="patched"
elif grep -qF "$STOCK_VAL" "$XML"; then
    tree_state="stock"
    case "$module_state" in
        post-pcn)
            bad "STOCK tree — the connected module REQUIRES the patch before flashing:"
            echo "         $REAPPLY_CMD"
            ;;
        pre-pcn)
            ok "stock tree — fine for this pre-PCN module (patched layout would also work)"
            ;;
        *)
            warn "STOCK tree — patch not applied. Required if the module turns out to be post-PCN:"
            echo "         $REAPPLY_CMD"
            ;;
    esac
else
    bad "neither stock nor patched value found in $XML — investigate before flashing"
fi

# ---------- 3. verdict ----------
warn_not_in_recovery "$pid"
hdr "== 3. Verdict =="
case "$module_state:$tree_state" in
    post-pcn:patched)  ok "GO — module needs the patch and the tree has it. Flash away."; exit 0;;
    post-pcn:*)        bad "DO NOT FLASH — this module needs the patch and the tree does NOT have it (see section 2)."; exit 1;;
    pre-pcn:patched)   ok "GO — pre-PCN module; patched tree lays its eMMC out 64 MiB short (harmless, rootfs is on NVMe)."; exit 0;;
    pre-pcn:stock)     ok "GO — pre-PCN module, stock layout fits."; exit 0;;
    pre-pcn:*)         warn "pre-PCN module, but the flash tree is in an unexpected state — fix section 2 first."; exit 2;;
    not-applicable:*)  warn "patch not applicable to the connected device — this script can't vouch for the flash."; exit 2;;
    *)                 warn "could not determine module type — resolve section 1 before flashing."; exit 2;;
esac
