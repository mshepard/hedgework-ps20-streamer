#!/usr/bin/env bash
# Field health glance for the Streamer service (current boot only).
#
# Install on the Pi:
#   cp scripts/check-cam.sh ~/check-cam.sh && chmod +x ~/check-cam.sh
#
# Wedge cluster = 3+ capture-timeout events for one camera within 60 s.
# Counts both legacy stream-handler and FramePublisher log lines.

set -euo pipefail

echo "== $(date) =="

python3 <<'PY'
import re
import subprocess
from datetime import datetime

CLUSTER_MIN = 3
CLUSTER_WINDOW_S = 60
STORM_MARKED_BROKEN = 500
STORM_ACQUIRE_FAILS = 100

proc = subprocess.run(
    ["journalctl", "-u", "streamer", "-b", "0", "--no-pager", "-o", "short-iso"],
    capture_output=True,
    text=True,
    check=False,
)
lines = proc.stdout.splitlines()

TIMEOUT_RE = re.compile(
    r"(?:Capture timeout on camera |Publisher capture timeout on camera )(\d+)"
)
MARKED_BROKEN_RE = re.compile(r"Camera (\d+) marked broken")
ACQUIRE_FAIL_RE = re.compile(r"publisher\.cam(\d+): Publisher acquire failed")
FRONTEND_TIMEOUT = "Camera frontend has timed out"
ZERO_FRAME_CLOSE = "after 0 frames"

timeouts: dict[int, list[datetime]] = {0: [], 1: []}
marked_broken = {0: 0, 1: 0}
marked_broken_total = 0
acquire_fails = {0: 0, 1: 0}
frontend_timeouts = 0
zero_frame_closes = 0

TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{4})?\s"
)


def parse_ts(line: str) -> datetime | None:
    m = TS_RE.match(line)
    if not m:
        return None
    ts = m.group(1)
    off = m.group(2) or "+0000"
    try:
        return datetime.strptime(ts + off, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


def cluster_count(times: list[datetime]) -> int:
    if len(times) < CLUSTER_MIN:
        return 0
    times = sorted(times)
    clusters = 0
    i = 0
    while i < len(times):
        j = i + 1
        while j < len(times) and (times[j] - times[i]).total_seconds() <= CLUSTER_WINDOW_S:
            j += 1
        if j - i >= CLUSTER_MIN:
            clusters += 1
        i += 1
    return clusters


def max_consecutive(times: list[datetime], gap_s: float = 120.0) -> int:
    if not times:
        return 0
    times = sorted(times)
    best = cur = 1
    for a, b in zip(times, times[1:]):
        if (b - a).total_seconds() <= gap_s:
            cur += 1
            best = max(best, cur)
        else:
            cur = 1
    return best


for line in lines:
    if FRONTEND_TIMEOUT in line:
        frontend_timeouts += 1
    if ZERO_FRAME_CLOSE in line and "Stream closed" in line:
        zero_frame_closes += 1

    ts = parse_ts(line)
    if ts is None:
        continue

    m = TIMEOUT_RE.search(line)
    if m:
        cam = int(m.group(1))
        if cam in timeouts:
            timeouts[cam].append(ts)

    m = MARKED_BROKEN_RE.search(line)
    if m:
        cam = int(m.group(1))
        marked_broken_total += 1
        if cam in marked_broken:
            marked_broken[cam] += 1

    m = ACQUIRE_FAIL_RE.search(line)
    if m:
        cam = int(m.group(1))
        if cam in acquire_fails:
            acquire_fails[cam] += 1

for cam in (0, 1):
    clusters = cluster_count(timeouts[cam])
    peak = max_consecutive(timeouts[cam])
    print(f"cam{cam} wedge clusters today: {clusters} (peak {peak} consecutive)")

print(f"transient broken events: {marked_broken_total}")
print(
    f"  cam0 marked broken: {marked_broken[0]}  "
    f"cam1 marked broken: {marked_broken[1]}"
)
print(
    f"  cam0 publisher acquire fails: {acquire_fails[0]}  "
    f"cam1 publisher acquire fails: {acquire_fails[1]}"
)
print(f"  libcamera frontend timeouts: {frontend_timeouts}")
print(f"0-frame stream closes:         {zero_frame_closes}")

alerts: list[str] = []
for cam in (0, 1):
    if marked_broken[cam] >= STORM_MARKED_BROKEN:
        alerts.append(
            f"cam{cam}: {marked_broken[cam]} marked-broken events "
            f"(recovery storm — check for hard wedge)"
        )
    if acquire_fails[cam] >= STORM_ACQUIRE_FAILS:
        alerts.append(
            f"cam{cam}: {acquire_fails[cam]} publisher acquire failures "
            f"(pipeline not staying open)"
        )
if frontend_timeouts >= 10:
    alerts.append(
        f"{frontend_timeouts} libcamera frontend timeouts "
        f"(CSI frame delivery failing)"
    )

if alerts:
    print()
    print("ALERTS:")
    for a in alerts:
        print(f"  * {a}")
PY

echo
echo "Current mode + next event:"
if command -v jq >/dev/null 2>&1; then
    curl -sfS --max-time 5 http://localhost:8080/api/public/status \
        | jq '{mode, next_event}' 2>/dev/null \
        || echo "  (status fetch failed — is streamer running?)"
else
    curl -sfS --max-time 5 http://localhost:8080/api/public/status \
        || echo "  (status fetch failed — is streamer running?)"
fi

CONFIG=/etc/streamer/streamer.toml
if [[ -f "$CONFIG" ]]; then
    TOKEN=$(grep -E '^\s*auth_token\s*=' "$CONFIG" | head -1 | cut -d'"' -f2)
    if [[ -n "$TOKEN" ]]; then
        echo
        echo "Camera state:"
        curl -sfS --max-time 5 -H "Authorization: Bearer $TOKEN" \
            http://localhost:8080/api/status \
            | python3 -c "
import json, sys
d = json.load(sys.stdin)
for c in d.get('cameras', []):
    n = c['camera_num']
    name = c.get('display_name') or f'Camera {n}'
    print(f\"  cam{n} ({name}): running={c.get('running')} refcount={c.get('refcount')}\")
print(f\"  active_streams: {d.get('active_streams', 0)}\")
" 2>/dev/null || echo "  (api/status failed)"
    fi
fi
