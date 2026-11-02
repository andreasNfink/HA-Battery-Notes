"""Forecast guardrails and integration behavior, without recorder access."""

from datetime import datetime, timedelta
from typing import Any

import pytest
import voluptuous as vol
from custom_components.battery_notes.const import (
    DOMAIN,
    SERVICE_BATTERY_REPLACED_SCHEMA,
)
from custom_components.battery_notes.forecast import estimate_days
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
        pytest.param(90, 70, 10, 0, False, 30, "estimated", id="average-loss"),
        pytest.param(80, 70, 10, 0, False, 60, "estimated", id="actual-start-not-100"),
        pytest.param(None, 70, 10, 0, False, None, "missing_baseline", id="legacy"),
        pytest.param(90, 89, 10, 0, False, None, "insufficient_data", id="small-drop"),
        pytest.param(90, 70, 2, 0, False, None, "insufficient_data", id="too-short"),
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


async def test_forecast_lifecycle(
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
    # Current fixture report is 55%; a recorded start of 75% ten days ago gives 23 d.
    await hass.services.async_call(
        DOMAIN,
        "set_battery_replaced",
        {
            **target,
            "datetime_replaced": NOW - timedelta(days=10),
            "battery_level": 75,
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
    assert (
        hass.states.get(entity.entity_id).attributes["forecast_status"]
        == "insufficient_data"
    )
    freezer.tick(timedelta(days=10))
    hass.states.async_set(battery_note_sensor.entity_id, "56")
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "46"
    # Unknown source does not retain a seemingly valid lifetime estimate.
    hass.states.async_set(battery_note_sensor.entity_id, "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "unknown"
    # Six-hour freshness timer expires a silent source without polling its device.
    hass.states.async_set(battery_note_sensor.entity_id, "56")
    await hass.async_block_till_done()
    freezer.tick(timedelta(days=4))
    async_fire_time_changed(hass, dt_util.utcnow())
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
