#!/usr/bin/env bash
# update-source-repos.sh — pull latest source in the er_robot container, then rebuild
# SKIP_CHECK — keep this. check_bash.yml source-executes unmarked bash files; this
# script probes docker/network and must only run when invoked explicitly.
#
# Usage: update-source-repos.sh <github-pat>     (or set GITHUB_PAT in the env)
#
# Runs on the Jetson HOST. Injects bin/update_source_repos.py into the er_robot
# container to fetch + fast-forward every repo under /cortex/.catkin_ws/src,
# then runs colcon_build inside the container when anything updated.
#
# Env overrides: PAYLOAD (local payload path — skips the raw.githubusercontent
#                fetch; used by tests/dev), ER_BUILD_TOOLS_BRANCH, NO_COLOR
# Exit codes: 0 ok; 97 helpers/colcon_build missing in container; otherwise the
#             failing step's code.

set -u

CONTAINER="er_robot"
RAW_URL_BASE="https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/${ER_BUILD_TOOLS_BRANCH:-main}"
PAYLOAD_REL_PATH="bin/update_source_repos.py"
PAYLOAD_TMP=""
CONTAINER_TMP=""
# shellcheck disable=SC2317  # invoked indirectly via the EXIT trap
cleanup() {
    rm -f "$PAYLOAD_TMP"
    if [ -n "$CONTAINER_TMP" ]; then
        docker exec "$CONTAINER" rm -f "$CONTAINER_TMP" >/dev/null 2>&1
    fi
}
trap cleanup EXIT

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_GRN=$'\033[32m'; C_YLW=$'\033[33m'; C_RED=$'\033[31m'; C_OFF=$'\033[0m'
else
    C_GRN=""; C_YLW=""; C_RED=""; C_OFF=""
fi
err()  { echo "${C_RED}ERROR: $*${C_OFF}" >&2; }
warn() { echo "${C_YLW}WARNING: $*${C_OFF}" >&2; }
ok()   { echo "${C_GRN}$*${C_OFF}"; }

usage() {
    echo "Usage: er_update_source_repos <github-pat>"
    echo "  (or set GITHUB_PAT in the environment)"
    echo "  The PAT needs read access to the Extend-Robotics repos:"
    echo "  classic (ghp_...) or fine-grained (github_pat_...)."
}

# ---------- PAT intake & validation ----------
pat="${1:-${GITHUB_PAT:-}}"
pat="${pat//[$'\t\r\n ']/}"    # strip all whitespace — PATs never contain any
if [ -z "$pat" ]; then
    err "no GitHub PAT supplied"
    usage
    exit 1
fi
if ! [[ "$pat" =~ ^ghp_[A-Za-z0-9]{36}$ || "$pat" =~ ^github_pat_[A-Za-z0-9_]{69,109}$ ]]; then
    err "that does not look like a GitHub PAT (expected ghp_... 40 chars, or github_pat_...)"
    usage
    exit 1
fi

# ---------- PAT live check: fail fast on a revoked/typo'd token ----------
# The token reaches curl via a config file on stdin, never argv (invisible to ps).
# printf is a builtin, so the token also never transits a herestring temp file.
http_code="$(printf 'header = "Authorization: token %s"\n' "$pat" \
             | curl -s -o /dev/null -w '%{http_code}' --max-time 10 -K - \
                    https://api.github.com/user)" \
    || http_code="000"
case "$http_code" in
    2??) ok "GitHub PAT verified" ;;
    401) err "GitHub rejected the PAT (HTTP 401) — expired, revoked, or mistyped?"; exit 1 ;;
    000) warn "could not reach api.github.com to verify the PAT — continuing anyway" ;;
    *)   warn "unexpected HTTP ${http_code} from api.github.com — continuing anyway" ;;
esac

# ---------- container check ----------
if ! command -v docker >/dev/null; then
    err "docker is not installed / not on PATH"
    exit 1
fi
running="$(docker container inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" || true
if [ "$running" != "true" ]; then
    err "container '${CONTAINER}' is not running — is the stack up on this Jetson?"
    exit 1
fi

# ---------- obtain the payload ----------
if [ -n "${PAYLOAD:-}" ] && [ -s "${PAYLOAD}" ]; then
    payload_src="$PAYLOAD"
else
    PAYLOAD_TMP="$(mktemp "${TMPDIR:-/tmp}/er_update_source_repos.XXXXXXXXXX.py")"
    # ?nocache= busts raw.githubusercontent's ~5-minute CDN cache, same pattern
    # as _fetch_and_call_remote_script in .helper_bash_functions.
    if ! curl -fsSL "${RAW_URL_BASE}/${PAYLOAD_REL_PATH}?nocache=$$-${RANDOM}" -o "$PAYLOAD_TMP"; then
        err "failed to fetch ${PAYLOAD_REL_PATH} (branch ${ER_BUILD_TOOLS_BRANCH:-main})"
        exit 1
    fi
    payload_src="$PAYLOAD_TMP"
fi

# ---------- run the payload inside the container ----------
dest="/tmp/er_update_source_repos.$$.py"
if ! docker cp "$payload_src" "${CONTAINER}:${dest}"; then
    err "failed to copy the update script into '${CONTAINER}'"
    exit 1
fi
CONTAINER_TMP="$dest"    # from here the EXIT trap removes it, even on Ctrl-C
tty_flags=(-i)
[ -t 0 ] && [ -t 1 ] && tty_flags=(-i -t)    # the branch picker needs a TTY when we have one
# -e GITHUB_PAT with no value forwards it from this process's env — the token
# never appears in argv on the host or in the container.
GITHUB_PAT="$pat" docker exec "${tty_flags[@]}" -e GITHUB_PAT "$CONTAINER" python3 "$dest"
payload_rc=$?

# ---------- build ----------
case "$payload_rc" in
    10)
        ok "Everything already up to date — skipping colcon_build."
        exit 0
        ;;
    0)
        echo ""
        ok "Source updated — running colcon_build in '${CONTAINER}'..."
        # shellcheck disable=SC2016  # $HOME must expand inside the container, not here
        docker exec "${tty_flags[@]}" "$CONTAINER" bash -c '
            [ -f "$HOME/.helper_bash_functions" ] || exit 97
            source "$HOME/.helper_bash_functions"
            type colcon_build >/dev/null 2>&1 || exit 97
            colcon_build'
        build_rc=$?
        if [ "$build_rc" -eq 97 ]; then
            err "the .helper_bash_functions file (or colcon_build) is missing inside '${CONTAINER}' — cannot build"
            exit 97
        elif [ "$build_rc" -ne 0 ]; then
            err "colcon_build failed (exit ${build_rc})"
            exit "$build_rc"
        fi
        ok "Build complete."
        exit 0
        ;;
    *)
        err "source update failed (exit ${payload_rc}) — skipping build"
        exit "$payload_rc"
        ;;
esac
