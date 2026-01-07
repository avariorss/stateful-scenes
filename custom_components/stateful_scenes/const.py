"""Constants for the Stateful Scenes integration (simplified rewrite).

This rewrite intentionally removes:
- External scene learning/discovery
- "restore on turn off"

It adds:
- Robust path handling (relative paths resolved against hass.config.path)
- Loading scenes from:
  * Home Assistant's scene section in configuration.yaml (includes supported)
  * A standalone YAML file (can itself contain !include_dir_merge_list, etc.)
  * A directory of YAML files (each containing a scene list or a single scene)
"""

from __future__ import annotations

from enum import StrEnum

DOMAIN = "stateful_scenes"

# Config entry / options
CONF_SOURCE = "source"
CONF_SCENE_FILE = "scene_file"
CONF_SCENE_DIR = "scene_dir"
CONF_NUMBER_TOLERANCE = "number_tolerance"
CONF_IGNORE_UNAVAILABLE = "ignore_unavailable"

# Exclude circadian lighting control helper entities from matching and turn-off.
CONF_EXCLUDE_CIRCADIAN = "exclude_circadian_controls"
CONF_CIRCADIAN_PATTERN = "circadian_pattern"
CONF_IGNORE_ATTRIBUTES = "ignore_attributes"

# Total delay (seconds) to keep a scene optimistically "active" after it is
# turned on, before evaluating entity states.
CONF_SETTLE_TIME = "settle_time"

DEFAULT_SOURCE = "configuration_yaml"  # Default to HA's configured scenes
DEFAULT_SCENE_FILE = "scenes.yaml"
DEFAULT_SCENE_DIR = "scenes"  # relative to config dir
DEFAULT_NUMBER_TOLERANCE = 4
DEFAULT_IGNORE_UNAVAILABLE = True
DEFAULT_IGNORE_ATTRIBUTES = False

DEFAULT_EXCLUDE_CIRCADIAN = True
DEFAULT_CIRCADIAN_PATTERN = "switch.circadian_lighting*"

# Default timing (seconds)
DEFAULT_SETTLE_TIME = 1.5

# NOTE: CONF_IGNORE_ATTRIBUTES remains supported in code for advanced/manual
# tweaking (e.g., editing the config entry in storage), but it is intentionally
# not exposed in the UI because it is usually too blunt a tool.


class SceneSource(StrEnum):
    """Scene source options."""
    
    CONFIGURATION_YAML = "configuration_yaml"
    SCENE_FILE = "scene_file"
    SCENE_DIR = "scene_dir"


# Legacy string constants for backward compatibility
SOURCE_CONFIGURATION_YAML = SceneSource.CONFIGURATION_YAML
SOURCE_SCENE_FILE = SceneSource.SCENE_FILE
SOURCE_SCENE_DIR = SceneSource.SCENE_DIR

# HA scene YAML keys
KEY_ID = "id"
KEY_NAME = "name"
KEY_ICON = "icon"
KEY_ENTITIES = "entities"


# Errors
class StatefulScenesError(Exception):
    """Base error for this integration."""


class ScenesSourceNotFound(StatefulScenesError):
    """Raised when the configured scene source cannot be found."""


class ScenesSourceInvalid(StatefulScenesError):
    """Raised when scenes cannot be parsed from the configured source."""
