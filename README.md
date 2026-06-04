# Streamer

Solar-powered, low-bandwidth MJPEG streamer for a Raspberry Pi 5 with two
Pi Camera 3 modules. The browser sees a live view of either camera at a
configurable framerate (default 1 fps) by visiting `/cam0` or `/cam1`.
Everything goes over HTTPS/TCP — the public delivery path has no UDP,
no WebRTC, no client-side polling state machine.

This project supersedes the earlier WebRTC-based **DualStream** prototype.
The installer detects and disables the legacy `dualstream.service` if it
finds one, leaving its venv and config in place for rollback.

## What you get

- `GET /cam0`, `GET /cam1` — branded per-camera viewer pages
- `GET /stream/cam0`, `/stream/cam1` — `multipart/x-mixed-replace` MJPEG
  served at the configured framerate; one capture loop per connection
- `GET /api/status` — JSON: version, mode, per-camera state, active
  stream count
- `GET /api/info` — JSON: site name + per-camera display labels (used
  by the viewer pages for branding)
- `GET /health` — liveness probe (no auth)
- Branded landing page at `/`

Single bearer token gates `/api/*` and `/stream/*`. Token may be
delivered as `Authorization: Bearer <token>` or as a `?key=<token>` URL
query parameter (so plain `<img src=...>` works).

## Architecture

```
Browser  <-- multipart/x-mixed-replace, ~1 fps -->  aiohttp StreamResponse
                                                            |
                                          per-connection capture loop
                                                            |
                                          ---  refcount  acquire/release  ---
                                                            |
                                              picamera2 (BGR888, buf=2)
```

The sensor's `FrameDurationLimits` is driven by `stream.framerate`, so
producer and consumer run at the same cadence and the libcamera buffer
pool never accumulates stale frames. A capture timeout (2 s) triggers
in-stream recovery: the camera is marked broken, released, and re-
acquired — the next acquire reopens a fresh picamera2 instance without
breaking the user's HTTP connection.

## Hardware

- Raspberry Pi 5 with the `imx708,cam0` + `imx708,cam1` dtoverlays
  enabled in `/boot/firmware/config.txt`
- Two Pi Camera 3 modules
- For the field deployment: 100 W solar panel + 12 V 18 Ah LiFePO4
  battery + RTC battery on J5 BAT (used in Phase 2 for `HARD_SLEEP`)
- LTE uplink (e.g. Linovision IOT-R41) and Tailscale for remote access

## Installation

```bash
# On a fresh Pi 5 (or upgrading from DualStream):
git clone <repo-url> Streamer
cd Streamer
sudo bash scripts/install.sh --install-tailscale   # omit flag if tailscale already installed

# Copy media assets (artwork, favicon). These ship in DualStream and are
# not in this repo; place them where the installed package can see them:
sudo cp /path/to/Hedge-icon.png \
    /opt/streamer/.venv/lib/python*/site-packages/streamer/webui/media/
sudo cp /path/to/PS20-hedgework-ARTWORK-JohannaKindvall-10-alpha.png \
    /opt/streamer/.venv/lib/python*/site-packages/streamer/webui/media/

sudo tailscale up   # if not already joined
sudo systemctl start streamer
sudo journalctl -u streamer -f
```

The installer prints a freshly generated `auth_token` and the two
shareable URLs at the end of its run. Save them — they are not
displayed again.

### Updating

After `git pull`, run:

```bash
sudo bash scripts/update.sh
```

This refreshes the venv install, updates the systemd unit if it
changed, and restarts the service with explicit stop / kill / start so
a wedged python process can't drag systemd's default 90 s timeout.

### Rolling back to DualStream

```bash
sudo systemctl stop streamer
sudo systemctl disable streamer
sudo systemctl enable --now dualstream
```

The DualStream venv and config under `/opt/dualstream/` and
`/etc/dualstream/` are left in place by the Streamer installer so this
works without re-cloning anything.

## Configuration

Default config: `/etc/streamer/streamer.toml` (created by the installer,
mode `0640 root:streamer`).

```toml
[server]
host = "0.0.0.0"
port = 8080
auth_token = "..."           # auto-generated on first install
site_name = "HEDGEWORK @ PS 20"

[camera0]
resolution = [1280, 720]
controls   = {}              # picamera2 sensor controls passthrough
name       = ""              # e.g. "Pasture View"; falls back to "Camera 0"

[camera1]
resolution = [1280, 720]
controls   = {}
name       = ""

[stream]
framerate    = 1.0           # 0.25..15; sensor matches this rate
jpeg_quality = 75            # 1..95

[power]
disable_act_led    = true    # best-effort; needs root in Phase 1
idle_grace_seconds = 10      # how long a refcount=0 camera lingers
```

### Bandwidth and power

- ~80 KB per frame at 1280x720 JPEG quality 75
- ~640 kbps per active viewer at 1 fps
- Camera draws zero power when no viewer is connected (refcount=0,
  picamera2 stopped after `idle_grace_seconds`)
- Two viewers on the same camera share the single picamera2 instance

### CSI overlays

If `rpicam-hello --list-cameras` shows only one camera, append the
following to `/boot/firmware/config.txt` and reboot:

```
camera_auto_detect=0
dtoverlay=imx708,cam0
dtoverlay=imx708,cam1
```

## Operations

```bash
# Service health
sudo systemctl status streamer
sudo journalctl -u streamer -f

# Programmatic status
TOKEN=$(sudo grep '^auth_token' /etc/streamer/streamer.toml | cut -d'"' -f2)
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/status | python3 -m json.tool

# Verify the MJPEG endpoint headers
curl -s -I -H "Authorization: Bearer $TOKEN" http://localhost:8080/stream/cam0
# Expect: HTTP/1.1 200 OK, Content-Type: multipart/x-mixed-replace; boundary=frame

# Update after pulling code changes
sudo bash scripts/update.sh
```

## Phase 2 (planned)

- Scheduled `HARD_SLEEP` via `rtcwake`: time-of-day + astral sunrise/
  sunset windows. The RTC battery on the Pi 5 J5 BAT header makes the
  full-halt strategy possible.
- `mode` field in `/api/status` (`AWAKE` / `ASLEEP`).
- `POST /api/admin/sleep-enabled` toggle for indefinite sleep
  suppression, persisted to a file so it survives restart.
- LTE modem ping probe surfaced in `/api/status`.
- `/stream/*` returns `503 SLEEPING` when the mode state machine has
  declared the Pi asleep (mostly relevant in `dry_run` since a real
  HARD_SLEEP halts the Pi).

None of these change the streaming surface; they layer on top of the
existing `cameras.py` and `server.py` cleanly.
