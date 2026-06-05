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
        """JSON-serialisable state for /api/status."""

        d = self._last_decision
        next_event: dict[str, str] | None = None
        if d is not None and self.schedule_enabled:
            if d.active:
                next_event = {
                    "type": "sleep",
                    "at": d.next_sleep.isoformat(),
                }
            else:
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
        if not self._config.power.keep_cameras_warm:
            return
        if self._warm_held:
            return
        for cam in self._cameras.all():
            try:
                await cam.acquire()
            except Exception:
                logger.exception(
                    "Re-acquire of camera %d failed on AWAKE entry",
                    cam.camera_num,
                )
            else:
                self._warm_held.append(cam)

    async def _enter_entering_sleep(self) -> None:
        # ENTERING_SLEEP does not yet release cameras — viewers keep
        # streaming, they just see the Warning header. The release
        # happens at the actual ASLEEP transition so we don't cool
        # the pipeline early.
        return

    async def _enter_asleep(
        self, decision: ScheduleDecision | None
    ) -> None:
        # 1. Drop the warmup-hold so the cameras can idle-stop.
        await self._release_warm_held()
        # 2. Cancel in-flight streams. Real ASLEEP is about to halt
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
