"""Calibration stays separate from source evidence and replacement detection."""

from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType

import pytest
from custom_components.battery_notes.const import DOMAIN, EVENT_BATTERY_REPLACED
from custom_components.battery_notes.percentage import PercentageProfile
from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import setup_integration

pytestmark = pytest.mark.usefixtures("mock_library_updater", "_mock_library_file")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0, 0), (38.5, 48.5), (77, 97), (80, 98.5), (83, 100), (95, 100)],
)
def test_nimh_mapping(raw: float, expected: float) -> None:
    """Preserve zero, compress settling, and cap fresh readings."""
    assert PercentageProfile("nimh").apply(raw) == pytest.approx(expected)
    assert PercentageProfile().apply(raw) == raw


def test_only_main_discharge_informs_nimh_trend() -> None:
    """The compressed top slope cannot predict the different lower segment."""
    profile = PercentageProfile("nimh")
    assert profile.settling_hours == 0
    assert profile.trend_level(80) is None
    assert profile.trend_level(77) == 97
    assert PercentageProfile().settling_hours == 72
    assert PercentageProfile().trend_level(80) == 80


@pytest.mark.parametrize("raw", [None, "unknown", "unavailable", -1, 101, "nan", "inf"])
def test_missing_is_not_empty(raw: object) -> None:
    """Do not manufacture a zero or full battery from invalid source data."""
    assert PercentageProfile("nimh").apply(raw) is None


@pytest.mark.parametrize(("full", "settled"), [(0, 0), (77, 83), (83, 83), (101, 77)])
def test_invalid_calibration(full: float, settled: float) -> None:
    """Require a monotonic mapping."""
    with pytest.raises(ValueError, match="calibration requires"):
        PercentageProfile("nimh", full, settled)


async def test_profile_reload_and_forecast(  # noqa: PLR0915
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_battery_note_subentry: ConfigSubentry,
    battery_note_sensor: er.RegistryEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Use corrected thresholds/trends, retain raw evidence, and undo by reload."""
    freezer.move_to("2026-11-07T12:00:00+00:00")
    note = replace(
        mock_battery_note_subentry,
        data=MappingProxyType(
            {**mock_battery_note_subentry.data, "battery_percentage_profile": "nimh"}
        ),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=mock_config_entry.version,
        data={},
        options=mock_config_entry.options,
        subentries_data=[note.as_dict()],
    )
    registry = er.async_get(hass)
    forecast_entity = registry.async_get_or_create(
        "sensor", DOMAIN, f"{note.unique_id}_battery_days_to_10"
    )
    await setup_integration(hass, entry)
    coordinator = entry.runtime_data.subentry_coordinators[note.subentry_id]
    plus = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{note.unique_id}_battery_plus"
    )
    assert plus is not None
    now = dt_util.utcnow()
    coordinator.record_replacement(now - timedelta(days=6), 83)
    await hass.async_block_till_done()
    assert (
        hass.states.get(forecast_entity.entity_id).attributes["forecast_status"]
        == "insufficient_data"
    )
    coordinator.record_replacement(
        now - timedelta(days=20),
        83,
        trend_samples=[
            {"time": now - timedelta(days=d), "level": 55 + d}
            for d in range(17, -1, -1)
        ],
    )
    await hass.async_block_till_done()
    assert coordinator.current_battery_level == "55"
    assert coordinator.last_reported_level == 55
    assert coordinator.forecast_baseline["level"] == 83
    assert hass.states.get(plus).state == "69.3"
    assert hass.states.get(plus).attributes["raw_battery_level"] == 55
    forecast = hass.states.get(forecast_entity.entity_id)
    assert forecast is not None
    assert forecast.state == "48"  # (55 * 97/77 - 10) / (97/77)
    assert forecast.attributes["initial_battery_level"] == 100
    assert forecast.attributes["daily_loss_percent"] == pytest.approx(
        97 / 77, abs=0.001
    )
    coordinator.current_battery_level = "8"
    assert not coordinator.battery_low  # 8 raw = 10.08 corrected, above 10%.
    coordinator.current_battery_level = "7"
    assert coordinator.battery_low

    # The clamped top must not hide an actual raw replacement jump.
    coordinator.current_battery_level = "60"
    coordinator.record_replacement(now, 60)
    coordinator.import_forecast_history(
        [{"time": now, "level": 60}], detect_replacements=True
    )
    events = []
    hass.bus.async_listen(EVENT_BATTERY_REPLACED, events.append)
    coordinator.current_battery_level = "83"
    await hass.async_block_till_done()
    assert len(events) == 1
    assert coordinator.forecast_baseline["level"] == 83
    original_replaced = coordinator.last_replaced
    original_baseline = coordinator.forecast_baseline.copy()

    # Profile changes never transform storage or create a replacement.
    hass.states.async_set(battery_note_sensor.entity_id, "83")
    await hass.async_block_till_done()
    hass.config_entries.async_update_subentry(
        entry,
        entry.subentries[note.subentry_id],
        data=dict(mock_battery_note_subentry.data),
    )
    await hass.async_block_till_done()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.subentry_coordinators[note.subentry_id]
    assert coordinator.last_replaced == original_replaced
    assert coordinator.forecast_baseline == original_baseline
    assert hass.states.get(plus).state == "83.0"
    assert len(events) == 1
