"""LTE / uplink reachability probe.

A long-running asyncio task that pings ``network.modem_probe_target``
once per ``network.modem_probe_interval_seconds`` and keeps the last
result in memory for ``/api/status`` to read. No automatic remediation
of any kind — Phase 2 is diagnosis only.

Why ``ping`` as a subprocess instead of a raw socket? Because:

* The streamer service runs unprivileged. Raw ICMP needs ``CAP_NET_RAW``
  or root.  ``ping`` is suid on Raspberry Pi OS, so the subprocess
  approach works without granting capabilities to our Python process.
* The result we care about (reachable / latency) is what ``ping``
  reports; we have no need to parse ICMP frames ourselves.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from streamer.config import NetworkConfig

logger = logging.getLogger("streamer.modem")


# Pull a float-ish "time=23.4 ms" out of the ``ping`` summary line. The
# exact wording varies very slightly across iputils versions; the
# `time=([\d.]+)` core is stable across all of them we ship on.
_LATENCY_RE = re.compile(rb"time[=<]([\d.]+)\s*ms")


@dataclass
class ModemStatus:
    reachable: bool
    latency_ms: float | None
    last_check: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "latency_ms": self.latency_ms,
            "last_check": self.last_check.isoformat(),
        }


class ModemProbe:
    """Periodically pings the configured target and caches the result."""

    def __init__(self, config: NetworkConfig) -> None:
        self._config = config
        self._status: ModemStatus | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def status(self) -> ModemStatus | None:
        return self._status

    def snapshot(self) -> dict[str, Any] | None:
        return self._status.to_dict() if self._status is not None else None

    async def start(self) -> None:
        # Do one immediate probe so /api/status has data on first
        # call instead of None for the first interval.
        await self._probe_once()
        self._task = asyncio.create_task(self._run(), name="streamer.modem")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._config.modem_probe_interval_seconds)
                try:
                    await self._probe_once()
                except Exception:
                    logger.exception("Modem probe tick failed")
        except asyncio.CancelledError:
            return

    async def _probe_once(self) -> None:
        target = self._config.modem_probe_target
        timeout = self._config.modem_probe_timeout_seconds
        now = datetime.now(timezone.utc)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping",
                "-c",
                "1",
                "-W",
                str(int(timeout)),
                target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _stderr = await asyncio.wait_for(
                    proc.communicate(),
                    # Slightly above ping's own ``-W`` so we don't beat
                    # it to the punch on a slow link.
                    timeout=timeout + 2.0,
                )
            except asyncio.TimeoutError:
                proc.kill()
                with suppress(Exception):
                    await proc.communicate()
                self._status = ModemStatus(
                    reachable=False, latency_ms=None, last_check=now
                )
                logger.info(
                    "Modem probe to %s timed out (>%ss)", target, timeout
                )
                return
        except FileNotFoundError:
            # ``ping`` not installed; surface this loudly and stop
            # trying — the install.sh adds iputils-ping but field
            # systems sometimes drift.
            self._status = ModemStatus(
                reachable=False, latency_ms=None, last_check=now
            )
            logger.error(
                "ping binary not available; modem probe disabled until "
                "iputils-ping is installed"
            )
            if self._task is not None:
                self._task.cancel()
            return
        except Exception:
            self._status = ModemStatus(
                reachable=False, latency_ms=None, last_check=now
            )
            logger.exception("Failed to spawn ping for modem probe")
            return

        reachable = proc.returncode == 0
        latency_ms = _extract_latency_ms(stdout) if reachable else None
        self._status = ModemStatus(
            reachable=reachable, latency_ms=latency_ms, last_check=now
        )
        logger.debug(
            "Modem probe: target=%s reachable=%s latency_ms=%s",
            target,
            reachable,
            latency_ms,
        )


def _extract_latency_ms(stdout: bytes) -> float | None:
    m = _LATENCY_RE.search(stdout)
    if m is None:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None
