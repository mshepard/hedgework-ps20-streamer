# WordPress plugin: Hedgerow Camera Embed

Single-camera live view with snapshot and fullscreen controls, for
embedding on a WordPress page that stays reachable when the Pi sleeps
overnight.

## Install

1. Copy this entire `wordpress/` folder to
   `wp-content/plugins/hedgework-cam-embed/` on your WordPress host.
2. Activate **Hedgerow Camera Embed** in the Plugins screen.
3. On your Pi, set `[server] public_streams = true` in `streamer.toml`
   and restart the streamer service.

## Usage

Add one shortcode per camera page:

```
[hedgework_cam camera="0" pi_url="https://your-pi.tailfoo.ts.net"]
```

```
[hedgework_cam camera="1" pi_url="https://your-pi.tailfoo.ts.net"]
```

Optional: `poll_interval_ms="20000"` (default 20 seconds).

## Without the plugin

Upload `embed-cam.css` and `embed-cam.js` from this folder to your
WordPress media library and paste a Custom HTML block:

```html
<link rel="stylesheet" href="/wp-content/uploads/embed-cam.css" />
<div
  class="hedgework-cam-embed-single"
  data-pi-url="https://your-pi.tailfoo.ts.net"
  data-camera="0"
></div>
<script src="/wp-content/uploads/embed-cam.js"></script>
```

See the Pi's `/embed/cam0` page for full documentation.
