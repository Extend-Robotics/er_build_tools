# jetson-flash-preflight integration — design

Date: 2026-07-02
Scope decided with Tom: "Light" parity with PR #43 (no test suite), shared
`_fetch_and_call_remote_script` abstraction refactoring both helpers, fully
autonomous through to an open PR + local `/review-pr` pass.

## Goal

Make `bin/jetson-flash-preflight.sh` callable exactly like
`bin/usb-camera-healthcheck.sh` (PR #43): a curl-bash one-liner and an
`er_jetson_flash_preflight` function in `.helper_bash_functions`, with docs,
README entry, and a shellcheck CI workflow.

## What the script does (unchanged)

GO / DO-NOT-FLASH preflight for flashing AGX Orin 64GB with JetPack 5.1.2 after
PCN210100 (post-PCN modules ship a slightly smaller eMMC; the stock flash
layout no longer fits). It detects the connected Jetson's state over USB
(flash initrd / booted L4T / forced recovery / absent), classifies the module
pre/post-PCN, checks the local flash tree for the `num_sectors` patch, and
prints a verdict.

## Components

### 1. `bin/check-emmc-pcn.sh` (new in repo, provided by Tom)

Companion run *on* the Jetson (piped over ssh) — prints eMMC model/sectors/
TNSPEC and exits 0=pre-PCN, 1=post-PCN, 2=unknown. Committed as-is plus a
`# SKIP_CHECK` marker (defensive: `check_bash.yml` sources every
`#!/bin/bash`-shebang file; these scripts must never execute in CI).

### 2. `bin/jetson-flash-preflight.sh` — targeted fixes only (no restructure)

- **Companion auto-fetch**: `$COMPANION` defaults to `~/check-emmc-pcn.sh`,
  which won't exist on a fresh host. If unreadable, fetch
  `bin/check-emmc-pcn.sh` from this repo's raw GitHub URL on branch
  `${ER_BUILD_TOOLS_BRANCH:-main}` into a mktemp file (cleaned up via EXIT
  trap). On fetch failure, warn and continue — downstream fallbacks handle it.
- **Bug fix (misread verdict)**: `companion_via` runs `ssh … 'bash -s' <
  "$COMPANION"`; with a missing companion the failed redirect yields rc=1,
  which the exit-code map reads as "post-PCN". Guard: companion unreadable →
  return transport failure (rc 1 of the *function*), which the callers already
  treat as "could not check", triggering the initrd sector-read fallback /
  booted-path warning instead of a false verdict.
- **Skip pointless interactive retries**: the booted-L4T interactive
  credential loop only runs if the companion is actually available.
- **Exit code contract** (deliberate small addition, mirroring the camera
  check's scriptable contract): exit 0 on GO, 1 on DO NOT FLASH, 2 on
  undetermined / not-applicable. Today the script always exits 0, which is a
  footgun for `er_jetson_flash_preflight && ./flash.sh` automation. Flagged
  explicitly in the PR body.
- **`$0` hint fix**: one warn message interpolates `$0`, which is a mktemp
  path when run via the helper; reword generically.
- `# SKIP_CHECK` marker + shellcheck-clean.

### 3. `.helper_bash_functions`

```bash
_fetch_and_call_remote_script() {
    # $1 = repo-relative path; rest = script args
    # THIS_SCRIPT_BRANCH guard, mktemp, curl -fsSL, run with stdin intact
    # (interactive prompts keep working), preserve exit code, rm temp.
    # Exports ER_BUILD_TOOLS_BRANCH=$THIS_SCRIPT_BRANCH to the child so a
    # script fetching sibling files (companion) stays on the same branch.
}
er_usb_camera_healthcheck() { _fetch_and_call_remote_script "bin/usb-camera-healthcheck.sh" "$@"; }
er_jetson_flash_preflight() { _fetch_and_call_remote_script "bin/jetson-flash-preflight.sh" "$@"; }
```

`er_usb_camera_healthcheck` behaviour is unchanged except the error message
names the path generically and the (harmless) env export.

### 4. `docs/jetson-flash-preflight.md`

Mirrors `docs/usb-camera-healthcheck.md`: purpose + PCN backstory, curl-bash
one-liner, helper usage, detection-state table (USB PID 7035 initrd / 7020
booted / 7023 recovery / none), env overrides (`L4T`, `COMPANION`,
`JETSON_SSH`, `JETSON_PASS`, `ER_BUILD_TOOLS_BRANCH`, `NO_COLOR`), verdict
matrix (module_state × tree_state), exit codes, companion-script section.

### 5. `README.md`

One bullet linking the docs page, style-matched to the existing entries.

### 6. `.github/workflows/check_jetson_flash_preflight.yml`

shellcheck on `bin/jetson-flash-preflight.sh` and `bin/check-emmc-pcn.sh`
(no test job — "Light" scope).

## Error handling summary

| Failure | Behaviour |
|---|---|
| curl of main script fails (helper) | error to stderr, return 1, temp removed |
| companion missing + fetch fails | warn; initrd path falls back to direct sector read; booted path prints the existing "options" warning (no false verdict) |
| unexpected sector count / EEPROM unparsable | existing WARN paths unchanged |
| any DO NOT FLASH | exit 1 (new) |

## Out of scope (explicitly)

Test suite, functions/main() restructure, `--help`/arg parsing, booted-path
direct sector-read fallback, changes to detection logic.

## Delivery

Branch `jetson-flash-preflight` → PR to `main` mirroring PR #43's body
(post-merge `.bashrc` snippet + pre-merge branch-test snippet using
`ER_BUILD_TOOLS_BRANCH`), then a local `/review-pr` pass; judged fixes pushed;
Tom does the final PR review.
