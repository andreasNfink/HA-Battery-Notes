"""Bounded, robust battery trends; never query recorder or infer capacity."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from math import ceil, isfinite
from statistics import median
from typing import Any

TARGET_LEVEL = 10
SETTLING_HOURS = 72
MIN_TREND_DAYS = 3
TREND_WINDOW_DAYS = 14
MIN_DROP = 3
MAX_REPORT_AGE_DAYS = 3
SAMPLE_HOURS = 6
HISTORY_DAYS = 90
MAX_SAMPLES = 140


@dataclass(frozen=True)
class BatteryForecast:
    """An estimate and stable diagnostics about the usable trend."""

    days: int | None
    status: str
    daily_loss: float | None = None
    trend_days: float = 0
    sample_days: int = 0


def valid_level(value: object) -> bool:
    """Accept only finite battery percentages."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    return isfinite(number) and 0 <= number <= 100


def sample_points(samples: list[dict[str, Any]]) -> list[tuple[datetime, float]]:
    """Decode bounded persisted evidence, ignoring malformed legacy data."""
    points = []
    for sample in samples[-MAX_SAMPLES:]:
        try:
            timestamp = datetime.fromisoformat(str(sample["time"]))
            if timestamp.tzinfo is not None and valid_level(sample["level"]):
                points.append((timestamp.astimezone(UTC), float(sample["level"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(points)


def add_sample(
    samples: list[dict[str, Any]],
    timestamp: datetime,
    level: float,
    started: datetime,
) -> list[dict[str, Any]]:
    """Keep six-hour observations for 15 days and one per older day, bounded."""
    if not valid_level(level):
        return samples
    timestamp = timestamp.astimezone(UTC)
    day = timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = max(
        started + timedelta(hours=SETTLING_HOURS),
        day - timedelta(days=HISTORY_DAYS + 1),
    )
    points = [(t, v) for t, v in sample_points(samples) if t >= cutoff]
    slot = int(timestamp.timestamp() // (SAMPLE_HOURS * 3600))
    if timestamp >= cutoff and not any(
        int(t.timestamp() // (SAMPLE_HOURS * 3600)) == slot for t, _ in points
    ):
        points.append((timestamp, float(level)))
    compact: dict[tuple[str, int], tuple[datetime, float]] = {}
    recent = day - timedelta(days=TREND_WINDOW_DAYS + 1)
    for t, v in sorted(points):
        key = (
            ("recent", int(t.timestamp() // (SAMPLE_HOURS * 3600)))
            if t >= recent
            else ("day", t.date().toordinal())
        )
        compact.setdefault(key, (t, v))
    return [
        {"time": t.isoformat(), "level": v}
        for t, v in sorted(compact.values())[-MAX_SAMPLES:]
    ]


def estimate_days(  # noqa: PLR0911, PLR0913
    initial_level: float | None,
    started: datetime | None,
    current_level: float | None,
    reported: datetime | None,
    now: datetime,
    *,
    suspended: bool = False,
    samples: list[dict[str, Any]] | None = None,
    settling_hours: int = SETTLING_HOURS,
) -> BatteryForecast:
    """Use daily medians and a robust slope after the initial voltage settling."""
    if not valid_level(current_level) or reported is None:
        return BatteryForecast(None, "missing_reading")
    if not 0 <= (now - reported).total_seconds() <= MAX_REPORT_AGE_DAYS * 86400:
        return BatteryForecast(None, "stale_reading")
    assert current_level is not None
    if current_level <= TARGET_LEVEL:
        return BatteryForecast(0, "target_reached")
    if suspended:
        return BatteryForecast(None, "replacement_suspected")
    if started is None or (not valid_level(initial_level) and not samples):
        return BatteryForecast(None, "missing_baseline")
    settled = started + timedelta(hours=settling_hours)
    if reported < settled:
        return BatteryForecast(None, "settling")
    # Prefer recent behavior; only widen the window for slow, unresolved discharge.
    result = BatteryForecast(None, "insufficient_data")
    for window in (TREND_WINDOW_DAYS, 30, HISTORY_DAYS):
        result = _trend(samples or [], settled, current_level, reported, window)
        if result.days is not None:
            return result
    return result


def _trend(
    samples: list[dict[str, Any]],
    settled: datetime,
    current_level: float,
    reported: datetime,
    window: int,
) -> BatteryForecast:
    """Fit completed observed days; no interpolation or replacement-to-now average."""
    cutoff = max(settled, reported - timedelta(days=window))
    groups: dict[date, list[tuple[datetime, float]]] = defaultdict(list)
    for t, value in sample_points(samples):
        # No interpolation across outages and no partially observed current day.
        if cutoff <= t <= reported and t.date() < reported.astimezone(UTC).date():
            groups[t.date()].append((t, value))
    daily = [
        (median(t.timestamp() for t, _ in values) / 86400, median(v for _, v in values))
        for _, values in sorted(groups.items())
    ]
    span = daily[-1][0] - daily[0][0] if len(daily) > 1 else 0
    if len(daily) < MIN_TREND_DAYS or span < MIN_TREND_DAYS:
        return BatteryForecast(
            None, "insufficient_data", trend_days=round(span, 1), sample_days=len(daily)
        )
    # Do not bridge a multi-day outage or extrapolate an old, disconnected trend.
    if (
        any(b[0] - a[0] > MAX_REPORT_AGE_DAYS for a, b in pairwise(daily))
        or reported.timestamp() / 86400 - daily[-1][0] > MAX_REPORT_AGE_DAYS
    ):
        return BatteryForecast(
            None, "insufficient_data", trend_days=round(span, 1), sample_days=len(daily)
        )
    # Theil-Sen slope of daily medians. Equal weight per day, robust against a
    # single anomalous day; ignore short pairs dominated by 1%-quantisation.
    slopes = [
        (a[1] - b[1]) / (b[0] - a[0])
        for i, a in enumerate(daily)
        for b in daily[i + 1 :]
        if b[0] - a[0] >= 2
    ]
    rate = median(slopes)
    if rate <= 0 or rate * span < MIN_DROP:
        return BatteryForecast(
            None, "trend_unclear", trend_days=round(span, 1), sample_days=len(daily)
        )
    return BatteryForecast(
        ceil((current_level - TARGET_LEVEL) / rate),
        "estimated",
        daily_loss=round(rate, 3),
        trend_days=round(span, 1),
        sample_days=len(daily),
    )
