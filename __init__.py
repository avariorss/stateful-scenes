"""Stateful Scenes integration.

Creates one switch entity per YAML-defined scene.

- Turning the switch *on* activates the underlying Home Assistant scene
  (scene.turn_on).
- The switch state reflects whether the scene is currently *active* (i.e., all
  member entities match the scene's desired state/attributes).

This integration is designed to be predictable, event-driven, and to resolve
paths relative to Home Assistant's config directory.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify

from .const import (
    CONF_CIRCADIAN_PATTERN,
    CONF_EXCLUDE_CIRCADIAN,
    CONF_IGNORE_ATTRIBUTES,
    CONF_IGNORE_UNAVAILABLE,
    CONF_NUMBER_TOLERANCE,
    CONF_SCENE_DIR,
    CONF_SCENE_FILE,
    CONF_SOURCE,
    CONF_SETTLE_TIME,
    DEFAULT_CIRCADIAN_PATTERN,
    DEFAULT_EXCLUDE_CIRCADIAN,
    DEFAULT_IGNORE_ATTRIBUTES,
    DEFAULT_IGNORE_UNAVAILABLE,
    DEFAULT_NUMBER_TOLERANCE,
    DEFAULT_SCENE_DIR,
    DEFAULT_SCENE_FILE,
    DEFAULT_SOURCE,
    DEFAULT_SETTLE_TIME,
    DOMAIN,
    SOURCE_SCENE_DIR,
)
from .matcher import MatchOptions
from .scene_loader import ParsedScene, async_load_scenes
from .scene_manager import SceneManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["switch"]

SERVICE_RELOAD = "reload"
SERVICE_SCHEMA = vol.Schema({vol.Optional("entry_id"): str})


def _domain_data(hass: HomeAssistant) -> dict[str, Any]:
    """Return (and initialise) the domain data structure."""
    data = hass.data.setdefault(DOMAIN, {})
    data.setdefault("entries", {})
    return data


def get_option(entry: ConfigEntry, key: str, default: Any = None) -> Any:
    """Read an option with graceful fallback to entry.data then default."""
    return entry.options.get(key, entry.data.get(key, default))


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Set up the integration domain."""
    _domain_data(hass)
    return True


def _ensure_reload_service_registered(hass: HomeAssistant) -> None:
    """Register a reload service once for this domain."""
    data = _domain_data(hass)
    if data.get("_reload_service_registered"):
        return

    async def _handle_reload(call: ServiceCall) -> None:
        entry_id = call.data.get("entry_id")
        if entry_id:
            await hass.config_entries.async_reload(entry_id)
            return

        for entry in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_reload(entry.entry_id)

    hass.services.async_register(
        DOMAIN, SERVICE_RELOAD, _handle_reload, schema=SERVICE_SCHEMA
    )
    data["_reload_service_registered"] = True


async def _async_cleanup_orphan_entities(
    hass: HomeAssistant, entry_id: str, scene_ids: set[str]
) -> None:
    """Remove entity registry entries for scenes that no longer exist."""
    ent_reg = er.async_get(hass)
    valid_unique_ids = {f"{entry_id}:{scene_id}" for scene_id in scene_ids}

    # Copy to list() since we may mutate the registry.
    for reg_entry in list(ent_reg.entities.values()):
        if reg_entry.config_entry_id != entry_id:
            continue
        if reg_entry.platform != DOMAIN:
            continue
        unique_id = reg_entry.unique_id
        if not unique_id or not unique_id.startswith(f"{entry_id}:"):
            continue
        if unique_id not in valid_unique_ids:
            _LOGGER.debug(
                "Removing orphan entity from registry: %s (unique_id=%s)",
                reg_entry.entity_id,
                unique_id,
            )
            ent_reg.async_remove(reg_entry.entity_id)


def _build_scene_entity_resolver(hass: HomeAssistant) -> tuple[Callable[[ParsedScene], str | None], Callable[[], None]]:
    """Resolve a YAML scene definition to a Home Assistant scene entity_id.

    YAML-defined scenes do *not* require an explicit `id`, so we cannot rely on
    `state.attributes["id"]` being present. We try:

    1) Match by attributes["id"], if present.
    2) Guess entity_id from scene id / name (scene.<slug>).
    3) Match by friendly_name.

    The resolver caches state-derived maps for speed, but will invalidate the cache
    when the entity registry is updated.
    
    Returns: (resolver_function, cleanup_function)
    """
    cache: dict[str, Any] = {"_version": 0}

    def _rebuild() -> None:
        scene_states = hass.states.async_all("scene")
        cache["entity_ids"] = {st.entity_id for st in scene_states}

        id_to_eid: dict[str, str] = {}
        name_to_eid: dict[str, str] = {}
        for st in scene_states:
            sid = st.attributes.get("id")
            if isinstance(sid, str) and sid:
                id_to_eid[sid] = st.entity_id

            fn = st.attributes.get("friendly_name")
            if isinstance(fn, str) and fn:
                name_to_eid[fn.strip().casefold()] = st.entity_id

        cache["id_to_eid"] = id_to_eid
        cache["name_to_eid"] = name_to_eid

    @callback
    def _on_entity_registry_updated(event) -> None:
        """Invalidate cache when scene entities are added/removed/updated."""
        event_data = event.data
        if not isinstance(event_data, dict):
            return
        
        action = event_data.get("action")
        entity_id = event_data.get("entity_id")
        
        # Only invalidate for scene entities
        if isinstance(entity_id, str) and entity_id.startswith("scene."):
            cache["_version"] = cache.get("_version", 0) + 1
            cache.pop("entity_ids", None)
            cache.pop("id_to_eid", None)
            cache.pop("name_to_eid", None)

    _rebuild()
    
    # Listen for entity registry updates
    unsub = hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        _on_entity_registry_updated
    )
    cache["_unsub"] = unsub

    def _resolve_once(scene: ParsedScene) -> str | None:
        entity_ids: set[str] = cache.get("entity_ids", set())
        id_to_eid: dict[str, str] = cache.get("id_to_eid", {})
        name_to_eid: dict[str, str] = cache.get("name_to_eid", {})

        # 1) Match by attributes["id"] (if present)
        if scene.scene_id and scene.scene_id in id_to_eid:
            return id_to_eid[scene.scene_id]

        # 2) Guess entity_id (YAML scenes usually become scene.<slugified name>)
        candidates: list[str] = []
        if scene.scene_id:
            candidates.append(f"scene.{slugify(scene.scene_id)}")
            candidates.append(f"scene.{scene.scene_id}")  # already slugified
        if scene.name:
            candidates.append(f"scene.{slugify(scene.name)}")

        for eid in candidates:
            if eid in entity_ids:
                return eid

        # 3) Match by friendly name
        target = (scene.name or "").strip().casefold()
        if target:
            return name_to_eid.get(target)

        return None

    def _resolve(scene: ParsedScene) -> str | None:
        resolved = _resolve_once(scene)
        if resolved is not None:
            return resolved

        # Cache miss: scenes may have loaded after we built maps.
        _rebuild()
        return _resolve_once(scene)
    
    def _cleanup() -> None:
        """Cleanup function to unsubscribe from events."""
        unsub = cache.get("_unsub")
        if unsub is not None:
            unsub()

    return _resolve, _cleanup


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Stateful Scenes from a config entry."""
    data = _domain_data(hass)
    _ensure_reload_service_registered(hass)

    source = get_option(entry, CONF_SOURCE, DEFAULT_SOURCE)
    scene_file = get_option(entry, CONF_SCENE_FILE, DEFAULT_SCENE_FILE)
    scene_dir = get_option(entry, CONF_SCENE_DIR, DEFAULT_SCENE_DIR)
    exclude_circadian = bool(get_option(entry, CONF_EXCLUDE_CIRCADIAN, DEFAULT_EXCLUDE_CIRCADIAN))
    circadian_pattern = str(get_option(entry, CONF_CIRCADIAN_PATTERN, DEFAULT_CIRCADIAN_PATTERN))

    # Back-compat: allow the UI's single path field to act as a directory path.
    if source == SOURCE_SCENE_DIR and (not scene_dir) and scene_file:
        scene_dir = scene_file

    opts = MatchOptions(
        number_tolerance=int(get_option(entry, CONF_NUMBER_TOLERANCE, DEFAULT_NUMBER_TOLERANCE)),
        ignore_unavailable=bool(get_option(entry, CONF_IGNORE_UNAVAILABLE, DEFAULT_IGNORE_UNAVAILABLE)),
        # NOTE: ignore_attributes is intentionally not exposed in the UI.
        ignore_attributes=bool(get_option(entry, CONF_IGNORE_ATTRIBUTES, DEFAULT_IGNORE_ATTRIBUTES)),
    )

    settle_time = float(get_option(entry, CONF_SETTLE_TIME, DEFAULT_SETTLE_TIME))

    scenes = await async_load_scenes(
        hass,
        source=source,
        scene_file=scene_file,
        scene_dir=scene_dir,
    )

    _LOGGER.info(
        "Loaded %d YAML scene definitions from source=%s",
        len(scenes),
        source,
        extra={"source": source, "scene_count": len(scenes)},
    )

    resolve_scene, cleanup_resolver = _build_scene_entity_resolver(hass)
    
    mgr = SceneManager(
        hass,
        scenes,
        opts,
        settle_time=settle_time,
        resolve_scene_entity_id=resolve_scene,
        exclude_circadian=exclude_circadian,
        circadian_pattern=circadian_pattern,
    )

    await mgr.async_start()

    data["entries"][entry.entry_id] = {
        "manager": mgr,
        "cleanup_resolver": cleanup_resolver,
    }

    # Cleanup orphan entities (e.g. when scenes were removed/renamed).
    await _async_cleanup_orphan_entities(hass, entry.entry_id, set(mgr.scenes.keys()))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    entry_data = _domain_data(hass).get("entries", {}).pop(entry.entry_id, None)
    if entry_data is not None:
        mgr = entry_data.get("manager")
        cleanup_resolver = entry_data.get("cleanup_resolver")
        
        if mgr is not None:
            await mgr.async_stop()
        
        if cleanup_resolver is not None:
            cleanup_resolver()

    return unload_ok
