"use strict";

// Streamer per-camera viewer.
//
// MJPEG over HTTPS/TCP: Start sets <img>.src to /stream/camN?key=... and
// the browser's native multipart/x-mixed-replace handler updates the
// image in place as frames arrive. Stop clears src, which closes the
// underlying connection and releases the camera refcount on the server.
// No JavaScript polling, no client-side state machine — the browser
// does the work.

const els = {
  brand: document.getElementById("brand-name"),
  label: document.getElementById("cam-label"),
  frame: document.getElementById("frame"),
  stage: document.getElementById("cam-stage"),
  overlay: document.getElementById("overlay"),
  overlayMsg: document.getElementById("overlay-message"),
  liveIndicator: document.getElementById("live-indicator"),
  start: document.getElementById("start-btn"),
  stop: document.getElementById("stop-btn"),
  fullscreen: document.getElementById("fullscreen-btn"),
};

const state = {
  cameraNum: null,
  key: null,
  siteName: "HEDGEWORK @ PS 20",
  cameraName: null,
  streaming: false,
};

function parseCameraNum() {
  const m = location.pathname.match(/\/cam(\d+)/);
  return m ? parseInt(m[1], 10) : null;
}

function parseKey() {
  const params = new URLSearchParams(location.search);
  const k = (params.get("key") || "").trim();
  return k || null;
}

function showOverlay(message, kind = "info") {
  els.overlay.classList.remove("hidden", "info", "warn", "error");
  els.overlay.classList.add(kind);
  els.overlayMsg.textContent = message;
}

function hideOverlay() {
  els.overlay.classList.add("hidden");
}

function setLiveIndicator(active) {
  els.liveIndicator.classList.toggle("hidden", !active);
}

function applyBranding(data) {
  if (data && data.site_name) state.siteName = data.site_name;
  if (data && data.cameras) {
    const cam = data.cameras.find((c) => c.camera_num === state.cameraNum);
    if (cam && cam.display_name) state.cameraName = cam.display_name;
  }
  if (state.cameraName == null) state.cameraName = `Camera ${state.cameraNum}`;
  els.brand.textContent = state.siteName;
  els.label.textContent = state.cameraName;
  document.title = `${state.siteName} · ${state.cameraName}`;
}

async function fetchBranding() {
  if (!state.key) return;
  try {
    const resp = await fetch(
      `/api/info?key=${encodeURIComponent(state.key)}`,
      { cache: "no-store" },
    );
    if (resp.ok) applyBranding(await resp.json());
  } catch (_) {
    // Best-effort; default branding stays in place.
  }
}

function startStreaming() {
  if (state.streaming || !state.key || state.cameraNum == null) return;
  state.streaming = true;
  els.start.disabled = true;
  els.stop.disabled = false;
  // A bust query parameter forces a fresh connection on Start->Stop->
  // Start cycles. Without it, Chrome occasionally reuses the previous
  // (now-closed) HTTP/1.1 connection from cache and the <img> just
  // shows a broken-image icon.
  const bust = Date.now();
  els.frame.src =
    `/stream/cam${state.cameraNum}` +
    `?key=${encodeURIComponent(state.key)}&t=${bust}`;
  hideOverlay();
  setLiveIndicator(true);
}

function stopStreaming() {
  state.streaming = false;
  els.start.disabled = false;
  els.stop.disabled = true;
  // Setting src to "" closes the multipart connection. The browser
  // tears down the TCP socket, which the aiohttp handler observes as
  // a CancelledError / ConnectionResetError and uses to release the
  // camera refcount.
  els.frame.removeAttribute("src");
  setLiveIndicator(false);
  showOverlay("Stopped. Press Start to resume.", "info");
}

function toggleFullscreen() {
  if (!document.fullscreenElement) {
    if (els.stage.requestFullscreen) els.stage.requestFullscreen();
  } else if (document.exitFullscreen) {
    document.exitFullscreen();
  }
}

function init() {
  state.cameraNum = parseCameraNum();
  state.key = parseKey();

  if (state.cameraNum == null) {
    showOverlay("This URL does not specify a camera.", "error");
    els.start.disabled = true;
    return;
  }
  applyBranding(null);

  if (!state.key) {
    showOverlay(
      "Access key required. Open the shareable URL that includes ?key=…",
      "error",
    );
    els.start.disabled = true;
    return;
  }

  els.start.addEventListener("click", startStreaming);
  els.stop.addEventListener("click", stopStreaming);
  els.fullscreen.addEventListener("click", toggleFullscreen);

  // If the browser drops the MJPEG connection (carrier flap, server
  // restart, etc.) the <img> fires onerror. Surface that to the user
  // and snap back to the Stopped state so the next Start re-establishes
  // the connection cleanly.
  els.frame.addEventListener("error", () => {
    if (state.streaming) {
      stopStreaming();
      showOverlay("Stream interrupted. Press Start to retry.", "warn");
    }
  });

  fetchBranding();
}

window.addEventListener("DOMContentLoaded", init);
