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

parent_hub() {
  local name="$1"
  case "$name" in
    *.*) printf '%s' "${name%.*}" ;;
    *)   printf '' ;;
  esac
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
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi
