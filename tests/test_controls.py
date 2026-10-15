"""Migration, real HA automation controls, and scheduler/control ordering."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_climate_scheduler.const import DOMAIN
from custom_components.nimbus_climate_scheduler.scheduler import NimbusClimateScheduler
from custom_components.nimbus_climate_scheduler.store import NimbusClimateSchedulerStore
from custom_components.nimbus_climate_scheduler.websocket import async_setup_websocket_api

from .test_scheduler import make_zone

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
async def backend(hass, hass_storage):
    original = {
        "climates": [{"entity": entity, "name": "Living room"} for entity in ("climate.left", "climate.right")],
        "zones": {entity: make_zone() for entity in ("climate.left", "climate.right")},
    }
    original["zones"]["climate.left"]["modes"]["heat"]["draftWeek"] = deepcopy(original["zones"]["climate.left"]["modes"]["heat"]["savedWeek"])
    original["zones"]["climate.left"]["modes"]["heat"]["draftWeek"]["Tue"][0]["temp"] = 23
    hass_storage[DOMAIN] = {"version": 1, "minor_version": 1, "key": DOMAIN, "data": deepcopy(original)}
    store = NimbusClimateSchedulerStore(hass)
    await store.async_load()
    for entity in original["zones"]:
        hass.states.async_set(entity, "heat", {
            "hvac_modes": ["off", "heat", "cool"], "temperature": 20,
            "min_temp": 5, "max_temp": 35,
        })
    scheduler = NimbusClimateScheduler(hass, store, [])
    hass.data[DOMAIN] = {"store": store, "scheduler": scheduler}
    return store, scheduler, original


async def test_additive_migration_and_stable_group_identity(hass, backend):
    store, _, original = backend
    assert store.as_dict()["zones"] == original["zones"]
    group_id = store.zone_groups()[0]["id"]
    assert len(store.zone_groups()) == 1

    await store.async_set_climates([
        {"entity": "climate.right", "name": "Renamed"},
        {"entity": "climate.left", "name": "Renamed"},
    ])
    assert store.zone_groups()[0]["id"] == group_id
    assert store.as_dict()["zones"] == original["zones"]
    await store.async_set_climates([{"entity": "climate.right", "name": "Renamed"}])
    reloaded = NimbusClimateSchedulerStore(hass)
    await reloaded.async_load()
    assert reloaded.zone_groups()[0]["id"] == group_id
    assert reloaded.get_zone("climate.right") == original["zones"]["climate.right"]


async def test_failed_write_keeps_last_committed_state(backend):
    store, scheduler, _ = backend
    before = store.as_dict()
    store._store.async_save = AsyncMock(side_effect=OSError("disk full"))
    with pytest.raises(OSError):
        await scheduler.async_set_control("climate.left", enabled=False)
    assert store.as_dict() == before


async def test_pause_resume_and_mode_preserve_schedules(hass, backend):
    store, scheduler, original = backend
    scheduler.async_tick = AsyncMock()
    await scheduler.async_set_control("climate.left", enabled=False)
    scheduler.async_tick.assert_not_awaited()
    for entity in original["zones"]:
        zone = store.get_zone(entity)
        assert zone["activeScheduleMode"] is None
        assert zone["resumeScheduleMode"] == "heat"
        assert zone["modes"] == original["zones"][entity]["modes"]
    await scheduler.async_set_control("climate.right", mode="cool")
    assert all(store.get_zone(entity)["activeScheduleMode"] is None for entity in original["zones"])
    scheduler.async_tick.assert_not_awaited()
    await scheduler.async_set_control("climate.left", enabled=True)
    assert all(store.get_zone(entity)["activeScheduleMode"] == "cool" for entity in original["zones"])
    scheduler.async_tick.assert_awaited_once()


async def test_pause_waits_for_in_flight_tick(hass, backend):
    store, scheduler, _ = backend
    started, release = asyncio.Event(), asyncio.Event()
    applied = []

    async def apply(plan):
        started.set()
        await release.wait()
        applied.append(plan.entity_id)

    scheduler.plan_needs_apply = lambda plan: True
    scheduler.async_apply_plan = apply
    tick = asyncio.create_task(scheduler.async_tick())
    await started.wait()
    pause = asyncio.create_task(scheduler.async_set_control("climate.left", enabled=False))
    await asyncio.sleep(0)
    assert not pause.done()
    release.set()
    await tick
    await pause
    applied.clear()
    await scheduler.async_tick()
    assert applied == []


async def test_save_cannot_resume_a_paused_schedule_and_rejects_stale_drafts(backend):
    store, scheduler, _ = backend
    await scheduler.async_set_control("climate.left", enabled=False)
    update = {"modes": deepcopy(store.get_zone("climate.left")["modes"])}
    update["modes"]["heat"]["savedWeek"]["Mon"][0]["temp"] = 21
    await scheduler.async_set_zone("climate.left", update, expected_revision=0)
    assert store.get_zone("climate.left")["activeScheduleMode"] is None
    assert store.get_zone("climate.right")["modes"] == update["modes"]
    with pytest.raises(HomeAssistantError, match="changed elsewhere"):
        await scheduler.async_set_zone("climate.left", update, expected_revision=0)


async def test_unsupported_group_mode_is_rejected_atomically(hass, backend):
    store, scheduler, _ = backend
    hass.states.async_set("climate.right", "heat", {"hvac_modes": ["off", "heat"]})
    before = store.as_dict()
    with pytest.raises(HomeAssistantError, match="not supported"):
        await scheduler.async_set_control("climate.left", mode="cool")
    assert store.as_dict() == before


async def test_real_ha_switch_select_lifecycle(hass, backend):
    store, scheduler, _ = backend
    scheduler.async_tick = AsyncMock()
    # Browser assets are exercised separately; keep this a real HA entity and
    # config-entry lifecycle test without loading the optional frontend wheel.
    hass.config.components.update({"frontend", "http", "lovelace", "panel_custom", "websocket_api"})
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    group_id = store.zone_groups()[0]["id"]
    switch_id = registry.async_get_entity_id("switch", DOMAIN, f"{group_id}_schedule")
    select_id = registry.async_get_entity_id("select", DOMAIN, f"{group_id}_schedule_mode")
    assert hass.states.get(switch_id).state == "on"
    assert hass.states.get(select_id).state == "heat"

    await hass.services.async_call("switch", "turn_off", {"entity_id": switch_id}, blocking=True)
    await hass.services.async_call("select", "select_option", {"entity_id": select_id, "option": "cool"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(switch_id).state == "off"
    assert hass.states.get(select_id).state == "cool"
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch_id}, blocking=True)
    await hass.async_block_till_done()
    assert store.get_zone("climate.right")["activeScheduleMode"] == "cool"

    with patch("homeassistant.components.frontend.async_remove_panel"):
        assert await hass.config_entries.async_unload(entry.entry_id)
    hass.data[DOMAIN] = {"store": store, "scheduler": scheduler}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(hass.states.async_all("switch")) == 1
    assert hass.states.get(switch_id).state == "on"
    await scheduler.async_set_climates([])
    await hass.async_block_till_done()
    assert registry.async_get(switch_id) is None
    assert registry.async_get(select_id) is None
    with patch("homeassistant.components.frontend.async_remove_panel"):
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.timeout(10)
async def test_websocket_control_notifies_subscribers(hass, backend, hass_ws_client):
    store, _, _ = backend
    client = await hass_ws_client(hass)
    async_setup_websocket_api(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/subscribe"})
    assert (await client.receive_json())["success"]
    await client.send_json({"id": 2, "type": f"{DOMAIN}/set_control", "entity_id": "climate.left", "enabled": False})
    messages = [await client.receive_json(), await client.receive_json()]
    assert any(message.get("success") and message["id"] == 2 for message in messages)
    assert {"id": 1, "type": "event", "event": {}} in messages
    assert store.get_zone("climate.right")["activeScheduleMode"] is None


@pytest.mark.timeout(10)
async def test_readonly_client_cannot_change_schedules(hass, backend, hass_ws_client, hass_read_only_access_token):
    store, _, _ = backend
    client = await hass_ws_client(hass, hass_read_only_access_token)
    async_setup_websocket_api(hass)
    before = store.as_dict()
    await client.send_json({"id": 1, "type": f"{DOMAIN}/get_zones"})
    assert (await client.receive_json())["success"]
    await client.send_json({"id": 2, "type": f"{DOMAIN}/set_control", "entity_id": "climate.left", "enabled": False})
    assert not (await client.receive_json())["success"]
    assert store.as_dict() == before
