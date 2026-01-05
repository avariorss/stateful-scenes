# Stateful Scenes (Avario fork)

Home Assistant scenes are *stateless*: you can activate them, but they don’t naturally tell you “I’m currently active”. This integration adds that missing piece by creating a **stateful `switch` entity for every YAML scene** and keeping those switches synchronized with the real-time states of the entities inside each scene.

- Turn **ON** the switch → the underlying `scene.<name>` is activated.
- The switch state (**ON/OFF**) reflects whether the scene is **currently active** (i.e., entity states match the scene definition).
- Turn **OFF** the switch → all entities in the scene are turned off (with configurable exclusions).

This fork is optimized for **fast runtime matching**, supports **scenes loaded from `configuration.yaml`**, and is designed to work smoothly with dynamic scene editors (via a reload service).

---

## Algorithm overview

### What “active” means
A scene is considered **active** when, for every (non-excluded) entity in the scene:

1. The entity’s current **state** matches the scene’s desired state (`on`, `off`, etc.)
2. Any attributes specified in the scene YAML (e.g., brightness, color_temp, position) match the entity’s current attributes.

Matching also includes these **global rules**:

- **Numeric tolerance:** Numeric attributes (brightness, position, etc.) are considered a match if they differ by no more than the configured tolerance.
- **Unavailable handling:** If enabled, entities that are `unavailable` or `unknown` are ignored rather than causing the whole scene to be inactive.
- **Entity exclusions (e.g. circadian):** If enabled, entities matching the configured pattern are excluded entirely from both matching and the “turn off” action.

**Special rule (important):**
- If the scene expects `state: off` and the entity is currently `off`, attributes are treated as satisfied (to avoid false negatives and unnecessary strictness when something is off).

### How updates happen (event-driven, efficient)
The integration is **event-driven**, not polling:

- It builds an index of **which scenes depend on which entities**.
- Whenever any entity in a scene changes, only the scenes that reference that entity are re-evaluated.
- Evaluation is optimized using per-scene counters (so a single entity change is an O(1) update and “active?” can be decided quickly).

### Why there’s a “Settle Time”
Scene activation/deactivation often triggers a burst of state changes:
- lights fade
- groups update in waves
- zigbee devices report out-of-order

To prevent the stateful switch from flapping, the integration uses a **Settle Time** grace window:
- When a scene is activated (either via the switch or via `scene.turn_on` elsewhere), the switch is set **optimistically ON**.
- The integration waits **Settle Time** seconds before doing strict matching.
- A small retry/hysteresis is used to reduce a brief OFF blip right at the end of the settle window.

Similarly, when a scene switch is turned **OFF**, the integration applies a suppression window to avoid “OFF → ON → OFF” bounce while devices turn off.

---

## What entities this integration creates

For each YAML scene, the integration creates:

- `switch.<scene_name_slug>`

Example:
- `scene.relaxed` (native HA scene)
- `switch.relaxed` (stateful scene switch)

The switch:
- appears in the UI
- can be used in automations as a condition (“only if relaxed is active”)
- can be toggled like a normal switch

---

## Supported scene sources

This integration can load scenes from one of three sources:

### 1) From `configuration.yaml` (default)
Uses the scenes that Home Assistant is already loading, including `!include_dir_merge_list` setups.

✅ Recommended if your scenes are managed via include folders.

### 2) Specify scene YAML file
Reads a single scene YAML file (e.g., `scenes.yaml`).

### 3) Specify scene YAML directory
Reads all `*.yaml` and `*.yml` files in a directory.

---

## Installation

### Manual installation
1. Copy the folder `custom_components/stateful_scenes/` into your Home Assistant config folder:
   - `<config>/custom_components/stateful_scenes/`
2. Restart Home Assistant.
3. Add the integration via:
   - **Settings → Devices & services → Add integration → Stateful Scenes**

---

## Configuration & Options

All settings are configured via the integration options UI.

### Scene Source
Where to load scenes from.

- **From configuration.yaml** *(default)*
- **Specify scene YAML file**
- **Specify scene YAML directory**

### Scene Path
A single input used depending on Scene Source:

- If Scene Source = **From configuration.yaml** → ignored
- If Scene Source = **Specify scene YAML file** → path to a file (default: `scenes.yaml`)
- If Scene Source = **Specify scene YAML directory** → path to a directory

Relative paths are resolved from your Home Assistant config directory.

### Settle Time
**Seconds to keep a scene optimistically active after it is turned on before verifying entity states.**

- Prevents ON → OFF → ON flapping during transitions.
- Also used as a suppression window on turn-off to prevent OFF bounce.

### Number Tolerance (default: 4)
**Allowed difference when comparing numeric attributes (brightness 0-255, position, etc.).**

Examples:
- scene expects brightness 90, device reports 88 → match if tolerance ≥ 2
- cover position 100 vs 97 → match if tolerance ≥ 3

### Ignore Unavailable (default: enabled)
When enabled:
- entities whose state is `unavailable` or `unknown` are treated as “ignored” for determining whether a scene is active, instead of making the whole scene inactive.

Useful for sleepy Zigbee devices or occasional dropouts.

### Exclude Circadian Controls (default: enabled)
Excludes entities matching a pattern from:
- **scene matching**
- **turn-off action** when the stateful scene switch is turned off

This is mainly intended to prevent “circadian lighting” control helper switches from interfering.

### Circadian Pattern (default: `switch.circadian_lighting*`)
Glob-style match (supports comma-separated patterns). Examples:
- `switch.circadian_lighting*`
- `switch.circadian_lighting*,switch.adaptive_lighting*`

---

## Behaviour details

### Turning ON a stateful scene switch
When you turn ON `switch.<scene>`:
1. The integration calls `scene.turn_on` for the corresponding HA scene.
2. The switch is set **optimistically ON** immediately.
3. After **Settle Time**, the integration evaluates the real states and keeps ON/OFF accordingly.

### Turning ON a scene via `scene.turn_on`
If you activate `scene.<scene>` directly (automation/UI):
- the integration detects it and applies the same optimistic window for the corresponding switch.

### Turning OFF a stateful scene switch
When you turn OFF `switch.<scene>`:
1. The integration calls `homeassistant.turn_off` for all scene entities
2. Entities matching the exclusion pattern (e.g., circadian) are skipped
3. The scene is **suppressed** from bouncing back ON during the Settle Time window
4. After the window, the switch reflects the true “active” status:
   - Normal scenes should remain OFF
   - “All-off” scenes can show ON again if they match by definition (e.g., a scene that means “everything off”)

---

## Scene identification and mapping

The integration maps YAML scenes to Home Assistant `scene.*` entities using a fallback strategy:

1. If a YAML scene has an explicit `id`, attempt to match it to `scene` entity attributes.
2. Otherwise, guess `scene.<slugified_name>` from the YAML `name`
3. Fallback to matching by `friendly_name` where appropriate

> Tip: if you rename a scene entity in the entity registry so it no longer resembles the YAML name, the switch may not be able to call the correct `scene.turn_on`.

---

## Services

### `stateful_scenes.reload`
Reloads scenes from the configured source and rebuilds internal matching structures.

This is ideal if you have a custom scene editor/creator: update files → call reload → done.

Example automation action:
```yaml
service: stateful_scenes.reload
