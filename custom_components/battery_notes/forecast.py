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
MIN_TREND_DAYS = 7
TREND_WINDOW_DAYS = 14
MIN_DROP = 3
MAX_REPORT_AGE_DAYS = 3
SAMPLE_HOURS = 6
MAX_SAMPLES = 60


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
    """Keep the first real report per six-hour slot, at most sixty records."""
    if not valid_level(level):
        return samples
    timestamp = timestamp.astimezone(UTC)
    cutoff = max(
        started + timedelta(hours=SETTLING_HOURS),
        timestamp - timedelta(days=TREND_WINDOW_DAYS + 1),
    )
    points = [(t, v) for t, v in sample_points(samples) if t >= cutoff]
    slot = int(timestamp.timestamp() // (SAMPLE_HOURS * 3600))
    if timestamp >= cutoff and not any(
        int(t.timestamp() // (SAMPLE_HOURS * 3600)) == slot for t, _ in points
    ):
        points.append((timestamp, float(level)))
    return [
        {"time": t.isoformat(), "level": v} for t, v in sorted(points)[-MAX_SAMPLES:]
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
    if not valid_level(initial_level) or started is None:
        return BatteryForecast(None, "missing_baseline")
    settled = started + timedelta(hours=SETTLING_HOURS)
    if reported < settled:
        return BatteryForecast(None, "settling")
    cutoff = max(settled, reported - timedelta(days=TREND_WINDOW_DAYS))
    groups: dict[date, list[tuple[datetime, float]]] = defaultdict(list)
    for t, value in sample_points(samples or []):
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
