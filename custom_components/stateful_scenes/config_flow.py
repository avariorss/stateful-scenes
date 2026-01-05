"""Config flow for Stateful Scenes.

UX notes
- Home Assistant's config-flow UI does not support true hover tooltips.
  The standard mechanism is "data_description" translations, which are
  rendered as help text under fields.

This flow uses a single dropdown to choose the YAML source and conditionally
shows a file or directory input depending on that choice.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectOptionDict

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
    DEFAULT_SCENE_FILE,
    DEFAULT_SETTLE_TIME,
    DEFAULT_SOURCE,
    DOMAIN,
    SOURCE_CONFIGURATION_YAML,
    SOURCE_SCENE_DIR,
    SOURCE_SCENE_FILE,
)
from .scene_loader import async_load_scenes

_LOGGER = logging.getLogger(__name__)

# Per your Avario Home layout preference, use an absolute default for the
# directory option in the UI. The loader still supports relative paths.
DEFAULT_SCENE_DIR_UI = "/home/avario/avario_home/scenes/scenes"


def _source_selector(default: str) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=SOURCE_CONFIGURATION_YAML, label="From configuration.yaml"),
                SelectOptionDict(value=SOURCE_SCENE_FILE, label="Specify scene YAML file"),
                SelectOptionDict(value=SOURCE_SCENE_DIR, label="Specify scene YAML directory"),
            ],
            mode="dropdown",
        )
    )


def _build_schema(*, defaults: dict, selected_source: str) -> vol.Schema:
    """Build the schema (single path input; used depending on source)."""

    data: dict = {
        vol.Required(CONF_SOURCE, default=selected_source): _source_selector(selected_source),
    }

    # Single input that can represent either a file path or a directory path.
    # - If Source == Scene YAML File: this is the file path.
    # - If Source == Scene YAML Directory: this is the directory path.
    # - If Source == Configuration.yaml: this field is ignored.
    path_default = DEFAULT_SCENE_FILE
    if selected_source == SOURCE_SCENE_DIR:
        path_default = str(defaults.get(CONF_SCENE_DIR, "/home/avario/avario_home/scenes/scenes"))
    else:
        path_default = str(defaults.get(CONF_SCENE_FILE, DEFAULT_SCENE_FILE))

    data[
        vol.Optional(
            CONF_SCENE_FILE,  # labelled as Scene Path in translations
            default=path_default,
        )
    ] = str

    data[
        vol.Required(
            CONF_SETTLE_TIME,
            default=float(defaults.get(CONF_SETTLE_TIME, DEFAULT_SETTLE_TIME)),
        )
    ] = vol.Coerce(float)

    data[
        vol.Required(
            CONF_NUMBER_TOLERANCE,
            default=int(defaults.get(CONF_NUMBER_TOLERANCE, DEFAULT_NUMBER_TOLERANCE)),
        )
    ] = vol.Coerce(int)

    data[
        vol.Optional(
            CONF_IGNORE_UNAVAILABLE,
            default=bool(defaults.get(CONF_IGNORE_UNAVAILABLE, DEFAULT_IGNORE_UNAVAILABLE)),
        )
    ] = bool

    data[
        vol.Optional(
            CONF_EXCLUDE_CIRCADIAN,
            default=bool(defaults.get(CONF_EXCLUDE_CIRCADIAN, DEFAULT_EXCLUDE_CIRCADIAN)),
        )
    ] = bool

    data[
        vol.Optional(
            CONF_CIRCADIAN_PATTERN,
            default=str(defaults.get(CONF_CIRCADIAN_PATTERN, DEFAULT_CIRCADIAN_PATTERN)),
        )
    ] = str

    return vol.Schema(data)


def _clean_user_input(user_input: dict) -> dict:
    """Normalize and drop irrelevant keys."""

    cleaned = dict(user_input)
    source = cleaned.get(CONF_SOURCE, DEFAULT_SOURCE)

    # In this UI revision we always show CONF_SCENE_FILE as a generic "Scene Path".
    # Preserve it, but also mirror into CONF_SCENE_DIR when a directory source is selected.
    if source == SOURCE_SCENE_DIR:
        cleaned[CONF_SCENE_DIR] = cleaned.get(CONF_SCENE_FILE, DEFAULT_SCENE_DIR)

    # If not using a directory source, keep any existing scene_dir only if it was already stored.
    # (We don't actively remove it to avoid breaking older entries.)
    return cleaned


class StatefulScenesConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Stateful Scenes."""

    VERSION = 2

    def __init__(self) -> None:
        self._selected_source: str = DEFAULT_SOURCE

    async def async_step_user(self, user_input=None):
        """Handle the initial step.

        NOTE: Home Assistant config flows must always return a FlowResult.
        Returning None causes a server-side 500 when the frontend attempts to
        render the result.
        """

        errors: dict[str, str] = {}

        if user_input is not None:
            cleaned = _clean_user_input(user_input)
            self._selected_source = cleaned.get(CONF_SOURCE, DEFAULT_SOURCE)

            try:
                await async_load_scenes(
                    self.hass,
                    source=cleaned.get(CONF_SOURCE, DEFAULT_SOURCE),
                    scene_file=cleaned.get(CONF_SCENE_FILE),
                    scene_dir=cleaned.get(CONF_SCENE_DIR),
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Failed to load scenes during config flow")
                msg = str(err).lower()
                if "not found" in msg:
                    errors["base"] = "source_not_found"
                else:
                    errors["base"] = "source_invalid"
            else:
                return self.async_create_entry(title="Stateful Scenes", data=cleaned)

            defaults = cleaned
        else:
            defaults = {}

        # Keep UI stable: always show the Scene Path input, but it is ignored
        # when Source == Configuration.yaml.
        self._selected_source = defaults.get(CONF_SOURCE, self._selected_source)

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
        self._selected_source: str = (entry.options or entry.data or {}).get(CONF_SOURCE, DEFAULT_SOURCE)

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}

        if user_input is not None:
            cleaned = _clean_user_input(user_input)
            self._selected_source = cleaned.get(CONF_SOURCE, DEFAULT_SOURCE)
            try:
                await async_load_scenes(
                    self.hass,
                    source=cleaned.get(CONF_SOURCE, DEFAULT_SOURCE),
                    scene_file=cleaned.get(CONF_SCENE_FILE),
                    scene_dir=cleaned.get(CONF_SCENE_DIR),
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Failed to load scenes during options flow")
                msg = str(err).lower()
                if "not found" in msg:
                    errors["base"] = "source_not_found"
                else:
                    errors["base"] = "source_invalid"
            else:
                return self.async_create_entry(title="", data=cleaned)

            defaults = {**(self.entry.options or self.entry.data or {}), **cleaned}
        else:
            defaults = dict(self.entry.options or self.entry.data or {})

        self._selected_source = defaults.get(CONF_SOURCE, self._selected_source)

        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(defaults=defaults, selected_source=self._selected_source),
            errors=errors,
        )
