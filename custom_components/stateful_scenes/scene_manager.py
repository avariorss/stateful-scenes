"""Runtime manager for stateful scene switches.

Key behaviors
- Track scene *active* state by comparing current entity states/attributes against
  the YAML scene definition.
- Re-evaluate whenever any entity referenced by a scene changes.
- Apply an optimistic "settle" window after activations to avoid UI flapping.
- Apply a short suppression window after user-initiated OFF to avoid OFF→ON→OFF
  bounce while entities transition.
"""

from __future__ import annotations

import fnmatch
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import slugify

from .matcher import MatchOptions, entity_matches
from .scene_loader import ParsedScene

_LOGGER = logging.getLogger(__name__)

# After the optimistic/settle window ends, some devices emit a final burst of
# state updates. Evaluating exactly at the end of the settle window can produce
# a brief OFF blip. We allow a small post-settle retry.
POST_ACTIVATION_RETRY_DELAY = 0.75  # seconds
POST_ACTIVATION_MAX_RETRIES = 1


def _cancel(cb: Callable[[], None] | None) -> None:
    """Best-effort cancel/unsubscribe helper."""

    if cb is None:
        return
    with suppress(Exception):
        cb()


@dataclass
class SceneRuntime:
    definition: ParsedScene
    ha_scene_entity_id: str | None
    matches: dict[str, bool | None] = field(default_factory=dict)
    is_active: bool = False

    # Precomputed attribute keys that matter for each entity (expected keys excluding "state")
    watched_attrs: dict[str, tuple[str, ...]] = field(default_factory=dict)

    # Counters for O(1) active-state computation
    total_entities: int = 0
    true_count: int = 0
    false_count: int = 0
    ignored_count: int = 0

    # Optimistic window
    optimistic_until: float | None = None
    cancel_eval: Callable[[], None] | None = None

    # Post-settle retries to reduce flapping right after activation
    post_activation_retries_remaining: int = 0

    # Deactivation suppression window
    suppress_on_until: float | None = None
    cancel_suppress: Callable[[], None] | None = None


class SceneManager:
    """Owns state, listeners, and evaluation for all scenes."""

    def __init__(
        self,
        hass: HomeAssistant,
        scenes: list[ParsedScene],
        opts: MatchOptions,
        *,
        resolve_scene_entity_id: Callable[[ParsedScene], str | None],
        settle_time: float = 1.5,
        exclude_circadian: bool = True,
        circadian_pattern: str = "switch.circadian_lighting*",
    ) -> None:
        self.hass = hass
        self.opts = opts
        self.settle_time = max(0.0, float(settle_time))

        self._resolve_scene_entity_id = resolve_scene_entity_id

        self._exclude_enabled = bool(exclude_circadian)
        self._exclude_patterns = [
            p.strip() for p in str(circadian_pattern).split(",") if p.strip()
        ]

        self._listeners: list[Callable[[], None]] = []
        self._entities: dict[str, Any] = {}  # scene_id -> switch entity

        # Runtimes + indexes
        self.scenes: dict[str, SceneRuntime] = {}
        self._index: dict[str, set[str]] = {}  # member entity_id -> set(scene_id)
        self._ha_scene_to_scene_id: dict[str, str] = {}
        self._guess_ha_eid_to_scene_id: dict[str, str] = {}

        for scene in scenes:
            filtered_scene = self._apply_exclusions(scene)

            ha_eid = resolve_scene_entity_id(filtered_scene)
            runtime = SceneRuntime(definition=filtered_scene, ha_scene_entity_id=ha_eid)

            # Precompute likely HA entity_ids for robustness (used for optimistic handling
            # even if HA states were not available at init time)
            for cand in self._guess_candidates(filtered_scene):
                self._guess_ha_eid_to_scene_id.setdefault(cand, filtered_scene.scene_id)

            # Initialise matches keys and watched attrs so we can update quickly
            for ent_id, expected in filtered_scene.entities.items():
                runtime.matches[ent_id] = None
                runtime.watched_attrs[ent_id] = tuple(
                    k for k in expected.keys() if k != "state"
                )
                self._index.setdefault(ent_id, set()).add(filtered_scene.scene_id)

            runtime.total_entities = len(runtime.matches)

            self.scenes[filtered_scene.scene_id] = runtime
            if ha_eid:
                self._ha_scene_to_scene_id[ha_eid] = filtered_scene.scene_id

    # ---------------------------------------------------------------------
    # Registration: switch entities call these
    # ---------------------------------------------------------------------
    def register_entity(self, scene_id: str, entity: Any) -> None:
        self._entities[scene_id] = entity

    def unregister_entity(self, scene_id: str) -> None:
        self._entities.pop(scene_id, None)

    # ---------------------------------------------------------------------
    # Public queries
    # ---------------------------------------------------------------------
    def is_scene_active(self, scene_id: str) -> bool:
        runtime = self.scenes.get(scene_id)
        return bool(runtime and runtime.is_active)

    def get_ha_scene_entity_id(self, scene_id: str) -> str | None:
        runtime = self.scenes.get(scene_id)
        return runtime.ha_scene_entity_id if runtime else None

    # ---------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------
    async def async_start(self) -> None:
        """Compute initial states and register listeners."""

        for scene_id in list(self.scenes):
            self._recompute_scene(scene_id, touched_entity_id=None, touched_state=None)

        if self._index:
            self._listeners.append(
                async_track_state_change_event(
                    self.hass,
                    list(self._index.keys()),
                    self._handle_member_state_change,
                )
            )
        else:
            _LOGGER.warning("No entities referenced by any loaded scenes")

        self._listeners.append(
            self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._handle_call_service)
        )

    async def async_stop(self) -> None:
        for unsub in self._listeners:
            _cancel(unsub)
        self._listeners.clear()

        for runtime in self.scenes.values():
            _cancel(runtime.cancel_eval)
            runtime.cancel_eval = None
            _cancel(runtime.cancel_suppress)
            runtime.cancel_suppress = None

            runtime.optimistic_until = None
            runtime.suppress_on_until = None

    # ---------------------------------------------------------------------
    # Actions (called by our switch)
    # ---------------------------------------------------------------------
    async def async_activate_scene(self, scene_id: str) -> None:
        runtime = self.scenes.get(scene_id)
        if not runtime:
            _LOGGER.error("Unknown scene_id=%s", scene_id)
            return

        # Re-resolve if we couldn't resolve at init time (or the scene entity was not loaded yet)
        ha_eid = runtime.ha_scene_entity_id
        if not ha_eid or self.hass.states.get(ha_eid) is None:
            ha_eid = self._resolve_scene_entity_id(runtime.definition)
            if ha_eid:
                runtime.ha_scene_entity_id = ha_eid
                self._ha_scene_to_scene_id[ha_eid] = scene_id

        if not ha_eid:
            _LOGGER.error("No matching HA scene entity for scene_id=%s", scene_id)
            return

        # External activation should always clear any suppression window.
        _cancel(runtime.cancel_suppress)
        runtime.cancel_suppress = None
        runtime.suppress_on_until = None

        # Mark optimistic immediately, and re-evaluate after settle_time.
        self._set_scene_optimistic(scene_id, delay=self.settle_time)

        await self.hass.services.async_call(
            "scene",
            "turn_on",
            {"entity_id": ha_eid},
            blocking=True,
        )

    async def async_turn_off_scene(self, scene_id: str) -> None:
        """Turn off all member entities of a scene (best-effort)."""

        runtime = self.scenes.get(scene_id)
        if not runtime:
            _LOGGER.error("Unknown scene_id=%s", scene_id)
            return

        _cancel(runtime.cancel_eval)
        runtime.cancel_eval = None
        runtime.optimistic_until = None
        runtime.post_activation_retries_remaining = 0

        # Suppress re-activation while entities transition towards OFF.
        self._set_scene_suppressed(scene_id, delay=self.settle_time)

        entity_ids = [
            eid
            for eid in runtime.definition.entities.keys()
            if not self._is_excluded(eid)
        ]
        if not entity_ids:
            return

        with suppress(Exception):  # Some domains may not implement turn_off
            await self.hass.services.async_call(
                "homeassistant",
                "turn_off",
                {"entity_id": entity_ids},
                blocking=False,
            )

    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------
    def _is_excluded(self, entity_id: str) -> bool:
        if not self._exclude_enabled or not self._exclude_patterns:
            return False
        return any(fnmatch.fnmatchcase(entity_id, pat) for pat in self._exclude_patterns)

    def _apply_exclusions(self, scene: ParsedScene) -> ParsedScene:
        if not self._exclude_enabled or not self._exclude_patterns:
            return scene

        filtered = {
            ent_id: expected
            for ent_id, expected in scene.entities.items()
            if not self._is_excluded(ent_id)
        }

        if len(filtered) == len(scene.entities):
            return scene

        return ParsedScene(
            scene_id=scene.scene_id,
            name=scene.name,
            icon=scene.icon,
            entities=filtered,
        )

    def _guess_candidates(self, scene: ParsedScene) -> list[str]:
        candidates: list[str] = []
        if scene.scene_id:
            candidates.append(f"scene.{scene.scene_id}")
            candidates.append(f"scene.{slugify(scene.scene_id)}")
        if scene.name:
            candidates.append(f"scene.{slugify(scene.name)}")
        return candidates

    @callback
    def _notify_entity(self, scene_id: str) -> None:
        ent = self._entities.get(scene_id)
        if ent is not None:
            ent.async_write_ha_state()

    # ---------------------------------------------------------------------
    # Event handlers
    # ---------------------------------------------------------------------
    @callback
    def _handle_member_state_change(self, event) -> None:
        """Triggered when any member entity changes."""

        entity_id = event.data.get("entity_id")
        if not isinstance(entity_id, str):
            return

        scene_ids = self._index.get(entity_id)
        if not scene_ids:
            return

        old_state: State | None = event.data.get("old_state")
        new_state: State | None = event.data.get("new_state")

        now = self.hass.loop.time()

        for scene_id in scene_ids:
            runtime = self.scenes.get(scene_id)
            if runtime is None:
                continue

            if runtime.optimistic_until is not None and now < runtime.optimistic_until:
                continue

            if runtime.suppress_on_until is not None and now < runtime.suppress_on_until:
                continue

            if not self._is_interesting_update(runtime, entity_id, old_state, new_state):
                continue

            if self._recompute_scene(
                scene_id, touched_entity_id=entity_id, touched_state=new_state
            ):
                self._notify_entity(scene_id)

    @callback
    def _handle_call_service(self, event) -> None:
        """Detect external scene.turn_on calls to apply an optimistic window."""

        data = event.data or {}
        if data.get("domain") != "scene" or data.get("service") != "turn_on":
            return

        service_data = data.get("service_data") or {}
        target = data.get("target") or {}

        entity_ids: list[str] = []

        def _extend(val: Any) -> None:
            if isinstance(val, str):
                entity_ids.append(val)
            elif isinstance(val, list):
                entity_ids.extend([x for x in val if isinstance(x, str)])

        _extend(target.get("entity_id"))
        _extend(service_data.get("entity_id"))

        if not entity_ids:
            return

        transition: float | None
        try:
            transition = float(service_data.get("transition")) if "transition" in service_data else None
        except (TypeError, ValueError):
            transition = None

        delay = self.settle_time
        if transition is not None:
            delay = max(delay, max(0.0, transition))

        for ha_scene_eid in entity_ids:
            scene_id = self._ha_scene_to_scene_id.get(ha_scene_eid) or self._guess_ha_eid_to_scene_id.get(
                ha_scene_eid
            )
            if not scene_id:
                continue

            runtime = self.scenes.get(scene_id)
            if runtime is None:
                continue

            # External activation should override any suppression window.
            _cancel(runtime.cancel_suppress)
            runtime.cancel_suppress = None
            runtime.suppress_on_until = None

            # If we only matched via guess, store the mapping for next time.
            if runtime.ha_scene_entity_id is None:
                runtime.ha_scene_entity_id = ha_scene_eid
                self._ha_scene_to_scene_id[ha_scene_eid] = scene_id

            self._set_scene_optimistic(scene_id, delay=delay)

    # ---------------------------------------------------------------------
    # Optimistic/suppression windows
    # ---------------------------------------------------------------------
    @callback
    def _set_scene_optimistic(
        self, scene_id: str, *, delay: float, reset_retries: bool = True
    ) -> None:
        runtime = self.scenes.get(scene_id)
        if runtime is None:
            return

        _cancel(runtime.cancel_eval)
        runtime.cancel_eval = None

        delay = max(0.0, float(delay))

        runtime.is_active = True
        if reset_retries:
            runtime.post_activation_retries_remaining = POST_ACTIVATION_MAX_RETRIES

        runtime.optimistic_until = self.hass.loop.time() + delay

        @callback
        def _delayed_eval(_now) -> None:
            self.hass.async_create_task(self.async_evaluate_scene(scene_id))

        runtime.cancel_eval = async_call_later(self.hass, delay, _delayed_eval)

        self._notify_entity(scene_id)

    @callback
    def _set_scene_suppressed(self, scene_id: str, *, delay: float) -> None:
        runtime = self.scenes.get(scene_id)
        if runtime is None:
            return

        _cancel(runtime.cancel_suppress)
        runtime.cancel_suppress = None

        delay = max(0.0, float(delay))

        runtime.is_active = False
        runtime.post_activation_retries_remaining = 0

        runtime.suppress_on_until = self.hass.loop.time() + delay

        @callback
        def _delayed_eval(_now) -> None:
            self.hass.async_create_task(self.async_evaluate_scene(scene_id))

        runtime.cancel_suppress = async_call_later(self.hass, delay, _delayed_eval)

        self._notify_entity(scene_id)

    async def async_evaluate_scene(self, scene_id: str) -> None:
        """Evaluate scene state at the end of the optimistic/suppression window."""

        runtime = self.scenes.get(scene_id)
        if runtime is None:
            return

        now = self.hass.loop.time()
        if runtime.suppress_on_until is not None and now < runtime.suppress_on_until:
            return

        # Clear guards; we are now authoritative.
        runtime.suppress_on_until = None
        runtime.optimistic_until = None
        runtime.cancel_suppress = None
        runtime.cancel_eval = None

        changed = self._recompute_scene(
            scene_id, touched_entity_id=None, touched_state=None
        )

        # Post-settle retry to reduce ON→OFF→ON flapping.
        if not runtime.is_active and runtime.post_activation_retries_remaining > 0:
            runtime.post_activation_retries_remaining -= 1
            self._set_scene_optimistic(
                scene_id, delay=POST_ACTIVATION_RETRY_DELAY, reset_retries=False
            )
            return

        if changed:
            self._notify_entity(scene_id)

    # ---------------------------------------------------------------------
    # Core evaluation
    # ---------------------------------------------------------------------
    def _recompute_scene(
        self,
        scene_id: str,
        *,
        touched_entity_id: str | None,
        touched_state: State | None,
    ) -> bool:
        """Recompute a scene's active state.

        Returns True if the scene's overall active state changed.
        """

        runtime = self.scenes[scene_id]
        definition = runtime.definition

        states_get = self.hass.states.get
        match = entity_matches
        opts = self.opts

        if touched_entity_id is None:
            runtime.true_count = runtime.false_count = runtime.ignored_count = 0
            for ent_id, expected in definition.entities.items():
                st = states_get(ent_id)
                v = match(st, expected, opts=opts)
                runtime.matches[ent_id] = v
                if v is True:
                    runtime.true_count += 1
                elif v is False:
                    runtime.false_count += 1
                else:
                    runtime.ignored_count += 1
        else:
            expected = definition.entities.get(touched_entity_id)
            if expected is not None:
                old_v = runtime.matches.get(touched_entity_id)
                st = touched_state if touched_state is not None else states_get(touched_entity_id)
                new_v = match(st, expected, opts=opts)

                if new_v != old_v:
                    if old_v is True:
                        runtime.true_count -= 1
                    elif old_v is False:
                        runtime.false_count -= 1
                    elif old_v is None and runtime.ignored_count > 0:
                        runtime.ignored_count -= 1

                    if new_v is True:
                        runtime.true_count += 1
                    elif new_v is False:
                        runtime.false_count += 1
                    else:
                        runtime.ignored_count += 1

                    runtime.matches[touched_entity_id] = new_v

        prev_active = runtime.is_active

        if runtime.false_count > 0:
            runtime.is_active = False
        else:
            non_ignored = runtime.total_entities - runtime.ignored_count
            runtime.is_active = non_ignored > 0 and runtime.true_count == non_ignored

        return runtime.is_active != prev_active

    # ---------------------------------------------------------------------
    # Update filtering
    # ---------------------------------------------------------------------
    @callback
    def _is_interesting_update(
        self,
        runtime: SceneRuntime,
        entity_id: str,
        old_state: State | None,
        new_state: State | None,
    ) -> bool:
        """Return True if this update could affect match status."""

        if old_state is None or new_state is None:
            return True

        expected = runtime.definition.entities.get(entity_id)
        if expected is None:
            return False

        if "state" in expected and old_state.state != new_state.state:
            return True

        if self.opts.ignore_attributes:
            return False

        keys = runtime.watched_attrs.get(entity_id)
        if not keys:
            return False

        old_attrs = old_state.attributes
        new_attrs = new_state.attributes
        return any(old_attrs.get(k) != new_attrs.get(k) for k in keys)
