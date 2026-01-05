"""Stateful Scenes integration (simplified rewrite).

Creates one switch entity per YAML-defined scene.

- The scene is *activated* via the underlying Home Assistant scene entity
  (scene.turn_on).
- The switch state reflects whether all entities in the scene match the
  scene definition.

This rewrite is designed to be more predictable and to handle paths correctly
relative to Home Assistant's config directory.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
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


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Set up the integration domain."""
    hass.data.setdefault(DOMAIN, {})
    return True


def _ensure_reload_service_registered(hass: HomeAssistant) -> None:
    """Register a reload service once for this domain."""
    data = hass.data.setdefault(DOMAIN, {})
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
    for entry in list(ent_reg.entities.values()):
        if entry.config_entry_id != entry_id:
            continue
        if entry.platform != DOMAIN:
            continue
        unique_id = entry.unique_id
        if not unique_id or not unique_id.startswith(f"{entry_id}:"):
            continue
        if unique_id not in valid_unique_ids:
            _LOGGER.debug(
                "Removing orphan entity from registry: %s (unique_id=%s)",
                entry.entity_id,
                unique_id,
            )
            ent_reg.async_remove(entry.entity_id)


def _build_scene_entity_resolver(hass: HomeAssistant):
    """Build a fast resolver from YAML scenes to HA scene entity_ids.

    Important nuance from Home Assistant docs: YAML-defined scenes only *require*
    `name` and `entities` and do not require an `id`. So we cannot rely on
    `state.attributes["id"]` being present. We try several strategies:

    1) Match by `attributes["id"]` if present (covers cases where an id exists).
    2) Direct entity_id guess from the scene name/slug (common case: scene.<slug>).
    3) Match by friendly name.

    Returns None if no match is found.

    We precompute maps once to avoid O(N^2) scans when many scenes exist.
    """

    scene_states = hass.states.async_all("scene")
    entity_ids = {st.entity_id for st in scene_states}
    id_to_eid: dict[str, str] = {}
    name_to_eid: dict[str, str] = {}
    for st in scene_states:
        sid = st.attributes.get("id")
        if isinstance(sid, str) and sid:
            id_to_eid[sid] = st.entity_id
        fn = st.attributes.get("friendly_name")
        if isinstance(fn, str) and fn:
            name_to_eid[fn.strip().casefold()] = st.entity_id

    def _resolve(scene: ParsedScene) -> str | None:
        # 1) Match by attributes["id"] (if present)
        if scene.scene_id and scene.scene_id in id_to_eid:
            return id_to_eid[scene.scene_id]

        # 2) Guess entity_id (YAML examples typically become scene.<slugified name>)
        candidates: list[str] = []
        if scene.scene_id:
            candidates.append(f"scene.{slugify(scene.scene_id)}")
            candidates.append(f"scene.{scene.scene_id}")  # if already slugified
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

    return _resolve


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Stateful Scenes from a config entry."""

    hass.data.setdefault(DOMAIN, {})

    _ensure_reload_service_registered(hass)

    source = entry.options.get(CONF_SOURCE, entry.data.get(CONF_SOURCE, DEFAULT_SOURCE))
    scene_file = entry.options.get(CONF_SCENE_FILE, entry.data.get(CONF_SCENE_FILE, DEFAULT_SCENE_FILE))
    scene_dir = entry.options.get(CONF_SCENE_DIR, entry.data.get(CONF_SCENE_DIR, DEFAULT_SCENE_DIR))
    exclude_circadian = bool(entry.options.get(CONF_EXCLUDE_CIRCADIAN, entry.data.get(CONF_EXCLUDE_CIRCADIAN, DEFAULT_EXCLUDE_CIRCADIAN)))
    circadian_pattern = str(entry.options.get(CONF_CIRCADIAN_PATTERN, entry.data.get(CONF_CIRCADIAN_PATTERN, DEFAULT_CIRCADIAN_PATTERN)))
    # Back-compat: allow a single path field to act as the directory too.
    if source == SOURCE_SCENE_DIR and (not scene_dir) and scene_file:
        scene_dir = scene_file


    opts = MatchOptions(
        number_tolerance=int(entry.options.get(CONF_NUMBER_TOLERANCE, entry.data.get(CONF_NUMBER_TOLERANCE, DEFAULT_NUMBER_TOLERANCE))),
        ignore_unavailable=bool(entry.options.get(CONF_IGNORE_UNAVAILABLE, entry.data.get(CONF_IGNORE_UNAVAILABLE, DEFAULT_IGNORE_UNAVAILABLE))),
        # NOTE: ignore_attributes is intentionally NOT exposed in the UI.
        # It's kept in code for advanced/manual tweaks, but it's usually too
        # blunt (it can make very different states look "matching").
        ignore_attributes=bool(entry.options.get(CONF_IGNORE_ATTRIBUTES, entry.data.get(CONF_IGNORE_ATTRIBUTES, DEFAULT_IGNORE_ATTRIBUTES))),
    )

    # "Settle Time" is the total optimistic window after a scene is turned on.
    settle_time = float(entry.options.get(CONF_SETTLE_TIME, entry.data.get(CONF_SETTLE_TIME, DEFAULT_SETTLE_TIME)))

    scenes = await async_load_scenes(
        hass,
        source=source,
        scene_file=scene_file,
        scene_dir=scene_dir,
    )

    _LOGGER.info("Loaded %d YAML scene definitions from source=%s", len(scenes), source)

    resolver = _build_scene_entity_resolver(hass)
    mgr = SceneManager(
        hass,
        scenes,
        opts,
        settle_time=settle_time,
        resolve_scene_entity_id=resolver,
        exclude_circadian=exclude_circadian,
        circadian_pattern=circadian_pattern,
    )

    await mgr.async_start()

    hass.data[DOMAIN][entry.entry_id] = mgr

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

    mgr: SceneManager | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if mgr is not None:
        await mgr.async_stop()

    return unload_ok
