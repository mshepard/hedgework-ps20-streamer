"use strict";

// Hedgerow single-camera cross-origin embed.
//
// Mount point: <div class="hedgework-cam-embed-single"
//   data-pi-url="https://your-pi.example"
//   data-camera="0"
//   data-poll-interval-ms="20000"></div>
//
// Requires [server] public_streams = true on the Pi. Polls
// /api/public/status for sleep state; streams MJPEG into an <img>.
// Snapshot captures the current frame via canvas; fullscreen toggles
// the stage element.
//
// Streams do not auto-reconnect after they end (server max-duration
// timeout, sleep, or connection drop). The visitor must press
// "Watch live" — so a forgotten tab does not hold the camera open.

(function () {
  const SCHEDULE_CACHE_KEY = "hcamScheduleV1";
  const SCHEDULE_CACHE_MAX_AGE_MS = 7 * 24 * 3600 * 1000;

  const mounts = document.querySelectorAll(".hedgework-cam-embed-single");
  mounts.forEach((root) => {
    if (!root.dataset.camera) {
      const m = window.location.pathname.match(/\/embed\/cam(\d+)/);
      if (m) root.dataset.camera = m[1];
    }
    initOne(root);
  });

  function initOne(root) {
    const dataUrl = (root.dataset.piUrl || "").trim().replace(/\/+$/, "");
    const piUrl = dataUrl || window.location.origin;
    const pollMs = parseInt(root.dataset.pollIntervalMs || "20000", 10);
    const cameraNum = parseInt(root.dataset.camera ?? "", 10);
    if (!Number.isFinite(cameraNum) || cameraNum < 0) {
      root.textContent = "Camera embed: set data-camera to 0, 1, …";
      return;
    }

    const ui = buildScaffold(root, cameraNum);
    const state = {
      piUrl,
      pollMs,
      cameraNum,
      cameraName: `Camera ${cameraNum}`,
      status: null,
      error: null,
      live: false,
      // True on first page load; cleared when a stream ends so status
      // polls do not silently reopen a forgotten tab.
      watchRequested: true,
    };

    ui.watchBtn.addEventListener("click", () => {
      state.watchRequested = true;
      ui.watchBtn.classList.add("hidden");
      applyLive(state, ui);
    });
    ui.snapshotBtn.addEventListener("click", () => takeSnapshot(state, ui));
    ui.fullscreenBtn.addEventListener("click", () => toggleFullscreen(ui.stage));

    ui.img.addEventListener("error", () => {
      if (!state.live) {
        return;
      }
      state.watchRequested = false;
      applyPaused(state, ui);
    });
    ui.img.addEventListener("load", () => {
      ui.msg.classList.add("hidden");
      state.live = true;
      ui.watchBtn.classList.add("hidden");
    });

    poll(state, ui);
    setInterval(() => poll(state, ui), pollMs);
    setInterval(() => render(state, ui), 15_000);
  }

  function buildScaffold(root, cameraNum) {
    root.innerHTML = "";

    const banner = el("div", "hcs-banner hidden");
    const panel = el("div", "hcs-panel");
    const header = el("div", "hcs-header");
    const name = el("span", "hcs-name");
    name.textContent = `Camera ${cameraNum}`;
    const dot = el("span", "hcs-live-dot dim");
    header.appendChild(name);
    header.appendChild(dot);

    const stage = el("div", "hcs-stage");
    const img = document.createElement("img");
    img.alt = `Camera ${cameraNum} stream`;
    img.decoding = "async";
    img.referrerPolicy = "no-referrer";
    img.crossOrigin = "anonymous";
    img.className = "hidden";
    const msg = el("div", "hcs-stage-msg");
    const msgTitle = el("div", "hcs-msg-title");
    const msgSub = el("div", "hcs-msg-sub");
    msg.appendChild(msgTitle);
    msg.appendChild(msgSub);
    stage.appendChild(img);
    stage.appendChild(msg);

    const toolbar = el("div", "hcs-toolbar");
    const watchBtn = el("button", "hcs-btn hidden");
    watchBtn.type = "button";
    watchBtn.textContent = "Watch live";
    const snapshotBtn = el("button", "hcs-btn");
    snapshotBtn.type = "button";
    snapshotBtn.textContent = "Snapshot";
    snapshotBtn.disabled = true;
    const flash = el("span", "hcs-flash");
    const fullscreenBtn = el("button", "hcs-btn hcs-btn-icon");
    fullscreenBtn.type = "button";
    fullscreenBtn.title = "Toggle fullscreen";
    fullscreenBtn.setAttribute("aria-label", "Toggle fullscreen");
    fullscreenBtn.textContent = "⛶";
    toolbar.appendChild(watchBtn);
    toolbar.appendChild(snapshotBtn);
    toolbar.appendChild(flash);
    toolbar.appendChild(fullscreenBtn);

    panel.appendChild(header);
    panel.appendChild(stage);
    panel.appendChild(toolbar);

    const foot = el("div", "hcs-foot");

    root.appendChild(banner);
    root.appendChild(panel);
    root.appendChild(foot);

    return {
      root,
      banner,
      panel,
      name,
      dot,
      stage,
      img,
      msg,
      msgTitle,
      msgSub,
      watchBtn,
      snapshotBtn,
      flash,
      fullscreenBtn,
      foot,
    };
  }

  async function poll(state, ui) {
    const abort =
      "AbortSignal" in window && AbortSignal.timeout
        ? AbortSignal.timeout(15000)
        : undefined;
    try {
      const resp = await fetch(`${state.piUrl}/api/public/status`, {
        cache: "no-store",
        signal: abort,
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      state.status = await resp.json();
      state.error = null;
      saveScheduleCache(state.status.schedule);
      const cam = (state.status.cameras || []).find(
        (c) => c.camera_num === state.cameraNum,
      );
      if (cam && cam.display_name) state.cameraName = cam.display_name;
    } catch (e) {
      state.error = e.message || "unreachable";
    }
    render(state, ui);
  }

  function render(state, ui) {
    ui.name.textContent = state.cameraName;

    const mode = state.status ? state.status.mode : null;
    const next = state.status ? state.status.next_event : null;
    if (mode === "ENTERING_SLEEP" && next && next.at) {
      const mins = minutesUntil(next.at);
      ui.banner.textContent =
        `Camera going to sleep in ${mins} minute${mins === 1 ? "" : "s"} ` +
        `(at ${formatTime(next.at)})`;
      ui.banner.classList.remove("hidden");
    } else {
      ui.banner.classList.add("hidden");
    }

    const inferredWake = state.error ? inferAsleepWake() : null;

    if (state.error) {
      if (inferredWake) {
        applyAsleep(state, ui, { at: inferredWake });
        ui.foot.textContent = "";
        ui.snapshotBtn.disabled = true;
        return;
      }
      // A failed /api/public/status poll does not mean the MJPEG
      // stream is dead — tearing it down here caused visible
      // drop/reconnect cycles (especially on a busy Pi or slow LTE).
      if (!state.live) {
        applyOffline(state, ui);
        ui.foot.textContent =
          "Live feed currently unreachable. Will retry automatically.";
        ui.snapshotBtn.disabled = true;
        return;
      }
      ui.foot.textContent = "Status check failed; live stream continues.";
    } else {
      ui.foot.textContent = "";
    }

    if (mode === "ASLEEP") {
      applyAsleep(state, ui, next);
      ui.foot.textContent = "";
      ui.snapshotBtn.disabled = true;
      return;
    }

    if (!state.watchRequested) {
      applyPaused(state, ui);
      ui.foot.textContent = "";
      ui.snapshotBtn.disabled = true;
      return;
    }

    applyLive(state, ui);
    ui.foot.textContent = "";
    ui.snapshotBtn.disabled = !state.live;
  }

  function applyLive(state, ui) {
    ui.dot.classList.remove("dim");
    ui.watchBtn.classList.add("hidden");
    const want = `${state.piUrl}/stream/cam${state.cameraNum}`;
    if (ui.img.dataset.streamUrl !== want) {
      ui.msgTitle.textContent = "Loading…";
      ui.msgSub.textContent =
        "Starting camera. First frame can take a few seconds.";
      ui.msg.classList.remove("hidden");
      state.live = false;
      ui.snapshotBtn.disabled = true;

      ui.img.dataset.streamUrl = want;
      ui.img.src = `${want}?t=${Date.now()}`;
    } else if (state.live) {
      ui.msg.classList.add("hidden");
    }
    ui.img.classList.remove("hidden");
  }

  function applyPaused(state, ui) {
    state.live = false;
    ui.dot.classList.add("dim");
    ui.img.classList.add("hidden");
    ui.img.removeAttribute("src");
    delete ui.img.dataset.streamUrl;
    ui.msgTitle.textContent = "Stream paused";
    ui.msgSub.textContent = pausedMessage(state);
    ui.msg.classList.remove("hidden");
    ui.watchBtn.classList.remove("hidden");
    ui.snapshotBtn.disabled = true;
  }

  function pausedMessage(state) {
    const maxDur = state.status?.stream?.max_duration_seconds;
    if (maxDur && maxDur > 0) {
      const mins = Math.max(1, Math.round(maxDur / 60));
      return (
        `Live view ends after ${mins} minute${mins === 1 ? "" : "s"}. ` +
        "Press Watch live when you want to resume."
      );
    }
    return "Press Watch live when you want to resume.";
  }

  function applyAsleep(state, ui, next) {
    state.live = false;
    state.watchRequested = false;
    ui.dot.classList.add("dim");
    ui.img.classList.add("hidden");
    ui.img.removeAttribute("src");
    delete ui.img.dataset.streamUrl;
    ui.watchBtn.classList.add("hidden");
    ui.msgTitle.textContent = "Camera is asleep";
    ui.msgSub.textContent = next && next.at
      ? `Next wake: ${formatDateTime(next.at)}`
      : "Will return at the next scheduled wake.";
    ui.msg.classList.remove("hidden");
    ui.snapshotBtn.disabled = true;
  }

  function applyOffline(state, ui) {
    state.live = false;
    ui.dot.classList.add("dim");
    ui.img.classList.add("hidden");
    ui.img.removeAttribute("src");
    delete ui.img.dataset.streamUrl;
    ui.watchBtn.classList.add("hidden");
    ui.msgTitle.textContent = "Live feed unavailable";
    ui.msgSub.textContent = "Trying to reconnect to the camera service…";
    ui.msg.classList.remove("hidden");
    ui.snapshotBtn.disabled = true;
  }

  function takeSnapshot(state, ui) {
    ui.flash.textContent = "";
    const img = ui.img;
    if (!state.live || !img.naturalWidth) {
      ui.flash.textContent = "No frame yet — wait for the stream.";
      return;
    }
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    try {
      ctx.drawImage(img, 0, 0);
      canvas.toBlob(
        (blob) => {
          if (!blob) {
            ui.flash.textContent = "Could not save snapshot.";
            return;
          }
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          const slug = state.cameraName
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, "-")
            .replace(/^-|-$/g, "");
          const ts = new Date().toISOString().replace(/[:.]/g, "-");
          a.href = url;
          a.download = `${slug || "camera"}-${ts}.jpg`;
          a.click();
          URL.revokeObjectURL(url);
          ui.flash.textContent = "Saved.";
          setTimeout(() => {
            ui.flash.textContent = "";
          }, 2500);
        },
        "image/jpeg",
        0.92,
      );
    } catch (_) {
      ui.flash.textContent = "Snapshot blocked by browser security.";
    }
  }

  function toggleFullscreen(stage) {
    if (!document.fullscreenElement) {
      if (stage.requestFullscreen) stage.requestFullscreen();
    } else if (document.exitFullscreen) {
      document.exitFullscreen();
    }
  }

  function saveScheduleCache(schedule) {
    if (!schedule || !schedule.sleep_at || !schedule.wake_at) return;
    try {
      localStorage.setItem(
        SCHEDULE_CACHE_KEY,
        JSON.stringify({
          sleep_at: schedule.sleep_at,
          wake_at: schedule.wake_at,
          saved_at: Date.now(),
        }),
      );
    } catch (_) {
      /* best effort */
    }
  }

  function inferAsleepWake() {
    let cached;
    try {
      cached = JSON.parse(localStorage.getItem(SCHEDULE_CACHE_KEY));
    } catch (_) {
      return null;
    }
    if (!cached || !cached.sleep_at || !cached.wake_at) return null;
    if (
      !cached.saved_at ||
      Date.now() - cached.saved_at > SCHEDULE_CACHE_MAX_AGE_MS
    ) {
      return null;
    }

    let sleep = new Date(cached.sleep_at).getTime();
    let wake = new Date(cached.wake_at).getTime();
    const DAY_MS = 24 * 3600 * 1000;
    if (
      !isFinite(sleep) ||
      !isFinite(wake) ||
      wake <= sleep ||
      wake - sleep >= DAY_MS
    ) {
      return null;
    }

    const now = Date.now();
    while (wake < now) {
      sleep += DAY_MS;
      wake += DAY_MS;
    }
    if (now >= sleep && now < wake) {
      return new Date(wake).toISOString();
    }
    return null;
  }

  function el(tag, cls) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    return e;
  }

  function minutesUntil(iso) {
    const target = new Date(iso).getTime();
    return Math.max(0, Math.round((target - Date.now()) / 60000));
  }

  function formatTime(iso) {
    return new Date(iso).toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
    });
  }

  function formatDateTime(iso) {
    const d = new Date(iso);
    const now = new Date();
    const sameDay =
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate();
    if (sameDay) return formatTime(iso);
    return d.toLocaleString([], {
      weekday: "short",
      hour: "numeric",
      minute: "2-digit",
    });
  }
})();
