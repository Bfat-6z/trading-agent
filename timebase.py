"""Timebase and event-ordering helpers for paper/shadow learning."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def is_future_ts(value: Any, tolerance_seconds: float = 300.0, now: datetime | None = None) -> bool:
    parsed = parse_utc(value)
    if not parsed:
        return False
    current = now or datetime.now(timezone.utc)
    return (parsed - current).total_seconds() > tolerance_seconds


def seconds_between(start: Any, end: Any) -> float | None:
    a = parse_utc(start)
    b = parse_utc(end)
    if not a or not b:
        return None
    return (b - a).total_seconds()


def event_time_fields(event_ts: Any | None = None, observed_ts: Any | None = None) -> dict:
    now = utc_now()
    return {
        "event_ts_utc": str(event_ts or observed_ts or now),
        "observed_ts_utc": str(observed_ts or now),
        "local_written_ts_utc": now,
    }


def validate_event_order(open_ts: Any, close_ts: Any | None = None) -> list[str]:
    errors: list[str] = []
    if is_future_ts(open_ts):
        errors.append("open_ts_future")
    if close_ts is not None:
        if is_future_ts(close_ts):
            errors.append("close_ts_future")
        delta = seconds_between(open_ts, close_ts)
        if delta is None:
            errors.append("invalid_ts")
        elif delta < 0:
            errors.append("close_before_open")
    elif parse_utc(open_ts) is None:
        errors.append("invalid_open_ts")
    return errors
