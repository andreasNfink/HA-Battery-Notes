"""Constant-cost battery estimates; never query recorder or infer a full battery."""

from dataclasses import dataclass
from datetime import datetime
from math import ceil, isfinite

TARGET_LEVEL = 10
MIN_AGE_DAYS = 7
MIN_DROP = 5
MAX_REPORT_AGE_DAYS = 3


@dataclass(frozen=True)
class BatteryForecast:
    """An estimate and the reason when an estimate cannot be supplied."""

    days: int | None
    status: str


def valid_level(value: object) -> bool:
    """Accept only finite battery percentages."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    return isfinite(number) and 0 <= number <= 100


def estimate_days(  # noqa: PLR0911, PLR0913
    initial_level: float | None,
    started: datetime | None,
    current_level: float | None,
    reported: datetime | None,
    now: datetime,
    *,
    suspended: bool = False,
) -> BatteryForecast:
    """Extrapolate the observed average loss to 10%, guarded against weak data."""
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
    assert initial_level is not None
    # Use the measurement timestamp, not wall-clock age of an unchanged stale value.
    age_days = (reported - started).total_seconds() / 86400
    drop = initial_level - current_level
    if age_days < MIN_AGE_DAYS or drop < MIN_DROP:
        return BatteryForecast(None, "insufficient_data")
    return BatteryForecast(
        ceil((current_level - TARGET_LEVEL) * age_days / drop), "estimated"
    )
