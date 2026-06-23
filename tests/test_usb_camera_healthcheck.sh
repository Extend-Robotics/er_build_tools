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

# --- Task 2 tests: read_attr / parent_hub ---

t2_root="$(new_root)"
echo "5000" > "${t2_root}/speed_file"
assert_eq "read_attr present" "5000" "$( source "$SCRIPT"; read_attr "${t2_root}/speed_file" )"
assert_eq "read_attr absent"  ""     "$( source "$SCRIPT"; read_attr "${t2_root}/nope" )"

assert_eq "parent_hub nested" "2-3"  "$( source "$SCRIPT"; parent_hub "2-3.4" )"
assert_eq "parent_hub deep"   "2-3.4" "$( source "$SCRIPT"; parent_hub "2-3.4.1" )"
assert_eq "parent_hub root"   ""      "$( source "$SCRIPT"; parent_hub "usb2" )"

# --- Task 3 tests: is_camera ---

t3_root="$(new_root)"
make_device "$t3_root" "2-3.4" "2bc5" "066b" "480"  yes   # camera (0e)
make_device "$t3_root" "1-4"   "0bda" "5420" "480"  no    # hub (09)

( source "$SCRIPT"; is_camera "${t3_root}/2-3.4" ); assert_eq "camera detected" 0 "$?"
( source "$SCRIPT"; is_camera "${t3_root}/1-4" );   assert_eq "hub not a camera" 1 "$?"
( source "$SCRIPT"; is_camera "${t3_root}/2-9.9" ); assert_eq "absent not a camera" 1 "$?"

# --- Task 4 tests: lookup_model ---

assert_eq "femto known"  "Femto Bolt|REQUIRES_USB3" "$( source "$SCRIPT"; lookup_model 2bc5:066b )"
assert_eq "gemini known" "Gemini 336|USB2_TOLERANT" "$( source "$SCRIPT"; lookup_model 2bc5:0803 )"
assert_eq "unknown id"   ""                          "$( source "$SCRIPT"; lookup_model 1234:5678 )"

# --- Task 5 tests: verdict_for (the full matrix from the spec) ---

assert_eq "usb3 requires"      "OK"         "$( source "$SCRIPT"; verdict_for 5000 REQUIRES_USB3 )"
assert_eq "usb3 tolerant"      "OK"         "$( source "$SCRIPT"; verdict_for 10000 USB2_TOLERANT )"
assert_eq "usb3 unknown"       "OK"         "$( source "$SCRIPT"; verdict_for 5000 '' )"
assert_eq "usb2 requires"      "WILL_FAIL"  "$( source "$SCRIPT"; verdict_for 480 REQUIRES_USB3 )"
assert_eq "usb2 tolerant"      "OK_REDUCED" "$( source "$SCRIPT"; verdict_for 480 USB2_TOLERANT )"
assert_eq "usb2 unknown"       "WARN"       "$( source "$SCRIPT"; verdict_for 480 '' )"
assert_eq "usb1 requires"      "WILL_FAIL"  "$( source "$SCRIPT"; verdict_for 12 REQUIRES_USB3 )"
assert_eq "usb1 tolerant"      "WARN"       "$( source "$SCRIPT"; verdict_for 12 USB2_TOLERANT )"
assert_eq "usb1 unknown"       "WARN"       "$( source "$SCRIPT"; verdict_for 12 '' )"

printf '\n%d passed, %d failed\n' "$pass_count" "$fail_count"
[ "$fail_count" -eq 0 ]
