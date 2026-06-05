#!/usr/bin/env bash
#
# Streamer update script: fast in-place reinstall + service restart.
#
# Usage: sudo bash scripts/update.sh
#
# Assumes scripts/install.sh has been run at least once. Does NOT touch
# apt packages, the user account, or the configuration file — only
# updates the Python code in /opt/streamer/.venv, refreshes the systemd
# unit if it changed, and restarts the service with explicit
# stop/kill/start so a wedged python process can't drag systemd's
# default 90s timeout.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="streamer"
INSTALL_PREFIX="/opt/streamer"
STATE_DIR="/var/lib/streamer"
SERVICE_FILE="/etc/systemd/system/streamer.service"
SUDOERS_FILE="/etc/sudoers.d/streamer"
STOP_TIMEOUT=12

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        echo "ERROR: this updater must be run as root (sudo bash $0)" >&2
        exit 1
    fi
}

log() { printf '[update] %s\n' "$*"; }

refresh_systemd_unit() {
    local repo_unit="${REPO_ROOT}/scripts/streamer.service"
    if [[ ! -f "${repo_unit}" ]]; then
        return
    fi
    if [[ ! -f "${SERVICE_FILE}" ]] || ! cmp -s "${repo_unit}" "${SERVICE_FILE}"; then
        log "Updating systemd unit ${SERVICE_FILE}"
        install -o root -g root -m 0644 "${repo_unit}" "${SERVICE_FILE}"
        systemctl daemon-reload
    fi
}

# Phase 2: idempotently ensure the state dir and sudoers entry exist
# so updating from a Phase 1 install picks up the new privileges.
ensure_state_dir() {
    if [[ ! -d "${STATE_DIR}" ]]; then
        log "Creating ${STATE_DIR}"
        install -d -o "${USER_NAME}" -g "${USER_NAME}" -m 0750 "${STATE_DIR}"
    fi
}

ensure_sudoers() {
    local desired
    desired=$(cat <<EOF
# Streamer HARD_SLEEP privileges. Managed by scripts/install.sh — do not
# edit by hand; re-running the installer rewrites this file.
${USER_NAME} ALL=(root) NOPASSWD: /usr/sbin/rtcwake, /usr/bin/systemctl poweroff
EOF
)
    if [[ -f "${SUDOERS_FILE}" ]] && diff -q \
        <(printf '%s\n' "${desired}") "${SUDOERS_FILE}" >/dev/null 2>&1
    then
        return
    fi
    log "Refreshing ${SUDOERS_FILE}"
    local tmp
    tmp=$(mktemp)
    printf '%s\n' "${desired}" > "${tmp}"
    if ! visudo -cf "${tmp}" >/dev/null; then
        log "ERROR: generated sudoers file failed visudo check; leaving existing file untouched"
        rm -f "${tmp}"
        return
    fi
    install -o root -g root -m 0440 "${tmp}" "${SUDOERS_FILE}"
    rm -f "${tmp}"
}

stop_service() {
    if ! systemctl is-active --quiet streamer.service; then
        log "streamer.service was not running"
        return
    fi
    log "Stopping streamer.service (max ${STOP_TIMEOUT}s)"
    if ! timeout "${STOP_TIMEOUT}" systemctl stop streamer.service; then
        log "Stop exceeded ${STOP_TIMEOUT}s; force-killing main PID"
        local pid
        pid=$(systemctl show -p MainPID --value streamer.service || echo 0)
        if [[ -n "${pid}" && "${pid}" != "0" ]]; then
            kill -s KILL "${pid}" 2>/dev/null || true
        fi
        sleep 1
    fi
}

pip_install() {
    log "Reinstalling Streamer from ${REPO_ROOT}"
    # Phase 2 added the ``astral`` dependency. Drop --no-deps so any
    # newly-listed runtime dependency in pyproject.toml is picked up
    # automatically on update. pip is smart enough to skip what's
    # already satisfied, so this only meaningfully slows the first
    # update after a new dep is added.
    #
    # --no-warn-conflicts: the venv inherits --system-site-packages so it
    # can see the apt-installed picamera2. That means pip's resolver also
    # sees every unrelated package in /usr/lib/python3/dist-packages, and
    # whines about any pre-existing dependency conflict there (e.g.
    # types-seaborn missing matplotlib). Those aren't ours to fix; mute
    # the noise so genuine install failures stand out.
    "${INSTALL_PREFIX}/.venv/bin/pip" install \
        --upgrade --force-reinstall --no-warn-conflicts \
        "${REPO_ROOT}"
    chown -R "${USER_NAME}:${USER_NAME}" "${INSTALL_PREFIX}"
}

start_service() {
    log "Starting streamer.service"
    systemctl start streamer.service
    sleep 1
    if systemctl is-active --quiet streamer.service; then
        log "Service is running"
    else
        log "WARNING: service did not enter active state; check 'journalctl -u streamer -n 60 --no-pager'"
    fi
}

main() {
    require_root
    refresh_systemd_unit
    ensure_state_dir
    ensure_sudoers
    stop_service
    pip_install
    start_service
}

main "$@"
