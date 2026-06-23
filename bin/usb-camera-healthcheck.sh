#!/bin/bash
# usb-camera-healthcheck.sh — report negotiated USB link speed + a verdict for
# every UVC camera on this host, using sysfs only (no lsusb/dmesg, no root).
#
# Verdict per camera:
#   OK            linked at USB3 (>=5000 Mbps)
#   OK (reduced)  linked at USB2 (480 Mbps) and the model is known to tolerate it
#   WILL FAIL     linked below the model's required link (driver aborts): a model
#                 that REQUIRES USB3 linked below USB3, or a known USB2-tolerant
#                 model linked below USB2 (e.g. 12 Mbps)
#   WARN          linked below USB3 with requirement unknown
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
watch_interval=5
verbose=false
quiet=false

usage="Usage: usb-camera-healthcheck.sh [-w|--watch] [-i|--interval N] [-v|--verbose] [-q|--quiet] [-s|--strict] [-h|--help]"

read_attr() {
  local path="$1"
  [ -e "$path" ] || { printf ''; return 0; }
  cat "$path" || { echo "ERROR: failed to read $path" >&2; exit 3; }
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
  local dev_dir="$1" dev_name interface_path interface_class
  dev_name="$(basename "$dev_dir")"
  for interface_path in "${dev_dir}/${dev_name}":*; do
    [ -e "$interface_path/bInterfaceClass" ] || continue
    interface_class="$(read_attr "$interface_path/bInterfaceClass")"
    [ "$interface_class" = "0e" ] && return 0
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
    USB2_TOLERANT) if [ "$speed" -ge 480 ]; then printf 'OK_REDUCED'; else printf 'WILL_FAIL'; fi ;;
    *)             printf 'WARN' ;;
  esac
}

# Each CAMERA_ROWS entry is field_sep-joined: name|vidpid|speed|requirement|model|product|parent
# Verdict is derived (never stored) via verdict_for, so it cannot drift from speed/requirement.
# Every site that unpacks a row with `IFS="$field_sep" read` must use this exact field order
# (row_verdict and render_report).
scan_cameras() {
  CAMERA_ROWS=()
  local dev_dir name vid pid vidpid speed product parent entry model requirement
  for dev_dir in "$SYSFS_USB_ROOT"/*; do
    # Filter by the actual predicate (is this a UVC camera?), not by the presence
    # of an attribute we later read. Gating on speed/idVendor presence would
    # silently drop a camera missing that attribute; instead a confirmed camera
    # with an unreadable speed or id reaches the fail-fast checks below.
    is_camera "$dev_dir" || continue
    name="$(basename "$dev_dir")"
    vid="$(read_attr "$dev_dir/idVendor")"
    pid="$(read_attr "$dev_dir/idProduct")"
    if [ -z "$vid" ] || [ -z "$pid" ]; then
      echo "ERROR: unreadable USB vendor/product id for $name (got '${vid}:${pid}')" >&2
      exit 3
    fi
    vidpid="${vid}:${pid}"
    speed="$(read_attr "$dev_dir/speed")"
    case "$speed" in
      ''|*[!0-9]*)
        echo "ERROR: unreadable or non-numeric USB speed '$speed' for $name" >&2
        exit 3
        ;;
    esac
    product="$(read_attr "$dev_dir/product")"
    parent="$(parent_hub "$name")"
    entry="$(lookup_model "$vidpid")"
    if [ -n "$entry" ]; then
      model="${entry%|*}"
      requirement="${entry##*|}"
    else
      model="?"
      requirement=""
    fi
    CAMERA_ROWS+=("${name}${field_sep}${vidpid}${field_sep}${speed}${field_sep}${requirement}${field_sep}${model}${field_sep}${product}${field_sep}${parent}")
  done
}

# Parse one CAMERA_ROWS entry and print its derived verdict. The single
# field_sep read site shared by compute_exit_code and any_problem.
row_verdict() {
  local speed requirement
  IFS="$field_sep" read -r _ _ speed requirement _ _ _ <<< "$1"
  verdict_for "$speed" "$requirement"
}

compute_exit_code() {
  if [ "${#CAMERA_ROWS[@]}" -eq 0 ]; then printf '2'; return 0; fi
  local worst=0 row verdict
  for row in "${CAMERA_ROWS[@]}"; do
    verdict="$(row_verdict "$row")"
    # WARN only arises when a camera's requirement is unknown (verdict_for maps
    # every known requirement to OK/OK_REDUCED/WILL_FAIL), so --strict escalates
    # exactly the unknown-camera-below-USB3 case here.
    case "$verdict" in
      OK|OK_REDUCED) ;;
      WILL_FAIL) worst=1 ;;
      WARN) if [ "$fail_on_unknown_usb2" = true ]; then worst=1; fi ;;
      *) echo "ERROR: internal: unexpected verdict '$verdict'" >&2; exit 3 ;;
    esac
  done
  printf '%s' "$worst"
}

any_problem() {
  local row verdict
  for row in "${CAMERA_ROWS[@]}"; do
    verdict="$(row_verdict "$row")"
    case "$verdict" in
      WILL_FAIL|WARN) return 0 ;;
      OK|OK_REDUCED) ;;
      *) echo "ERROR: internal: unexpected verdict '$verdict'" >&2; exit 3 ;;
    esac
  done
  return 1
}

render_report() {
  printf '%bUVC camera USB healthcheck%b\n' "$c_bold" "$c_reset"
  local row name vidpid speed requirement model product parent verdict label color shown
  for row in "${CAMERA_ROWS[@]}"; do
    IFS="$field_sep" read -r name vidpid speed requirement model product parent <<< "$row"
    verdict="$(verdict_for "$speed" "$requirement")"
    case "$verdict" in
      OK)         label='OK (USB3)';               color="$c_green" ;;
      OK_REDUCED) label='OK (USB2, reduced spec)'; color="$c_green" ;;
      WILL_FAIL)  label='WILL FAIL';               color="$c_red" ;;
      WARN)       label='WARN: linked below USB3'; color="$c_yellow" ;;
      *)          echo "ERROR: internal: unexpected verdict '$verdict' for $name" >&2; exit 3 ;;
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
     the cable (a USB2/charge-only or damaged cable drops to a slower link).
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
  local d name vid pid speed device_class product
  for d in "$SYSFS_USB_ROOT"/*; do
    [ -e "$d/speed" ] || continue
    name="$(basename "$d")"
    vid="$(read_attr "$d/idVendor")"
    pid="$(read_attr "$d/idProduct")"
    speed="$(read_attr "$d/speed")"
    device_class="$(read_attr "$d/bDeviceClass")"
    product="$(read_attr "$d/product")"
    printf '  %-10s %-10s %6s Mbps  class=%-3s %s\n' \
      "$name" "${vid}:${pid}" "$speed" "$device_class" "$product"
  done | sort
}

render_output() {
  if [ "${#CAMERA_ROWS[@]}" -eq 0 ]; then
    echo "No USB cameras (UVC devices) found."
  else
    render_report
    any_problem && print_remediation
  fi
  if [ "$verbose" = true ]; then
    render_tree
  fi
}

watch_loop() {
  while true; do
    printf '\033[H\033[2J'
    scan_cameras
    render_output
    printf '\n(refresh %ss — Ctrl-C to exit)\n' "$watch_interval"
    sleep "$watch_interval"
  done
}

run_once() {
  scan_cameras
  local code
  code="$(compute_exit_code)"
  if [ "$quiet" != true ]; then
    render_output
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
