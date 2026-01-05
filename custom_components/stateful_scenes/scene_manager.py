"""Runtime manager for stateful scene switches.

Key behaviors (mirrors the original upstream integration's working patterns):

- Track scene *active* state by comparing current entity states/attributes against
  the YAML scene definition.
- Re-evaluate whenever *any* entity referenced by a scene changes.
- Avoid thread-safety violations by ensuring callbacks run in the event loop
  (use @callback and/or async coroutines).
- "Settle time" (naggle/debounce): when a scene is activated, mark it active
  optimistically for a short period before evaluating, to avoid the switch
  bouncing off during transitions and bursty state updates.
"""

from __future__ import annotations

import logging
import fnmatch
from dataclasses import dataclass, field
from typing import Any, Callable

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import slugify

from .matcher import MatchOptions, entity_matches
from .scene_loader import ParsedScene

_LOGGER = logging.getLogger(__name__)

# After the optimistic/settle window ends, we sometimes see a final burst of
# state updates (lights with slow reporting, zigbee groups, etc.). If we evaluate
# exactly at the end of the settle window, the scene can briefly appear inactive
# and then flip back active. To avoid UI flapping, we allow a small, fixed
# post-settle retry window.
POST_ACTIVATION_RETRY_DELAY = 0.75  # seconds
POST_ACTIVATION_MAX_RETRIES = 1


@dataclass
class SceneRuntime:
    definition: ParsedScene
    ha_scene_entity_id: str | None
    matches: dict[str, bool | None] = field(default_factory=dict)
    is_active: bool = False

    # Precomputed keys that matter for each entity (expected keys excluding "state")
    watched_attrs: dict[str, tuple[str, ...]] = field(default_factory=dict)

    # Counters for O(1) active-state computation
    total_entities: int = 0
    true_count: int = 0
    false_count: int = 0
    ignored_count: int = 0

    # optimistic window
    optimistic_until: float | None = None
    _cancel_eval: Callable[[], None] | None = None

    # Post-settle retries to reduce flapping right after activation
    post_activation_retries_remaining: int = 0

    # Deactivation suppression window: when a user turns a scene switch OFF we
    # may still temporarily "match" the scene while member entities are
    # transitioning towards OFF. During this window we suppress re-activating
    # the switch (prevents OFF→ON→OFF bounce). A delayed evaluation runs after
    # the window to determine the authoritative state.
    suppress_on_until: float | None = None
    _cancel_suppress: Callable[[], None] | None = None


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

        self._exclude_circadian = bool(exclude_circadian)
        # Allow comma-separated patterns; empty => no exclusions
        self._exclude_patterns = [p.strip() for p in str(circadian_pattern).split(",") if p.strip()]

        self._listeners: list[Callable[[], None]] = []
        self._entities: dict[str, Any] = {}  # scene_id -> switch entity

        # Store resolver so we can re-resolve later if scenes load after us
        self._resolve_scene_entity_id = resolve_scene_entity_id

        # Build runtimes + indexes
        self.scenes: dict[str, SceneRuntime] = {}
        self._index: dict[str, set[str]] = {}  # member entity_id -> set(scene_id)
        self._ha_scene_to_scene_id: dict[str, str] = {}
        self._guess_ha_eid_to_scene_id: dict[str, str] = {}

        for scene in scenes:
            # Optionally exclude helper entities (e.g. circadian lighting controls) from
            # matching and from the "turn off scene" behavior.
            if self._exclude_circadian and self._exclude_patterns:
                filtered_entities = {
                    ent_id: expected
                    for ent_id, expected in scene.entities.items()
                    if not any(fnmatch.fnmatchcase(ent_id, pat) for pat in self._exclude_patterns)
                }
                if len(filtered_entities) != len(scene.entities):
                    scene = ParsedScene(
                        scene_id=scene.scene_id,
                        name=scene.name,
                        icon=scene.icon,
                        entities=filtered_entities,
                    )

            ha_eid = resolve_scene_entity_id(scene)
            runtime = SceneRuntime(definition=scene, ha_scene_entity_id=ha_eid)

            # Precompute likely HA entity_ids for robustness (used for optimistic handling
            # even if HA states were not available at init time)
            guess_candidates: list[str] = []
            if scene.scene_id:
                guess_candidates.append(f"scene.{scene.scene_id}")
                guess_candidates.append(f"scene.{slugify(scene.scene_id)}")
            if scene.name:
                guess_candidates.append(f"scene.{slugify(scene.name)}")
            for cand in guess_candidates:
                self._guess_ha_eid_to_scene_id.setdefault(cand, scene.scene_id)

            # initialise matches keys and watched attrs so we can update quickly
            for ent_id, expected in scene.entities.items():
                runtime.matches[ent_id] = None
                runtime.watched_attrs[ent_id] = tuple(k for k in expected.keys() if k != "state")
                self._index.setdefault(ent_id, set()).add(scene.scene_id)

            runtime.total_entities = len(runtime.matches)

            self.scenes[scene.scene_id] = runtime
            if ha_eid:
                self._ha_scene_to_scene_id[ha_eid] = scene.scene_id

    # ---------------------------------------------------------------------
    # Entity registration
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
        # Initial evaluation
        for scene_id in list(self.scenes.keys()):
            self._recompute_scene(scene_id, touched_entity_id=None, touched_state=None)

        # Member entity state changes
        if self._index:
            unsub = async_track_state_change_event(
                self.hass,
                list(self._index.keys()),
                self._handle_member_state_change,
            )
            self._listeners.append(unsub)
        else:
            _LOGGER.warning("No entities referenced by any loaded scenes")

        # Service calls to detect external scene activations (scene.turn_on)
        unsub2 = self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._handle_call_service)
        self._listeners.append(unsub2)

    async def async_stop(self) -> None:
        for unsub in self._listeners:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._listeners.clear()

        # cancel any pending eval timers
        for runtime in self.scenes.values():
            if runtime._cancel_eval:
                try:
                    runtime._cancel_eval()
                except Exception:  # noqa: BLE001
                    pass
                runtime._cancel_eval = None
            if runtime._cancel_suppress:
                try:
                    runtime._cancel_suppress()
                except Exception:  # noqa: BLE001
                    pass
                runtime._cancel_suppress = None
            runtime.optimistic_until = None
            runtime.suppress_on_until = None

    # ---------------------------------------------------------------------
    # Activation (called by our switch)
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

        # If the user previously turned this scene OFF, we may still be within
        # a suppression window that prevents the switch from re-activating.
        # Activating the scene should always clear that suppression.
        if runtime._cancel_suppress:
            try:
                runtime._cancel_suppress()
            except Exception:  # noqa: BLE001
                pass
            runtime._cancel_suppress = None
        runtime.suppress_on_until = None

        # Mark optimistic immediately, and re-evaluate after settle_time.
        self._set_scene_optimistic(scene_id, delay=self.settle_time)

        # Activate the underlying HA scene. We intentionally do not inject a
        # default transition here, so behaviour matches a normal scene activation
        # (users can still pass `transition` when calling scene.turn_on directly).
        await self.hass.services.async_call(
            "scene",
            "turn_on",
            {"entity_id": ha_eid},
            blocking=True,
        )

    async def async_turn_off_scene(self, scene_id: str) -> None:
        """Turn off all member entities of a scene.

        This is an explicit user action (turning the stateful switch OFF),
        so we do not attempt to "restore" prior states. We simply turn off
        all entities listed in the scene definition, excluding any entities
        filtered out by the exclusion pattern(s).
        """
        runtime = self.scenes.get(scene_id)
        if not runtime:
            _LOGGER.error("Unknown scene_id=%s", scene_id)
            return

        # Cancel any optimistic activation timer.
        if runtime._cancel_eval:
            try:
                runtime._cancel_eval()
            except Exception:  # noqa: BLE001
                pass
            runtime._cancel_eval = None

        runtime.optimistic_until = None
        runtime.post_activation_retries_remaining = 0

        # Start a suppression window so this scene switch doesn't bounce back ON
        # while entities are transitioning towards off.
        self._set_scene_suppressed(scene_id, delay=self.settle_time)

        entity_ids = list(runtime.definition.entities.keys())
        # Safety: even though we filter the definition at load time, apply the
        # exclusion patterns again here so a misconfigured runtime can't turn off
        # circadian helper entities.
        if self._exclude_circadian and self._exclude_patterns and entity_ids:
            entity_ids = [
                eid
                for eid in entity_ids
                if not any(fnmatch.fnmatchcase(eid, pat) for pat in self._exclude_patterns)
            ]
        if not entity_ids:
            return

        try:
            # Best-effort: some domains may not implement turn_off.
            await self.hass.services.async_call(
                "homeassistant",
                "turn_off",
                {"entity_id": entity_ids},
                blocking=False,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to turn off entities for %s: %s", scene_id, err)

        # Note: we do NOT evaluate immediately. Instead, we evaluate at the end
        # of the suppression window. This prevents OFF→ON→OFF bounce on scenes
        # that still match briefly while turning entities off, and still allows
        # "all off" scenes to bounce back to ON when they remain active.


    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------
    @callback
    def _notify_entity(self, scene_id: str) -> None:
        ent = self._entities.get(scene_id)
        if ent is None:
            return
        # Push state update (we are on the event loop; safe & cheapest).
        ent.async_write_ha_state()

    @callback
    def _handle_member_state_change(self, event) -> None:
        """Triggered when any member entity changes.

        We only recompute for scenes that actually reference this entity.
        Additionally, we avoid needless work by checking whether the update is
        "interesting" for a given scene (i.e., a relevant state/attribute changed).
        """

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

            # During optimistic window, do not evaluate (prevents bounce)
            if runtime.optimistic_until is not None and now < runtime.optimistic_until:
                continue

            # During suppression window (user turned switch OFF), do not
            # re-activate this scene based on transient matching states.
            if runtime.suppress_on_until is not None and now < runtime.suppress_on_until:
                continue

            # Skip uninteresting updates (saves lots of churn for light attribute noise)
            if not self._is_interesting_update(runtime, entity_id, old_state, new_state):
                continue

            if self._recompute_scene(scene_id, touched_entity_id=entity_id, touched_state=new_state):
                self._notify_entity(scene_id)

    @callback
    def _handle_call_service(self, event) -> None:
        """Detect external scene.turn_on calls to apply optimistic window."""
        data = event.data or {}
        if data.get("domain") != "scene":
            return
        if data.get("service") != "turn_on":
            return

        service_data = data.get("service_data") or {}
        target = data.get("target") or {}

        # entity_id can be a string or list, and can appear either in target or service_data
        entity_ids: list[str] = []

        def _extend(val: Any) -> None:
            if isinstance(val, str):
                entity_ids.append(val)
            elif isinstance(val, list):
                for x in val:
                    if isinstance(x, str):
                        entity_ids.append(x)

        _extend(target.get("entity_id"))
        _extend(service_data.get("entity_id"))

        if not entity_ids:
            return

        # If a transition is explicitly provided in the service call,
        # wait at least that long before evaluating. Otherwise, use settle_time.
        transition_val = service_data.get("transition")
        transition: float | None = None
        try:
            if transition_val is not None:
                transition = float(transition_val)
        except (TypeError, ValueError):
            transition = None

        delay = self.settle_time
        if transition is not None:
            delay = max(delay, max(0.0, transition))

        for ha_scene_eid in entity_ids:
            scene_id = self._ha_scene_to_scene_id.get(ha_scene_eid) or self._guess_ha_eid_to_scene_id.get(ha_scene_eid)
            if not scene_id:
                continue

            # External activation should override any suppression window.
            runtime = self.scenes.get(scene_id)
            if runtime is not None:
                if runtime._cancel_suppress:
                    try:
                        runtime._cancel_suppress()
                    except Exception:  # noqa: BLE001
                        pass
                    runtime._cancel_suppress = None
                runtime.suppress_on_until = None

            # If we only matched via guess, store the mapping for next time.
            runtime = self.scenes.get(scene_id)
            if runtime is not None and runtime.ha_scene_entity_id is None:
                runtime.ha_scene_entity_id = ha_scene_eid
                self._ha_scene_to_scene_id[ha_scene_eid] = scene_id

            self._set_scene_optimistic(scene_id, delay=delay)

    @callback
    def _set_scene_optimistic(self, scene_id: str, *, delay: float, reset_retries: bool = True) -> None:
        runtime = self.scenes.get(scene_id)
        if runtime is None:
            return

        # Cancel previous timer (if any)
        if runtime._cancel_eval:
            try:
                runtime._cancel_eval()
            except Exception:  # noqa: BLE001
                pass
            runtime._cancel_eval = None

        delay = max(0.0, float(delay))
        runtime.is_active = True
        if reset_retries:
            runtime.post_activation_retries_remaining = POST_ACTIVATION_MAX_RETRIES

        # Set optimistic window
        runtime.optimistic_until = self.hass.loop.time() + delay

        # Schedule evaluation after delay
        @callback
        def _delayed_eval(_now) -> None:
            # Must run on the event loop; if this callable is not decorated with
            # @callback, Avario Home may run it in an executor thread, and then
            # hass.async_create_task would be unsafe.
            self.hass.async_create_task(self.async_evaluate_scene(scene_id))

        runtime._cancel_eval = async_call_later(
            self.hass,
            delay,
            _delayed_eval,
        )

        self._notify_entity(scene_id)

    @callback
    def _set_scene_suppressed(self, scene_id: str, *, delay: float) -> None:
        """Suppress a scene from re-activating for a short period.

        This is used after the user turns a scene switch OFF. While member
        entities are transitioning towards OFF, the scene may still temporarily
        match its definition (causing OFF→ON→OFF bounce). During the suppression
        window we skip evaluations for this scene and schedule a single
        authoritative evaluation at the end.
        """

        runtime = self.scenes.get(scene_id)
        if runtime is None:
            return

        # Cancel previous suppression timer (if any)
        if runtime._cancel_suppress:
            try:
                runtime._cancel_suppress()
            except Exception:  # noqa: BLE001
                pass
            runtime._cancel_suppress = None

        delay = max(0.0, float(delay))

        # Suppression implies we are currently OFF
        runtime.is_active = False
        runtime.post_activation_retries_remaining = 0

        runtime.suppress_on_until = self.hass.loop.time() + delay

        @callback
        def _delayed_eval(_now) -> None:
            self.hass.async_create_task(self.async_evaluate_scene(scene_id))

        runtime._cancel_suppress = async_call_later(self.hass, delay, _delayed_eval)

        self._notify_entity(scene_id)

    async def async_evaluate_scene(self, scene_id: str) -> None:
        """Evaluate scene state at the end of the optimistic window."""
        runtime = self.scenes.get(scene_id)
        if runtime is None:
            return

        # If a suppression window is active, do not evaluate early.
        now = self.hass.loop.time()
        if runtime.suppress_on_until is not None and now < runtime.suppress_on_until:
            return

        # Suppression window has ended (or was not set); clear it.
        runtime.suppress_on_until = None
        if runtime._cancel_suppress:
            runtime._cancel_suppress = None

        # Clear optimistic guard first; we are now authoritative
        runtime.optimistic_until = None
        if runtime._cancel_eval:
            runtime._cancel_eval = None

        changed = self._recompute_scene(scene_id, touched_entity_id=None, touched_state=None)

        # If we just left an optimistic window and the first authoritative evaluation
        # says the scene is inactive, allow a short retry window before showing OFF.
        # This avoids ON→OFF→ON flapping when devices are still finishing transitions.
        if not runtime.is_active and runtime.post_activation_retries_remaining > 0:
            runtime.post_activation_retries_remaining -= 1

            # Keep the scene optimistic a little longer, then evaluate again.
            self._set_scene_optimistic(scene_id, delay=POST_ACTIVATION_RETRY_DELAY, reset_retries=False)
            return

        if changed:
            self._notify_entity(scene_id)

    def _recompute_scene(
        self,
        scene_id: str,
        *,
        touched_entity_id: str | None,
        touched_state: State | None,
    ) -> bool:
        """Recompute a scene's active state.

        Returns True if the scene's *overall* active state changed.
        """
        runtime = self.scenes[scene_id]
        definition = runtime.definition

        # Micro-optimisations: avoid attribute lookups inside tight loops
        states_get = self.hass.states.get
        match = entity_matches
        opts = self.opts

        if touched_entity_id is None:
            # Full recompute (startup, end of optimistic window)
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
                # Incremental update with counters
                old_v = runtime.matches.get(touched_entity_id)
                st = touched_state if touched_state is not None else states_get(touched_entity_id)
                new_v = match(st, expected, opts=opts)
                if new_v != old_v:
                    # Remove old bucket
                    if old_v is True:
                        runtime.true_count -= 1
                    elif old_v is False:
                        runtime.false_count -= 1
                    elif old_v is None:
                        # Guard against counter drift
                        if runtime.ignored_count > 0:
                            runtime.ignored_count -= 1
                    # Add new bucket
                    if new_v is True:
                        runtime.true_count += 1
                    elif new_v is False:
                        runtime.false_count += 1
                    else:
                        runtime.ignored_count += 1
                    runtime.matches[touched_entity_id] = new_v

        prev_active = runtime.is_active

        # Determine active (O(1)):
        # - Any False => inactive
        # - Otherwise, all non-ignored must be True, and there must be at least one non-ignored
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
        """Return True if this update could affect match status.

        We avoid recomputation when only irrelevant attributes change.
        This becomes important with lights that update many attributes frequently.
        """

        if old_state is None or new_state is None:
            return True

        expected = runtime.definition.entities.get(entity_id)
        if expected is None:
            return False

        # If the scene cares about state and it changed, it's interesting
        if "state" in expected and old_state.state != new_state.state:
            return True

        if self.opts.ignore_attributes:
            return False

        keys = runtime.watched_attrs.get(entity_id)
        if not keys:
            return False

        old_attrs = old_state.attributes
        new_attrs = new_state.attributes
        for k in keys:
            if old_attrs.get(k) != new_attrs.get(k):
                return True
        return False
