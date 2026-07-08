#!/usr/bin/env bash
#
# Side-by-side dual-camera Hailo preview.
#
# Two rpicam-hello processes cannot share the Hailo device — the second
# one fails with "HailoRT not ready!". This wrapper runs a single-process
# Python preview that opens both cameras and both models on one VDevice.
#
# Usage:
#   bash scripts/dual_hailo_preview.sh --zoo
#   bash scripts/dual_hailo_preview.sh --zoo --headless --out-dir /tmp/dual_preview_out
#   bash scripts/dual_hailo_preview.sh --zoo --threshold1 0.3
#
# For a single-camera rpicam-hello smoke test (original one-process flow):
#   rpicam-hello -t 0 --camera 0 \
#     --post-process-file /usr/share/rpi-camera-assets/hailo_yolov8_inference.json
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_SCRIPT="${REPO_ROOT}/scripts/dual_hailo_preview.py"

if [[ ! -f "${PY_SCRIPT}" ]]; then
    echo "ERROR: missing ${PY_SCRIPT}" >&2
    exit 1
fi

if systemctl is-active --quiet streamer 2>/dev/null; then
    echo "ERROR: streamer service is running and holds the cameras." >&2
    echo "       Stop it first: sudo systemctl stop streamer" >&2
    exit 1
fi

PYTHON=""
for candidate in \
    "/opt/streamer/.venv/bin/python" \
    "${REPO_ROOT}/.venv/bin/python" \
    python3; do
    if command -v "${candidate}" >/dev/null 2>&1; then
        PYTHON="${candidate}"
        break
    fi
done

if [[ -z "${PYTHON}" ]]; then
    echo "ERROR: python3 not found" >&2
    exit 1
fi

exec "${PYTHON}" "${PY_SCRIPT}" "$@"
