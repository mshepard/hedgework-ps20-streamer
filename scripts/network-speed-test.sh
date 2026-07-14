#!/usr/bin/env bash
set -euo pipefail

TEST_MB="${TEST_MB:-10}"
TARGET="${TARGET:-1.1.1.1}"

# Override with: IFACE=eth1 ./network-speed-test.sh
IFACE="${IFACE:-$(ip route get "$TARGET" |
  awk '{for (i=1; i<=NF; i++) if ($i=="dev") {print $(i+1); exit}}')}"

if [[ -z "$IFACE" ]]; then
  echo "Could not determine the active network interface." >&2
  exit 1
fi

GATEWAY="$(ip route show default dev "$IFACE" |
  awk '/default/ {print $3; exit}')"

echo "Interface: $IFACE"
ip -brief address show dev "$IFACE"
echo
echo "Route:"
ip route get "$TARGET"
echo

if [[ -n "$GATEWAY" ]]; then
  echo "Gateway latency:"
  ping -I "$IFACE" -c 10 "$GATEWAY" || true
  echo
fi

echo "Internet latency:"
ping -I "$IFACE" -c 10 "$TARGET" || true
echo

echo "Testing ${TEST_MB} MB download..."
DOWNLOAD_BPS="$(
  curl --interface "$IFACE" \
    --fail --silent --show-error \
    --connect-timeout 15 --max-time 180 \
    --output /dev/null \
    --write-out '%{speed_download}' \
    "https://speed.cloudflare.com/__down?bytes=$((TEST_MB * 1000000))"
)"

awk -v bps="$DOWNLOAD_BPS" \
  'BEGIN {printf "Download: %.2f Mbps\n", bps * 8 / 1000000}'

TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT
dd if=/dev/zero of="$TMP_FILE" bs=1000000 count="$TEST_MB" status=none

echo "Testing ${TEST_MB} MB upload..."
UPLOAD_BPS="$(
  curl --interface "$IFACE" \
    --fail --silent --show-error \
    --connect-timeout 15 --max-time 180 \
    --output /dev/null \
    --write-out '%{speed_upload}' \
    -H "Content-Type: application/octet-stream" \
    --data-binary "@$TMP_FILE" \
    "https://speed.cloudflare.com/__up"
)"

awk -v bps="$UPLOAD_BPS" \
  'BEGIN {printf "Upload:   %.2f Mbps\n", bps * 8 / 1000000}'