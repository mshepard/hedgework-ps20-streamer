"""Phase 2 power state machine.

Owns the AWAKE / ENTERING_SLEEP / ASLEEP lifecycle and the side-effects
that go with it: camera warmup-hold refcount management, sleep-override
persistence, and the actual ``rtcwake`` + ``systemctl poweroff`` dispatch
at sunset.

Schedule-disabled (``schedule.enabled = false``, the Phase 1 default)
is a fully supported configuration: the manager stays in ``Mode.AWAKE``
indefinitely, keeps the cameras warm if ``power.keep_cameras_warm`` is
on, and never tries to halt the Pi. The Phase 2 surface is opt-in.

State transitions:

  Boot
    -> AWAKE              when schedule disabled, or schedule says active,
                          or sleep_enabled = false (override pin)
    -> ASLEEP             when schedule enabled, says inactive, sleep
                          override is on, and we're not in dry_run
                          (in dry_run we still enter ASLEEP but do not
                          power off)

  AWAKE -> ENTERING_SLEEP at (sunset - warn_minutes_before_sleep) while
                          remaining schedule-active
  ENTERING_SLEEP -> ASLEEP at sunset
  ENTERING_SLEEP -> AWAKE  if sleep_enabled is toggled off mid-window
  ASLEEP -> AWAKE          in dry_run only; the real path reboots through
                           the RTC wake alarm and starts a fresh process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import suppress
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from streamer.cameras import Camera, CameraManager
from streamer.config import AppConfig
from streamer.schedule import ScheduleDecision, decide, now_in_location_tz

logger = logging.getLogger("streamer.power")


class Mode(str, Enum):
    AWAKE = "AWAKE"
    ENTERING_SLEEP = "ENTERING_SLEEP"
    ASLEEP = "ASLEEP"


# How often the state machine re-evaluates the schedule. 30 s is fast
# enough that an API-driven sleep_enabled toggle takes effect within
# half a minute, but slow enough that the loop is essentially free.
# A ``set_sleep_enabled`` call also triggers an immediate re-eval so
# the user-facing latency for the toggle is effectively zero.
POLL_INTERVAL_SECONDS = 30.0

# Per-camera acquire timeout when re-entering AWAKE. A wedged
# libcamera state (e.g. lingering CFE dequeue fault) can make
# ``cam.acquire()`` hang indefinitely. We'd rather drop the warm-hold
# for that one camera — the next viewer connect runs its own recovery
# path via ``mark_broken()`` / ``_recover_blocking()`` — than wedge
# the state machine and hold the asyncio.Lock, which would also block
# the admin endpoint that's trying to rescue us.
AWAKE_ACQUIRE_TIMEOUT_SECONDS = 15.0

# Sudo-fronted commands for HARD_SLEEP. The installer's sudoers.d
# entry grants the streamer user exactly these two invocations.
RTCWAKE_CMD = ["sudo", "/usr/sbin/rtcwake"]
POWEROFF_CMD = ["sudo", "/usr/bin/systemctl", "poweroff"]


class PowerManager:
    """Drives the power mode and owns the warmup-hold camera refcounts.

    Lifetime: created in ``__main__.py``, fed the warmup-held cameras
    via ``apply_initial_hold``, started with ``start()``, stopped with
    ``stop()`` on service shutdown. Server registers a stream-cancel
    callback so the manager can drain in-flight viewers on the ASLEEP
    transition.
    """

    def __init__(
        self,
        config: AppConfig,
        cameras: CameraManager,
        state_dir: Path,
    ) -> None:
        self._config = config
        self._cameras = cameras
        self._state_path = state_dir / "sleep_enabled.json"
        self._sleep_enabled = self._load_sleep_enabled()
        # ---- self power-cycle recovery state ----
        # Persisted daily counter so the max-cycles-per-day cap
        # survives the power cycles it is counting.
        self._recovery_state_path = state_dir / "recovery_cycles.json"
        self._recovery_state = self._load_recovery_state()
        # Process-start reference for the boot grace window.
        self._started_monotonic = time.monotonic()
        # True once a recovery cycle has been committed; suppresses
        # further triggers while the poweroff is in flight.
        self._recovery_in_flight = False
        # One-shot log flags so a wedged camera retriggering every
        # ~16 s doesn't flood the journal with identical refusals.
        self._recovery_refusals_logged: set[str] = set()
        self._mode = Mode.AWAKE
        self._last_decision: ScheduleDecision | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        # Cameras currently held at refcount=1 by power.py for the
        # warmup-hold pattern. Populated by ``apply_initial_hold``
        # after boot warmup, drained on transition to ASLEEP.
        self._warm_held: list[Camera] = []
        # Server-installed callback that cancels all active /stream/*
        # tasks. Set via ``attach_stream_canceller``.
        self._stream_canceller: Callable[[], Awaitable[None]] | None = None
        # Wildlife sync flush before sleep (ENTERING_SLEEP).
        self._sleep_flush_callback: Callable[[], Awaitable[None]] | None = None

    # ---------- read-only accessors ----------

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def sleep_enabled(self) -> bool:
        return self._sleep_enabled

    @property
    def dry_run(self) -> bool:
        return self._config.power.dry_run

    @property
    def schedule_enabled(self) -> bool:
        return self._config.schedule.enabled

    def snapshot(self) -> dict[str, Any]:
        """JSON-serialisable state for /api/status.

        ``next_event`` is derived from ``self._mode`` rather than from
        ``decision.active`` so the field is self-consistent with the
        reported mode even if the state machine is mid-transition (or,
        in pathological cases, wedged): AWAKE/ENTERING_SLEEP both look
        forward to the upcoming sleep, ASLEEP looks forward to the
        upcoming wake.
        """

        d = self._last_decision
        next_event: dict[str, str] | None = None
        if d is not None and self.schedule_enabled:
            if self._mode in (Mode.AWAKE, Mode.ENTERING_SLEEP):
                next_event = {
                    "type": "sleep",
                    "at": d.next_sleep.isoformat(),
                }
            elif self._mode == Mode.ASLEEP:
                next_event = {
                    "type": "wake",
                    "at": d.next_wake.isoformat(),
                }
        return {
            "mode": self._mode.value,
            "sleep_enabled": self._sleep_enabled,
            "dry_run": self.dry_run,
            "schedule_enabled": self.schedule_enabled,
            "next_event": next_event,
            "recovery": {
                "enabled": self._config.power.recovery_power_cycle,
                "cycles_today": self._recovery_cycles_today(),
                "max_per_day": self._config.power.recovery_max_cycles_per_day,
            },
        }

    def schedule_window(self) -> dict[str, str] | None:
        """Upcoming sleep/wake pair for the public embed.

        Unlike ``next_event`` (which only looks forward to the single
        next transition), this exposes both bounds of the upcoming
        off-window. The embed caches it client-side so that when the
        Pi is powered off overnight — and therefore unreachable — the
        page can still tell visitors "asleep until ~HH:MM" instead of
        a generic offline message.
        """

        d = self._last_decision
        if d is None or not self.schedule_enabled:
            return None
        return {
            "sleep_at": d.next_sleep.isoformat(),
            "wake_at": d.next_wake.isoformat(),
        }

    def minutes_until_sleep(self) -> int | None:
        """Whole minutes between now and next sleep, or None if N/A.

        Used by the /stream/* Warning header during ENTERING_SLEEP.
        """

        d = self._last_decision
        if d is None or self._mode != Mode.ENTERING_SLEEP:
            return None
        now = now_in_location_tz(self._config.location)
        remaining = (d.next_sleep - now).total_seconds()
        return max(0, int(remaining // 60))

    # ---------- mutators ----------

    def attach_stream_canceller(
        self, cb: Callable[[], Awaitable[None]]
    ) -> None:
        self._stream_canceller = cb

    def attach_sleep_flush_callback(
        self, cb: Callable[[], Awaitable[None]]
    ) -> None:
        self._sleep_flush_callback = cb

    async def set_sleep_enabled(self, value: bool) -> bool:
        async with self._lock:
            self._sleep_enabled = bool(value)
            self._persist_sleep_enabled()
            logger.info(
                "Sleep override set: sleep_enabled=%s", self._sleep_enabled
            )
        # Immediate re-evaluation so a flip from ENTERING_SLEEP back
        # to AWAKE (or vice versa) takes effect without a poll delay.
        await self._tick()
        return self._sleep_enabled

    async def apply_initial_hold(self, holders: list[Camera]) -> None:
        """Register the cameras that boot warmup left at refcount=1.

        The state machine takes ownership of releasing these on
        transition to ASLEEP and re-acquires on the return path.
        """

        async with self._lock:
            self._warm_held = list(holders)

    async def start(self) -> None:
        """Compute initial mode and launch the poll loop."""

        await self._tick()
        self._task = asyncio.create_task(self._run(), name="streamer.power")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # ---------- override file ----------

    def _load_sleep_enabled(self) -> bool:
        try:
            with self._state_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return bool(data.get("enabled", True))
        except FileNotFoundError:
            return True
        except (json.JSONDecodeError, OSError):
            logger.exception(
                "Sleep-override file %s is unreadable; defaulting to true",
                self._state_path,
            )
            return True

    def _persist_sleep_enabled(self) -> None:
        payload = {"enabled": self._sleep_enabled}
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._state_path)
        except OSError:
            logger.exception(
                "Failed to persist sleep_enabled override to %s",
                self._state_path,
            )

    # ---------- self power-cycle recovery ----------

    def _load_recovery_state(self) -> dict[str, Any]:
        try:
            with self._recovery_state_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError):
            logger.exception(
                "Recovery-cycle state file %s is unreadable; starting fresh",
                self._recovery_state_path,
            )
        return {"date": "", "count": 0}

    def _persist_recovery_state(self) -> None:
        tmp = self._recovery_state_path.with_suffix(".json.tmp")
        try:
            self._recovery_state_path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._recovery_state, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._recovery_state_path)
        except OSError:
            logger.exception(
                "Failed to persist recovery-cycle state to %s",
                self._recovery_state_path,
            )

    def _recovery_cycles_today(self) -> int:
        today = now_in_location_tz(self._config.location).date().isoformat()
        if self._recovery_state.get("date") != today:
            return 0
        return int(self._recovery_state.get("count", 0))

    def _log_recovery_refusal_once(self, key: str, msg: str, *args: Any) -> None:
        if key in self._recovery_refusals_logged:
            return
        self._recovery_refusals_logged.add(key)
        logger.warning(msg, *args)

    async def handle_camera_failure(
        self, camera_num: int, consecutive: int
    ) -> None:
        """Camera persistent-failure notification (from ``mark_broken``).

        Decides whether the wedge warrants a self power-cycle: the
        in-process recovery path (rebuild the picamera2 instance) has
        failed ``consecutive`` times in a row, and field experience
        shows a wedged SerDes link only recovers on a genuine power
        cut. All guardrails live here; ``cameras.py`` just counts.
        """

        cfg = self._config.power
        if not cfg.recovery_power_cycle:
            return
        if consecutive < cfg.recovery_failure_threshold:
            return
        if self._recovery_in_flight:
            return
        if self._mode != Mode.AWAKE:
            # ENTERING_SLEEP/ASLEEP: the sunset path owns power; a
            # wedged camera will get its power cut at sunset anyway.
            self._log_recovery_refusal_once(
                f"mode-{camera_num}",
                "Camera %d wedged (%d consecutive failures) but mode is "
                "%s; skipping self power-cycle",
                camera_num,
                consecutive,
                self._mode.value,
            )
            return
        uptime_minutes = (time.monotonic() - self._started_monotonic) / 60.0
        if uptime_minutes < cfg.recovery_boot_grace_minutes:
            self._log_recovery_refusal_once(
                f"grace-{camera_num}",
                "Camera %d wedged (%d consecutive failures) within the "
                "boot grace window (%.1f of %d min); not power-cycling — "
                "a camera that fails this early may be dead rather than "
                "wedged, and a power-cycle loop would drain the battery",
                camera_num,
                consecutive,
                uptime_minutes,
                cfg.recovery_boot_grace_minutes,
            )
            return
        cycles_today = self._recovery_cycles_today()
        if cycles_today >= cfg.recovery_max_cycles_per_day:
            self._log_recovery_refusal_once(
                "daily-cap",
                "Camera %d wedged (%d consecutive failures) but the "
                "daily self power-cycle cap (%d) is exhausted; staying "
                "up to serve the remaining camera(s)",
                camera_num,
                consecutive,
                cfg.recovery_max_cycles_per_day,
            )
            return

        self._recovery_in_flight = True
        logger.warning(
            "Camera %d wedged for %d consecutive recovery attempts; "
            "initiating self power-cycle %d/%d for today (RTC wake in "
            "%d s)",
            camera_num,
            consecutive,
            cycles_today + 1,
            cfg.recovery_max_cycles_per_day,
            cfg.recovery_wake_delay_seconds,
        )

        # Commit the counter BEFORE the poweroff — after it, this
        # process no longer exists to do the bookkeeping.
        today = now_in_location_tz(self._config.location).date().isoformat()
        self._recovery_state = {"date": today, "count": cycles_today + 1}
        self._persist_recovery_state()

        if self.dry_run:
            logger.info(
                "dry_run: self power-cycle decision logged; "
                "rtcwake/poweroff skipped"
            )
            self._recovery_in_flight = False
            return

        wake_ts = int(time.time()) + cfg.recovery_wake_delay_seconds
        ok = await self._run_rtcwake(wake_ts)
        if not ok:
            logger.error(
                "rtcwake failed; aborting recovery poweroff to avoid an "
                "unwakeable Pi"
            )
            self._recovery_in_flight = False
            return
        await self._run_poweroff()

    # ---------- main loop + transitions ----------

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                try:
                    await self._tick()
                except Exception:
                    logger.exception("Power state machine tick failed")
        except asyncio.CancelledError:
            return

    async def _tick(self) -> None:
        """Evaluate schedule + override, apply any needed transition.

        Idempotent: calling repeatedly with the same wall-clock and
        override state is a no-op.
        """

        async with self._lock:
            if not self.schedule_enabled:
                # Schedule disabled = always AWAKE. The warm-hold
                # established at boot stays in place.
                self._last_decision = None
                await self._ensure_mode(Mode.AWAKE, decision=None)
                return

            now = now_in_location_tz(self._config.location)
            decision = decide(
                now=now,
                location=self._config.location,
                schedule=self._config.schedule,
            )
            self._last_decision = decision
            desired = self._desired_mode(decision)
            if desired != self._mode:
                logger.info(
                    "Power mode transition: %s -> %s "
                    "(active=%s, in_warning=%s, sleep_enabled=%s, dry_run=%s)",
                    self._mode.value,
                    desired.value,
                    decision.active,
                    decision.in_warning_window,
                    self._sleep_enabled,
                    self.dry_run,
                )
            await self._ensure_mode(desired, decision=decision)

    def _desired_mode(self, decision: ScheduleDecision) -> Mode:
        if not self._sleep_enabled:
            # Override pinned: never leave AWAKE regardless of schedule.
            return Mode.AWAKE
        if decision.active:
            if decision.in_warning_window:
                return Mode.ENTERING_SLEEP
            return Mode.AWAKE
        return Mode.ASLEEP

    async def _ensure_mode(
        self,
        target: Mode,
        decision: ScheduleDecision | None,
    ) -> None:
        if target == self._mode:
            return
        if target == Mode.AWAKE:
            await self._enter_awake()
        elif target == Mode.ENTERING_SLEEP:
            await self._enter_entering_sleep()
        elif target == Mode.ASLEEP:
            await self._enter_asleep(decision)
        self._mode = target

    async def _enter_awake(self) -> None:
        # Re-acquire any cameras released on the way out. On first
        # _tick after boot, ``_warm_held`` is already populated by
        # ``apply_initial_hold`` so this is a no-op (the cameras are
        # already held).
        if self._config.power.keep_cameras_warm and not self._warm_held:
            for cam in self._cameras.all():
                try:
                    await asyncio.wait_for(
                        cam.acquire(),
                        timeout=AWAKE_ACQUIRE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "Re-acquire of camera %d timed out after %.0fs on "
                        "AWAKE entry; leaving it unheld so the state "
                        "machine doesn't wedge (viewer-initiated acquire "
                        "will trigger normal recovery)",
                        cam.camera_num,
                        AWAKE_ACQUIRE_TIMEOUT_SECONDS,
                    )
                except Exception:
                    logger.exception(
                        "Re-acquire of camera %d failed on AWAKE entry",
                        cam.camera_num,
                    )
                else:
                    self._warm_held.append(cam)
        await self._cameras.start_publishers()

    async def _enter_entering_sleep(self) -> None:
        # ENTERING_SLEEP does not yet release cameras — viewers keep
        # streaming, they just see the Warning header. The release
        # happens at the actual ASLEEP transition so we don't cool
        # the pipeline early.
        if self._sleep_flush_callback is not None:
            try:
                await self._sleep_flush_callback()
            except Exception:
                logger.exception("Sleep flush callback failed")

    async def _enter_asleep(
        self, decision: ScheduleDecision | None
    ) -> None:
        # 1. Stop shared frame publishers before releasing cameras.
        await self._cameras.stop_publishers()
        # 2. Drop the warmup-hold so the cameras can idle-stop.
        await self._release_warm_held()
        # 3. Cancel in-flight streams. Real ASLEEP is about to halt
        #    the Pi; dry_run ASLEEP still wants the disconnect so
        #    viewers' next request hits the 503 SLEEPING path.
        if self._stream_canceller is not None:
            try:
                await self._stream_canceller()
            except Exception:
                logger.exception("Stream canceller raised on ASLEEP entry")

        if self.dry_run:
            logger.info(
                "ASLEEP entered in dry_run mode; rtcwake/poweroff skipped"
            )
            return

        # 3. HARD_SLEEP: arm the RTC and shut down.
        if decision is None:
            logger.error(
                "ASLEEP entry without a schedule decision; refusing to "
                "power off without a wake target"
            )
            return
        wake_target = decision.next_wake - timedelta(
            minutes=self._config.schedule.wake_lead_minutes
        )
        wake_ts = int(wake_target.timestamp())
        logger.info(
            "Arming RTC wake for %s (unix %d); poweroff to follow",
            wake_target.isoformat(),
            wake_ts,
        )
        ok = await self._run_rtcwake(wake_ts)
        if not ok:
            logger.error(
                "rtcwake failed; aborting poweroff to avoid an unwakeable Pi"
            )
            return
        await self._run_poweroff()

    async def _release_warm_held(self) -> None:
        if not self._warm_held:
            return
        for cam in self._warm_held:
            try:
                await cam.release()
            except Exception:
                logger.exception(
                    "Release of camera %d on sleep entry failed",
                    cam.camera_num,
                )
        self._warm_held.clear()

    # ---------- subprocess plumbing ----------

    async def _run_rtcwake(self, unix_ts: int) -> bool:
        # ``-m no`` sets the alarm without suspending; the subsequent
        # ``systemctl poweroff`` does the actual halt with a clean
        # systemd shutdown sequence (which stops this service and
        # closes the cameras through the normal on_cleanup path).
        cmd = RTCWAKE_CMD + ["-m", "no", "-t", str(unix_ts)]
        return await self._run_cmd(cmd, label="rtcwake")

    async def _run_poweroff(self) -> None:
        await self._run_cmd(POWEROFF_CMD, label="poweroff")
        # systemd will SIGTERM us in a moment. Sleep so we don't loop
        # back into the state machine before that lands.
        await asyncio.sleep(60)

    async def _run_cmd(self, cmd: list[str], label: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        except FileNotFoundError:
            logger.exception("%s binary not found (%s)", label, cmd[0])
            return False
        except Exception:
            logger.exception("Failed to spawn %s (%s)", label, cmd)
            return False
        if proc.returncode != 0:
            logger.error(
                "%s exited rc=%s stdout=%r stderr=%r",
                label,
                proc.returncode,
                stdout.decode("utf-8", errors="replace").strip(),
                stderr.decode("utf-8", errors="replace").strip(),
            )
            return False
        logger.info("%s succeeded: %s", label, " ".join(cmd))
        return True
