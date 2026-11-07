"""Early NiMH estimates and honest, independent fallback evidence."""

from dataclasses import replace
from datetime import datetime, timedelta
from types import MappingProxyType

import pytest
from custom_components.battery_notes.const import DOMAIN
from custom_components.battery_notes.forecast import (
    average_forecast,
    estimate_days,
    reference_forecast,
)
from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from . import setup_integration

pytestmark = pytest.mark.usefixtures("mock_library_updater", "_mock_library_file")
NOW = datetime.fromisoformat("2026-11-07T12:00:00+00:00")


def test_early_nimh_trend_uses_one_day_and_small_delta() -> None:
    """Do not require a week or three points of loss on the calibrated tail."""
    samples = [
        {"time": (NOW - timedelta(hours=h)).isoformat(), "level": 90 + h / 24}
        for h in range(48, -1, -6)
    ]
    result = estimate_days(
        100,
        NOW - timedelta(days=2),
        90,
        NOW,
        NOW,
        samples=samples,
        settling_hours=0,
        early=True,
    )
    assert result.days == 80
    assert result.daily_loss == 1
    assert (
        estimate_days(
            100,
            NOW - timedelta(days=2),
            90,
            NOW,
            NOW,
            samples=samples,
            settling_hours=0,
        ).days
        is None
    )
    flat = [{**s, "level": 90} for s in samples]
    assert (
        estimate_days(
            100,
            NOW - timedelta(days=2),
            90,
            NOW,
            NOW,
            samples=flat,
            settling_hours=0,
            early=True,
        ).days
        is None
    )


def test_gap_average_is_labelled_and_not_a_fitted_trend() -> None:
    """An observed endpoint average is useful despite gaps, without inventing days."""
    samples = [{"time": (NOW - timedelta(days=10)).isoformat(), "level": 70}]
    result = average_forecast(
        samples, 65, NOW, NOW - timedelta(days=20), settling_hours=0
    )
    assert result.days == 110
    assert result.method == "observed_average"
    assert result.status == "average_estimate"
    assert result.daily_loss == 0.5
    assert result.sample_days == 1
    assert average_forecast(samples, 70, NOW, NOW - timedelta(days=20)).days is None


def test_reference_requires_three_devices_and_resists_one_extreme() -> None:
    """Do not manufacture a default lifetime from chemistry or a single peer."""
    assert reference_forecast(90, []).days is None
    assert reference_forecast(90, [0.5, 0.5]).days is None
    result = reference_forecast(90, [0.5, 0.5, 9])
    assert result.days == 160
    assert result.reference_count == 3
    assert result.method == "comparable_device_median"


async def test_available_unchanged_state_is_sampled_without_report_events(
    hass: HomeAssistant,
    battery_note_config_entry: MockConfigEntry,
    battery_note_sensor: er.RegistryEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Passive observations close future plateaus, while outages stay missing."""
    freezer.move_to(NOW)
    note = next(iter(battery_note_config_entry.subentries.values()))
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", DOMAIN, f"{note.unique_id}_battery_days_to_10"
    )
    await setup_integration(hass, battery_note_config_entry)
    coordinator = battery_note_config_entry.runtime_data.subentry_coordinators[
        note.subentry_id
    ]
    coordinator.record_replacement(NOW - timedelta(days=10), 80)
    report = coordinator.last_reported
    replaced = coordinator.last_replaced
    for step in range(1, 9):
        freezer.move_to(NOW + timedelta(hours=6 * step))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    points = coordinator.forecast_baseline["samples"]
    assert len(points) == 9
    assert all(p["level"] == 55 for p in points)
    assert coordinator.last_reported == report
    assert coordinator.last_replaced == replaced
    hass.states.async_set(battery_note_sensor.entity_id, "unavailable")
    await hass.async_block_till_done()
    freezer.tick(timedelta(hours=6))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert coordinator.forecast_baseline["samples"] == points
    assert coordinator.forecast_at(dt_util.utcnow())[0].status == "missing_reading"


@pytest.mark.parametrize("battery_note_source", ["device", "entity"], indirect=True)
async def test_reference_fallback_becomes_own_trend(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_battery_note_subentry: ConfigSubentry,
    battery_note_sensor: er.RegistryEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Exclude mismatched cells and duplicate notes; update as own evidence arrives."""
    freezer.move_to(NOW)
    target = replace(
        mock_battery_note_subentry,
        data=MappingProxyType(
            {
                **mock_battery_note_subentry.data,
                "battery_percentage_profile": "nimh",
            }
        ),
    )
    notes = [target]
    owner = MockConfigEntry(domain="test")
    owner.add_to_hass(hass)
    for index in range(4):
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=owner.entry_id,
            identifiers={("test", f"peer-{index}")},
            manufacturer="Test Manufacturer",
            model="Door sensor",
            name=f"Peer {index}",
        )
        source = er.async_get(hass).async_get_or_create(
            "sensor",
            "test",
            f"peer-{index}",
            device_id=device.id,
            original_device_class=SensorDeviceClass.BATTERY,
            unit_of_measurement="%",
        )
        hass.states.async_set(
            source.entity_id,
            "55",
            {"device_class": "battery", "unit_of_measurement": "%"},
        )
        note = replace(
            target,
            subentry_id=f"peer-{index}",
            unique_id=f"bn_peer_{index}",
            title=f"Peer {index}",
            data=MappingProxyType(
                {
                    **target.data,
                    "device_id": device.id,
                    "source_entity_id": source.entity_id,
                    "battery_quantity": 3 if index == 3 else 2,
                }
            ),
        )
        notes.append(note)
        if index == 0:
            notes.append(
                replace(note, subentry_id="duplicate", unique_id="bn_duplicate")
            )
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=4,
        data={},
        options=mock_config_entry.options,
        subentries_data=[n.as_dict() for n in notes],
    )
    hass.states.async_set(
        battery_note_sensor.entity_id,
        "83",
        {"device_class": "battery", "unit_of_measurement": "%"},
    )
    forecast_entity = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{target.unique_id}_battery_days_to_10"
    )
    await setup_integration(hass, entry)
    coordinators = entry.runtime_data.subentry_coordinators
    coordinator = coordinators[target.subentry_id]
    coordinator.record_replacement(NOW, 83)
    for index, rate in enumerate([0.25, 0.5, 0.75, 4]):
        coordinators[f"peer-{index}"].record_replacement(
            NOW - timedelta(days=20),
            83,
            trend_samples=[
                {"time": NOW - timedelta(days=d), "level": 55 + rate * d}
                for d in range(17, -1, -1)
            ],
        )
    await hass.async_block_till_done()
    state = hass.states.get(forecast_entity.entity_id)
    assert state.state == "143"
    assert state.attributes["estimate_source"] == "similar_devices"
    assert state.attributes["reference_devices"] == 3
    assert state.attributes["trend_quality"] == "reference"
    assert len(coordinator.forecast_peers()) == 3
    freezer.move_to(NOW + timedelta(hours=54))
    hass.states.async_set(
        battery_note_sensor.entity_id,
        "74",
        {"device_class": "battery", "unit_of_measurement": "%"},
    )
    coordinator.import_forecast_history(
        [
            {"time": NOW + timedelta(hours=h), "level": v}
            for h, v in [(6, 76), (30, 75), (54, 74)]
        ]
    )
    await hass.async_block_till_done()
    state = hass.states.get(forecast_entity.entity_id)
    assert state.attributes["estimate_source"] == "own_trend"
    assert state.attributes["forecast_status"] == "estimated"
    assert state.attributes["trend_quality"] == "provisional"
    hass.states.async_set(battery_note_sensor.entity_id, "unavailable")
    await hass.async_block_till_done()
    state = hass.states.get(forecast_entity.entity_id)
    assert state.state == "unknown"
    assert state.attributes["forecast_status"] == "missing_reading"
