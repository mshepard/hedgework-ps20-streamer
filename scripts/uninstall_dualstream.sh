#!/usr/bin/env bash
#
# Uninstall the legacy DualStream service from a Raspberry Pi.
#
# Streamer supersedes DualStream and binds the same port (8080), so once
# Streamer is in production the old install is just clutter (and a port
# conflict risk if anyone ever re-enables it). This script removes every
# artefact the DualStream installer created:
#
#   * systemd unit:   /etc/systemd/system/dualstream.service
#   * install prefix: /opt/dualstream            (venv + source)
#   * config dir:     /etc/dualstream            (incl. dualstream.toml)
#   * state dir:      /var/lib/dualstream        (incl. snapshots/)
#   * system user:    dualstream
#
# Shared apt packages (picamera2, libcamera, ffmpeg, etc.) are NOT removed
# because Streamer still depends on them.
#
# Usage:
#   sudo bash scripts/uninstall_dualstream.sh                 # dry run
#   sudo bash scripts/uninstall_dualstream.sh --yes           # actually remove
#   sudo bash scripts/uninstall_dualstream.sh --yes --keep-config
#   sudo bash scripts/uninstall_dualstream.sh --yes --keep-data
#   sudo bash scripts/uninstall_dualstream.sh --yes --keep-user
#
# Without --yes the script only prints what it WOULD do (recommended first
# pass). With --yes it executes the removals. All steps are idempotent:
# re-running on a clean system is a no-op.

set -euo pipefail

USER_NAME="dualstream"
INSTALL_PREFIX="/opt/dualstream"
CONFIG_DIR="/etc/dualstream"
DATA_DIR="/var/lib/dualstream"
SERVICE_NAME="dualstream.service"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}"

APPLY=0
KEEP_CONFIG=0
KEEP_DATA=0
KEEP_USER=0
FORCE_ANYWAY=0

print_help() {
    # Extract the top-of-file comment block (every leading `#` line after
    # the shebang, stopping at the first non-comment line). Strips the
    # leading "# " so it reads as plain text.
    awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
}

for arg in "$@"; do
    case "${arg}" in
        --yes|-y)        APPLY=1 ;;
        --keep-config)   KEEP_CONFIG=1 ;;
        --keep-data)     KEEP_DATA=1 ;;
        --keep-user)     KEEP_USER=1 ;;
        --force-anyway)  FORCE_ANYWAY=1 ;;
        -h|--help)       print_help; exit 0 ;;
        *)
            echo "ERROR: unknown argument '${arg}'" >&2
            echo "Try: sudo bash $0 --help" >&2
            exit 2
            ;;
    esac
done

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        echo "ERROR: this uninstaller must be run as root (sudo bash $0)" >&2
        exit 1
    fi
}

log() {
    if (( APPLY )); then
        printf '[uninstall] %s\n' "$*"
    else
        printf '[uninstall DRY-RUN] %s\n' "$*"
    fi
}

# run <description> -- <command...>
# Logs the action and either executes it (--yes) or prints it (dry run).
run() {
    local desc="$1"; shift
    if [[ "${1:-}" != "--" ]]; then
        echo "internal error: missing -- before command for '${desc}'" >&2
        exit 99
    fi
    shift
    log "${desc}"
    if (( APPLY )); then
        "$@"
    else
        printf '             would run: %s\n' "$*"
    fi
}

guard_streamer_running() {
    # Refuse to proceed if dualstream.service is the currently-active
    # publisher on port 8080 and streamer.service is NOT. That's almost
    # certainly a misconfiguration (we expect Streamer to be live) and
    # ripping DualStream out now would leave the Pi with no camera
    # service at all.
    if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        if ! systemctl is-active --quiet streamer.service 2>/dev/null; then
            if (( FORCE_ANYWAY )); then
                log "Continuing despite warning (--force-anyway given)"
                return
            fi
            cat >&2 <<EOF
WARNING: ${SERVICE_NAME} is currently running and streamer.service is NOT.
         Removing DualStream now would leave you with no video service.
         Either:
           * start streamer first:  sudo systemctl enable --now streamer
           * or pass --force-anyway to this script to suppress this check.
EOF
            exit 3
        fi
    fi
}

stop_and_disable_unit() {
    if [[ -f "${SERVICE_FILE}" ]] || systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}"; then
        if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
            run "Stopping ${SERVICE_NAME}" -- systemctl stop "${SERVICE_NAME}"
        else
            log "${SERVICE_NAME} is not currently running"
        fi
        if systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
            run "Disabling ${SERVICE_NAME}" -- systemctl disable "${SERVICE_NAME}"
        else
            log "${SERVICE_NAME} is not enabled"
        fi
        # If the unit was previously masked, unmask so removal is clean.
        if [[ "$(systemctl is-enabled "${SERVICE_NAME}" 2>/dev/null || true)" == "masked" ]]; then
            run "Unmasking ${SERVICE_NAME}" -- systemctl unmask "${SERVICE_NAME}"
        fi
    else
        log "${SERVICE_NAME} not installed; nothing to stop or disable"
    fi
}

remove_unit_file() {
    if [[ -f "${SERVICE_FILE}" ]]; then
        run "Removing ${SERVICE_FILE}" -- rm -f "${SERVICE_FILE}"
        run "Reloading systemd" -- systemctl daemon-reload
        run "Resetting failed unit state" -- systemctl reset-failed "${SERVICE_NAME}" || true
    else
        log "${SERVICE_FILE} not present"
    fi
}

remove_install_prefix() {
    if [[ -d "${INSTALL_PREFIX}" ]]; then
        run "Removing ${INSTALL_PREFIX} (venv + source)" -- rm -rf "${INSTALL_PREFIX}"
    else
        log "${INSTALL_PREFIX} not present"
    fi
}

remove_config_dir() {
    if (( KEEP_CONFIG )); then
        log "Keeping ${CONFIG_DIR} (--keep-config)"
        return
    fi
    if [[ -d "${CONFIG_DIR}" ]]; then
        run "Removing ${CONFIG_DIR} (config, tokens)" -- rm -rf "${CONFIG_DIR}"
    else
        log "${CONFIG_DIR} not present"
    fi
}

remove_data_dir() {
    if (( KEEP_DATA )); then
        log "Keeping ${DATA_DIR} (--keep-data)"
        return
    fi
    if [[ -d "${DATA_DIR}" ]]; then
        run "Removing ${DATA_DIR} (state, snapshots)" -- rm -rf "${DATA_DIR}"
    else
        log "${DATA_DIR} not present"
    fi
}

remove_system_user() {
    if (( KEEP_USER )); then
        log "Keeping user '${USER_NAME}' (--keep-user)"
        return
    fi
    if id "${USER_NAME}" >/dev/null 2>&1; then
        # Safety check: only delete if no remaining processes are owned by
        # the user. Otherwise userdel will fail anyway, but the error is
        # less informative than spelling it out here.
        if pgrep -u "${USER_NAME}" >/dev/null 2>&1; then
            echo "ERROR: user '${USER_NAME}' still owns running processes; refusing to delete." >&2
            echo "       Stop them first, then re-run this script." >&2
            exit 4
        fi
        run "Deleting system user '${USER_NAME}'" -- userdel "${USER_NAME}"
    else
        log "System user '${USER_NAME}' does not exist"
    fi
}

print_summary() {
    printf '\n'
    if (( APPLY )); then
        log "Done. DualStream artefacts removed."
    else
        log "Dry run complete. Re-run with --yes to apply the changes:"
        log "    sudo bash $0 --yes"
    fi
}

main() {
    require_root
    guard_streamer_running
    log "Target paths:"
    log "    unit:   ${SERVICE_FILE}"
    log "    bin:    ${INSTALL_PREFIX}"
    log "    config: ${CONFIG_DIR}$( (( KEEP_CONFIG )) && echo '  (kept)')"
    log "    state:  ${DATA_DIR}$( (( KEEP_DATA )) && echo '  (kept)')"
    log "    user:   ${USER_NAME}$( (( KEEP_USER )) && echo '  (kept)')"
    printf '\n'

    stop_and_disable_unit
    remove_unit_file
    remove_install_prefix
    remove_config_dir
    remove_data_dir
    remove_system_user

    print_summary
}

main "$@"
