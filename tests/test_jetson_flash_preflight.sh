#!/bin/bash
# SKIP_CHECK — keep this. check_bash.yml `source`s every bash file to syntax-check
# it (skipping those marked SKIP_CHECK); sourcing this would run the suite.
# Self-contained tests for bin/jetson-flash-preflight.sh (+ the helper fetch fn).
# No bats, no hardware, no network: lsusb/sshpass/sudo/curl are PATH shims,
# the flash tree is a fixture, and the companion is a controllable fake.

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-${here}/..}"
SCRIPT="${REPO}/bin/jetson-flash-preflight.sh"
COMPANION_REAL="${REPO}/bin/check-emmc-pcn.sh"
HELPERS="${REPO}/.helper_bash_functions"

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

assert_not_contains() { # name haystack needle
  case "$2" in
    *"$3"*) fail_count=$((fail_count + 1))
            printf 'FAIL: %s\n  expected NOT to contain: [%s]\n' "$1" "$3" >&2 ;;
    *) pass_count=$((pass_count + 1)) ;;
  esac
}

base_tmp="$(mktemp -d)"
trap 'status=$?; rm -rf "$base_tmp"; exit $status' EXIT

# ---------- shims: fake lsusb / sshpass / sudo / curl on PATH ----------
shims="$base_tmp/shims"
mkdir -p "$shims"

cat > "$shims/lsusb" <<'EOF'
#!/bin/bash
# FAKE_LSUSB_LINE="" -> no NVIDIA device
[ -n "${FAKE_LSUSB_LINE:-}" ] && echo "$FAKE_LSUSB_LINE"
exit 0
EOF

cat > "$shims/sshpass" <<'EOF'
#!/bin/bash
# Stands in for `sshpass ... ssh ... target <command>`.
# companion_via appends 'bash -s' and pipes the companion on stdin -> run it.
# The initrd fallback passes 'cat /sys/block/mmcblk0/size' as the last arg ->
# answer with FAKE_REMOTE_SECTORS, or fail like a dead link when unset.
last="${!#}"
if [ "$last" = "bash -s" ]; then exec bash -s; fi
if [ -n "${FAKE_REMOTE_SECTORS:-}" ]; then echo "$FAKE_REMOTE_SECTORS"; exit 0; fi
exit 255
EOF
cp "$shims/sshpass" "$shims/ssh"   # 7020 key-auth path calls plain ssh

cat > "$shims/sudo" <<'EOF'
#!/bin/bash
[ "$1" = "-v" ] && exit 0   # credential check/caching — always succeeds in tests
exec "$@"
EOF

cat > "$shims/curl" <<'EOF'
#!/bin/bash
# ensure_companion fetch: honour -o <file>; FAKE_CURL_BODY or fail (exit 22).
# The URL is appended to $CURL_URLS_LOG when set.
out=""
prev=""
for a in "$@"; do
  [ "$prev" = "-o" ] && out="$a"
  case "$a" in http*) [ -n "${CURL_URLS_LOG:-}" ] && printf '%s\n' "$a" >> "$CURL_URLS_LOG" ;; esac
  prev="$a"
done
if [ -n "${FAKE_CURL_BODY:-}" ] && [ -n "$out" ]; then
  printf '%s\n' "$FAKE_CURL_BODY" > "$out"
  exit 0
fi
exit 22
EOF

chmod +x "$shims"/*
PATH_WITH_SHIMS="$shims:$PATH"

# ---------- fixtures ----------
STOCK='num_sectors="124321792"'
PATCH='num_sectors="124190720"'

make_l4t() { # $1=dir  $2=stock|patched|neither|noxml  $3=boardid-line (optional)
  local d="$1" cfg="$1/bootloader/t186ref/cfg"
  mkdir -p "$cfg"
  case "$2" in
    stock)   echo "<partition $STOCK />" > "$cfg/flash_t234_qspi_sdmmc.xml" ;;
    patched) echo "<partition $PATCH />" > "$cfg/flash_t234_qspi_sdmmc.xml" ;;
    neither) echo "<partition num_sectors=\"1\" />" > "$cfg/flash_t234_qspi_sdmmc.xml" ;;
    noxml)   ;;
  esac
  printf '#!/bin/bash\necho "%s"\n' "${3:-}" > "$d/nvautoflash.sh"
  chmod +x "$d/nvautoflash.sh"
}

fake_companion() { # $1=exit-code -> path; prints the matching VERDICT line
  # (companion_via corroborates rc against the VERDICT text, so a bare exit
  # code without the line is deliberately NOT a valid verdict — see section 8)
  local f="$base_tmp/companion_rc$1.sh" verdict
  case "$1" in
    0) verdict="VERDICT: PRE-PCN (fake)" ;;
    1) verdict="VERDICT: POST-PCN (fake)" ;;
    2) verdict="VERDICT: UNKNOWN (fake)" ;;
  esac
  printf '#!/bin/bash\necho "%s"\nexit %s\n' "$verdict" "$1" > "$f"
  echo "$f"
}

run_preflight() { # args: l4t-dir companion; state via FAKE_* env vars
  PATH="$PATH_WITH_SHIMS" L4T="$1" COMPANION="$2" bash "$SCRIPT" </dev/null
}

LSUSB_RECOVERY="Bus 001 Device 009: ID 0955:7023 NVIDIA Corp. APX"
LSUSB_INITRD="Bus 001 Device 010: ID 0955:7035 NVIDIA Corp. Linux for Tegra"
LSUSB_BOOTED="Bus 001 Device 011: ID 0955:7020 NVIDIA Corp. L4T (Linux for Tegra) running on Tegra"
BOARD_POST="Board ID(3701) version(501) sku(0004) revision(J.0)"
BOARD_PRE="Board ID(3701) version(500) sku(0004) revision(H.0)"

# ---------- 1. forced-recovery verdicts (the state you flash from) ----------

l4t_post_stock="$base_tmp/l4t_ps";  make_l4t "$l4t_post_stock" stock   "$BOARD_POST"
l4t_post_patch="$base_tmp/l4t_pp";  make_l4t "$l4t_post_patch" patched "$BOARD_POST"
l4t_pre_stock="$base_tmp/l4t_rs";   make_l4t "$l4t_pre_stock"  stock   "$BOARD_PRE"
l4t_post_noxml="$base_tmp/l4t_px";  make_l4t "$l4t_post_noxml" noxml   "$BOARD_POST"

out=$(FAKE_LSUSB_LINE="$LSUSB_RECOVERY" run_preflight "$l4t_post_stock" "$COMPANION_REAL"); rc=$?
assert_eq       "recovery post-PCN + stock tree -> 1" 1 "$rc"
assert_contains "recovery post-PCN + stock says DO NOT FLASH" "$out" "DO NOT FLASH"
assert_contains "recovery post-PCN + stock prints reapply cmd" "$out" "sed -i"

out=$(FAKE_LSUSB_LINE="$LSUSB_RECOVERY" run_preflight "$l4t_post_patch" "$COMPANION_REAL"); rc=$?
assert_eq       "recovery post-PCN + patched tree -> 0" 0 "$rc"
assert_contains "recovery post-PCN + patched says GO" "$out" "GO"

out=$(FAKE_LSUSB_LINE="$LSUSB_RECOVERY" run_preflight "$l4t_pre_stock" "$COMPANION_REAL"); rc=$?
assert_eq       "recovery pre-PCN (FAB 500) + stock tree -> 0" 0 "$rc"

out=$(FAKE_LSUSB_LINE="$LSUSB_RECOVERY" run_preflight "$l4t_post_noxml" "$COMPANION_REAL"); rc=$?
assert_eq       "recovery post-PCN + missing XML must NOT be GO" 1 "$rc"

# unparsable EEPROM -> undetermined -> 2
l4t_junk="$base_tmp/l4t_junk"; make_l4t "$l4t_junk" stock "no board info here"
out=$(FAKE_LSUSB_LINE="$LSUSB_RECOVERY" run_preflight "$l4t_junk" "$COMPANION_REAL"); rc=$?
assert_eq "recovery unparsable EEPROM -> 2" 2 "$rc"

# ---------- 2. companion exit-code map (initrd path, fake companion) ----------

comp_post="$(fake_companion 1)"
comp_pre="$(fake_companion 0)"
comp_unk="$(fake_companion 2)"

out=$(FAKE_LSUSB_LINE="$LSUSB_INITRD" run_preflight "$l4t_post_stock" "$comp_post"); rc=$?
assert_eq "initrd companion rc1 (post) + stock -> 1" 1 "$rc"

out=$(FAKE_LSUSB_LINE="$LSUSB_INITRD" run_preflight "$l4t_post_stock" "$comp_pre"); rc=$?
assert_eq "initrd companion rc0 (pre) + stock -> 0" 0 "$rc"

out=$(FAKE_LSUSB_LINE="$LSUSB_INITRD" run_preflight "$l4t_post_stock" "$comp_unk"); rc=$?
assert_eq "initrd companion rc2 (unknown) -> 2" 2 "$rc"

# ---------- 3. missing-companion regression (the PR's own bug fix) ----------
# Companion absent + fetch fails + dead fallback link: must be exit 2 with a
# stock tree, NOT the pre-fix behaviour (failed stdin redirect -> rc 1 ->
# misread as post-PCN -> exit 1).

out=$(FAKE_LSUSB_LINE="$LSUSB_INITRD" run_preflight "$l4t_post_stock" "/nonexistent/companion" 2>&1); rc=$?
assert_eq           "initrd missing companion + failed fetch -> 2 (not 1)" 2 "$rc"
assert_contains     "missing companion warns fetch failed" "$out" "fetching"
assert_contains     "missing companion falls back to link warn" "$out" "could not reach initrd"
assert_not_contains "missing companion must not claim post-PCN" "$out" "POST-PCN"

# honest fallback: same absent companion, but the direct sector read works
out=$(FAKE_LSUSB_LINE="$LSUSB_INITRD" FAKE_REMOTE_SECTORS=124190720 \
      run_preflight "$l4t_post_patch" "/nonexistent/companion" 2>&1); rc=$?
assert_eq       "initrd fallback sector read post-PCN + patched -> 0" 0 "$rc"
assert_contains "fallback classifies POST-PCN from sectors" "$out" "POST-PCN"

out=$(FAKE_LSUSB_LINE="$LSUSB_INITRD" FAKE_REMOTE_SECTORS=124321792 \
      run_preflight "$l4t_post_stock" "/nonexistent/companion" 2>&1); rc=$?
assert_eq "initrd fallback sector read pre-PCN + stock -> 0" 0 "$rc"

# ---------- 4. booted-L4T path must not hang or misread when ssh fails ----------

out=$(FAKE_LSUSB_LINE="$LSUSB_BOOTED" run_preflight "$l4t_post_stock" "$comp_post" 2>&1); rc=$?
assert_eq "booted ssh ok, companion post + stock -> 1" 1 "$rc"

# companion unavailable, fetch fails, stdin not a tty: must warn + exit 2
# without prompting (a hang here would freeze the suite)
out=$(FAKE_LSUSB_LINE="$LSUSB_BOOTED" \
      PATH="$shims:$PATH" L4T="$l4t_post_stock" COMPANION="/nonexistent/companion" \
      timeout 10 bash "$SCRIPT" </dev/null 2>&1); rc=$?
assert_eq       "booted unreachable + no companion -> 2, no hang" 2 "$rc"
assert_contains "booted failure prints JETSON_PASS hint" "$out" "JETSON_PASS"

# ---------- 5. absent device ----------

out=$(FAKE_LSUSB_LINE="" run_preflight "$l4t_post_stock" "$COMPANION_REAL"); rc=$?
assert_eq       "no NVIDIA usb device -> 2" 2 "$rc"
assert_contains "absent device message" "$out" "no NVIDIA USB device"

# unrecognized NVIDIA PID -> not-applicable -> 2
out=$(FAKE_LSUSB_LINE="Bus 001 Device 003: ID 0955:7c99 NVIDIA Corp." run_preflight "$l4t_post_stock" "$COMPANION_REAL"); rc=$?
assert_eq "unrecognized NVIDIA pid -> 2" 2 "$rc"

# ---------- 6. companion standalone on a host with no eMMC ----------

out=$(bash "$COMPANION_REAL" 2>/dev/null); rc=$?
assert_eq       "companion on non-jetson host -> 2" 2 "$rc"
assert_contains "companion says UNKNOWN" "$out" "UNKNOWN"

# ---------- 7. _fetch_and_call_remote_script (shared helper plumbing) ----------

helper_curl="$base_tmp/helper_shims"; mkdir -p "$helper_curl"
cat > "$helper_curl/curl" <<'EOF'
#!/bin/bash
# Two callers: the branch->commit API lookup (no -o, api.github.com; answers
# FAKE_SHA or fails when FAKE_SHA=fail) and the raw script fetch (-o <file>;
# FAKE_FETCH body or fail). Every raw URL is appended to $CURL_URLS_LOG.
out=""; prev=""; url=""
for a in "$@"; do
  [ "$prev" = "-o" ] && out="$a"
  case "$a" in http*) url="$a" ;; esac
  prev="$a"
done
case "$url" in
  *api.github.com*)
    [ "${FAKE_SHA:-}" = "fail" ] && exit 22
    printf '%s' "${FAKE_SHA:-1111111111111111111111111111111111111111}"; exit 0 ;;
esac
[ -n "${CURL_URLS_LOG:-}" ] && printf '%s\n' "$url" >> "$CURL_URLS_LOG"
[ "${FAKE_FETCH:-}" = "fail" ] && exit 22
printf '%s\n' "$FAKE_FETCH" > "$out"
EOF
chmod +x "$helper_curl/curl"

# exit-code passthrough (the && gate the docs advertise)
rc=$(PATH="$helper_curl:$PATH" FAKE_FETCH='exit 7' \
     bash -c "source '$HELPERS'; er_jetson_flash_preflight >/dev/null 2>&1; echo \$?")
assert_eq "helper passes script exit code through" 7 "$rc"

# fetch failure -> ERROR + rc 1
out=$(PATH="$helper_curl:$PATH" FAKE_FETCH=fail \
      bash -c "source '$HELPERS'; er_jetson_flash_preflight; echo rc=\$?" 2>&1)
assert_contains "helper fetch failure msg" "$out" "ERROR: failed to fetch"
assert_contains "helper fetch failure rc" "$out" "rc=1"

# branch coherence: child must see ER_BUILD_TOOLS_BRANCH=THIS_SCRIPT_BRANCH
out=$(PATH="$helper_curl:$PATH" FAKE_FETCH='echo "branch=${ER_BUILD_TOOLS_BRANCH:-unset}"' \
      bash -c "source '$HELPERS'; er_jetson_flash_preflight" 2>&1)
assert_contains "helper exports branch to child" "$out" "branch=main"

# temp hygiene: fetched scripts are removed on success, failure, and fetch-fail
tmpbox="$base_tmp/tmpbox"; mkdir -p "$tmpbox"
PATH="$helper_curl:$PATH" TMPDIR="$tmpbox" FAKE_FETCH='exit 0' \
  bash -c "source '$HELPERS'; er_jetson_flash_preflight" >/dev/null 2>&1
PATH="$helper_curl:$PATH" TMPDIR="$tmpbox" FAKE_FETCH='exit 7' \
  bash -c "source '$HELPERS'; er_jetson_flash_preflight" >/dev/null 2>&1
PATH="$helper_curl:$PATH" TMPDIR="$tmpbox" FAKE_FETCH=fail \
  bash -c "source '$HELPERS'; er_jetson_flash_preflight" >/dev/null 2>&1
assert_eq "no temp files left behind by the helper" 0 "$(find "$tmpbox" -type f | wc -l)"

# ---------- 7b. python payloads via _fetch_and_call_remote_script_with (er_jetson_flash) ----------

# exit-code passthrough with the python3 interpreter
rc=$(PATH="$helper_curl:$PATH" FAKE_FETCH='import sys; sys.exit(9)' \
     bash -c "source '$HELPERS'; er_jetson_flash >/dev/null 2>&1; echo \$?")
assert_eq "python wrapper passes exit code through" 9 "$rc"

# branch coherence: the python child must also see ER_BUILD_TOOLS_BRANCH
out=$(PATH="$helper_curl:$PATH" \
      FAKE_FETCH='import os; print("branch=" + os.environ.get("ER_BUILD_TOOLS_BRANCH", "unset"))' \
      bash -c "source '$HELPERS'; er_jetson_flash" 2>&1)
assert_contains "python wrapper exports branch to child" "$out" "branch=main"

# fetch failure -> ERROR + rc 1, same contract as the bash path
out=$(PATH="$helper_curl:$PATH" FAKE_FETCH=fail \
      bash -c "source '$HELPERS'; er_jetson_flash; echo rc=\$?" 2>&1)
assert_contains "python wrapper fetch failure msg" "$out" "ERROR: failed to fetch"
assert_contains "python wrapper fetch failure rc" "$out" "rc=1"

# ---------- 7c. fetch by commit SHA (raw.githubusercontent caches refs/heads/<branch> ~5 min) ----------

sha="0123456789abcdef0123456789abcdef01234567"
urls="$base_tmp/urls.log"; : > "$urls"
out=$(PATH="$helper_curl:$PATH" CURL_URLS_LOG="$urls" FAKE_SHA="$sha" \
      FAKE_FETCH='echo "ref=${ER_BUILD_TOOLS_REF:-unset} branch=${ER_BUILD_TOOLS_BRANCH:-unset}"' \
      bash -c "source '$HELPERS'; er_jetson_flash_preflight" 2>&1)
assert_contains "script fetched by commit sha" "$(cat "$urls")" "/er_build_tools/$sha/bin/jetson-flash-preflight.sh"
assert_not_contains "no refs/heads when the sha resolved" "$(cat "$urls")" "refs/heads"
assert_not_contains "no ?nocache= (the CDN ignores it)" "$(cat "$urls")" "nocache"
assert_contains "child sees the pinned commit" "$out" "ref=$sha branch=main"

# API unreachable -> say so, fall back to the branch ref (previous behaviour)
: > "$urls"
out=$(PATH="$helper_curl:$PATH" CURL_URLS_LOG="$urls" FAKE_SHA=fail FAKE_FETCH='echo "ref=${ER_BUILD_TOOLS_REF:-unset}"' \
      bash -c "source '$HELPERS'; er_jetson_flash_preflight" 2>&1)
assert_contains "api failure is announced" "$out" "WARN"
assert_contains "api failure falls back to the branch ref" "$(cat "$urls")" "/er_build_tools/refs/heads/main/bin/jetson-flash-preflight.sh"
assert_contains "child sees the branch ref on fallback" "$out" "ref=refs/heads/main"

# API answered with something that is not a sha (error body, HTML) -> same fallback
: > "$urls"
out=$(PATH="$helper_curl:$PATH" CURL_URLS_LOG="$urls" FAKE_SHA='{"message":"Not Found"}' FAKE_FETCH='exit 0' \
      bash -c "source '$HELPERS'; er_jetson_flash_preflight" 2>&1)
assert_contains "non-sha api body is announced" "$out" "WARN"
assert_contains "non-sha api body falls back to the branch ref" "$(cat "$urls")" "refs/heads/main"

# python payloads pin the same commit
: > "$urls"
out=$(PATH="$helper_curl:$PATH" CURL_URLS_LOG="$urls" FAKE_SHA="$sha" \
      FAKE_FETCH='import os; print("ref=" + os.environ.get("ER_BUILD_TOOLS_REF", "unset"))' \
      bash -c "source '$HELPERS'; er_jetson_flash" 2>&1)
assert_contains "python payload fetched by commit sha" "$(cat "$urls")" "/er_build_tools/$sha/bin/er_jetson_flash.py"
assert_contains "python child sees the pinned commit" "$out" "ref=$sha"

# the preflight's own companion fetch follows ER_BUILD_TOOLS_REF when set
comp_urls="$base_tmp/comp_urls.log"; : > "$comp_urls"
FAKE_LSUSB_LINE="$LSUSB_INITRD" CURL_URLS_LOG="$comp_urls" ER_BUILD_TOOLS_REF="$sha" FAKE_CURL_BODY="$(cat "$comp_post")" \
  run_preflight "$l4t_post_stock" "$base_tmp/absent-companion" >/dev/null 2>&1
assert_contains "companion fetched from the pinned commit" "$(cat "$comp_urls")" "/er_build_tools/$sha/bin/check-emmc-pcn.sh"
: > "$comp_urls"
FAKE_LSUSB_LINE="$LSUSB_INITRD" CURL_URLS_LOG="$comp_urls" FAKE_CURL_BODY="$(cat "$comp_post")" \
  run_preflight "$l4t_post_stock" "$base_tmp/absent-companion" >/dev/null 2>&1
assert_contains "companion falls back to refs/heads/main without a ref" "$(cat "$comp_urls")" "refs/heads/main/bin/check-emmc-pcn.sh"
assert_not_contains "companion fetch has no ?nocache=" "$(cat "$comp_urls")" "nocache"

# ---------- 8. review regressions: fabricated verdicts + half-edited XML ----------

# empty companion (e.g. a failed `wget -qO` leaves one): rc 0 over a healthy
# transport must NOT read as pre-PCN GO
empty_comp="$base_tmp/empty.sh"; : > "$empty_comp"
out=$(FAKE_LSUSB_LINE="$LSUSB_BOOTED" run_preflight "$l4t_post_stock" "$empty_comp" 2>&1); rc=$?
assert_eq           "empty companion -> 2, not a verdict" 2 "$rc"
assert_not_contains "empty companion must not GO" "$out" "GO —"

# companion output without a VERDICT line (wrapper noise, truncation) must be
# a transport failure, never a verdict
noverdict="$base_tmp/noverdict.sh"
printf '#!/bin/bash\necho "banner only"\nexit 0\n' > "$noverdict"
out=$(FAKE_LSUSB_LINE="$LSUSB_BOOTED" run_preflight "$l4t_post_stock" "$noverdict" 2>&1); rc=$?
assert_eq           "uncorroborated rc0 -> 2" 2 "$rc"
assert_not_contains "uncorroborated rc0 must not GO" "$out" "GO —"

# half-edited XML containing BOTH sector values: unknown tree, post-PCN -> 1
l4t_both="$base_tmp/l4t_both"; make_l4t "$l4t_both" stock "$BOARD_POST"
echo "<partition $PATCH />" >> "$l4t_both/bootloader/t186ref/cfg/flash_t234_qspi_sdmmc.xml"
out=$(FAKE_LSUSB_LINE="$LSUSB_RECOVERY" run_preflight "$l4t_both" "$COMPANION_REAL" 2>&1); rc=$?
assert_eq       "both-values XML + post-PCN -> 1" 1 "$rc"
assert_contains "both-values XML named" "$out" "BOTH stock and patched"

# unpadded sku(4) must still classify post-PCN (padding varies between logs)
l4t_sku4="$base_tmp/l4t_sku4"; make_l4t "$l4t_sku4" patched "Board ID(3701) version(501) sku(4) revision(J.0)"
out=$(FAKE_LSUSB_LINE="$LSUSB_RECOVERY" run_preflight "$l4t_sku4" "$COMPANION_REAL"); rc=$?
assert_eq "recovery unpadded sku(4) + patched -> 0" 0 "$rc"

# ---------- 9. not-in-recovery banner (visual only; exit codes unaffected) ----------

out=$(FAKE_LSUSB_LINE="$LSUSB_BOOTED" run_preflight "$l4t_post_stock" "$comp_post" 2>&1); rc=$?
assert_contains "banner when booted L4T" "$out" "NOT in FORCED RECOVERY"
assert_eq       "banner does not change exit code" 1 "$rc"

out=$(FAKE_LSUSB_LINE="" run_preflight "$l4t_post_stock" "$COMPANION_REAL" 2>&1)
assert_contains "banner variant when no device" "$out" "flashing needs one in FORCED RECOVERY"

out=$(FAKE_LSUSB_LINE="$LSUSB_RECOVERY" run_preflight "$l4t_post_patch" "$COMPANION_REAL" 2>&1)
assert_not_contains "no banner in forced recovery" "$out" "can only start from forced recovery"

# ---------- 10. real JP5.1.2 RCM output (per-field '--- Parsing ...' lines) ----------
# Captured from a real AGX Orin devkit on the deployment machine; the original
# single-line regex matched nothing here and classification failed.

BOARD_PRE_ML=$'--- Parsing board ID (3701) succeeded.\n--- Parsing board version (500) succeeded.\n--- Parsing board SKU (0000) succeeded.\n--- Parsing board REV (J.0) succeeded.\njetson-agx-orin-devkit found.'
BOARD_POST_ML=$'--- Parsing board ID (3701) succeeded.\n--- Parsing board version (501) succeeded.\n--- Parsing board SKU (0004) succeeded.\n--- Parsing board REV (J.0) succeeded.\njetson-agx-orin-devkit found.'

l4t_ml_pre="$base_tmp/l4t_mlpre"; make_l4t "$l4t_ml_pre" patched "$BOARD_PRE_ML"
out=$(FAKE_LSUSB_LINE="$LSUSB_RECOVERY" run_preflight "$l4t_ml_pre" "$COMPANION_REAL"); rc=$?
assert_eq       "real-format pre-PCN (FAB 500/SKU 0000) + patched -> 0" 0 "$rc"
assert_contains "real-format parsed as pre-PCN" "$out" "PRE-PCN"

l4t_ml_post="$base_tmp/l4t_mlpost"; make_l4t "$l4t_ml_post" stock "$BOARD_POST_ML"
out=$(FAKE_LSUSB_LINE="$LSUSB_RECOVERY" run_preflight "$l4t_ml_post" "$COMPANION_REAL"); rc=$?
assert_eq       "real-format post-PCN (FAB 501/SKU 0004) + stock -> 1" 1 "$rc"
assert_contains "real-format DO NOT FLASH" "$out" "DO NOT FLASH"

printf '\n%d passed, %d failed\n' "$pass_count" "$fail_count"
[ "$fail_count" -eq 0 ]
