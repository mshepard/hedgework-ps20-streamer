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
SERVICE_FILE="/etc/systemd/system/streamer.service"
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
    "${INSTALL_PREFIX}/.venv/bin/pip" install --upgrade --force-reinstall --no-deps "${REPO_ROOT}"
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
    stop_service
    pip_install
    start_service
}

main "$@"
