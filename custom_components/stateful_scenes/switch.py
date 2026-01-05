"""Switch platform for Stateful Scenes.

Each YAML-defined scene gets a corresponding switch entity:
- switch.<scene_id> indicates whether the scene is currently *active*
- turning the switch *on* calls scene.turn_on for the matching Home Assistant
  scene entity

Turning the switch off turns off all member entities in the scene (best-effort).
"""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .scene_manager import SceneManager

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    mgr: SceneManager = hass.data[DOMAIN]["entries"][entry.entry_id]

    entities = [StatefulSceneSwitch(mgr, entry, scene_id) for scene_id in mgr.scenes]
    async_add_entities(entities)


class StatefulSceneSwitch(SwitchEntity):
    """A switch that reflects whether a scene is active."""

    _attr_should_poll = False

    def __init__(self, mgr: SceneManager, entry: ConfigEntry, scene_id: str) -> None:
        self._mgr = mgr
        self._entry = entry
        self._scene_id = scene_id
        self._def = mgr.scenes[scene_id].definition

        self._attr_unique_id = f"{entry.entry_id}:{scene_id}"
        self._attr_name = self._def.name
        self._attr_icon = self._def.icon

        # Cache tracked entities for attributes to avoid rebuilding on every state write
        self._tracked_entities = list(self._def.entities.keys())

        # Present a single device for the integration to avoid device-registry clutter.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Stateful Scenes",
            manufacturer="Stateful Scenes",
        )

    async def async_added_to_hass(self) -> None:
        self._mgr.register_entity(self._scene_id, self)

    async def async_will_remove_from_hass(self) -> None:
        self._mgr.unregister_entity(self._scene_id)

    @property
    def is_on(self) -> bool:
        return self._mgr.is_scene_active(self._scene_id)

    @property
    def extra_state_attributes(self):
        ha_scene_eid = self._mgr.get_ha_scene_entity_id(self._scene_id)
        return {
            "scene_id": self._scene_id,
            "scene_entity_id": ha_scene_eid,
            "tracked_entities": self._tracked_entities,
        }

    async def async_turn_on(self, **kwargs) -> None:
        """Activate the underlying Home Assistant scene."""
        await self._mgr.async_activate_scene(self._scene_id)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off all member entities in the scene."""
        await self._mgr.async_turn_off_scene(self._scene_id)
