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
- `GET /embed/cam0`, `/embed/cam1` — per-camera cross-origin embed
  demo + docs (snapshot, fullscreen, sleep fallback)
- `GET /embed` — legacy dual-tile embed snippet (deprecated)
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

### Self power-cycle recovery

Field experience with CSI-over-Cat6 camera extenders (THSER102A):
the SerDes video link can wedge in a way that survives service
restarts and even warm reboots — the sensor still answers on I2C
("Configuration successful" in the logs) but no frames ever reach
the CFE (`Dequeue timer expired`). Only a genuine power cut
re-trains the link. With `POWER_OFF_ON_HALT=1` in the EEPROM,
`rtcwake + poweroff` *is* a genuine power cut — the same mechanism
the nightly sleep already uses.

When `power.recovery_power_cycle = true`, the service uses that to
rescue itself: a camera that fails `recovery_failure_threshold`
consecutive in-stream recovery attempts (each ~16 s apart, so the
default 5 ≈ 80 s of confirmed wedge) triggers an RTC alarm
`recovery_wake_delay_seconds` out followed by a poweroff. Total
outage is ~2–3 minutes, after which the Pi boots with freshly
re-trained camera links.

Guardrails:

- At most `recovery_max_cycles_per_day` self-cycles per calendar
  day (persisted to `/var/lib/streamer/recovery_cycles.json`, so
  the counter survives the power cycles it counts). Once exhausted
  the service stays up and serves whichever cameras still work.
- No self-cycle within `recovery_boot_grace_minutes` of service
  start: a camera that is dead (not just wedged) must not boot-loop
  the Pi and drain the battery.
- Never during `ENTERING_SLEEP`/`ASLEEP` — the sunset path owns
  power there, and the wedge gets its power cut at sunset anyway.
- `power.dry_run = true` logs the decision but skips the poweroff.

Current cycle count is visible under `power.recovery` in
`GET /api/status`.

Validation status: the trigger logic and the self-power-off /
self-wake mechanics are proven, but the *cure* is not yet — every
confirmed wedge so far was cleared by a physical unplug, and the
rtcwake path (electrically equivalent, ~2 min rails-down) has not
yet been demonstrated against a live wedge. The feature therefore
ships disabled. Run the playbook below on the first field wedge
before enabling it.

### Wedged-camera playbook

When a camera stops streaming in the field, work through this on
the Pi (SSH in over Tailscale):

```bash
# 1. Confirm the wedge signature: recovery attempts looping with
#    "Dequeue timer expired" / "marked broken", while the sensor
#    still configures successfully (I2C alive, CSI dead).
journalctl -u streamer -b 0 | grep -iE "dequeue|broken|timeout" | tail -10

# 2. Rule out the cheap fixes first. A service restart rebuilds the
#    picamera2 instances; a warm reboot resets the SoC. Neither cuts
#    power to the camera links, so a true SerDes wedge survives both.
sudo systemctl restart streamer    # wait ~30 s, then re-test stream
sudo reboot                        # if restart didn't help

# 3. Manual power-cycle test: does a poweroff cycle cure the wedge?
#    With POWER_OFF_ON_HALT=1 this genuinely cuts the rails; the RTC
#    alarm brings the Pi back ~2 minutes later.
sudo rtcwake -m no -t $(date -d '+2 minutes' +%s)
sudo systemctl poweroff

# 4. After the Pi self-powers back on (~2-3 min), test the stream:
curl -sS --max-time 25 -o /dev/null \
  -w 'cam1: %{size_download} bytes\n' http://localhost:8080/stream/cam1
```

Interpreting step 4:

- **Streams again** → the rtcwake power cycle cures a real wedge.
  Enable the automated version (`recovery_power_cycle = true` in
  `/etc/streamer/streamer.toml`, then
  `sudo systemctl restart streamer`) so future wedges self-heal
  without a site visit.
- **Still 0 bytes** → only a physical unplug re-trains this link;
  do not enable the automated recovery (it would burn its daily
  cycle budget for nothing). The fix is at the hardware layer:
  reseat/replace the flex cables and extender pair on the affected
  camera (a marginal FFC contact has been the root cause before).

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
- A per-camera embed is published at `/embed/cam0` and
  `/embed/cam1` (docs + live demo). Each page gets its own widget
  with snapshot and fullscreen controls. A WordPress shortcode plugin
  lives in `wordpress/`; alternatively upload `embed-cam.css` and
  `embed-cam.js` to your CMS.
- The legacy dual-tile snippet at `/embed` still works but is not
  recommended for new pages.

To deploy (one embed per camera page):

1. Set `public_streams = true` in `streamer.toml` and restart the
   service.
2. Ensure the Pi is reachable on a stable public URL (Tailscale
   Funnel and Cloudflare Tunnel both work).
3. **WordPress plugin (recommended):** copy `wordpress/` to
   `wp-content/plugins/hedgework-cam-embed/`, activate, and add a
   shortcode per camera page:
   ```
   [hedgework_cam camera="0" pi_url="https://hedgebuggy.tailfoo.ts.net"]
   [hedgework_cam camera="1" pi_url="https://hedgebuggy.tailfoo.ts.net"]
   ```
4. **Custom HTML:** upload `embed-cam.css` and `embed-cam.js` from
   `src/streamer/webui/` (or fetch from `/static/` on the Pi) to your
   WordPress media library, then paste a mount `<div>` plus `<link>`
   and `<script>` tags on each camera page. See `/embed/cam0` for the
   full example.

Set `data-pi-url` / shortcode `pi_url` to your Pi's public URL (no
trailing slash). Set `data-camera` / shortcode `camera` to `0` or `1`.

The widget polls `/api/public/status` every 20 s (configurable via
`data-poll-interval-ms` on the mount div or shortcode
`poll_interval_ms`) and switches between three states:

- **AWAKE** — live MJPEG via `<img src>`. Snapshot saves the current
  frame as a JPEG download; fullscreen expands the video stage.
- **ENTERING_SLEEP** — stream stays live; a yellow banner counts
  down to the impending sleep transition.
- **ASLEEP or Pi unreachable** — the `<img>` is replaced with a
  dimmed card showing the next scheduled wake. The hosted page
  itself never becomes broken; only the live frame portion goes
  dark, and only until the next sunrise.

Because the real sleep path powers the Pi (and its USB-powered LTE
router) fully off, the status endpoint is unreachable overnight —
the page can't *ask* whether the Pi is asleep. To bridge that, the
widget caches the upcoming sleep/wake window (published as
`schedule` on `/api/public/status`) in `localStorage` on every
successful poll. When a poll then fails and the clock falls inside
the cached window (day-shifted for returning visitors, capped at a
week of staleness), the card shows "Camera is asleep — next wake
…" instead of the generic offline card. A brand-new visitor whose
browser has never reached the Pi sees the offline card until
morning; everyone else gets the friendly message.

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
max_duration_seconds = 3600  # per-viewer MJPEG cap; 0 = unlimited
                             # embed shows "Watch live" after — no auto-reconnect

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

# Self power-cycle recovery (see "Self power-cycle recovery" above).
recovery_power_cycle        = false  # opt-in; requires POWER_OFF_ON_HALT=1
recovery_failure_threshold  = 5      # consecutive failed recoveries (~16 s apart)
recovery_max_cycles_per_day = 3      # hard daily cap, persisted across cycles
recovery_boot_grace_minutes = 10     # no self-cycle this soon after start
recovery_wake_delay_seconds = 120    # RTC alarm lead before poweroff

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

### Tuning the cameras

The `controls` field on each `[camera*]` block is a dict of
[libcamera/picamera2 controls](https://libcamera.org/api-html/namespacelibcamera_1_1controls.html)
that get applied when the camera starts. Leave it empty for sensible
defaults (continuous autofocus, auto exposure, auto white balance).
The two most useful overrides for fixed-mount cameras are manual
focus and exposure clamps.

**Manual focus** (e.g. for a close-range bird-feeder cam at ~7 inches):

```toml
[camera0]
controls = { AfMode = 0, LensPosition = 5.6 }
```

- `AfMode`: `0` = Manual, `1` = single Auto on first frame, `2` =
  Continuous (default when unset).
- `LensPosition`: focus distance in **diopters** (1 / distance in
  metres). The IMX708 lens covers ~10 cm (LP=10) to infinity (LP=0).
  Common targets:

  | Distance       | LensPosition |
  |----------------|-------------:|
  | 6 in (15 cm)   |          6.6 |
  | 7 in (18 cm)   |          5.6 |
  | 8 in (20 cm)   |          4.9 |
  | 30 cm          |          3.3 |
  | 1 m            |          1.0 |
  | infinity       |          0.0 |

  Formula: `LensPosition = 1 / (distance_in_inches × 0.0254)`.

**Dialing in the value empirically.** Rather than guess, stop the
service and snap test shots with `libcamera-still`:

```bash
sudo systemctl stop streamer

for lp in 4.9 5.3 5.6 6.0 6.6; do
  libcamera-still --camera 0 --autofocus-mode manual \
    --lens-position $lp --width 1280 --height 720 -n \
    -o /tmp/cam0_lp${lp}.jpg
done

# scp /tmp/cam0_lp*.jpg back to your workstation, pick the sharpest,
# put its value in streamer.toml, then:
sudo systemctl start streamer
```

After restarting the service, the lens motor will click briefly as
it moves to the configured position.

**Other useful controls** (passed through identically):

- `ExposureTime` (integer microseconds) — pin shutter speed for
  consistent motion blur across lighting changes.
- `AnalogueGain` (float) — pin ISO. Combine with `ExposureTime` for
  a fully manual exposure.
- `AwbEnable = false` + `ColourGains = [red, blue]` — pin white
  balance. Useful when the scene's average colour throws AWB off
  (lots of green foliage, etc.).
- `Brightness`, `Contrast`, `Saturation`, `Sharpness` — image
  cosmetics in the range `-1.0..+1.0` (`0.0..32.0` for `Sharpness`).

A full list lives in the libcamera control IDs documentation linked
above; everything you pass in `controls = {…}` is forwarded verbatim
to `picam2.create_video_configuration(controls=…)`.

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
  `/api/public/status` + `/stream/*` with CORS headers, plus per-camera
  widgets at `/embed/cam0` and `/embed/cam1` (WordPress shortcode in
  `wordpress/`).

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
