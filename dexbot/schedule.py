"""Sleep until the next hourly bar."""

from __future__ import annotations


def sleep_seconds(now: float, interval_seconds: int, align_to_hour: bool) -> float:
    if align_to_hour and interval_seconds % 3600 == 0:
        nxt = (int(now) // interval_seconds + 1) * interval_seconds
        return max(1.0, nxt - now)
    return float(interval_seconds)
