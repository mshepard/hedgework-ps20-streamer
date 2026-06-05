"""Astral-based AWAKE/ASLEEP schedule decisions.

Pure functions. No async, no I/O, no global state. Given a ``now`` and
the two relevant config blocks, return a ``ScheduleDecision`` describing
the current activity window and the next transitions. The power state
machine in ``streamer.power`` consumes the decision and drives the
actual side effects (mode changes, ``rtcwake`` calls, camera refcount
release).

The active window for each civil day is

    [sunrise + sunrise_offset, sunset + sunset_offset]

where sunrise/sunset are computed by the ``astral`` library for the
configured ``[location]``. Offsets are signed minutes, e.g.
``sunrise_offset_minutes = -30`` wakes the camera half an hour before
civil sunrise.

This module is deliberately easy to unit test: feed it a fake ``now``
and the same ``location`` / ``schedule`` blocks the live service uses,
and the resulting decision is fully determined.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun

from streamer.config import LocationConfig, ScheduleConfig


@dataclass(frozen=True)
class ScheduleDecision:
    """Result of evaluating the schedule at one moment in time.

    All datetimes are timezone-aware (in the configured ``location``
    timezone). The caller is responsible for formatting them for
    presentation or converting to UNIX timestamps for ``rtcwake``.
    """

    active: bool
    next_sleep: datetime
    next_wake: datetime
    in_warning_window: bool


def _tzinfo(location: LocationConfig) -> ZoneInfo:
    return ZoneInfo(location.timezone)


def _civil_day(
    location: LocationConfig, day: date
) -> tuple[datetime, datetime]:
    """Return (raw_sunrise, raw_sunset) for ``day`` at ``location``.

    Both returned values are timezone-aware in the location timezone.
    No offsets applied yet.
    """

    tz = _tzinfo(location)
    info = LocationInfo(
        name="streamer",
        region="streamer",
        timezone=location.timezone,
        latitude=location.latitude,
        longitude=location.longitude,
    )
    s = sun(info.observer, date=day, tzinfo=tz)
    return s["sunrise"], s["sunset"]


def _apply_offsets(
    sunrise: datetime,
    sunset: datetime,
    schedule: ScheduleConfig,
) -> tuple[datetime, datetime]:
    return (
        sunrise + timedelta(minutes=schedule.sunrise_offset_minutes),
        sunset + timedelta(minutes=schedule.sunset_offset_minutes),
    )


def decide(
    now: datetime,
    location: LocationConfig,
    schedule: ScheduleConfig,
) -> ScheduleDecision:
    """Evaluate the schedule at ``now``.

    ``now`` must be timezone-aware. If you have a naive datetime, wrap
    it with the location's tzinfo first — passing UTC for a non-UTC
    location would silently shift the active window.
    """

    if now.tzinfo is None:
        raise ValueError("schedule.decide() requires a tz-aware datetime")

    tz = _tzinfo(location)
    today = now.astimezone(tz).date()
    tomorrow = today + timedelta(days=1)

    today_sr, today_ss = _apply_offsets(
        *_civil_day(location, today), schedule=schedule
    )
    tmrw_sr, tmrw_ss = _apply_offsets(
        *_civil_day(location, tomorrow), schedule=schedule
    )

    active = today_sr <= now < today_ss

    # Next sleep transition: today's sunset+offset if we haven't passed
    # it yet, otherwise tomorrow's.
    next_sleep = today_ss if now < today_ss else tmrw_ss
    # Next wake transition: today's sunrise+offset if we haven't passed
    # it yet, otherwise tomorrow's. This is the "system ready" target,
    # not the RTC alarm time — ``power.py`` subtracts
    # ``wake_lead_minutes`` to derive the actual alarm.
    next_wake = today_sr if now < today_sr else tmrw_sr

    warn_delta = timedelta(minutes=schedule.warn_minutes_before_sleep)
    in_warning_window = active and (next_sleep - now) <= warn_delta

    return ScheduleDecision(
        active=active,
        next_sleep=next_sleep,
        next_wake=next_wake,
        in_warning_window=in_warning_window,
    )


def now_in_location_tz(location: LocationConfig) -> datetime:
    """Convenience: return the current time in the location's timezone.

    Lets ``power.py`` keep a single source of truth for "what time is
    it" without sprinkling ``ZoneInfo(location.timezone)`` lookups
    around the codebase.
    """

    return datetime.now(_tzinfo(location))
