# Streamer

Solar-powered, low-bandwidth MJPEG streamer for a Raspberry Pi 5 with two
Pi Camera 3 modules. The browser sees a live view of either camera at a
configurable framerate (default 1 fps) by visiting `/cam0` or `/cam1`.
Everything goes over HTTPS/TCP — the public delivery path has no UDP,
no WebRTC, no client-side polling state machine.

## What you get

- `GET /cam0`, `GET /cam1` — branded per-camera viewer pages
- `GET /stream/cam0`, `/stream/cam1` — `multipart/x-mixed-replace` MJPEG
  served at the configured framerate; one capture loop per connection.
  During the scheduled `ENTERING_SLEEP` window the response carries a
  `Warning: 299 - "sleeping in N minutes"` header that the viewer JS
  surfaces as a banner.
- `GET /api/status` — JSON: version, power mode, next schedule event,
  per-camera state, active stream count, LTE modem reachability
- `GET /api/info` — JSON: site name + per-camera display labels (used
  by the viewer pages for branding)
- `GET /api/public/status` — minimal CORS-friendly viewer state for
  cross-origin embeds (mode, next_event, site_name, cameras,
  stream.framerate); anonymous when `[server] public_streams = true`,
  token-gated otherwise
- `GET /embed` — copy-pasteable HTML/JS snippet that renders both
  camera tiles in an externally-hosted page (e.g. WordPress) with a
  friendly sleep / offline fallback
- `GET  /api/admin/sleep-enabled` — JSON: current sleep override state
- `POST /api/admin/sleep-enabled` — `{"enabled": bool}` to suppress
  (or re-enable) the schedule, persisted to
  `/var/lib/streamer/sleep_enabled.json`
- `GET /health` — liveness probe (no auth)
- Branded landing page at `/`

Single bearer token gates `/api/*` and `/stream/*` by default. Token
may be delivered as `Authorization: Bearer <token>` or as a
`?key=<token>` URL query parameter (so plain `<img src=...>` works).
With `[server] public_streams = true`, `/api/public/status` and the
two `/stream/*` endpoints become anonymously accessible with
permissive CORS headers so they can be `<img>`-embedded on a
third-party origin.

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
pool never accumulates stale frames. A capture timeout triggers
in-stream recovery: the camera is marked broken, released, and re-
acquired — the next acquire reopens a fresh picamera2 instance without
breaking the user's HTTP connection. The timeout is split: 15 s for
the first frame after a fresh acquire (libcamera AGC/AWB convergence
at 1 fps can take 5-10 s) and `max(2 s, 3 × frame_interval)` for
steady-state frames (tight enough to catch a real wedge fast).

At service boot both cameras are warmed in parallel and (with
`power.keep_cameras_warm = true`, the default) left running for the
lifetime of the service, so viewer connects never pay the cold-start
cost.

## Power state machine (Phase 2)

When `schedule.enabled = true`, a small state machine in
[src/streamer/power.py](src/streamer/power.py) drives the service
between three modes based on astronomical sunrise/sunset for the
configured `[location]`:

```
        AWAKE  ──(sunset − warn_minutes_before_sleep)──▶  ENTERING_SLEEP
          ▲                                                     │
          │                                                     │ (sunset)
          │                                                     ▼
   (RTC fires near sunrise,                                 ASLEEP
    Pi boots, fresh
    service starts)                                  (sudo rtcwake -m no -t <next_wake>
                                                      then sudo systemctl poweroff)
```

In `ENTERING_SLEEP` the stream keeps running but each `/stream/*`
response carries a `Warning` header that the viewer UI surfaces as
a "Sleeping in N minutes" banner.

In `ASLEEP` the manager releases the warmup-held camera refcounts,
cancels in-flight streams, sets the RTC alarm for the next sunrise
(minus `wake_lead_minutes`), and powers the Pi down. On wake the
system boots fresh; the service starts, recomputes the schedule,
and either resumes serving (if it's daytime now) or sleeps again
(if RTC fired early).

`power.dry_run = true` short-circuits the actual `rtcwake` /
`poweroff` calls. The state machine still transitions to ASLEEP and
`/stream/*` returns `503 SLEEPING`, so you can rehearse a sleep
cycle on a bench Pi without halting it.

The sleep override (`POST /api/admin/sleep-enabled` with
`{"enabled": false}`) suppresses sleep indefinitely: the state
machine stays in AWAKE regardless of the schedule. The value is
persisted to `/var/lib/streamer/sleep_enabled.json` and reloaded
across restarts and (real) wake cycles.

## Cross-origin embed

For the common deployment — Pi on solar/LTE, sleeping at night — the
two `/cam0` and `/cam1` viewer pages have one inherent limitation:
they're served from the Pi, so the URL itself goes offline when the
Pi powers off at sunset. If the page lives somewhere else (WordPress,
a static host, your own site) and only the *frames* come from the
Pi, the URL stays reachable 24/7 and visitors see a friendly
"Cameras asleep — back at 5:20 AM" card during the off hours.

Setting `[server] public_streams = true` enables this:

- `/api/public/status` and `/stream/cam{0,1}` become anonymously
  reachable (no `auth_token` required) and serve
  `Access-Control-Allow-Origin: *` so a page on another origin can
  poll the status endpoint and embed the streams as plain
  `<img src="...">` tags.
- The token-protected `/api/status`, `/api/info`, and
  `/api/admin/*` endpoints stay locked. Admins keep their bearer-
  token surface; only the read-only viewer endpoints open up.
- A copy-pasteable snippet is published at `/embed`. It renders a
  live two-tile demo using the same code you'd paste into your
  CMS, with sleep/offline fallbacks already wired in.

To deploy:

1. Set `public_streams = true` in `streamer.toml` and restart the
   service.
2. Ensure the Pi is reachable on a stable public URL (Tailscale
   Funnel and Cloudflare Tunnel both work; the embed JS uses
   absolute URLs against whatever you put in `data-pi-url`).
3. Open `https://<your-pi-url>/embed` and view-source. Copy
   everything between the `===== SNIPPET BEGIN` and
   `===== SNIPPET END` comments.
4. Paste into a WordPress Custom HTML block (or any plain
   `<div>`-rooted container).
5. Edit the mount `<div>`'s `data-pi-url` to your Pi's URL, e.g.
   `data-pi-url="https://hedgebuggy.tailfoo.ts.net"`.

The widget polls `/api/public/status` every 20 s (configurable via
`data-poll-interval-ms` on the mount div) and switches between
three states:

- **AWAKE** — both tiles render live MJPEG via `<img src>`. Browser
  handles the multipart parsing natively; no JS in the per-frame
  path.
- **ENTERING_SLEEP** — streams stay live; a yellow banner counts
  down to the impending sleep transition.
- **ASLEEP or Pi unreachable** — tiles replace their `<img>` with
  a dimmed card showing the next scheduled wake. The hosted page
  itself never becomes broken; only the live frame portion goes
  dark, and only until the next sunrise.

Security note: `public_streams = true` is a public-internet opt-in.
Treat the Funnel/Tunnel URL itself as a shared secret if you don't
want anyone-with-the-URL watching. There is no rate-limiting on the
stream endpoints in this release — viewers who hold the URL can
hold a connection indefinitely.

## Hardware

- Raspberry Pi 5 with the `imx708,cam0` + `imx708,cam1` dtoverlays
  enabled in `/boot/firmware/config.txt`
- Two Pi Camera 3 modules
- For the field deployment: 100 W solar panel + 12 V 18 Ah LiFePO4
  battery + RTC battery on J5 BAT (required for `HARD_SLEEP` wake)
- LTE uplink (e.g. Linovision IOT-R41) and Tailscale for remote access

## Installation

```bash
# On a fresh Pi 5:
git clone <repo-url> Streamer
cd Streamer
sudo bash scripts/install.sh --install-tailscale   # omit flag if tailscale already installed

# Copy media assets (artwork, favicon) into the installed package so
# the viewer pages can render with branding:
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

## Configuration

Default config: `/etc/streamer/streamer.toml` (created by the installer,
mode `0640 root:streamer`).

```toml
[server]
host = "0.0.0.0"
port = 8080
auth_token = "..."           # auto-generated on first install
site_name = "HEDGEWORK @ PS 20"
public_streams = false       # opt-in to anonymous /stream/* + /api/public/status
                             # for cross-origin embeds; see Cross-origin embed below

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
keep_cameras_warm  = true    # hold both cameras open from boot;
                             # eliminates ~5-10 s viewer cold-start
                             # latency at the cost of ~1 W idle power.
                             # Released automatically outside the
                             # active window by the Phase 2 power
                             # state machine.
dry_run            = false   # when true, the state machine still
                             # transitions to ASLEEP at sunset but
                             # never calls rtcwake / poweroff —
                             # /stream/* returns 503 SLEEPING.

# ----- Phase 2 (opt-in) -----

[location]                   # required when [schedule].enabled = true
latitude  = 0.0
longitude = 0.0
timezone  = "UTC"            # IANA tz name, e.g. "America/New_York"

[schedule]
enabled                   = false  # master opt-in for the power state machine
sunrise_offset_minutes    = 0      # +/- minutes around astronomical sunrise
sunset_offset_minutes     = 0      # +/- minutes around astronomical sunset
warn_minutes_before_sleep = 15     # ENTERING_SLEEP lead-time
wake_lead_minutes         = 5      # RTC fires this many minutes early

[network]
modem_probe_target           = "1.1.1.1"
modem_probe_interval_seconds = 60
modem_probe_timeout_seconds  = 5
```

When `[schedule].enabled = true` but `[location]` is still at its
default `(0, 0)`, the service logs a loud error at startup and runs
with the schedule disabled — refusing to compute sunrise/sunset for
the Null Island origin is safer than silently halting the Pi at the
wrong time. Set real coordinates and restart.

### Bandwidth and power

- ~80 KB per frame at 1280x720 JPEG quality 75
- ~640 kbps per active viewer at 1 fps
- With `power.keep_cameras_warm = true` (default), both cameras run
  continuously at ~0.5 W each. Set it to `false` to recover that
  power when no viewer is connected, at the cost of a ~5-10 s
  first-frame delay on cold viewer connects.
- N viewers on the same camera currently split the configured
  framerate — each one gets `framerate / N` fps because the
  per-camera capture executor is single-threaded and serializes
  `cam.capture()`. The sensor is still producing at the full rate;
  see Phase 2.5 / *shared frame fanout* for the planned fix.

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
# During ENTERING_SLEEP an extra `Warning: 299 - "sleeping in N minutes"` header
# also appears.

# Read the sleep override
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/admin/sleep-enabled

# Suppress scheduled sleep indefinitely (e.g. for a special-event night)
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"enabled": false}' \
     http://localhost:8080/api/admin/sleep-enabled

# Re-enable the schedule
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"enabled": true}' \
     http://localhost:8080/api/admin/sleep-enabled

# Test the cross-origin embed surface (no token; expects 200 when
# [server] public_streams = true, 401 otherwise).
curl -s -i http://localhost:8080/api/public/status | head -10
curl -s -i -X OPTIONS -H "Origin: https://ps20.hedgework.net" \
     http://localhost:8080/api/public/status | head -10

# Update after pulling code changes (also refreshes sudoers + state dir)
sudo bash scripts/update.sh
```

## Phase 2 (shipped)

Implemented in this release:

- Scheduled `HARD_SLEEP` via `rtcwake -m no -t <next_wake>` followed by
  `systemctl poweroff`. Astral-based; the active window is
  `[sunrise + sunrise_offset, sunset + sunset_offset]`.
- `mode` field in `/api/status`: `AWAKE` / `ENTERING_SLEEP` / `ASLEEP`.
- `next_event` field in `/api/status`: `{type: "sleep" | "wake", at: "..."}`.
- `POST /api/admin/sleep-enabled` toggle (and `GET` to read), persisted
  to `/var/lib/streamer/sleep_enabled.json` so it survives restart and
  HARD_SLEEP cycles.
- LTE modem ping probe in [src/streamer/modem.py](src/streamer/modem.py);
  result surfaces as the `modem` field of `/api/status`.
- `/stream/*` returns `503 SLEEPING` when the state machine has declared
  the Pi asleep (only observable in `dry_run`; the real path halts the
  Pi before the response could be sent).
- `/stream/*` carries `Warning: 299 - "sleeping in N minutes"` during
  `ENTERING_SLEEP`; the viewer JS surfaces it as a banner.
- Camera coupling: the state machine releases `keep_cameras_warm`
  refcounts on transition to ASLEEP, so the ~1 W camera-idle cost is
  only paid while the system is supposed to be serving frames.
- Sudoers entry installed at `/etc/sudoers.d/streamer` granting the
  service user exactly `/usr/sbin/rtcwake` and `/usr/bin/systemctl
  poweroff` (no other privileged commands).
- Cross-origin embed (`[server] public_streams = true`): anonymous
  `/api/public/status` + `/stream/*` with CORS headers, plus a
  copy-pasteable snippet at `/embed` for hosting both camera tiles
  on a third-party page.

## Phase 2.5 (planned)

- **Shared frame fanout per camera.** Replace the per-connection
  capture loop with a single per-camera capture-and-encode loop that
  publishes each finished JPEG to an `asyncio.Event` (or per-viewer
  queue). Stream handlers await the latest frame and write it out.
  N concurrent viewers on the same camera then each receive the full
  configured framerate (instead of `framerate / N`), with no extra
  camera work and no extra JPEG encoding. Estimated change: ~50
  lines across `cameras.py` (publisher) and `server.py` (subscriber
  in `_stream`); the public surface is unchanged.
