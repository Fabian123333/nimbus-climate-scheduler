from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import SIGNAL_UPDATED, STORAGE_KEY, STORAGE_VERSION
from .settings import DEFAULT_COLOR_THRESHOLDS, merge_settings


class NimbusClimateSchedulerStore:
    """Persist scheduler configuration in Home Assistant storage."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._lock = asyncio.Lock()
        self.data: dict[str, Any] = {"zones": {}}

    async def async_load(self, climates: list[dict] | None = None) -> None:
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self.data = deepcopy(stored)
        self.data.setdefault("zones", {})
        # Add stable group identities without rewriting a single saved/draft
        # setpoint. Version 1 remains readable by older releases on rollback.
        configs = self.data.get("climates")
        if configs is None:
            configs = climates or [{"entity": entity_id} for entity_id in self.zone_entity_ids()]
        self.data["climates"] = self._with_zone_ids(configs, configs)
        if self.data != stored:
            await self._store.async_save(self.data)

    async def _async_commit(self, data: dict) -> None:
        """Publish only after persistence succeeds; callers hold the store lock."""
        await self._store.async_save(data)
        self.data = data
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    def get_zone(self, entity_id: str) -> dict[str, Any] | None:
        zone = self.data.get("zones", {}).get(entity_id)
        return deepcopy(zone) if isinstance(zone, dict) else None

    def get_climates(self) -> list[dict[str, Any]]:
        climates = self.data.get("climates")
        return deepcopy(climates) if isinstance(climates, list) else []

    async def async_set_climates(self, climates: list[dict[str, Any]]) -> None:
        async with self._lock:
            data = deepcopy(self.data)
            data["climates"] = self._with_zone_ids(climates, self.get_climates())
            # An explicit removal deletes only the deselected thermostats.
            allowed = {entry["entity"] for entry in climates}
            data["zones"] = {key: value for key, value in data["zones"].items() if key in allowed}
            await self._async_commit(data)

    @staticmethod
    def _with_zone_ids(climates: list[dict], previous: list[dict]) -> list[dict]:
        """Keep identities across rename, reorder and membership changes."""
        old_ids = {item["entity"]: item.get("zone_id") for item in previous}
        groups: dict[str, list[dict]] = {}
        for item in climates:
            groups.setdefault(item.get("name") or item["entity"], []).append(item)
        used = set()
        result = []
        for members in groups.values():
            candidates = Counter(old_ids.get(item["entity"]) for item in members)
            zone_id = next((key for key, _ in candidates.most_common() if key and key not in used), None)
            zone_id = zone_id or uuid4().hex
            used.add(zone_id)
            result.extend({**item, "zone_id": zone_id} for item in members)
        return result

    def zone_groups(self) -> list[dict]:
        groups: dict[str, dict] = {}
        for config in self.get_climates():
            zone_id = config["zone_id"]
            group = groups.setdefault(zone_id, {
                "id": zone_id,
                "name": config.get("name") or config["entity"],
                "entity_ids": [],
            })
            group["entity_ids"].append(config["entity"])
        return list(groups.values())

    def get_group(self, zone_or_entity_id: str) -> dict | None:
        return next((group for group in self.zone_groups()
                     if zone_or_entity_id == group["id"] or zone_or_entity_id in group["entity_ids"]), None)

    def zone_entity_ids(self) -> list[str]:
        return list(self.data.get("zones", {}).keys())

    async def async_set_zones(
        self, updates: dict[str, dict], expected_revision: int | None = None,
    ) -> None:
        """Atomically merge a group update, retaining unrelated state and fields."""
        async with self._lock:
            data = deepcopy(self.data)
            for entity_id, update in updates.items():
                zone = data["zones"].setdefault(entity_id, {})
                revision = zone.get("revision", 0)
                if expected_revision is not None and revision != expected_revision:
                    raise HomeAssistantError("This schedule changed elsewhere. Reload it before saving.")
                if "modes" in update and update["modes"] != zone.get("modes"):
                    revision += 1
                zone.update(deepcopy(update))
                zone["revision"] = revision
            await self._async_commit(data)

    async def async_set_zone(self, entity_id: str, zone_data: dict[str, Any]) -> None:
        await self.async_set_zones({entity_id: zone_data})

    def get_settings(self) -> dict:
        return deepcopy(self.data.get("settings", {"color_thresholds": DEFAULT_COLOR_THRESHOLDS}))

    async def async_set_settings(self, update: dict, expected_revision: int | None = None) -> None:
        async with self._lock:
            revision = self.data.get("settings_revision", 0)
            if expected_revision is not None and revision != expected_revision:
                raise HomeAssistantError("Color settings changed elsewhere. Reopen Settings before saving.")
            data = deepcopy(self.data)
            data["settings"] = merge_settings(self.get_settings(), update)
            data["settings_revision"] = revision + 1
            await self._async_commit(data)

    def as_dict(self) -> dict[str, Any]:
        return {**deepcopy(self.data), "settings": self.get_settings(), "settings_revision": self.data.get("settings_revision", 0)}
