#!/usr/bin/env bash
#
# Streamer installer for Raspberry Pi 5 (Raspberry Pi OS Bookworm or Trixie).
#
# Usage: sudo bash scripts/install.sh [--install-tailscale]
#
# What this does:
#   * Installs apt dependencies (picamera2, libcamera, venv tooling)
#   * Optionally installs Tailscale
#   * Creates the 'streamer' system user (member of `video`)
#   * Creates /etc/streamer/, /opt/streamer/
#   * Builds a venv at /opt/streamer/.venv with --system-site-packages
#     so it inherits the apt-installed python3-picamera2
#   * pip-installs this project into the venv
#   * Installs and enables the streamer systemd unit
#   * On first install, auto-generates a random auth_token and tightens
#     the config file mode to 0640 root:streamer
#
# What this does NOT do (hardware-sensitive):
#   * Modify /boot/firmware/config.txt (CSI dtoverlays). See README.
#   * Run `tailscale up`. You'll do that with your own auth flow.
#
# Re-running this script is safe; it's idempotent.

set -euo pipefail

INSTALL_TAILSCALE=0
if [[ "${1:-}" == "--install-tailscale" ]]; then
    INSTALL_TAILSCALE=1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="streamer"
INSTALL_PREFIX="/opt/streamer"
CONFIG_DIR="/etc/streamer"
STATE_DIR="/var/lib/streamer"
SERVICE_FILE="/etc/systemd/system/streamer.service"
SUDOERS_FILE="/etc/sudoers.d/streamer"

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        echo "ERROR: this installer must be run as root (sudo bash $0)" >&2
        exit 1
    fi
}

log() { printf '[install] %s\n' "$*"; }

check_platform() {
    if [[ ! -e /proc/device-tree/model ]]; then
        log "WARNING: /proc/device-tree/model missing; cannot confirm platform"
        return
    fi
    local model
    model=$(tr -d '\0' </proc/device-tree/model)
    log "Detected platform: ${model}"
    if [[ "${model}" != *"Raspberry Pi 5"* ]]; then
        log "WARNING: this project targets Raspberry Pi 5. Detected '${model}'."
        log "Continuing anyway, but picamera2 / CSI behaviour may differ."
    fi
}

install_apt_deps() {
    log "Updating apt and installing system dependencies"
    DEBIAN_FRONTEND=noninteractive apt-get update -y
    # iputils-ping: used by streamer.modem for the LTE reachability probe.
    # util-linux: provides /usr/sbin/rtcwake (ships in the default Pi OS
    # image but we list it for clarity and to catch slimmed installs).
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        python3-picamera2 \
        libcamera-tools \
        iputils-ping \
        util-linux \
        ca-certificates \
        curl
}

install_tailscale() {
    if (( INSTALL_TAILSCALE == 0 )); then
        log "Skipping Tailscale install (pass --install-tailscale to enable)"
        return
    fi
    if command -v tailscale >/dev/null 2>&1; then
        log "Tailscale already installed"
        return
    fi
    log "Installing Tailscale"
    curl -fsSL https://tailscale.com/install.sh | sh
    log "Run 'sudo tailscale up' to authenticate with your tailnet"
}

create_user() {
    if ! id "${USER_NAME}" >/dev/null 2>&1; then
        log "Creating system user ${USER_NAME}"
        useradd --system --home-dir "${INSTALL_PREFIX}" --shell /usr/sbin/nologin \
            --groups video "${USER_NAME}"
    else
        log "User ${USER_NAME} already exists"
        usermod -aG video "${USER_NAME}" || true
    fi
}

create_directories() {
    log "Creating ${INSTALL_PREFIX}, ${CONFIG_DIR}, and ${STATE_DIR}"
    install -d -o "${USER_NAME}" -g "${USER_NAME}" -m 0755 "${INSTALL_PREFIX}"
    install -d -o root -g root -m 0755 "${CONFIG_DIR}"
    # State directory: holds the sleep_enabled.json override file (and
    # whatever else the runtime needs to persist across restarts). Mode
    # 0750 so only the streamer user and root can read it.
    install -d -o "${USER_NAME}" -g "${USER_NAME}" -m 0750 "${STATE_DIR}"
}

install_sudoers() {
    # The streamer user needs to invoke rtcwake (set the RTC alarm) and
    # systemctl poweroff (initiate clean shutdown) to perform HARD_SLEEP.
    # The sudoers.d entry below is the minimum that allows exactly those
    # two commands without a password and nothing else. ``visudo -cf``
    # validates syntax before the file goes live so a typo can't lock
    # the system out of sudo.
    log "Installing sudoers entry at ${SUDOERS_FILE}"
    local tmp
    tmp=$(mktemp)
    cat > "${tmp}" <<EOF
# Streamer HARD_SLEEP privileges. Managed by scripts/install.sh — do not
# edit by hand; re-running the installer rewrites this file.
${USER_NAME} ALL=(root) NOPASSWD: /usr/sbin/rtcwake, /usr/bin/systemctl poweroff
EOF
    if ! visudo -cf "${tmp}" >/dev/null; then
        log "ERROR: generated sudoers file failed visudo check; aborting"
        rm -f "${tmp}"
        exit 1
    fi
    install -o root -g root -m 0440 "${tmp}" "${SUDOERS_FILE}"
    rm -f "${tmp}"
}

# Replace one TOML string-valued key on a line of the form
#     <key> = "<placeholder>"
# with a freshly generated random token, only if the line still contains
# the literal placeholder. Idempotent across re-runs (a customised value
# is left alone). Uses Python for the rewrite rather than sed so it
# works on both GNU and BSD platforms and so the token never appears in
# argv.
#
# Arguments: $1 = config path, $2 = key name, $3 = placeholder value.
# Prints the substituted token on stdout (empty string if no change made).
rotate_placeholder_token() {
    local cfg="$1"
    local key="$2"
    local placeholder="$3"
    CFG="${cfg}" KEY="${key}" PLACEHOLDER="${placeholder}" python3 - <<'PY'
import os
import re
import secrets
import sys

cfg = os.environ["CFG"]
key = os.environ["KEY"]
placeholder = os.environ["PLACEHOLDER"]

with open(cfg, "r", encoding="utf-8") as fh:
    text = fh.read()

pattern = re.compile(
    r'^(?P<lead>[ \t]*' + re.escape(key) + r'[ \t]*=[ \t]*)"'
    + re.escape(placeholder) + r'"[ \t]*$',
    re.MULTILINE,
)
if not pattern.search(text):
    print("")
    sys.exit(0)

token = secrets.token_urlsafe(32)
new_text, count = pattern.subn(lambda m: m.group("lead") + f'"{token}"', text)
if count == 0:
    print("")
    sys.exit(0)

tmp = cfg + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(new_text)
os.replace(tmp, cfg)
print(token)
PY
}

install_config() {
    local cfg="${CONFIG_DIR}/streamer.toml"
    if [[ ! -f "${cfg}" ]]; then
        log "Installing default config to ${cfg}"
        install -o root -g root -m 0640 \
            "${REPO_ROOT}/config/streamer.toml" \
            "${cfg}"
    else
        log "Existing ${cfg} preserved; rotating placeholder token if present"
    fi

    chown root:"${USER_NAME}" "${cfg}"
    chmod 0640 "${cfg}"

    local auth_token
    auth_token="$(rotate_placeholder_token "${cfg}" "auth_token" "change-me")"

    if [[ -n "${auth_token}" ]]; then
        log "Generated random auth_token in ${cfg}"
        AUTH_TOKEN_GENERATED="${auth_token}"
    else
        log "auth_token already customised; leaving as-is"
    fi
}

setup_venv() {
    # We deliberately do venv creation and pip install AS ROOT, not as
    # the streamer system user. The cloned repo usually sits under
    # /home/<some-user>/... which is mode 700/750 on Pi OS, so the
    # streamer system user cannot read it. Running pip as that user
    # then fails with a confusing "Invalid requirement / File does not
    # exist" because pip can't see the source tree. We chown the
    # install prefix back to streamer at the end; file ownership only
    # matters for writes, and the venv binaries are world-
    # readable/executable.
    if [[ ! -d "${INSTALL_PREFIX}/.venv" ]]; then
        log "Creating venv at ${INSTALL_PREFIX}/.venv (with --system-site-packages)"
        python3 -m venv --system-site-packages "${INSTALL_PREFIX}/.venv"
    else
        log "Reusing existing venv at ${INSTALL_PREFIX}/.venv"
    fi
    log "Upgrading pip"
    "${INSTALL_PREFIX}/.venv/bin/pip" install --upgrade pip
    log "Installing Streamer from ${REPO_ROOT}"
    "${INSTALL_PREFIX}/.venv/bin/pip" install "${REPO_ROOT}"
    log "Setting ownership of ${INSTALL_PREFIX} to ${USER_NAME}"
    chown -R "${USER_NAME}:${USER_NAME}" "${INSTALL_PREFIX}"
}

install_systemd_unit() {
    log "Installing systemd unit at ${SERVICE_FILE}"
    install -o root -g root -m 0644 "${REPO_ROOT}/scripts/streamer.service" "${SERVICE_FILE}"
    systemctl daemon-reload
    systemctl enable streamer.service
    log "Use 'systemctl start streamer' to start the service"
    log "Use 'journalctl -u streamer -f' to follow logs"
}

check_camera_overlays() {
    local config_txt="/boot/firmware/config.txt"
    if [[ ! -f "${config_txt}" ]]; then
        log "WARNING: ${config_txt} not found; cannot check CSI overlays"
        return
    fi
    local seen_cam0=0 seen_cam1=0
    if grep -Eq '^dtoverlay=imx708,cam0' "${config_txt}"; then seen_cam0=1; fi
    if grep -Eq '^dtoverlay=imx708,cam1' "${config_txt}"; then seen_cam1=1; fi
    if (( seen_cam0 && seen_cam1 )); then
        log "Both Pi Camera 3 dtoverlays already present in config.txt"
    else
        log "NOTE: did not find explicit dtoverlays for both Pi Camera 3 modules in ${config_txt}."
        log "If both cameras don't enumerate with 'rpicam-hello --list-cameras', add:"
        log "    camera_auto_detect=0"
        log "    dtoverlay=imx708,cam0"
        log "    dtoverlay=imx708,cam1"
        log "to the end of ${config_txt} and reboot."
    fi
}

print_token_summary() {
    if [[ -z "${AUTH_TOKEN_GENERATED:-}" ]]; then
        return
    fi
    printf '\n'
    log "================================================================"
    log "AUTH TOKEN"
    log "Freshly generated and written to ${CONFIG_DIR}/streamer.toml."
    log "Save it somewhere safe; it is not displayed again."
    log "----------------------------------------------------------------"
    log "  auth_token:"
    log "      ${AUTH_TOKEN_GENERATED}"
    log "  Shareable URLs (replace <host> with this Pi's tailnet name):"
    log "      http://<host>:8080/cam0?key=${AUTH_TOKEN_GENERATED}"
    log "      http://<host>:8080/cam1?key=${AUTH_TOKEN_GENERATED}"
    log "================================================================"
}

main() {
    require_root
    check_platform
    install_apt_deps
    install_tailscale
    create_user
    create_directories
    install_sudoers
    install_config
    setup_venv
    install_systemd_unit
    check_camera_overlays
    log "Install complete. Next steps:"
    log "  1. (If needed) update ${CONFIG_DIR}/streamer.toml then reboot"
    log "  2. Copy media assets if not already present:"
    log "       Hedge-icon.png and the PS20-...-alpha.png background into"
    log "       ${INSTALL_PREFIX}/.venv/lib/python*/site-packages/streamer/webui/media/"
    log "  3. Run: sudo tailscale up   (if installed and not yet joined)"
    log "  4. Run: sudo systemctl start streamer"
    log "  5. Browse to http://<this-host-tailnet-name>:8080/"
    print_token_summary
}

main "$@"
