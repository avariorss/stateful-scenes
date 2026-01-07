"""Config flow for Stateful Scenes.

Notes
- Home Assistant's config-flow UI does not support true hover tooltips.
  The standard mechanism is "data_description" translations, which are
  rendered as help text under fields.

This flow uses a dropdown to choose the YAML source and a single "Scene Path"
input which is interpreted as either a file path or a directory path depending
on the chosen source.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from .const import (
    CONF_CIRCADIAN_PATTERN,
    CONF_EXCLUDE_CIRCADIAN,
    CONF_IGNORE_UNAVAILABLE,
    CONF_NUMBER_TOLERANCE,
    CONF_SCENE_DIR,
    CONF_SCENE_FILE,
    CONF_SETTLE_TIME,
    CONF_SOURCE,
    DEFAULT_CIRCADIAN_PATTERN,
    DEFAULT_EXCLUDE_CIRCADIAN,
    DEFAULT_IGNORE_UNAVAILABLE,
    DEFAULT_NUMBER_TOLERANCE,
    DEFAULT_SCENE_DIR,
    DEFAULT_SCENE_FILE,
    DEFAULT_SETTLE_TIME,
    DEFAULT_SOURCE,
    DOMAIN,
    SceneSource,
)
from .scene_loader import async_load_scenes

_LOGGER = logging.getLogger(__name__)

# Conservative bounds for UI values (can be widened later without breaking).
_SETTLE_TIME_RANGE = vol.Range(min=0.0, max=300.0)
_NUMBER_TOL_RANGE = vol.Range(min=0, max=15)


def _source_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(
                    value=SceneSource.CONFIGURATION_YAML,
                    label="From configuration.yaml",
                ),
                SelectOptionDict(
                    value=SceneSource.SCENE_FILE,
                    label="Specify scene YAML file",
                ),
                SelectOptionDict(
                    value=SceneSource.SCENE_DIR,
                    label="Specify scene YAML directory",
                ),
            ],
            mode="dropdown",
        )
    )


def _build_schema(*, defaults: dict[str, Any], selected_source: str) -> vol.Schema:
    """Build the schema.

    We intentionally keep a single path field (CONF_SCENE_FILE) labeled as
    "Scene Path" in translations:
    - If Source == Scene YAML File: it is the file path.
    - If Source == Scene YAML Directory: it is the directory path.
    - If Source == Configuration.yaml: it is ignored.

    This keeps the UI stable while still supporting 3 load modes.
    """
    path_default: str
    if selected_source == SceneSource.SCENE_DIR:
        path_default = str(defaults.get(CONF_SCENE_DIR, DEFAULT_SCENE_DIR))
    else:
        path_default = str(defaults.get(CONF_SCENE_FILE, DEFAULT_SCENE_FILE))

    schema: dict[Any, Any] = {
        vol.Required(CONF_SOURCE, default=selected_source): _source_selector(),
        vol.Optional(CONF_SCENE_FILE, default=path_default): str,
        vol.Required(
            CONF_SETTLE_TIME,
            default=float(defaults.get(CONF_SETTLE_TIME, DEFAULT_SETTLE_TIME)),
        ): vol.All(vol.Coerce(float), _SETTLE_TIME_RANGE),
        vol.Required(
            CONF_NUMBER_TOLERANCE,
            default=int(defaults.get(CONF_NUMBER_TOLERANCE, DEFAULT_NUMBER_TOLERANCE)),
        ): vol.All(vol.Coerce(int), _NUMBER_TOL_RANGE),
        vol.Optional(
            CONF_IGNORE_UNAVAILABLE,
            default=bool(defaults.get(CONF_IGNORE_UNAVAILABLE, DEFAULT_IGNORE_UNAVAILABLE)),
        ): bool,
        vol.Optional(
            CONF_EXCLUDE_CIRCADIAN,
            default=bool(defaults.get(CONF_EXCLUDE_CIRCADIAN, DEFAULT_EXCLUDE_CIRCADIAN)),
        ): bool,
        vol.Optional(
            CONF_CIRCADIAN_PATTERN,
            default=str(defaults.get(CONF_CIRCADIAN_PATTERN, DEFAULT_CIRCADIAN_PATTERN)),
        ): str,
    }

    return vol.Schema(schema)


def _clean_user_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalize and drop irrelevant keys."""
    cleaned: dict[str, Any] = dict(user_input)
    source = cleaned.get(CONF_SOURCE, DEFAULT_SOURCE)

    # Normalize strings
    for k in (CONF_SCENE_FILE, CONF_CIRCADIAN_PATTERN):
        v = cleaned.get(k)
        if isinstance(v, str):
            v = v.strip()
            cleaned[k] = v if v else None

    # Mirror path into scene_dir when directory mode is selected.
    if source == SceneSource.SCENE_DIR:
        cleaned[CONF_SCENE_DIR] = cleaned.get(CONF_SCENE_FILE) or DEFAULT_SCENE_DIR

    return cleaned


async def _async_validate(hass, cleaned: dict[str, Any]) -> dict[str, str]:
    """Validate the user input by attempting to load scenes."""
    try:
        await async_load_scenes(
            hass,
            source=cleaned.get(CONF_SOURCE, DEFAULT_SOURCE),
            scene_file=cleaned.get(CONF_SCENE_FILE),
            scene_dir=cleaned.get(CONF_SCENE_DIR),
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Failed to load scenes during config flow validation")
        msg = str(err).lower()
        if "not found" in msg:
            return {"base": "source_not_found"}
        return {"base": "source_invalid"}

    return {}


class StatefulScenesConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Stateful Scenes."""

    VERSION = 2

    def __init__(self) -> None:
        self._selected_source: str = DEFAULT_SOURCE

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            cleaned = _clean_user_input(user_input)
            self._selected_source = cleaned.get(CONF_SOURCE, DEFAULT_SOURCE)

            errors = await _async_validate(self.hass, cleaned)
            if not errors:
                return self.async_create_entry(title="Stateful Scenes", data=cleaned)

            defaults = cleaned
        else:
            defaults = {}

        self._selected_source = str(defaults.get(CONF_SOURCE, self._selected_source))

        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(defaults=defaults, selected_source=self._selected_source),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return StatefulScenesOptionsFlowHandler(config_entry)


class StatefulScenesOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options."""

    def __init__(self, entry: config_entries.ConfigEntry):
        self.entry = entry
        base = dict(entry.options or entry.data or {})
        self._selected_source: str = str(base.get(CONF_SOURCE, DEFAULT_SOURCE))

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}

        if user_input is not None:
            cleaned = _clean_user_input(user_input)
            self._selected_source = cleaned.get(CONF_SOURCE, DEFAULT_SOURCE)

            errors = await _async_validate(self.hass, cleaned)
            if not errors:
                return self.async_create_entry(title="", data=cleaned)

            defaults = {**(self.entry.options or self.entry.data or {}), **cleaned}
        else:
            defaults = dict(self.entry.options or self.entry.data or {})

        self._selected_source = str(defaults.get(CONF_SOURCE, self._selected_source))

        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(defaults=defaults, selected_source=self._selected_source),
            errors=errors,
        )
