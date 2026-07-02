# jetson-flash-preflight Integration Implementation Plan

<!-- # SKIP_CHECK — this doc quotes #!/bin/bash in prose, which check_bash.yml's
     content grep matches; this explicit marker keeps CI from source-executing
     a markdown file (previously it was skipped only by coincidence). -->

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> Execution choice for this run: inline (superpowers:executing-plans) — small, tightly-coupled shell edits; session is running autonomously with Tom's approval.

**Goal:** Make `bin/jetson-flash-preflight.sh` callable like `usb-camera-healthcheck` (PR #43): curl-bash + `er_jetson_flash_preflight` helper, docs, README, shellcheck CI — with a shared `_fetch_and_call_remote_script`.

**Architecture:** Two hardware scripts stay straight-line (no restructure, "Light" scope); the helper file gains one shared fetch-and-run function both `er_*` wrappers delegate to; companion script availability is solved by auto-fetch from raw.githubusercontent with branch coherence via `ER_BUILD_TOOLS_BRANCH`.

**Tech Stack:** bash, curl, shellcheck, GitHub Actions.

Spec: `docs/superpowers/specs/2026-07-02-jetson-flash-preflight-integration-design.md`

## Global Constraints

- No test suite, no functions/main() restructure of the two bin scripts (Tom's "Light" scope decision).
- `bin/jetson-flash-preflight.sh` and `bin/check-emmc-pcn.sh` must be shellcheck-clean (CI will enforce).
- `er_usb_camera_healthcheck` behaviour must not change (same fetch source, args, stdin-tty, exit-code passthrough).
- Both bin scripts get a `# SKIP_CHECK` marker (defence against `check_bash.yml`, which `source`s every file whose shebang greps as `#!/bin/bash`).
- New exit contract for the preflight: 0 = GO, 1 = DO NOT FLASH, 2 = undetermined/not-applicable.
- Branch: `jetson-flash-preflight`; PR mirrors #43's body (post-merge + pre-merge snippets).
- Commits: originals first, fixes as separate reviewable commits.

---

### Task 1: Commit both scripts verbatim (baseline)

**Files:**
- Add (already on disk, untracked): `bin/jetson-flash-preflight.sh`, `bin/check-emmc-pcn.sh`

- [x] **Step 1.1:** `git add bin/jetson-flash-preflight.sh bin/check-emmc-pcn.sh && git commit -m "feat: add jetson-flash-preflight + check-emmc-pcn scripts as provided"`
  Expected: clean commit; `git show --stat HEAD` lists both files.

### Task 2: Targeted fixes to `bin/jetson-flash-preflight.sh` + marker in `bin/check-emmc-pcn.sh`

**Files:**
- Modify: `bin/jetson-flash-preflight.sh` (header L2–16, constants after L30, `companion_via` L53, case branches 7035/7020, interactive gate L99, hint L118, verdict L185–193)
- Modify: `bin/check-emmc-pcn.sh` (header)

**Interfaces:**
- Produces: env knob `ER_BUILD_TOOLS_BRANCH` (default `main`) — consumed by Task 3's helper and Task 4's docs.
- Produces: exit codes 0/1/2 — documented in Task 4, mentioned in Task 8's PR body.

- [x] **Step 2.1: `bin/check-emmc-pcn.sh` — SKIP_CHECK marker.** After the L3 comment, insert:

```bash
# SKIP_CHECK — straight-line script with side effects; check_bash.yml must never source-execute it.
```

- [x] **Step 2.2: preflight header.** L8 verdict line + L11 env line become:

```bash
#   3. Verdict: GO / DO NOT FLASH.  Exit: 0 GO, 1 DO NOT FLASH, 2 undetermined.
```
```bash
# Env overrides: L4T, COMPANION, JETSON_SSH (user@host), JETSON_PASS, NO_COLOR,
#                ER_BUILD_TOOLS_BRANCH (branch the companion is fetched from when absent)
```
and insert below the L2 title comment:
```bash
# SKIP_CHECK — straight-line script that probes hardware (lsusb/ssh/sudo); check_bash.yml must never source-execute it.
```

- [x] **Step 2.3: constants (after `REAPPLY_CMD`, L30).** Insert:

```bash
RAW_URL_BASE="https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/${ER_BUILD_TOOLS_BRANCH:-main}"
COMPANION_TMP=""
trap 'rm -f "$COMPANION_TMP"' EXIT
```

- [x] **Step 2.4: guard `companion_via` (bug fix).** First body line becomes:

```bash
companion_via() {               # $@ = ssh command opening a shell on the target
    local out rc
    # Missing companion must be a transport failure, not a verdict: the failed
    # stdin redirect would otherwise exit 1, which the map below reads as post-PCN.
    [ -r "$COMPANION" ] || return 1
    out=$("$@" 'bash -s' < "$COMPANION" 2>/dev/null); rc=$?
```

- [x] **Step 2.5: `ensure_companion` (after `companion_via`, before section 1).**

```bash
# The companion lives in this repo; a fresh host won't have it at $COMPANION.
ensure_companion() {
    [ -r "$COMPANION" ] && return 0
    COMPANION_TMP=$(mktemp) || return 1
    if curl -fsSL "$RAW_URL_BASE/bin/check-emmc-pcn.sh" -o "$COMPANION_TMP"; then
        COMPANION="$COMPANION_TMP"
        return 0
    fi
    warn "companion not at \$COMPANION and fetching $RAW_URL_BASE/bin/check-emmc-pcn.sh failed"
    rm -f "$COMPANION_TMP"; COMPANION_TMP=""
    return 1
}
```

- [x] **Step 2.6: call it.** In the `7035)` branch insert `ensure_companion` directly after the `ok "state: FLASH INITRD …"` line; in the `7020)` branch insert `ensure_companion` directly after `booted_ok=0`.

- [x] **Step 2.7: gate the interactive retry loop.** L99 condition gains a readability check:

```bash
        if [ "$booted_ok" != 1 ] && [ -t 0 ] && [ -r "$COMPANION" ]; then
```

- [x] **Step 2.8: `$0` hint.** L118 becomes:

```bash
            warn "  re-run with JETSON_PASS='<password>' [JETSON_SSH=<user@host>]     # non-interactive"
```

- [x] **Step 2.9: verdict exit codes.** The final case becomes:

```bash
case "$module_state:$tree_state" in
    post-pcn:patched)  ok "GO — module needs the patch and the tree has it. Flash away."; exit 0;;
    post-pcn:*)        bad "DO NOT FLASH — this module needs the patch and the tree does NOT have it (see section 2)."; exit 1;;
    pre-pcn:patched)   ok "GO — pre-PCN module; patched tree lays its eMMC out 64 MiB short (harmless, rootfs is on NVMe)."; exit 0;;
    pre-pcn:stock)     ok "GO — pre-PCN module, stock layout fits."; exit 0;;
    pre-pcn:*)         warn "pre-PCN module, but the flash tree is in an unexpected state — fix section 2 first."; exit 2;;
    not-applicable:*)  warn "patch not applicable to the connected device — this script can't vouch for the flash."; exit 2;;
    *)                 warn "could not determine module type — resolve section 1 before flashing."; exit 2;;
esac
```

- [x] **Step 2.10: verify.** `bash -n` both scripts; `shellcheck bin/jetson-flash-preflight.sh bin/check-emmc-pcn.sh` (fix anything it reports, minimally); functional: `bash bin/jetson-flash-preflight.sh` on this host (no Jetson) → "no NVIDIA USB device" + tree FAIL + exit 2; `COMPANION=/nonexistent ER_BUILD_TOOLS_BRANCH=this-branch-does-not-exist bash bin/jetson-flash-preflight.sh` → fetch-fail warn, still exit 2, **no post-PCN misread**; `bash bin/check-emmc-pcn.sh` → UNKNOWN, exit 2.

- [x] **Step 2.11: commit** `fix: fetch companion when absent; exit codes; no false post-PCN on missing companion`

### Task 3: `.helper_bash_functions` — `_fetch_and_call_remote_script`

**Files:**
- Modify: `.helper_bash_functions` L210–226 (replace `er_usb_camera_healthcheck` body)

**Interfaces:**
- Consumes: `THIS_SCRIPT_BRANCH` (existing file-global), `ER_BUILD_TOOLS_BRANCH` knob from Task 2.
- Produces: `_fetch_and_call_remote_script <repo-relative-path> [args…]`, `er_usb_camera_healthcheck`, `er_jetson_flash_preflight`.

- [x] **Step 3.1:** Replace the existing `er_usb_camera_healthcheck()` block with:

```bash
_fetch_and_call_remote_script() {
    # $1 = repo-relative script path; remaining args are passed to the script.
    # Temp file rather than process substitution so the script keeps the
    # terminal on stdin — interactive prompts (ssh credentials) still work.
    local script_rel_path="$1" script_path exit_code
    shift
    if [ -z "${THIS_SCRIPT_BRANCH:-}" ]; then
        echo "ERROR: THIS_SCRIPT_BRANCH is unset; cannot determine which branch to fetch" >&2
        return 1
    fi
    script_path="$(mktemp)" || return 1
    if ! curl -fsSL "https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/${THIS_SCRIPT_BRANCH}/${script_rel_path}" -o "$script_path"; then
        echo "ERROR: failed to fetch ${script_rel_path}" >&2
        rm -f "$script_path"
        return 1
    fi
    # Keep any sibling-file fetches done by the script on the same branch.
    ER_BUILD_TOOLS_BRANCH="${THIS_SCRIPT_BRANCH}" bash "$script_path" "$@"
    exit_code=$?
    rm -f "$script_path"
    return "$exit_code"
}

er_usb_camera_healthcheck() { _fetch_and_call_remote_script "bin/usb-camera-healthcheck.sh" "$@"; }
er_jetson_flash_preflight() { _fetch_and_call_remote_script "bin/jetson-flash-preflight.sh" "$@"; }
```

- [x] **Step 3.2: verify.** `bash -n .helper_bash_functions`; `bash -c 'source .helper_bash_functions; _fetch_and_call_remote_script bin/does-not-exist.sh; echo rc=$?'` → ERROR + rc=1; `bash -c 'source .helper_bash_functions; er_usb_camera_healthcheck --help; echo rc=$?'` → usage from the real main-branch script, rc=0; `bash -c 'source .helper_bash_functions; THIS_SCRIPT_BRANCH= er_usb_camera_healthcheck'` → branch-unset ERROR, rc=1.

- [x] **Step 3.3: commit** `refactor: extract _fetch_and_call_remote_script; add er_jetson_flash_preflight`

### Task 4: `docs/jetson-flash-preflight.md`

**Files:**
- Create: `docs/jetson-flash-preflight.md`

- [x] **Step 4.1:** Write the page: purpose + PCN210100 background (post-PCN eMMC = 124190720 sectors vs 124321792; stock JP5.1.2 layout overflows; one-line `num_sectors` patch), curl-bash one-liner, `er_jetson_flash_preflight` helper, detection-state table (7035/7020/7023/none), companion-script section (auto-fetch + standalone one-liner), env-override table (`L4T`, `COMPANION`, `JETSON_SSH`, `JETSON_PASS`, `ER_BUILD_TOOLS_BRANCH`, `NO_COLOR`), verdict × exit-code table (0/1/2), automation example (`er_jetson_flash_preflight && … flash`). Match the tone/structure of `docs/usb-camera-healthcheck.md`.

- [x] **Step 4.2: README bullet** (Modify `README.md` Tools list, keep style):

```markdown
- [jetson-flash-preflight](docs/jetson-flash-preflight.md) — GO / DO NOT FLASH pre-flash check for the AGX Orin 64GB eMMC patch (PCN210100): module detection over USB + flash-tree patch check (curl-bash).
```

- [x] **Step 4.3: commit** `docs: add jetson-flash-preflight page + README entry`

### Task 5: CI workflow

**Files:**
- Create: `.github/workflows/check_jetson_flash_preflight.yml`

- [x] **Step 5.1:**

```yaml
name: Check jetson flash preflight

on: [push]

jobs:
  check_jetson_flash_preflight:
    runs-on: ubuntu-latest
    steps:
      - name: checkout repo
        uses: actions/checkout@v4

      - name: shellcheck jetson flash preflight scripts
        shell: bash
        run: shellcheck bin/jetson-flash-preflight.sh bin/check-emmc-pcn.sh
```

- [x] **Step 5.2: verify** `python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/check_jetson_flash_preflight.yml'))"` → no output.
- [x] **Step 5.3: commit** `ci: shellcheck jetson-flash-preflight scripts`

### Task 6: Verification sweep (pre-push)

- [x] **Step 6.1:** `shellcheck bin/jetson-flash-preflight.sh bin/check-emmc-pcn.sh bin/usb-camera-healthcheck.sh` → clean.
- [x] **Step 6.2:** `bash tests/test_usb_camera_healthcheck.sh` → "N passed, 0 failed".
- [x] **Step 6.3:** Simulate `check_bash.yml`: run its grep loop locally; assert neither new bin script is matched (env-bash shebang) and every matched file sources cleanly.
- [x] **Step 6.4:** Stub harness in the scratchpad (NOT committed): PATH shims for `lsusb`/`sudo`/`ssh`/`sshpass` + fake L4T tree with `flash_t234_qspi_sdmmc.xml` + fake `nvautoflash.sh` printing `Board ID(3701) version(501) sku(0004) revision(N.0)`. Assert: 7023+stock → DO NOT FLASH exit 1; 7023+patched → GO exit 0; no device+stock → exit 2; 7020 with `COMPANION=/nonexistent` and unfetchable branch → options warning, exit 2, output contains no "post-PCN" misread.

### Task 7: Push + end-to-end + PR

- [x] **Step 7.1:** `git push -u origin jetson-flash-preflight`.
- [x] **Step 7.2 (e2e):** `bash -c 'source .helper_bash_functions; THIS_SCRIPT_BRANCH=jetson-flash-preflight er_jetson_flash_preflight'` → real fetch from the branch; companion auto-fetch resolves on the branch (`ER_BUILD_TOOLS_BRANCH` passthrough); exit 2 on this dev box.
- [x] **Step 7.3:** `gh pr create` — title `Jetson flash preflight`, body mirroring #43: intro, `## Setup` post-merge `.bashrc` snippet + pre-merge branch snippet (`ER_BUILD_TOOLS_BRANCH=jetson-flash-preflight bash <(curl -Ls …/refs/heads/jetson-flash-preflight/bin/jetson-flash-preflight.sh)`), `## Usage`, notes-for-review (companion auto-fetch + misread bug fix; exit-code contract is a deliberate addition; `er_usb_camera_healthcheck` refactored onto `_fetch_and_call_remote_script`, no behaviour change). Footer: 🤖 Generated with [Claude Code](https://claude.com/claude-code)

### Task 8: /review-pr + judged fixes

- [x] **Step 8.1:** Run the `review-pr` skill on the new PR with two machine-specific adaptations: keep `GH_TOKEN` set (on this box gh is only authenticated through it), and inline the full `ponytail-review` ruleset (captured from the installed plugin) into the Ponytail Reviewer prompt.
- [x] **Step 8.2:** Triage findings with judgment (fix what's worth fixing, skip with stated reason otherwise), commit, push.
- [x] **Step 8.3:** notify-send + final summary for Tom's manual PR review.

## Self-Review

- Spec coverage: components 1–6 → Tasks 1–5; error-handling table → Steps 2.4/2.5/2.10; delivery → Tasks 7–8. ✓
- No placeholders; all code shown. ✓
- Names consistent: `ER_BUILD_TOOLS_BRANCH`, `RAW_URL_BASE`, `COMPANION_TMP`, `_fetch_and_call_remote_script`, `er_jetson_flash_preflight` used identically across tasks. ✓

## Post-review amendments (8-agent /review-pr pass on PR #44)

Where the shipped code deviates from the task blocks above, the `fix:`/`test:`
commits after `d40db5e` are the source of truth:

- `companion_via` corroborates the exit code against the companion's own
  `VERDICT:` line; guards tightened to `companion_ok()` (`-f`/`-r`/`-s`) —
  closes verified false-GO paths (empty companion → rc 0 → "pre-PCN";
  uncorroborated remote noise)
- tree check gains unreadable-XML and BOTH-sector-values branches (fail-closed)
- `require_sshpass()` (was misdiagnosed as "link down?"), `SSH_OPTS` array with
  `ServerAlive*` (bounds hangs), `--max-time` on the companion fetch
- SKU compared numerically (`10#`, padding-proof); unexpected sector count →
  `undetermined` (was `not-applicable`)
- 7020 no longer claims "running companion check" without a companion; initrd
  fallback distinguishes link-down from reachable-but-no-eMMC
- helper: Step 3.1's stdin-rationale comment was wrong (process substitution
  keeps stdin; only `curl | bash` loses it) — shipped text corrected; fetch
  errors name the branch
- docs: automation-safety caveat (`bash <(curl -Ls …)` exits 0 on network
  failure), verdict/states table rows, branch-pinning note
- **Scope deviation, flagged for Tom's review:** `tests/test_jetson_flash_preflight.sh`
  + a workflow test step. The "no test suite" decision predated the review,
  which produced a validated, mutation-tested suite for free; committed as its
  own revertable commit.
