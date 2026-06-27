#!/usr/bin/env bash
#
# Install (if needed) and run slskd on macOS (Intel / x64).
#
# slskd is the Soulseek client that download_wishlist.py talks to. On first run
# this downloads the binary; on later runs it just launches it. Either way it
# prompts for your Soulseek username/password, starts slskd with them, and
# verifies the login works before handing off.
#
# The same username/password are used for BOTH the Soulseek network login and
# the slskd web login — so pass them to the wishlist script as
# --slskd-username / --slskd-password.
#
# Usage:
#   ./run-slskd-macos-x64.sh [version]
#
# Environment overrides:
#   SLSKD_VERSION       release tag to (re)install (default: latest)
#   SLSKD_INSTALL_DIR   where the binary lives (default: $HOME/slskd)
#   SLSKD_PORT          slskd web port (default: 5030)
#
set -euo pipefail

REPO="slskd/slskd"
ARCH_TAG="osx-x64"
INSTALL_DIR="${SLSKD_INSTALL_DIR:-$HOME/slskd}"
BIN="$INSTALL_DIR/slskd"
PORT="${SLSKD_PORT:-5030}"
BASE_URL="http://localhost:${PORT}"

# Credentials are cached here (alongside the script) so later runs skip the prompt.
# Plaintext, so it's created mode 600 and git-ignored; removed if a login is rejected.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRED_FILE="${SLSKD_CRED_FILE:-$SCRIPT_DIR/slskd-credentials.env}"

# Force a deterministic downloads dir so it's the same on every OS and matches what
# we tell the wishlist script (--slskd-downloads-dir). Otherwise slskd picks an
# OS-specific default under its app-dir that's easy to get wrong.
DOWNLOADS_DIR="${SLSKD_DOWNLOADS_DIR:-$INSTALL_DIR/downloads}"

install_slskd() {
    local version asset url tmpzip
    version="${1:-${SLSKD_VERSION:-}}"
    if [ -z "$version" ]; then
        echo "Resolving latest slskd release…"
        version="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
            | grep -m1 '"tag_name"' | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')"
    fi
    if [ -z "$version" ]; then
        echo "error: could not determine slskd version (pass one explicitly, e.g. '$0 0.25.1')" >&2
        exit 1
    fi

    asset="slskd-${version}-${ARCH_TAG}.zip"
    url="https://github.com/${REPO}/releases/download/${version}/${asset}"

    echo "Installing slskd ${version} (${ARCH_TAG}) into ${INSTALL_DIR}"
    mkdir -p "$INSTALL_DIR"
    tmpzip="$(mktemp -t slskd-XXXXXX).zip"
    trap 'rm -f "$tmpzip"' RETURN

    echo "Downloading ${url}"
    curl -fL -o "$tmpzip" "$url"
    echo "Unzipping…"
    unzip -o "$tmpzip" -d "$INSTALL_DIR" >/dev/null

    if [ ! -f "$BIN" ]; then
        echo "error: slskd binary not found after unzip in $INSTALL_DIR" >&2
        exit 1
    fi
    # Clear the macOS quarantine flag so Gatekeeper doesn't block the unsigned binary.
    xattr -d com.apple.quarantine "$BIN" 2>/dev/null || true
    chmod +x "$BIN"
    echo "✓ Installed slskd ${version} at ${BIN}"
}

prompt_credentials() {
    echo
    echo "Enter your Soulseek credentials (a free account — if it doesn't exist yet,"
    echo "slskd registers it on first connect). These are reused as the slskd web login."
    while [ -z "${RUN_USERNAME:-}" ]; do
        printf "  Username: "
        read -r RUN_USERNAME || true
        if [ -z "${RUN_USERNAME:-}" ]; then echo "  (username can't be empty)"; fi
    done
    while [ -z "${RUN_PASSWORD:-}" ]; do
        printf "  Password: "
        read -rs RUN_PASSWORD || true
        echo
        if [ -z "${RUN_PASSWORD:-}" ]; then echo "  (password can't be empty)"; fi
    done
}

# Load cached creds into RUN_USERNAME/RUN_PASSWORD; succeeds only if both are present.
# Parsed line-by-line (not sourced) so the file can't execute arbitrary shell.
load_credentials() {
    [ -f "$CRED_FILE" ] || return 1
    local k v
    while IFS='=' read -r k v; do
        case "$k" in
            SLSKD_RUN_USERNAME) RUN_USERNAME="$v" ;;
            SLSKD_RUN_PASSWORD) RUN_PASSWORD="$v" ;;
        esac
    done < "$CRED_FILE"
    [ -n "${RUN_USERNAME:-}" ] && [ -n "${RUN_PASSWORD:-}" ]
}

save_credentials() {
    ( umask 077; printf 'SLSKD_RUN_USERNAME=%s\nSLSKD_RUN_PASSWORD=%s\n' \
        "$RUN_USERNAME" "$RUN_PASSWORD" > "$CRED_FILE" )
    chmod 600 "$CRED_FILE" 2>/dev/null || true
}

get_credentials() {
    if load_credentials; then
        echo "Using saved credentials for '${RUN_USERNAME}' from ${CRED_FILE}"
        echo "  (delete that file to re-enter; it's auto-removed if the login is rejected)"
        return 0
    fi
    prompt_credentials
    save_credentials
    echo "Saved credentials to ${CRED_FILE} (mode 600) — future runs won't prompt."
}

# Returns the HTTP status of a login attempt: 200 ok, 401 rejected, 000 not up yet.
login_status() {
    python3 - "$BASE_URL" "$RUN_USERNAME" "$RUN_PASSWORD" <<'PY'
import sys, json, urllib.request, urllib.error
base, user, pw = sys.argv[1:4]
req = urllib.request.Request(
    base + "/api/v0/session",
    data=json.dumps({"username": user, "password": pw}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print("000")
PY
}

run_slskd() {
    local log="$INSTALL_DIR/slskd-run.log"
    export SLSKD_SLSK_USERNAME="$RUN_USERNAME" SLSKD_SLSK_PASSWORD="$RUN_PASSWORD"
    export SLSKD_USERNAME="$RUN_USERNAME" SLSKD_PASSWORD="$RUN_PASSWORD"

    mkdir -p "$DOWNLOADS_DIR"

    echo
    echo "Starting slskd…"
    "$BIN" --downloads "$DOWNLOADS_DIR" >"$log" 2>&1 &
    local slskd_pid=$!
    local tail_pid=""
    cleanup() {
        [ -n "$tail_pid" ] && kill "$tail_pid" 2>/dev/null || true
        kill "$slskd_pid" 2>/dev/null || true
    }
    trap cleanup INT TERM EXIT

    local ok="" code
    for _ in $(seq 1 30); do
        if ! kill -0 "$slskd_pid" 2>/dev/null; then
            echo "error: slskd exited unexpectedly. Last log lines:" >&2
            tail -n 20 "$log" >&2
            exit 1
        fi
        code="$(login_status)"
        if [ "$code" = "200" ]; then ok=1; break; fi
        if [ "$code" = "401" ]; then
            echo "error: slskd rejected that username/password (HTTP 401)." >&2
            rm -f "$CRED_FILE"
            echo "Removed saved credentials (${CRED_FILE}). Re-run to enter new ones." >&2
            exit 1
        fi
        sleep 1
    done
    if [ -z "$ok" ]; then
        echo "error: slskd didn't become ready in time. Last log lines:" >&2
        tail -n 20 "$log" >&2
        exit 1
    fi

    echo "✓ slskd is running and your login works."
    echo "    Web UI:   ${BASE_URL}"
    echo "    Logs:     ${log}"
    echo "    Downloads: ${DOWNLOADS_DIR}"
    echo "    Wishlist: download_wishlist.py --slskd-url ${BASE_URL} \\"
    echo "                  --slskd-username '${RUN_USERNAME}' --slskd-password <same password> \\"
    echo "                  --slskd-downloads-dir '${DOWNLOADS_DIR}'"
    echo
    echo "Watch the log below for 'logged in' to confirm the Soulseek connection."
    echo "Leave this terminal open. Press Ctrl-C to stop slskd."
    echo "------------------------------------------------------------------------"
    tail -n +1 -f "$log" &
    tail_pid=$!
    wait "$slskd_pid"
}

main() {
    if [ -x "$BIN" ]; then
        echo "slskd already installed at ${BIN} (set SLSKD_VERSION + delete it to upgrade)."
    else
        install_slskd "${1:-}"
    fi
    get_credentials
    run_slskd
}

main "$@"
