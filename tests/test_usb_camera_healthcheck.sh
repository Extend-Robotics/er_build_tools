#!/bin/bash
# SKIP_CHECK
# Self-contained tests for bin/usb-camera-healthcheck.sh. No bats dependency.
# Builds fake sysfs trees under a temp dir (SYSFS_USB_ROOT) — no real hardware.

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${here}/../bin/usb-camera-healthcheck.sh"

fail_count=0
pass_count=0

assert_eq() { # name expected actual
  if [ "$2" = "$3" ]; then
    pass_count=$((pass_count + 1))
  else
    fail_count=$((fail_count + 1))
    printf 'FAIL: %s\n  expected: [%s]\n  actual:   [%s]\n' "$1" "$2" "$3" >&2
  fi
}

assert_contains() { # name haystack needle
  case "$2" in
    *"$3"*) pass_count=$((pass_count + 1)) ;;
    *) fail_count=$((fail_count + 1))
       printf 'FAIL: %s\n  expected to contain: [%s]\n  in: [%s]\n' "$1" "$3" "$2" >&2 ;;
  esac
}

# make_device ROOT NAME VID PID SPEED CAMERA(yes|no)
make_device() {
  local root="$1" name="$2" vid="$3" pid="$4" speed="$5" cam="$6"
  local d="${root}/${name}"
  mkdir -p "${d}/${name}:1.0"
  echo "$vid"   > "${d}/idVendor"
  echo "$pid"   > "${d}/idProduct"
  echo "$speed" > "${d}/speed"
  echo "Fake ${vid}:${pid}" > "${d}/product"
  echo "ef"     > "${d}/bDeviceClass"
  if [ "$cam" = yes ]; then
    echo "0e" > "${d}/${name}:1.0/bInterfaceClass"
  else
    echo "09" > "${d}/${name}:1.0/bInterfaceClass"
  fi
}

new_root() { mktemp -d; }

# --- Task 1 tests: argument parsing ---

help_out="$(bash "$SCRIPT" --help)"; help_code=$?
assert_eq "help exits 0" 0 "$help_code"
assert_contains "help prints usage" "$help_out" "Usage:"

bash "$SCRIPT" --bogus 2>/dev/null; assert_eq "unknown flag exits 1" 1 "$?"
bash "$SCRIPT" -h >/dev/null;       assert_eq "short help exits 0" 0 "$?"

bash "$SCRIPT" --quiet --watch 2>/dev/null
assert_eq "quiet+watch exits 1" 1 "$?"
bash "$SCRIPT" --quiet --verbose 2>/dev/null
assert_eq "quiet+verbose exits 1" 1 "$?"

bash "$SCRIPT" --interval 0 2>/dev/null;   assert_eq "interval 0 exits 1" 1 "$?"
bash "$SCRIPT" --interval abc 2>/dev/null; assert_eq "interval abc exits 1" 1 "$?"

printf '\n%d passed, %d failed\n' "$pass_count" "$fail_count"
[ "$fail_count" -eq 0 ]
