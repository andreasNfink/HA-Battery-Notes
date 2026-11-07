"""Explicit per-note calibration; persisted reports always remain raw."""

from dataclasses import dataclass

from .forecast import valid_level

DEFAULT_NIMH_FULL = 83
DEFAULT_NIMH_SETTLED = 77
NIMH_SETTLED_PERCENT = 97


@dataclass(frozen=True)
class PercentageProfile:
    """Compress the initial NiMH voltage drop, then use a linear estimate."""

    name: str = "none"
    full: float = DEFAULT_NIMH_FULL
    settled: float = DEFAULT_NIMH_SETTLED

    @property
    def settling_hours(self) -> int:
        """The NiMH knee replaces a fixed waiting period; keep other profiles."""
        return 0 if self.name == "nimh" else 72

    def trend_level(self, value: object) -> float | None:
        """Never extrapolate the compressed top segment down the linear tail."""
        level = self.apply(value)
        if self.name == "nimh" and level is not None and level > NIMH_SETTLED_PERCENT:
            return None
        return level

    def __post_init__(self) -> None:
        """Reject ambiguous or non-monotonic calibration."""
        if self.name not in {"none", "nimh"}:
            raise ValueError("Unknown battery percentage profile")
        if not 0 < self.settled < self.full <= 100:
            raise ValueError("NiMH calibration requires 0 < settled < full <= 100")

    def apply(self, value: object) -> float | None:
        """Map a real source percentage; missing/invalid data stays missing."""
        if not valid_level(value):
            return None
        level = float(value)  # type: ignore[arg-type]
        if self.name == "none":
            return level
        if level >= self.full:
            return 100.0
        if level <= self.settled:
            return level / self.settled * NIMH_SETTLED_PERCENT
        return NIMH_SETTLED_PERCENT + (level - self.settled) / (
            self.full - self.settled
        ) * (100 - NIMH_SETTLED_PERCENT)
