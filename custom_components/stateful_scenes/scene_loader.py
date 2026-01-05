"""Load scene definitions for Stateful Scenes.

We use Home Assistant's YAML loader so standard include tags work
(!include, !include_dir_merge_list, etc.).

Only YAML scene items that contain an `entities:` mapping are supported.
Platform-provided scenes (Hue, ZHA, etc.) do not expose enough detail to
reliably infer a target state, so they are skipped.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from homeassistant.core import HomeAssistant
from homeassistant.util import slugify
from homeassistant.util.yaml import load_yaml

from .const import (
    KEY_ENTITIES,
    KEY_ICON,
    KEY_ID,
    KEY_NAME,
    ScenesSourceInvalid,
    ScenesSourceNotFound,
    SOURCE_CONFIGURATION_YAML,
    SOURCE_SCENE_DIR,
    SOURCE_SCENE_FILE,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ParsedScene:
    """A parsed YAML scene definition."""

    scene_id: str
    name: str
    icon: str | None
    entities: dict[str, dict[str, Any]]  # entity_id -> expected (state + attrs)


def _normalize_entity_expectation(value: Any) -> dict[str, Any]:
    """Normalize a per-entity scene value into a dict."""

    if value is None:
        return {}

    if isinstance(value, dict):
        return dict(value)

    # Scalars: interpret as a desired state
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, bool):
            return {"state": "on" if value else "off"}
        return {"state": str(value)}

    # Fallback (best-effort)
    return {"state": str(value)}


def _parse_scene_items(items: Iterable[Any]) -> list[ParsedScene]:
    scenes: list[ParsedScene] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        entities = item.get(KEY_ENTITIES)
        if not isinstance(entities, dict):
            # Not a YAML scene item (e.g. platform-based scene) -> skip
            continue

        name = item.get(KEY_NAME)
        if not isinstance(name, str) or not name.strip():
            name = "Unnamed Scene"

        raw_id = item.get(KEY_ID)
        if isinstance(raw_id, str) and raw_id.strip():
            scene_id = raw_id.strip()
        else:
            scene_id = slugify(name) or f"scene_{len(scenes) + 1}"

        icon = item.get(KEY_ICON) if isinstance(item.get(KEY_ICON), str) else None

        normalized_entities: dict[str, dict[str, Any]] = {}
        for ent_id, ent_val in entities.items():
            if not isinstance(ent_id, str):
                continue
            normalized_entities[ent_id] = _normalize_entity_expectation(ent_val)

        scenes.append(
            ParsedScene(
                scene_id=scene_id,
                name=name,
                icon=icon,
                entities=normalized_entities,
            )
        )

    return scenes


def _dedupe_scenes(scenes: list[ParsedScene]) -> list[ParsedScene]:
    """Drop duplicate scene_ids to prevent entity registry collisions."""

    seen: set[str] = set()
    out: list[ParsedScene] = []
    for s in scenes:
        if s.scene_id in seen:
            _LOGGER.warning(
                "Duplicate scene id '%s' encountered; only the first will be used", s.scene_id
            )
            continue
        seen.add(s.scene_id)
        out.append(s)
    return out


def _load_yaml_file(path: str | os.PathLike[str]) -> Any:
    """Blocking YAML loader (run in executor)."""

    return load_yaml(path)


def _resolve_path(hass: HomeAssistant, path: str) -> str:
    return path if os.path.isabs(path) else hass.config.path(path)


async def async_load_scenes(
    hass: HomeAssistant,
    *,
    source: str,
    scene_file: str | None,
    scene_dir: str | None,
) -> list[ParsedScene]:
    """Load scenes from the configured source."""

    if source == SOURCE_CONFIGURATION_YAML:
        cfg_path = hass.config.path("configuration.yaml")
        if not os.path.exists(cfg_path):
            raise ScenesSourceNotFound(f"configuration.yaml not found at {cfg_path}")

        raw = await hass.async_add_executor_job(_load_yaml_file, cfg_path)
        if not isinstance(raw, dict):
            raise ScenesSourceInvalid("configuration.yaml did not parse to a dict")

        raw_scenes = raw.get("scene")
        if raw_scenes is None:
            return []

        if isinstance(raw_scenes, list):
            return _dedupe_scenes(_parse_scene_items(raw_scenes))

        if isinstance(raw_scenes, dict):
            return _dedupe_scenes(_parse_scene_items([raw_scenes]))

        raise ScenesSourceInvalid("scene section in configuration.yaml is not list/dict")

    if source == SOURCE_SCENE_FILE:
        if not scene_file:
            raise ScenesSourceNotFound("No scene_file configured")

        path = _resolve_path(hass, scene_file)
        if not os.path.exists(path):
            raise ScenesSourceNotFound(f"Scene file not found: {path}")

        raw = await hass.async_add_executor_job(_load_yaml_file, path)
        if raw is None:
            return []

        if isinstance(raw, list):
            return _dedupe_scenes(_parse_scene_items(raw))

        if isinstance(raw, dict):
            return _dedupe_scenes(_parse_scene_items([raw]))

        raise ScenesSourceInvalid(f"Scene file did not parse to list/dict: {path}")

    if source == SOURCE_SCENE_DIR:
        if not scene_dir:
            raise ScenesSourceNotFound("No scene_dir configured")

        dir_path = _resolve_path(hass, scene_dir)
        p = Path(dir_path)
        if not p.exists() or not p.is_dir():
            raise ScenesSourceNotFound(f"Scene directory not found: {dir_path}")

        scene_items: list[Any] = []
        for file_path in sorted(p.glob("*.y*ml")):
            raw = await hass.async_add_executor_job(_load_yaml_file, str(file_path))
            if raw is None:
                continue
            if isinstance(raw, list):
                scene_items.extend(raw)
            elif isinstance(raw, dict):
                scene_items.append(raw)
            else:
                _LOGGER.warning("Skipping %s (not list/dict)", file_path)

        return _dedupe_scenes(_parse_scene_items(scene_items))

    raise ScenesSourceInvalid(f"Unknown source: {source}")
