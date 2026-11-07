"""Forecast guardrails and integration behavior, without recorder access."""

from datetime import datetime, timedelta
from typing import Any

import pytest
import voluptuous as vol
from custom_components.battery_notes.const import (
    DOMAIN,
    SERVICE_BATTERY_REPLACED_SCHEMA,
)
from custom_components.battery_notes.forecast import (
    MAX_SAMPLES,
    add_sample,
    estimate_days,
)
from custom_components.battery_notes.store import STORAGE_KEY
from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import setup_integration

pytestmark = pytest.mark.usefixtures("mock_library_updater", "_mock_library_file")
NOW = datetime.fromisoformat("2026-11-02T10:00:00+00:00")


@pytest.mark.parametrize(
    ("initial", "level", "age", "stale", "suspended", "days", "status"),
    [
        pytest.param(
            90, 70, 10, 0, False, None, "insufficient_data", id="legacy-no-trend"
        ),
        pytest.param(
            80, 70, 10, 0, False, None, "insufficient_data", id="no-invented-trend"
        ),
        pytest.param(None, 70, 10, 0, False, None, "missing_baseline", id="legacy"),
        pytest.param(90, 89, 10, 0, False, None, "insufficient_data", id="small-drop"),
        pytest.param(90, 70, 2, 0, False, None, "settling", id="too-short"),
        pytest.param(90, 70, 10, 4, False, None, "stale_reading", id="stale"),
        pytest.param(90, 70, 10, -1, False, None, "stale_reading", id="future-report"),
        pytest.param(90, 95, 10, 0, True, None, "replacement_suspected", id="rise"),
        pytest.param(90, 90, 10, 0, False, None, "insufficient_data", id="flat"),
        pytest.param(90, 10, 10, 0, False, 0, "target_reached", id="ten-percent"),
        pytest.param(None, 0, 0, 0, False, 0, "target_reached", id="empty"),
        pytest.param(90, float("nan"), 10, 0, False, None, "missing_reading", id="nan"),
        pytest.param(90, -1, 10, 0, False, None, "missing_reading", id="negative"),
        pytest.param(90, None, 10, 0, False, None, "missing_reading", id="unavailable"),
    ],
)
def test_forecast_math(  # noqa: PLR0913
    initial: float | None,
    level: float | None,
    age: int,
    stale: int,
    suspended: bool,
    days: int | None,
    status: str,
) -> None:
    """Check numerical safety and quality limits."""
    result = estimate_days(
        initial,
        NOW - timedelta(days=age),
        level,
        NOW - timedelta(days=stale),
        NOW,
        suspended=suspended,
    )
    assert (result.days, result.status) == (days, status)


@pytest.mark.parametrize("level", [-1, 101, float("nan"), float("inf")])
def test_reject_invalid_import(level: float) -> None:
    """Invalid historical evidence must not reach storage."""
    with pytest.raises(vol.Invalid):
        SERVICE_BATTERY_REPLACED_SCHEMA({"device_id": "test", "battery_level": level})


async def test_forecast_lifecycle(  # noqa: PLR0915
    hass: HomeAssistant,
    battery_note_config_entry: MockConfigEntry,
    battery_note_sensor: er.RegistryEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """Import, persist/reload, suspect new replacement, recover, and expire data."""
    freezer.move_to(NOW)
    subentry = next(iter(battery_note_config_entry.subentries.values()))
    registry = er.async_get(hass)
    entity = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{subentry.unique_id}_battery_days_to_10",
        suggested_object_id="door_battery_days_to_10",
    )
    await setup_integration(hass, battery_note_config_entry)
    coordinator = battery_note_config_entry.runtime_data.subentry_coordinators[
        subentry.subentry_id
    ]
    target = {
        key: subentry.data[key]
        for key in ("device_id", "source_entity_id")
        if key in subentry.data
    }
    # Import a real post-settling slope of 2 percentage points/day; current is 55%.
    await hass.services.async_call(
        DOMAIN,
        "set_battery_replaced",
        {
            **target,
            "datetime_replaced": NOW - timedelta(days=20),
            "battery_level": 95,
            "trend_samples": [
                {"time": NOW - timedelta(days=d), "level": 55 + 2 * d}
                for d in range(17, -1, -1)
            ],
            "estimated": True,
            "uncertainty_hours": 1,
        },
        blocking=True,
    )
    await hass.async_block_till_done()
    state = hass.states.get(entity.entity_id)
    assert state is not None
    assert state.state == "23"
    assert state.attributes["replacement_source"] == "history_estimate"
    assert state.attributes["uncertainty_hours"] == 1
    await battery_note_config_entry.runtime_data.store.async_save()
    assert hass_storage[STORAGE_KEY]["data"]
    await hass.config_entries.async_reload(battery_note_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "23"
    coordinator = battery_note_config_entry.runtime_data.subentry_coordinators[
        subentry.subentry_id
    ]
    # An 11 point increase must invalidate the previous slope without fabricating a date.
    replaced = coordinator.last_replaced
    hass.states.async_set(
        battery_note_sensor.entity_id, "66", battery_note_sensor.capabilities or {}
    )
    await hass.async_block_till_done()
    assert coordinator.last_replaced == replaced
    assert hass.states.get(entity.entity_id).state == "unknown"
    assert (
        hass.states.get(entity.entity_id).attributes["forecast_status"]
        == "replacement_suspected"
    )
    # A real/manual confirmation establishes a new start, not immediately a forecast.
    await hass.services.async_call(
        DOMAIN, "set_battery_replaced", target, blocking=True
    )
    await hass.async_block_till_done()
    assert coordinator.forecast_baseline["level"] == 66
    assert hass.states.get(entity.entity_id).attributes["forecast_status"] == "settling"
    # The initial 16-point fall is excluded. Only the later 1 point/day counts.
    for day in range(3, 14):
        freezer.move_to(NOW + timedelta(days=day))
        hass.states.async_set(battery_note_sensor.entity_id, str(53 - day))
        await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "30"
    # MQTT startup can have no retained state; persisted evidence keeps its age.
    hass.states.async_set(battery_note_sensor.entity_id, "unknown")
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "30"
    assert (
        hass.states.get(entity.entity_id).attributes["reading_source"]
        == "stored_report"
    )
    # Unknown source does not retain a seemingly valid lifetime estimate.
    hass.states.async_set(battery_note_sensor.entity_id, "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "unknown"
    # Observe available unchanged states, without inventing a new device report.
    hass.states.async_set(battery_note_sensor.entity_id, "40")
    await hass.async_block_till_done()
    reported = coordinator.last_reported
    freezer.tick(timedelta(days=4))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert coordinator.last_reported == reported
    assert (
        hass.states.get(entity.entity_id).attributes["reading_source"]
        == "observed_state"
    )
    # An unknown source can only use the real stored report with its original age.
    hass.states.async_set(battery_note_sensor.entity_id, "unknown")
    await hass.async_block_till_done()
    assert (
        hass.states.get(entity.entity_id).attributes["forecast_status"]
        == "stale_reading"
    )


async def test_historical_date_without_level(
    hass: HomeAssistant,
    battery_note_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Backdating without a start measurement never uses today's percentage."""
    freezer.move_to(NOW)
    await setup_integration(hass, battery_note_config_entry)
    coordinator = next(
        iter(battery_note_config_entry.runtime_data.subentry_coordinators.values())
    )
    coordinator.record_replacement(NOW - timedelta(days=20))
    assert coordinator.forecast_baseline["level"] is None


@pytest.mark.parametrize("initial", [95, 100])
def test_settling_drop_does_not_set_slope(initial: float) -> None:
    """A large early decline never dominates the subsequent stable discharge."""
    start = NOW - timedelta(days=20)
    samples = []
    for hour in range(20 * 24 + 1):
        level = initial - 20 * hour / 72 if hour < 72 else 75 - (hour / 24 - 3)
        samples = add_sample(samples, start + timedelta(hours=hour), level, start)
    result = estimate_days(initial, start, 58, NOW, NOW, samples=samples)
    assert result.days == 48
    assert result.daily_loss == 1
    assert len(samples) <= MAX_SAMPLES
    assert all(
        datetime.fromisoformat(s["time"]) >= start + timedelta(hours=72)
        for s in samples
    )


def test_72_hour_boundary_and_no_old_average_fallback() -> None:
    """Wait exactly 72 hours, then require evidence, rather than reuse start loss."""
    start = NOW - timedelta(hours=72)
    assert (
        estimate_days(100, start, 70, NOW - timedelta(seconds=1), NOW).status
        == "settling"
    )
    assert estimate_days(100, start, 70, NOW, NOW).status == "insufficient_data"
    assert estimate_days(100, start, 9, NOW, NOW).days == 0


@pytest.mark.parametrize("anomaly", [0, -15, 10])
def test_daily_smoothing_rejects_single_anomalous_day(anomaly: float) -> None:
    """A single spurious day does not change an otherwise constant slope."""
    start = NOW - timedelta(days=30)
    samples = [
        {
            "time": (NOW - timedelta(days=d)).isoformat(),
            "level": 60 + d + (anomaly if d == 8 else 0),
        }
        for d in range(14, -1, -1)
    ]
    result = estimate_days(100, start, 60, NOW, NOW, samples=samples)
    assert result.days == 50
    assert result.daily_loss == 1


@pytest.mark.parametrize("rate", [0, -0.2, 0.1])
def test_flat_recovering_or_unresolved_trend_is_unknown(rate: float) -> None:
    """Never turn an almost flat plateau into an enormous lifetime estimate."""
    samples = [
        {"time": (NOW - timedelta(days=d)).isoformat(), "level": 60 + rate * d}
        for d in range(14, -1, -1)
    ]
    result = estimate_days(100, NOW - timedelta(days=30), 60, NOW, NOW, samples=samples)
    assert result.days is None
    assert result.status == "trend_unclear"


def test_gap_and_prior_cycle_do_not_supply_evidence() -> None:
    """Missing days and a previous battery cannot manufacture trend coverage."""
    samples = [
        {"time": (NOW - timedelta(days=d)).isoformat(), "level": 60 + d}
        for d in [14, 13, 12, 11, 10, 4, 3, 2, 1, 0]
    ]
    assert (
        estimate_days(
            100, NOW - timedelta(days=30), 60, NOW, NOW, samples=samples
        ).status
        == "insufficient_data"
    )
    assert (
        estimate_days(
            100, NOW - timedelta(days=5), 60, NOW, NOW, samples=samples
        ).status
        == "insufficient_data"
    )


def test_sampling_is_bounded_and_does_not_weight_chattery_sources() -> None:
    """One real value per six-hour slot, independent of report frequency."""
    start = NOW - timedelta(days=100)
    samples = []
    for hour in range(100 * 24):
        timestamp = start + timedelta(hours=hour)
        samples = add_sample(samples, timestamp, 70, start)
        unchanged = add_sample(samples, timestamp + timedelta(minutes=1), 69, start)
        assert unchanged == samples
    assert len(samples) <= MAX_SAMPLES
    assert (
        NOW - datetime.fromisoformat(samples[0]["time"])
    ).total_seconds() <= 92 * 86400


@pytest.mark.parametrize(
    "payload",
    [
        [{"time": NOW, "level": float("nan")}],
        [{"time": "invalid", "level": 50}],
        [{"time": NOW, "level": 50}] * (MAX_SAMPLES + 1),
    ],
)
def test_reject_invalid_or_oversized_history(payload: list[dict[str, Any]]) -> None:
    """Bound history imports and validate before changing existing evidence."""
    with pytest.raises(vol.Invalid):
        SERVICE_BATTERY_REPLACED_SCHEMA({"device_id": "test", "trend_samples": payload})


async def test_import_ignores_other_cycles_and_future_without_polling(
    hass: HomeAssistant,
    battery_note_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Import valid historical samples through the same bounded collection path."""
    freezer.move_to(NOW)
    await setup_integration(hass, battery_note_config_entry)
    coordinator = next(
        iter(battery_note_config_entry.runtime_data.subentry_coordinators.values())
    )
    started = NOW - timedelta(days=10)
    coordinator.record_replacement(
        started,
        95,
        trend_samples=[
            {"time": NOW - timedelta(days=11), "level": 10},
            {"time": started + timedelta(hours=71), "level": 90},
            {"time": started + timedelta(hours=72), "level": 80},
            {"time": started + timedelta(hours=73), "level": 79},
            {"time": NOW + timedelta(days=1), "level": 100},
        ],
    )
    baseline = coordinator.forecast_baseline
    assert baseline["suspended"] is False
    assert baseline["samples"][0] == {
        "time": (started + timedelta(hours=71)).isoformat(),
        "level": 90,
    }
    assert (
        len(baseline["samples"]) == 2
    )  # one imported slot and the real current report
    assert coordinator.last_replaced == started
    # A second replacement inside imported history cannot silently mix cycles.
    coordinator.record_replacement(
        started,
        95,
        trend_samples=[
            {"time": started + timedelta(days=4), "level": 60},
            {"time": started + timedelta(days=5), "level": 80},
        ],
    )
    assert coordinator.forecast_baseline["suspended"] is True


def test_early_smoothed_trend_and_slow_sensor_window() -> None:
    """A week-old TRV can forecast; slow sensors use longer observed history."""
    recent = [
        {"time": NOW - timedelta(days=d), "level": 60 + d} for d in range(6, -1, -1)
    ]
    early = estimate_days(100, NOW - timedelta(days=9), 60, NOW, NOW, samples=recent)
    assert (early.days, early.status) == (50, "estimated")
    slow = [
        {"time": NOW - timedelta(days=d), "level": 40 + int(d / 10)}
        for d in range(80, -1, -1)
    ]
    result = estimate_days(None, NOW - timedelta(days=90), 40, NOW, NOW, samples=slow)
    assert result.status == "estimated"
    assert result.days is not None
    assert 250 <= result.days <= 350
    assert result.trend_days > 30


async def test_observed_history_preserves_date_reload_and_opt_in_replacement(
    hass: HomeAssistant,
    battery_note_config_entry: MockConfigEntry,
    battery_note_sensor: er.RegistryEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """No inferred date from observation alone; a later >10-point rise starts a cycle."""
    freezer.move_to(NOW)
    await setup_integration(hass, battery_note_config_entry)
    coordinator = next(
        iter(battery_note_config_entry.runtime_data.subentry_coordinators.values())
    )
    subentry = next(iter(battery_note_config_entry.subentries.values()))
    target = {
        key: subentry.data[key]
        for key in ("device_id", "source_entity_id")
        if key in subentry.data
    }
    previous_date = coordinator.last_replaced
    events = []
    hass.bus.async_listen("battery_notes_battery_replaced", events.append)
    await hass.services.async_call(
        DOMAIN,
        "import_battery_history",
        {
            **target,
            "detect_replacements": True,
            "trend_samples": [
                {"time": NOW - timedelta(days=d), "level": 55 + d}
                for d in range(20, -1, -1)
            ],
        },
        blocking=True,
    )
    await hass.async_block_till_done()
    assert coordinator.last_replaced == previous_date
    assert coordinator.forecast_baseline.get("level") is None
    assert coordinator.forecast_started == NOW - timedelta(days=20)
    assert not events
    await battery_note_config_entry.runtime_data.store.async_save()
    assert hass_storage[STORAGE_KEY]["data"]
    await hass.config_entries.async_reload(battery_note_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = next(
        iter(battery_note_config_entry.runtime_data.subentry_coordinators.values())
    )
    assert coordinator.forecast_baseline["detect_replacements"]
    assert coordinator.last_replaced == previous_date
    # Exact threshold is not a replacement; more than ten is explicitly opted in.
    hass.states.async_set(battery_note_sensor.entity_id, "65")
    await hass.async_block_till_done()
    assert coordinator.last_replaced == previous_date
    freezer.tick(timedelta(hours=1))
    hass.states.async_set(battery_note_sensor.entity_id, "76")
    await hass.async_block_till_done()
    assert coordinator.last_replaced == dt_util.utcnow()
    assert coordinator.forecast_baseline["level"] == 76
    assert coordinator.forecast_baseline["source"] == "history_estimate"
    assert coordinator.forecast_baseline["detect_replacements"]
    assert coordinator.forecast_baseline["samples"] == [
        {"time": dt_util.utcnow().isoformat(), "level": 76.0}
    ]
    assert len(events) == 1
