#!/bin/bash
# SKIP_CHECK
# usb-camera-healthcheck.sh — report negotiated USB link speed + a verdict for
# every UVC camera on this host, using sysfs only (no lsusb/dmesg, no root).
#
# Verdict per camera:
#   OK            linked at USB3 (>=5000 Mbps)
#   OK (reduced)  linked at USB2 but the model is known to tolerate it
#   WILL FAIL     linked below USB3 and the model REQUIRES USB3 (driver aborts)
#   WARN          linked below USB3, requirement unknown
#
# Needs no root. Run on a bare Jetson with:
#   bash <(curl -Ls https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/usb-camera-healthcheck.sh)
# Or watch live while swapping cables:
#   bash <(curl -Ls .../usb-camera-healthcheck.sh) --watch

set -euo pipefail

: "${SYSFS_USB_ROOT:=/sys/bus/usb/devices}"

# Non-whitespace TSV delimiter (ASCII Unit Separator). Chosen so that empty
# fields (e.g. a root-port camera's empty parent) are not collapsed by `read`,
# which drops empty whitespace-delimited fields. It cannot appear in any sysfs
# string (device name, vid:pid, speed, model/product name).
field_sep=$'\x1f'

fail_on_unknown_usb2=false
watch_mode=false
watch_interval=1
verbose=false
quiet=false

usage="Usage: usb-camera-healthcheck.sh [-w|--watch] [-i|--interval N] [-v|--verbose] [-q|--quiet] [-s|--strict] [-h|--help]"

read_attr() {
  local path="$1"
  [ -e "$path" ] || { printf '' ; return 0; }
  cat "$path" 2>/dev/null || printf ''
}

setup_colors() {
  if [ -t 1 ]; then
    c_red=$'\033[31m'; c_yellow=$'\033[33m'; c_green=$'\033[32m'
    c_bold=$'\033[1m'; c_reset=$'\033[0m'
  else
    c_red=''; c_yellow=''; c_green=''; c_bold=''; c_reset=''
  fi
}

parent_hub() {
  local name="$1"
  case "$name" in
    *.*) printf '%s' "${name%.*}" ;;
    *)   printf '' ;;
  esac
}

is_camera() {
  local dev_dir="$1" dev_name intf
  dev_name="$(basename "$dev_dir")"
  for intf in "${dev_dir}/${dev_name}":*; do
    [ -e "$intf/bInterfaceClass" ] || continue
    [ "$(cat "$intf/bInterfaceClass" 2>/dev/null)" = "0e" ] && return 0
  done
  return 1
}

lookup_model() {
  case "$1" in
    2bc5:066b) printf 'Femto Bolt|REQUIRES_USB3' ;;
    2bc5:0803) printf 'Gemini 336|USB2_TOLERANT' ;;
    *)         printf '' ;;
  esac
}

verdict_for() {
  local speed="$1" requirement="$2"
  if [ "$speed" -ge 5000 ]; then
    printf 'OK'
    return 0
  fi
  case "$requirement" in
    REQUIRES_USB3) printf 'WILL_FAIL' ;;
    USB2_TOLERANT) if [ "$speed" -eq 480 ]; then printf 'OK_REDUCED'; else printf 'WARN'; fi ;;
    *)             printf 'WARN' ;;
  esac
}

# Each CAMERA_ROWS entry is field_sep-joined: name|vidpid|speed|requirement|model|product|parent|verdict
# (kept in sync with the IFS="$field_sep" read sites in compute_exit_code, any_problem, render_report)
scan_cameras() {
  CAMERA_ROWS=()
  local dev_dir name vid pid vidpid speed product parent entry model requirement verdict
  for dev_dir in "$SYSFS_USB_ROOT"/*; do
    [ -e "$dev_dir/speed" ] || continue
    is_camera "$dev_dir" || continue
    name="$(basename "$dev_dir")"
    vid="$(read_attr "$dev_dir/idVendor")"
    pid="$(read_attr "$dev_dir/idProduct")"
    vidpid="${vid}:${pid}"
    speed="$(read_attr "$dev_dir/speed")"
    product="$(read_attr "$dev_dir/product")"
    parent="$(parent_hub "$name")"
    entry="$(lookup_model "$vidpid")"
    if [ -n "$entry" ]; then
      model="${entry%%|*}"
      requirement="${entry##*|}"
    else
      model="?"
      requirement=""
    fi
    if [ -z "$speed" ]; then
      echo "ERROR: unreadable USB speed for $name" >&2
      exit 3
    fi
    verdict="$(verdict_for "$speed" "$requirement")"
    CAMERA_ROWS+=("${name}${field_sep}${vidpid}${field_sep}${speed}${field_sep}${requirement}${field_sep}${model}${field_sep}${product}${field_sep}${parent}${field_sep}${verdict}")
  done
}

compute_exit_code() {
  if [ "${#CAMERA_ROWS[@]}" -eq 0 ]; then printf '2'; return 0; fi
  local worst=0 row requirement verdict
  for row in "${CAMERA_ROWS[@]}"; do
    IFS="$field_sep" read -r _ _ _ requirement _ _ _ verdict <<< "$row"
    case "$verdict" in
      WILL_FAIL) worst=1 ;;
      WARN) if [ -z "$requirement" ] && [ "$fail_on_unknown_usb2" = true ]; then worst=1; fi ;;
    esac
  done
  printf '%s' "$worst"
}

any_problem() {
  local row verdict
  for row in "${CAMERA_ROWS[@]}"; do
    IFS="$field_sep" read -r _ _ _ _ _ _ _ verdict <<< "$row"
    case "$verdict" in WILL_FAIL|WARN) return 0 ;; esac
  done
  return 1
}

render_report() {
  printf '%bUVC camera USB healthcheck%b\n' "$c_bold" "$c_reset"
  local row name vidpid speed requirement model product parent verdict label color shown
  for row in "${CAMERA_ROWS[@]}"; do
    IFS="$field_sep" read -r name vidpid speed requirement model product parent verdict <<< "$row"
    case "$verdict" in
      OK)         label='OK (USB3)';               color="$c_green" ;;
      OK_REDUCED) label='OK (USB2, reduced spec)'; color="$c_green" ;;
      WILL_FAIL)  label='WILL FAIL';               color="$c_red" ;;
      WARN)       label='WARN: linked below USB3'; color="$c_yellow" ;;
      *)          label="UNKNOWN VERDICT: $verdict"; color="$c_yellow" ;;
    esac
    shown="$model"
    [ "$model" = "?" ] && shown="$product"
    [ -n "$shown" ] && [ "$shown" != "?" ] || shown="$vidpid"
    local location="$vidpid"
    [ -n "$parent" ] && location="$vidpid on $parent"
    printf '  %b%-28s%b %5s Mbps  %b%-26s%b  [%s]\n' \
      "$c_bold" "$shown" "$c_reset" "$speed" "$color" "$label" "$c_reset" "$location"
  done
}

print_remediation() {
  cat <<'EOF'

A camera is linked below USB3. To fix:
  1. Try a known-good USB3 cable first. The fault almost always travels with
     the cable (a USB2/charge-only or damaged cable falls back to 480 Mbps).
  2. Combo-hub note: a USB3 hub shows TWO faces on the bus — a "USB 3.0 Hub"
     and a "USB 2.0 Hub" (the same physical hub). A camera sitting under the
     "USB 2.0 Hub" face does NOT mean the hub/port is USB2-only; it means that
     camera linked at USB2. Do not blame the hub.
  3. Isolation test: move the camera onto a known-good port + cable to prove
     whether it is the cable or the camera.
  4. The driver checks USB speed only at launch — restart the ROS stack after
     fixing the cable, or the camera node will not come up.
EOF
}

render_tree() {
  echo
  echo "Full USB device tree:"
  local d
  for d in "$SYSFS_USB_ROOT"/*; do
    [ -e "$d/speed" ] || continue
    printf '  %-10s %-10s %6s Mbps  class=%-3s %s\n' \
      "$(basename "$d")" \
      "$(read_attr "$d/idVendor"):$(read_attr "$d/idProduct")" \
      "$(read_attr "$d/speed")" \
      "$(read_attr "$d/bDeviceClass")" \
      "$(read_attr "$d/product")"
  done | sort
}

watch_loop() {
  while true; do
    printf '\033[H\033[2J'
    scan_cameras
    if [ "${#CAMERA_ROWS[@]}" -eq 0 ]; then
      echo "No USB cameras (UVC devices) found."
    else
      render_report
      any_problem && print_remediation
    fi
    [ "$verbose" = true ] && render_tree
    printf '\n(refresh %ss — Ctrl-C to exit)\n' "$watch_interval"
    sleep "$watch_interval"
  done
}

run_once() {
  scan_cameras
  local code
  code="$(compute_exit_code)"
  if [ "$quiet" != true ]; then
    if [ "${#CAMERA_ROWS[@]}" -eq 0 ]; then
      echo "No USB cameras (UVC devices) found."
    else
      render_report
      any_problem && print_remediation
    fi
    [ "$verbose" = true ] && render_tree
  fi
  exit "$code"
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      -w|--watch)   watch_mode=true; shift ;;
      -i|--interval)
        watch_interval="${2:-}"
        case "$watch_interval" in
          ''|*[!0-9]*) echo "ERROR: --interval needs a positive integer" >&2; exit 1 ;;
        esac
        [ "$watch_interval" -ge 1 ] || { echo "ERROR: --interval must be >= 1" >&2; exit 1; }
        shift 2 ;;
      -v|--verbose) verbose=true; shift ;;
      -q|--quiet)   quiet=true; shift ;;
      -s|--strict)  fail_on_unknown_usb2=true; shift ;;
      -h|--help)    echo "$usage"; exit 0 ;;
      *) echo "ERROR: Unknown option: $1" >&2; exit 1 ;;
    esac
  done
  if [ "$quiet" = true ] && [ "$watch_mode" = true ]; then
    echo "ERROR: --quiet and --watch are mutually exclusive" >&2; exit 1
  fi
  if [ "$quiet" = true ] && [ "$verbose" = true ]; then
    echo "ERROR: --quiet and --verbose are mutually exclusive" >&2; exit 1
  fi
}

main() {
  parse_args "$@"
  setup_colors
  [ -d "$SYSFS_USB_ROOT" ] || { echo "ERROR: USB sysfs path not found: $SYSFS_USB_ROOT" >&2; exit 3; }
  if [ "$watch_mode" = true ]; then
    watch_loop
  fi
  run_once
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi
