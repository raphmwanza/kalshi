"""Sleep between trading cycles.

``BOT_INTERVAL=0`` means "run again immediately", but a zero sleep would
hammer Kalshi and SofaScore (SofaScore IP-blocks aggressive polling, and
Kalshi returns 429). Every cycle therefore waits at least
``MIN_LOOP_PAUSE_SECONDS``. After a failed cycle, a rate limit, or a
SofaScore block, the wait grows exponentially and is capped.
"""

from __future__ import annotations

MIN_LOOP_PAUSE_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0


def next_sleep_seconds(
    interval: float,
    consecutive_failures: int,
    *,
    min_pause: float = MIN_LOOP_PAUSE_SECONDS,
    max_backoff: float = MAX_BACKOFF_SECONDS,
) -> float:
    """Seconds to wait before the next cycle.

    ``consecutive_failures`` is 0 after a clean cycle. Each failure doubles
    the wait, starting from ``max(interval, min_pause)``, up to ``max_backoff``.
    """
    try:
        requested = float(interval)
    except (TypeError, ValueError):
        requested = 0.0
    if requested != requested or requested < 0:  # NaN or negative
        requested = 0.0
    floor = max(requested, float(min_pause))
    failures = max(0, int(consecutive_failures))
    if failures <= 0:
        return floor
    return min(float(max_backoff), floor * (2 ** failures))


def format_seconds(seconds: float) -> str:
    """Compact seconds label: ``1`` or ``2.5``, not ``1.00``."""
    text = f"{float(seconds):.2f}".rstrip("0").rstrip(".")
    return text or "0"
