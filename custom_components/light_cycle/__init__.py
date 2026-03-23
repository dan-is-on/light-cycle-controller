"""Light Cycle Controller integration."""

from __future__ import annotations

import asyncio
import colorsys
import logging
import time
from collections import Counter
from typing import Any, Callable

import voluptuous as vol

from homeassistant.components.light import ATTR_BRIGHTNESS, DOMAIN as LIGHT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_ARGS,
    CONF_CLUSTER_ID,
    CONF_COMMAND,
    CONF_DOUBLE_PRESS_BINDING,
    CONF_ENDPOINT_ID,
    CONF_GESTURE_TARGET_INDEX,
    CONF_LONG_PRESS_BINDING,
    CONF_REMOTE_IEEE,
    CONF_STEP_BRIGHTNESS_PCT,
    CONF_STEP_COLOR_HEX,
    CONF_STEP_COLOR_RGB,
    CONF_STEP_MODE,
    CONF_STEP_TEMP_PCT,
    CONF_STEPS,
    CONF_TARGET_ENTITY_ID,
    CONF_TARGET_ENTITY_IDS,
    DEFAULT_STEP_COLOR_HEX,
    DEFAULT_STEP_COLOR_RGB,
    DEFAULT_STEP_MODE,
    DEFAULT_STEP_TEMP_PCT,
    DEFAULT_TEMP_MAX_KELVIN,
    DEFAULT_TEMP_MIN_KELVIN,
    DOMAIN,
    GESTURE_DOUBLE_PRESS,
    GESTURE_LONG_PRESS,
    STEP_MODE_COLOR,
    STEP_MODE_WHITE_TEMP,
)
from .settings import async_get_settings, get_max_parallel_calls

LOGGER = logging.getLogger(__name__)

EVENT_ZHA_EVENT = "zha_event"

DATA_CONTROLLERS = "controllers"
DATA_SERVICES_REGISTERED = "services_registered"

SERVICE_DUMP = "dump"


def _coerce_target_entity_ids(data: dict[str, Any]) -> list[str]:
    """Return de-duplicated target entity IDs from entry data/options."""
    raw_targets = data.get(CONF_TARGET_ENTITY_IDS)
    values: list[Any]
    if isinstance(raw_targets, str):
        values = [raw_targets]
    elif isinstance(raw_targets, (list, tuple, set)):
        values = list(raw_targets)
    else:
        values = []

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        entity_id = raw.strip()
        if not entity_id.startswith(f"{LIGHT_DOMAIN}."):
            continue
        if entity_id in seen:
            continue
        seen.add(entity_id)
        normalized.append(entity_id)

    if normalized:
        return normalized

    legacy_target = data.get(CONF_TARGET_ENTITY_ID)
    if isinstance(legacy_target, str) and legacy_target.startswith(f"{LIGHT_DOMAIN}."):
        return [legacy_target]

    return []


def _coerce_rgb_channel(value: Any) -> int | None:
    """Parse a single RGB channel and return a bounded 0..255 value."""
    try:
        channel = int(value)
    except (TypeError, ValueError):
        return None
    if not (0 <= channel <= 255):
        return None
    return channel


def _normalize_hex_color(value: Any) -> str | None:
    """Normalize supported hex input to uppercase `#RRGGBB` format."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.startswith("#"):
        raw = raw[1:]
    if len(raw) != 6:
        return None
    try:
        int(raw, 16)
    except ValueError:
        return None
    return f"#{raw.upper()}"


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    """Convert an RGB tuple to a `#RRGGBB` string."""
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def _parse_rgb_color(value: Any) -> tuple[int, int, int] | None:
    """Parse RGB values from multiple UI/storage formats.

    Supported inputs:
    - `[r, g, b]` / `(r, g, b)`
    - `{"r": 1, "g": 2, "b": 3}`
    - `{"red": 1, "green": 2, "blue": 3}`
    - `#RRGGBB`
    """
    if isinstance(value, (list, tuple)) and len(value) == 3:
        channels = [_coerce_rgb_channel(channel) for channel in value]
        if all(channel is not None for channel in channels):
            return int(channels[0]), int(channels[1]), int(channels[2])
        return None

    if isinstance(value, dict):
        if {"r", "g", "b"}.issubset(value):
            channels = [
                _coerce_rgb_channel(value.get("r")),
                _coerce_rgb_channel(value.get("g")),
                _coerce_rgb_channel(value.get("b")),
            ]
            if all(channel is not None for channel in channels):
                return int(channels[0]), int(channels[1]), int(channels[2])
        if {"red", "green", "blue"}.issubset(value):
            channels = [
                _coerce_rgb_channel(value.get("red")),
                _coerce_rgb_channel(value.get("green")),
                _coerce_rgb_channel(value.get("blue")),
            ]
            if all(channel is not None for channel in channels):
                return int(channels[0]), int(channels[1]), int(channels[2])
        return None

    normalized_hex = _normalize_hex_color(value)
    if normalized_hex is None:
        return None
    raw = normalized_hex[1:]
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


def _normalize_step_mode(value: Any) -> str:
    """Return a supported step mode with a safe default."""
    if str(value or "").strip().lower() == STEP_MODE_COLOR:
        return STEP_MODE_COLOR
    return STEP_MODE_WHITE_TEMP


def _normalize_step(step: Any) -> dict[str, Any]:
    """Normalize one step dictionary to the full schema expected at runtime."""
    if not isinstance(step, dict):
        return {
            CONF_STEP_LABEL: "Step",
            CONF_STEP_BRIGHTNESS_PCT: 100,
            CONF_STEP_MODE: DEFAULT_STEP_MODE,
            CONF_STEP_TEMP_PCT: DEFAULT_STEP_TEMP_PCT,
            CONF_STEP_COLOR_HEX: DEFAULT_STEP_COLOR_HEX,
            CONF_STEP_COLOR_RGB: list(DEFAULT_STEP_COLOR_RGB),
        }

    normalized: dict[str, Any] = dict(step)
    normalized[CONF_STEP_MODE] = _normalize_step_mode(step.get(CONF_STEP_MODE))

    try:
        brightness_pct = int(step.get(CONF_STEP_BRIGHTNESS_PCT, 100))
    except (TypeError, ValueError):
        brightness_pct = 100
    normalized[CONF_STEP_BRIGHTNESS_PCT] = max(1, min(100, brightness_pct))

    try:
        temp_pct = int(step.get(CONF_STEP_TEMP_PCT, DEFAULT_STEP_TEMP_PCT))
    except (TypeError, ValueError):
        temp_pct = DEFAULT_STEP_TEMP_PCT
    normalized[CONF_STEP_TEMP_PCT] = max(0, min(100, temp_pct))

    rgb = _parse_rgb_color(step.get(CONF_STEP_COLOR_RGB))
    hex_color = _normalize_hex_color(step.get(CONF_STEP_COLOR_HEX))
    if rgb is None and hex_color is not None:
        rgb = _parse_rgb_color(hex_color)
    if rgb is None:
        rgb = tuple(DEFAULT_STEP_COLOR_RGB)
    if hex_color is None:
        hex_color = _rgb_to_hex(rgb)

    normalized[CONF_STEP_COLOR_RGB] = [rgb[0], rgb[1], rgb[2]]
    normalized[CONF_STEP_COLOR_HEX] = hex_color
    return normalized


def _normalize_steps(steps: Any) -> list[dict[str, Any]]:
    """Normalize a sequence of step definitions."""
    if not isinstance(steps, list):
        return []
    return [_normalize_step(step) for step in steps]


def _normalize_signature(
    endpoint_id: Any,
    command: Any,
    cluster_id: Any,
    args: Any,
) -> dict[str, Any] | None:
    """Normalize stored/captured signature fields to a stable matching structure."""
    if endpoint_id is None or command is None:
        return None

    try:
        normalized_endpoint_id = int(endpoint_id)
    except (TypeError, ValueError):
        return None

    normalized_cluster_id: int | None = None
    if cluster_id is not None:
        try:
            normalized_cluster_id = int(cluster_id)
        except (TypeError, ValueError):
            return None

    normalized_args = list(args) if args is not None else None
    return {
        CONF_ENDPOINT_ID: normalized_endpoint_id,
        CONF_COMMAND: str(command),
        CONF_CLUSTER_ID: normalized_cluster_id,
        CONF_ARGS: normalized_args,
    }


def _normalize_stored_gesture_binding(
    binding: Any, max_target_index: int
) -> dict[str, Any] | None:
    """Normalize one optional long/double press binding from storage."""
    if not isinstance(binding, dict):
        return None

    signature = _normalize_signature(
        binding.get(CONF_ENDPOINT_ID),
        binding.get(CONF_COMMAND),
        binding.get(CONF_CLUSTER_ID),
        binding.get(CONF_ARGS),
    )
    if signature is None:
        return None

    try:
        target_index = int(binding.get(CONF_GESTURE_TARGET_INDEX, 0))
    except (TypeError, ValueError):
        target_index = 0

    return {
        **signature,
        CONF_GESTURE_TARGET_INDEX: max(0, min(max_target_index, target_index)),
    }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Light Cycle Controller from a config entry."""
    # Keep all runtime integration state under `hass.data[DOMAIN]`.
    domain_data = hass.data.setdefault(DOMAIN, {})
    controllers: dict[str, LightCycleController] = domain_data.setdefault(DATA_CONTROLLERS, {})
    # Load persisted global settings early so controllers can read cached values immediately.
    await async_get_settings(hass)

    if not domain_data.get(DATA_SERVICES_REGISTERED):
        # Register services only once per integration domain, not once per entry.
        async def _handle_dump(call) -> None:
            await _async_handle_dump_service(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_DUMP,
            _handle_dump,
            schema=vol.Schema({vol.Optional("entry_id"): str}),
        )
        domain_data[DATA_SERVICES_REGISTERED] = True

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Create and start one runtime controller per config entry.
    controller = LightCycleController(hass, entry)
    await controller.async_start()
    controllers[entry.entry_id] = controller
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entries to newer schema versions."""
    if entry.version > 3:
        LOGGER.error("Unsupported config entry version %s", entry.version)
        return False

    if entry.version == 3:
        return True

    new_data = dict(entry.data)
    new_options = dict(entry.options)
    version = entry.version

    if version < 2:
        LOGGER.info(
            "Migrating entry %s from version %s to 2", entry.entry_id, entry.version
        )

        # Copy both sections because older entries may store values in either data or options.
        # Ensure every migrated step has explicit mode/temp/color fields so runtime logic is stable.
        if CONF_STEPS in new_data:
            new_data[CONF_STEPS] = _normalize_steps(new_data.get(CONF_STEPS))
        if CONF_STEPS in new_options:
            new_options[CONF_STEPS] = _normalize_steps(new_options.get(CONF_STEPS))
        version = 2

    if version < 3:
        LOGGER.info("Migrating entry %s from version %s to 3", entry.entry_id, version)

        for container in (new_data, new_options):
            max_target_index = len(_normalize_steps(container.get(CONF_STEPS)))
            for binding_key in (CONF_LONG_PRESS_BINDING, CONF_DOUBLE_PRESS_BINDING):
                normalized_binding = _normalize_stored_gesture_binding(
                    container.get(binding_key),
                    max_target_index,
                )
                if normalized_binding is not None:
                    container[binding_key] = normalized_binding
                else:
                    container.pop(binding_key, None)
        version = 3

    hass.config_entries.async_update_entry(
        entry,
        data=new_data,
        options=new_options,
        version=version,
    )
    LOGGER.info("Migration of entry %s to version %s complete", entry.entry_id, version)
    return True


async def _async_handle_dump_service(hass: HomeAssistant, call) -> None:
    """Dump controller/entry state to logs (for debugging)."""
    # Resolve currently loaded controller objects from integration state.
    domain_data = hass.data.get(DOMAIN, {})
    controllers: dict[str, LightCycleController] = domain_data.get(DATA_CONTROLLERS, {})

    # Optional `entry_id` allows users to focus on one controller.
    requested_entry_id = call.data.get("entry_id")
    entry_ids = [requested_entry_id] if requested_entry_id else list(controllers.keys())

    if not entry_ids:
        LOGGER.info("Dump: no loaded controllers")
        return

    for entry_id in entry_ids:
        controller = controllers.get(entry_id)
        entry = hass.config_entries.async_get_entry(entry_id)

        if controller is None:
            LOGGER.info("Dump: entry=%s not loaded", entry_id)
            continue

        merged = controller._merged_entry_data()
        steps = merged.get(CONF_STEPS, [])
        steps_list = steps if isinstance(steps, list) else []

        # Capture the currently configured target and a quick snapshot of its live state.
        target_entity_ids = _coerce_target_entity_ids(merged)
        primary_target = target_entity_ids[0] if target_entity_ids else None
        target_state = hass.states.get(primary_target) if primary_target else None
        target_state_str = None if target_state is None else target_state.state
        target_brightness = None if target_state is None else target_state.attributes.get(ATTR_BRIGHTNESS)
        try:
            classified = controller._classify_state()
        except Exception:
            classified = None

        try:
            next_from_classified = (
                None
                if classified is None
                else (int(classified) + 1) % (len(controller._steps) + 1)
            )
        except Exception:
            next_from_classified = None

        try:
            next_from_resolved = (int(controller._resolved_index) + 1) % (
                len(controller._steps) + 1
            )
        except Exception:
            next_from_resolved = None

        expanded_targets = controller._expanded_target_entity_ids()
        member_summary: dict[str, Any] | None = None
        if expanded_targets:
            # Summarize each expanded member so collection-level behavior is easier to debug.
            votes: Counter[int] = Counter()
            counts: Counter[str] = Counter()
            sample: list[dict[str, Any]] = []

            for entity_id in expanded_targets:
                st = hass.states.get(entity_id)
                if st is None:
                    counts["missing"] += 1
                    continue

                counts[f"state_{st.state}"] += 1
                if len(sample) < 10:
                    sample.append(
                        {
                            "entity_id": entity_id,
                            "state": st.state,
                            "brightness": st.attributes.get(ATTR_BRIGHTNESS),
                            "color_mode": st.attributes.get("color_mode"),
                        }
                    )

                if st.state != STATE_ON:
                    continue

                pct = controller._brightness_pct_from_state(st)
                if pct is None:
                    counts["on_no_brightness"] += 1
                    continue

                # Show how many members map nearest to each configured step.
                votes[controller._nearest_step_for_pct(pct)] += 1

            member_summary = {
                "total": len(expanded_targets),
                "counts": dict(counts),
                "step_votes": dict(votes),
                "sample": sample,
                "average_pct": round(getattr(controller, "_last_average_pct", 0.0), 2),
            }

        LOGGER.info(
            "Dump: entry=%s title=%s targets=%s primary_state=%s primary_brightness=%s controller_steps=%s entry_steps=%s resolved=%s classified=%s next(resolved)=%s next(classified)=%s expanded_targets=%s average_pct=%.2f max_parallel_calls=%s",
            controller.entry.entry_id,
            (entry.title if entry is not None else controller.entry.title),
            target_entity_ids,
            target_state_str,
            target_brightness,
            len(controller._steps),
            len(steps_list),
            controller._resolved_index,
            classified,
            next_from_resolved,
            next_from_classified,
            len(expanded_targets),
            getattr(controller, "_last_average_pct", 0.0),
            controller._max_parallel_calls(),
        )
        if member_summary is not None:
            LOGGER.info(
                "Dump: entry=%s members=%s",
                controller.entry.entry_id,
                member_summary,
            )
        LOGGER.info(
            "Dump: entry=%s ieee=%s endpoint=%s command=%s cluster_id=%s args=%s",
            controller.entry.entry_id,
            merged.get(CONF_REMOTE_IEEE),
            merged.get(CONF_ENDPOINT_ID),
            merged.get(CONF_COMMAND),
            merged.get(CONF_CLUSTER_ID),
            merged.get(CONF_ARGS),
        )
        for gesture_name, binding_key in (
            (GESTURE_LONG_PRESS, CONF_LONG_PRESS_BINDING),
            (GESTURE_DOUBLE_PRESS, CONF_DOUBLE_PRESS_BINDING),
        ):
            binding = _normalize_stored_gesture_binding(
                merged.get(binding_key),
                len(controller._steps),
            )
            LOGGER.info(
                "Dump: entry=%s gesture=%s target_index=%s endpoint=%s command=%s cluster_id=%s args=%s",
                controller.entry.entry_id,
                gesture_name,
                None if binding is None else binding.get(CONF_GESTURE_TARGET_INDEX),
                None if binding is None else binding.get(CONF_ENDPOINT_ID),
                None if binding is None else binding.get(CONF_COMMAND),
                None if binding is None else binding.get(CONF_CLUSTER_ID),
                None if binding is None else binding.get(CONF_ARGS),
            )
        LOGGER.info(
            "Dump: entry=%s steps=%s",
            controller.entry.entry_id,
            [
                {
                    "label": step.get("label"),
                    "brightness_pct": step.get(CONF_STEP_BRIGHTNESS_PCT),
                    "mode": step.get(CONF_STEP_MODE),
                    "temp_pct": step.get(CONF_STEP_TEMP_PCT),
                    "color_hex": step.get(CONF_STEP_COLOR_HEX),
                    "color_rgb": step.get(CONF_STEP_COLOR_RGB),
                }
                for step in steps_list
                if isinstance(step, dict)
            ],
        )


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry updates (options/data) by restarting the controller."""
    steps = entry.options.get(CONF_STEPS, entry.data.get(CONF_STEPS, []))
    steps_len = len(steps) if isinstance(steps, list) else "?"
    LOGGER.info("Entry %s updated; reloading (steps=%s)", entry.entry_id, steps_len)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return True

    controllers: dict[str, LightCycleController] = domain_data.get(DATA_CONTROLLERS, {})
    controller: LightCycleController | None = controllers.pop(entry.entry_id, None)
    if controller is not None:
        # Stop event listeners so this entry no longer reacts after removal/reload.
        await controller.async_stop()

    if not controllers:
        # If no entries remain, remove domain services and clear integration runtime state.
        if domain_data.get(DATA_SERVICES_REGISTERED):
            hass.services.async_remove(DOMAIN, SERVICE_DUMP)
        hass.data.pop(DOMAIN, None)
    return True


class LightCycleController:
    """Runtime controller for a single config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        # Persist Home Assistant and config entry references for lifecycle operations.
        self.hass = hass
        self.entry = entry

        # Merge entry data/options so the controller always starts from latest saved values.
        data: dict[str, Any] = {**entry.data, **entry.options}

        # Core matching configuration for the remote button and target collection.
        self._target_entity_ids: list[str] = _coerce_target_entity_ids(data)
        self._remote_ieee: str = data[CONF_REMOTE_IEEE]

        # Normalized step configuration and runtime caches derived from target entities.
        self._steps: list[dict[str, Any]] = _normalize_steps(data[CONF_STEPS])
        self._cycle_signature: dict[str, Any] = (
            _normalize_signature(
                data.get(CONF_ENDPOINT_ID),
                data.get(CONF_COMMAND),
                data.get(CONF_CLUSTER_ID),
                data.get(CONF_ARGS),
            )
            or {}
        )
        self._gesture_bindings: dict[str, dict[str, Any]] = {}
        self._expanded_targets_cache: list[str] = []
        self._targets_cache_dirty: bool = True
        self._watched_state_entity_ids: list[str] = []
        self._is_tuya_cache: dict[str, bool] = {}
        self._temp_range_cache: dict[str, tuple[int, int]] = {}

        # Event unsubscription callbacks are stored so unload/reload can cleanly detach.
        self._unsub_zha: Callable[[], None] | None = None
        self._unsub_state: Callable[[], None] | None = None

        # Runtime coordination and classification state.
        self._press_lock = asyncio.Lock()
        self._resolved_index: int = 0
        self._ignore_state_changes_until: float = 0.0
        self._last_average_pct: float = 0.0
        self._last_sample_counts: dict[str, int] = {}

    async def async_start(self) -> None:
        """Start listening for button presses and light state changes."""
        # Guard against accidental double-starts during reload races.
        if self._unsub_zha is not None or self._unsub_state is not None:
            return

        # Ensure local caches are in sync before subscriptions begin.
        self._refresh_targets_from_entry()
        self._refresh_steps_from_entry()
        self._refresh_event_bindings_from_entry()
        self._refresh_expanded_targets(force=True)
        self._resubscribe_state_listener()

        # Subscribe to ZHA button events and initialize current cycle position.
        self._unsub_zha = self.hass.bus.async_listen(EVENT_ZHA_EVENT, self._on_zha_event)
        self._resolved_index = self._classify_state()

        LOGGER.info(
            "Started controller %s for %s (expanded=%s steps=%s ieee=%s endpoint=%s command=%s)",
            self.entry.entry_id,
            self._target_entity_ids,
            len(self._expanded_targets_cache),
            len(self._steps),
            self._remote_ieee,
            self._cycle_signature.get(CONF_ENDPOINT_ID),
            self._cycle_signature.get(CONF_COMMAND),
        )
        LOGGER.info(
            "Controller %s steps for %s: %s optional_gestures=%s",
            self.entry.entry_id,
            self._target_entity_ids,
            [s.get(CONF_STEP_BRIGHTNESS_PCT) for s in self._steps],
            sorted(self._gesture_bindings),
        )

    async def async_stop(self) -> None:
        """Stop event subscriptions for this controller."""
        if self._unsub_zha is not None:
            self._unsub_zha()
            self._unsub_zha = None
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None

    @callback
    def _on_state_change(self, event: Event) -> None:
        """Handle state changes for watched entities and keep resolved index current."""
        # Ignore transient state churn while a button-triggered apply is still settling.
        if time.monotonic() < self._ignore_state_changes_until:
            return

        old_state: State | None = event.data.get("old_state")
        new_state: State | None = event.data.get("new_state")

        if self._group_membership_changed(old_state, new_state):
            # Re-expand targets on next use when group membership changes.
            self._targets_cache_dirty = True

        # Re-read latest config entry values so configure changes apply immediately.
        self._refresh_targets_from_entry()
        self._refresh_steps_from_entry()
        self._resolved_index = self._classify_state(new_state)

    @callback
    def _on_zha_event(self, event: Event) -> None:
        """Schedule press handling for matching ZHA events."""
        # Re-read saved signatures so freshly edited gesture bindings are reflected promptly.
        self._refresh_steps_from_entry()
        self._refresh_event_bindings_from_entry()

        match = self._matched_action_for_event(event.data)
        if match is None:
            return

        # Run press handling asynchronously so event bus processing stays non-blocking.
        action, target_index = match
        if action == "cycle":
            self.hass.async_create_task(self._async_handle_press())
            return

        self.hass.async_create_task(
            self._async_handle_direct_target(action, int(target_index or 0))
        )

    def _matches_signature(
        self, signature: dict[str, Any] | None, data: dict[str, Any]
    ) -> bool:
        """Return whether an incoming zha_event matches one normalized signature."""
        if not signature:
            return False

        device_ieee = data.get("device_ieee")
        if device_ieee != self._remote_ieee:
            return False

        endpoint_id = data.get(CONF_ENDPOINT_ID)
        try:
            endpoint_id_int = int(endpoint_id)
        except (TypeError, ValueError):
            return False
        if endpoint_id_int != int(signature[CONF_ENDPOINT_ID]):
            return False

        command = data.get(CONF_COMMAND)
        if command is None or str(command) != str(signature[CONF_COMMAND]):
            return False

        cluster_id = signature.get(CONF_CLUSTER_ID)
        if cluster_id is not None:
            # Optional cluster filtering helps disambiguate remotes with reused commands.
            event_cluster_id = data.get(CONF_CLUSTER_ID)
            try:
                event_cluster_id_int = int(event_cluster_id)
            except (TypeError, ValueError):
                return False
            if event_cluster_id_int != int(cluster_id):
                return False

        args = signature.get(CONF_ARGS)
        if args is not None:
            # Optional args filtering helps disambiguate field-specific button payloads.
            event_args = data.get(CONF_ARGS, [])
            if list(event_args) != list(args):
                return False

        return True

    def _matched_action_for_event(
        self, data: dict[str, Any]
    ) -> tuple[str, int | None] | None:
        """Return the controller action for one incoming zha_event, if any."""
        if self._matches_signature(self._cycle_signature, data):
            return ("cycle", None)

        for gesture_name in (GESTURE_LONG_PRESS, GESTURE_DOUBLE_PRESS):
            binding = self._gesture_bindings.get(gesture_name)
            if binding is None:
                continue
            if self._matches_signature(binding, data):
                return (
                    gesture_name,
                    int(binding.get(CONF_GESTURE_TARGET_INDEX, 0)),
                )

        return None

    def _classify_state(self, _state: State | None = None) -> int:
        """Classify cycle index from averaged brightness across the collection."""
        expanded = self._cached_expanded_target_entity_ids()
        return self._classify_expanded_members(expanded)

    def _classify_expanded_members(self, entity_ids: list[str]) -> int:
        """Classify the current step index from a list of expanded target entities."""
        average_pct, counts = self._average_collection_brightness_pct(entity_ids)
        self._last_average_pct = average_pct
        self._last_sample_counts = counts
        return self._classify_average_pct(average_pct)

    def _average_collection_brightness_pct(self, entity_ids: list[str]) -> tuple[float, dict[str, int]]:
        """Return average brightness percent and sample counters for a collection.

        Classification intentionally uses brightness only, regardless of color/temperature mode.
        """
        if not entity_ids:
            return 0.0, {"empty_collection": 1}

        samples: list[int] = []
        counts: Counter[str] = Counter()
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            sample_pct = self._sample_pct_for_state(state)
            samples.append(sample_pct)

            if state is None:
                counts["missing"] += 1
            else:
                counts[f"state_{state.state}"] += 1
                if state.state == STATE_ON and state.attributes.get(ATTR_BRIGHTNESS) is None:
                    counts["on_no_brightness"] += 1

        if not samples:
            return 0.0, dict(counts)

        average_pct = sum(samples) / len(samples)
        return average_pct, dict(counts)

    def _sample_pct_for_state(self, state: State | None) -> int:
        """Convert one state object into a brightness percentage sample."""
        if state is None:
            return 0

        if state.state in (STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN):
            return 0

        if state.state != STATE_ON:
            return 0

        brightness_pct = self._brightness_pct_from_state(state)
        if brightness_pct is not None:
            return brightness_pct

        # Some platforms report ON but no brightness; keep progression stable.
        fallback_index = self._resolved_index if self._resolved_index > 0 else 1
        return self._step_pct(fallback_index)

    def _classify_average_pct(self, average_pct: float) -> int:
        """Map a collection-average brightness percentage to a cycle step index."""
        if average_pct <= 0:
            return 0

        return self._nearest_step_for_pct(round(average_pct))

    @staticmethod
    def _brightness_pct_from_state(state: State) -> int | None:
        brightness = state.attributes.get(ATTR_BRIGHTNESS)
        if brightness is None:
            return None
        try:
            brightness_int = int(brightness)
        except (TypeError, ValueError):
            return None
        if brightness_int <= 0:
            return None
        return round((brightness_int / 255) * 100)

    def _nearest_step_for_pct(self, brightness_pct: int) -> int:
        """Return the configured step index with nearest brightness percentage.

        When multiple steps are equally close (for example, duplicate brightness levels),
        prefer the current resolved step if it is one of the tied candidates. This keeps
        cycle progression moving forward instead of repeatedly re-classifying to the same
        earliest duplicate-brightness step.
        """
        best_delta: int = 999
        candidate_steps: list[int] = []

        for step_num, step in enumerate(self._steps, start=1):
            try:
                step_pct = int(step[CONF_STEP_BRIGHTNESS_PCT])
            except (KeyError, TypeError, ValueError):
                continue

            delta = abs(step_pct - brightness_pct)
            if delta < best_delta:
                best_delta = delta
                candidate_steps = [step_num]
                continue

            if delta == best_delta:
                candidate_steps.append(step_num)

        if not candidate_steps:
            return 1

        if len(candidate_steps) == 1:
            return candidate_steps[0]

        if self._resolved_index in candidate_steps:
            LOGGER.debug(
                "Ambiguous brightness classification for entry %s at %s%%; tied_steps=%s using_resolved=%s",
                self.entry.entry_id,
                brightness_pct,
                candidate_steps,
                self._resolved_index,
            )
            return self._resolved_index

        chosen_step = min(candidate_steps)
        LOGGER.debug(
            "Ambiguous brightness classification for entry %s at %s%%; tied_steps=%s using_lowest=%s",
            self.entry.entry_id,
            brightness_pct,
            candidate_steps,
            chosen_step,
        )
        return chosen_step


    def _step_pct(self, index: int) -> int:
        """Return configured brightness percent for a step index."""
        if index <= 0:
            return 0
        try:
            return int(self._steps[index - 1][CONF_STEP_BRIGHTNESS_PCT])
        except (IndexError, KeyError, TypeError, ValueError):
            return 100

    def _step_mode(self, step: dict[str, Any]) -> str:
        """Return normalized step mode for a step object."""
        return _normalize_step_mode(step.get(CONF_STEP_MODE))

    def _step_temp_pct(self, step: dict[str, Any]) -> int:
        """Return bounded temperature percentage for white/temperature mode."""
        try:
            temp_pct = int(step.get(CONF_STEP_TEMP_PCT, DEFAULT_STEP_TEMP_PCT))
        except (TypeError, ValueError):
            temp_pct = DEFAULT_STEP_TEMP_PCT
        return max(0, min(100, temp_pct))

    def _step_rgb(self, step: dict[str, Any]) -> tuple[int, int, int]:
        """Return RGB tuple for a color-mode step, with safe defaults."""
        rgb = _parse_rgb_color(step.get(CONF_STEP_COLOR_RGB))
        if rgb is not None:
            return rgb

        hex_color = _normalize_hex_color(step.get(CONF_STEP_COLOR_HEX))
        if hex_color is not None:
            parsed = _parse_rgb_color(hex_color)
            if parsed is not None:
                return parsed

        return tuple(DEFAULT_STEP_COLOR_RGB)

    def _supported_color_modes(self, entity_id: str) -> set[str]:
        """Read supported color modes for an entity from its current state."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return set()

        raw_modes = state.attributes.get("supported_color_modes")
        if isinstance(raw_modes, (list, tuple, set)):
            return {
                str(mode).strip().lower()
                for mode in raw_modes
                if isinstance(mode, str)
            }
        return set()

    def _supports_color_temp(self, entity_id: str) -> bool:
        """Determine whether an entity can accept color temperature payloads."""
        modes = self._supported_color_modes(entity_id)
        if "color_temp" in modes:
            return True
        if modes:
            return False

        state = self.hass.states.get(entity_id)
        attrs = {} if state is None else state.attributes
        return any(
            attrs.get(key) is not None
            for key in (
                "color_temp_kelvin",
                "color_temp",
                "min_color_temp_kelvin",
                "max_color_temp_kelvin",
                "min_mireds",
                "max_mireds",
            )
        )

    def _refresh_temp_range_cache(self, entity_ids: list[str]) -> None:
        """Warm the per-entity temperature range cache for a list of entities."""
        for entity_id in entity_ids:
            self._get_temp_range(entity_id)

    def _extract_temp_range(self, entity_id: str) -> tuple[int, int]:
        """Extract min/max Kelvin range from entity attributes with fallbacks."""
        state = self.hass.states.get(entity_id)
        attrs = state.attributes if state is not None else {}

        min_kelvin = attrs.get("min_color_temp_kelvin")
        max_kelvin = attrs.get("max_color_temp_kelvin")

        if min_kelvin is None or max_kelvin is None:
            min_mired = attrs.get("max_mireds")
            max_mired = attrs.get("min_mireds")
            if min_mired is not None and max_mired is not None:
                try:
                    min_kelvin = round(1_000_000 / int(min_mired))
                    max_kelvin = round(1_000_000 / int(max_mired))
                except (TypeError, ValueError):
                    min_kelvin = None
                    max_kelvin = None

        try:
            min_kelvin_int = int(min_kelvin)
            max_kelvin_int = int(max_kelvin)
        except (TypeError, ValueError):
            min_kelvin_int = DEFAULT_TEMP_MIN_KELVIN
            max_kelvin_int = DEFAULT_TEMP_MAX_KELVIN

        if min_kelvin_int <= 0 or max_kelvin_int <= 0:
            min_kelvin_int = DEFAULT_TEMP_MIN_KELVIN
            max_kelvin_int = DEFAULT_TEMP_MAX_KELVIN

        low = min(min_kelvin_int, max_kelvin_int)
        high = max(min_kelvin_int, max_kelvin_int)
        if low == high:
            high = low + 1
        return low, high

    def _get_temp_range(self, entity_id: str) -> tuple[int, int]:
        """Return cached temperature range for an entity."""
        cached = self._temp_range_cache.get(entity_id)
        if cached is not None:
            return cached

        computed = self._extract_temp_range(entity_id)
        self._temp_range_cache[entity_id] = computed
        return computed

    def _target_kelvin_for_entity(self, entity_id: str, temp_pct: int) -> int:
        """Convert UI temp percentage into an entity-specific Kelvin target."""
        min_kelvin, max_kelvin = self._get_temp_range(entity_id)
        temp_ratio = max(0, min(100, temp_pct)) / 100
        return round(min_kelvin + ((max_kelvin - min_kelvin) * temp_ratio))

    def _color_payload_for_entity(
        self, entity_id: str, rgb: tuple[int, int, int]
    ) -> dict[str, Any]:
        """Build the best color payload for one entity.

        Tuya integrations generally map color more reliably through HS payloads, so HS is
        preferred there when supported.
        """
        supported_modes = self._supported_color_modes(entity_id)
        is_tuya = self._is_tuya_entity(entity_id)

        hue, sat, _value = colorsys.rgb_to_hsv(
            rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0
        )
        hs_color = (hue * 360.0, sat * 100.0)
        hs_payload = {"hs_color": [round(hs_color[0], 1), round(hs_color[1], 1)]}
        rgb_payload = {"rgb_color": [rgb[0], rgb[1], rgb[2]]}

        if is_tuya and "hs" in supported_modes:
            return hs_payload

        if {"rgb", "rgbw", "rgbww"} & supported_modes:
            return rgb_payload

        if "hs" in supported_modes:
            return hs_payload

        if "xy" in supported_modes:
            return hs_payload

        if is_tuya:
            return hs_payload
        return rgb_payload

    def _turn_on_payload_for_entity(
        self, entity_id: str, step: dict[str, Any]
    ) -> dict[str, Any]:
        """Build a full turn_on payload for one entity for the selected step."""
        brightness_pct = int(step[CONF_STEP_BRIGHTNESS_PCT])
        brightness = round((brightness_pct / 100) * 255)
        payload: dict[str, Any] = {
            # Home Assistant validates brightness keys as mutually exclusive, so
            # we send only one key here to avoid `MultipleInvalid` on turn_on.
            ATTR_BRIGHTNESS: brightness,
        }

        mode = self._step_mode(step)
        if mode == STEP_MODE_COLOR:
            # Color mode keeps brightness and color in one service call.
            payload.update(self._color_payload_for_entity(entity_id, self._step_rgb(step)))
            LOGGER.debug(
                "Built turn_on payload (entry=%s entity=%s label=%s mode=%s keys=%s)",
                self.entry.entry_id,
                entity_id,
                step.get("label"),
                mode,
                sorted(payload.keys()),
            )
            return payload

        if not self._supports_color_temp(entity_id):
            # If temperature control is unsupported, apply brightness-only.
            LOGGER.debug(
                "Built turn_on payload (entry=%s entity=%s label=%s mode=%s keys=%s color_temp_supported=False)",
                self.entry.entry_id,
                entity_id,
                step.get("label"),
                mode,
                sorted(payload.keys()),
            )
            return payload

        # White/temp mode maps 0..100 UI slider to this entity's Kelvin range.
        temp_pct = self._step_temp_pct(step)
        kelvin = self._target_kelvin_for_entity(entity_id, temp_pct)
        state = self.hass.states.get(entity_id)
        attrs = {} if state is None else state.attributes
        if attrs.get("min_color_temp_kelvin") is not None or attrs.get("max_color_temp_kelvin") is not None:
            payload["color_temp_kelvin"] = kelvin
        else:
            payload["color_temp"] = round(1_000_000 / max(1, kelvin))
        LOGGER.debug(
            "Built turn_on payload (entry=%s entity=%s label=%s mode=%s temp_pct=%s kelvin=%s keys=%s)",
            self.entry.entry_id,
            entity_id,
            step.get("label"),
            mode,
            temp_pct,
            kelvin,
            sorted(payload.keys()),
        )
        return payload

    def _merged_entry_data(self) -> dict[str, Any]:
        """Return merged entry data+options from the latest in-memory entry."""
        current_entry = self.hass.config_entries.async_get_entry(self.entry.entry_id)
        if current_entry is None:
            current_entry = self.entry
        else:
            self.entry = current_entry
        return {**current_entry.data, **current_entry.options}

    def _refresh_steps_from_entry(self) -> None:
        """Refresh step configuration from the latest config entry values.

        Options flow updates the config entry in-place; we keep the controller robust by
        re-reading steps on press/state changes (in addition to restart-on-update).
        """
        data = self._merged_entry_data()
        steps = data.get(CONF_STEPS, [])
        if isinstance(steps, list):
            new_steps = _normalize_steps(steps)
            if new_steps != self._steps:
                LOGGER.info(
                    "Refreshed steps for entry %s: %s -> %s",
                    self.entry.entry_id,
                    len(self._steps),
                    len(new_steps),
                )
                LOGGER.info(
                    "New steps for %s: %s",
                    self._target_entity_ids,
                    [s.get(CONF_STEP_BRIGHTNESS_PCT) for s in new_steps],
                )
            self._steps = new_steps
            if self._resolved_index > len(self._steps):
                self._resolved_index = len(self._steps)

    def _refresh_event_bindings_from_entry(self) -> None:
        """Refresh primary/optional button bindings from latest config entry values."""
        data = self._merged_entry_data()

        remote_ieee = data.get(CONF_REMOTE_IEEE)
        if isinstance(remote_ieee, str) and remote_ieee != self._remote_ieee:
            LOGGER.info(
                "Refreshed remote IEEE for entry %s: %s -> %s",
                self.entry.entry_id,
                self._remote_ieee,
                remote_ieee,
            )
            self._remote_ieee = remote_ieee

        new_cycle_signature = _normalize_signature(
            data.get(CONF_ENDPOINT_ID),
            data.get(CONF_COMMAND),
            data.get(CONF_CLUSTER_ID),
            data.get(CONF_ARGS),
        )
        if new_cycle_signature is not None and new_cycle_signature != self._cycle_signature:
            LOGGER.info(
                "Refreshed primary button signature for entry %s: %s -> %s",
                self.entry.entry_id,
                self._cycle_signature,
                new_cycle_signature,
            )
            self._cycle_signature = new_cycle_signature

        max_target_index = len(self._steps)
        new_gesture_bindings: dict[str, dict[str, Any]] = {}
        for gesture_name, binding_key in (
            (GESTURE_LONG_PRESS, CONF_LONG_PRESS_BINDING),
            (GESTURE_DOUBLE_PRESS, CONF_DOUBLE_PRESS_BINDING),
        ):
            normalized_binding = _normalize_stored_gesture_binding(
                data.get(binding_key),
                max_target_index,
            )
            if normalized_binding is not None:
                new_gesture_bindings[gesture_name] = normalized_binding

        if new_gesture_bindings != self._gesture_bindings:
            LOGGER.info(
                "Refreshed optional gesture bindings for entry %s: %s -> %s",
                self.entry.entry_id,
                sorted(self._gesture_bindings),
                sorted(new_gesture_bindings),
            )
            self._gesture_bindings = new_gesture_bindings

    def _refresh_targets_from_entry(self) -> None:
        """Refresh target entity collection from latest config entry values."""
        data = self._merged_entry_data()
        targets = _coerce_target_entity_ids(data)
        if not targets:
            return
        if targets != self._target_entity_ids:
            LOGGER.info(
                "Refreshed targets for entry %s: %s -> %s",
                self.entry.entry_id,
                self._target_entity_ids,
                targets,
            )
            self._target_entity_ids = targets
            self._targets_cache_dirty = True

    def _group_membership_changed(self, old_state: State | None, new_state: State | None) -> bool:
        """Return True when a watched light group's member list changed."""
        if old_state is None and new_state is None:
            return False
        old_members = None if old_state is None else old_state.attributes.get(ATTR_ENTITY_ID)
        new_members = None if new_state is None else new_state.attributes.get(ATTR_ENTITY_ID)
        if isinstance(old_members, list) or isinstance(new_members, list):
            return list(old_members or []) != list(new_members or [])
        return False

    def _state_subscription_entities(self) -> list[str]:
        """Return de-duplicated entity list to watch for state changes."""
        combined = self._target_entity_ids + self._expanded_targets_cache
        unique: list[str] = []
        seen: set[str] = set()
        for entity_id in combined:
            if entity_id in seen:
                continue
            seen.add(entity_id)
            unique.append(entity_id)
        return unique

    def _resubscribe_state_listener(self) -> None:
        """Ensure state listener tracks the current target and expanded entities."""
        watch_entities = self._state_subscription_entities()
        if not watch_entities:
            return
        if self._unsub_state is not None and watch_entities == self._watched_state_entity_ids:
            return

        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None

        self._unsub_state = async_track_state_change_event(
            self.hass,
            watch_entities,
            self._on_state_change,
        )
        self._watched_state_entity_ids = watch_entities

    async def _async_handle_press(self) -> None:
        """Process one matching button press end-to-end."""
        async with self._press_lock:
            # Refresh mutable config first so runtime reflects latest options flow changes.
            self._refresh_targets_from_entry()
            self._refresh_steps_from_entry()
            self._refresh_event_bindings_from_entry()

            expanded_before = self._cached_expanded_target_entity_ids()
            current_index = self._classify_expanded_members(expanded_before)
            self._resolved_index = current_index

            # Suppress state-event based reclassification while updates are propagating.
            target_count = len(expanded_before)
            settle_seconds = max(1.5, min(8.0, target_count * 0.08))
            self._ignore_state_changes_until = time.monotonic() + settle_seconds

            next_index = (current_index + 1) % (len(self._steps) + 1)
            LOGGER.debug(
                "Press: entry=%s title=%s targets=%s current=%s next=%s steps=%s avg=%.2f",
                self.entry.entry_id,
                self.entry.title,
                self._target_entity_ids,
                current_index,
                next_index,
                len(self._steps),
                self._last_average_pct,
            )
            try:
                await self._async_apply_index(next_index, expanded_before)
            except Exception:
                self._ignore_state_changes_until = 0.0
                LOGGER.exception(
                    "Failed applying cycle step (entry=%s title=%s targets=%s next=%s steps=%s)",
                    self.entry.entry_id,
                    self.entry.title,
                    self._target_entity_ids,
                    next_index,
                    len(self._steps),
                )
                return
            else:
                # Persist resolved index immediately for deterministic next-step behavior.
                self._resolved_index = next_index
                # Reconcile if group expansion changed during apply.
                await self._async_reconcile_expanded_targets(next_index, expanded_before)
                self._ignore_state_changes_until = max(
                    self._ignore_state_changes_until,
                    time.monotonic() + 0.5,
                )

    async def _async_handle_direct_target(
        self, gesture_name: str, target_index: int
    ) -> None:
        """Apply a directly mapped long/double press target."""
        async with self._press_lock:
            self._refresh_targets_from_entry()
            self._refresh_steps_from_entry()
            self._refresh_event_bindings_from_entry()

            bounded_target_index = max(0, min(len(self._steps), int(target_index)))
            expanded_before = self._cached_expanded_target_entity_ids()

            target_count = len(expanded_before)
            settle_seconds = max(1.5, min(8.0, target_count * 0.08))
            self._ignore_state_changes_until = time.monotonic() + settle_seconds

            LOGGER.debug(
                "Direct gesture: entry=%s title=%s gesture=%s target=%s steps=%s targets=%s",
                self.entry.entry_id,
                self.entry.title,
                gesture_name,
                bounded_target_index,
                len(self._steps),
                self._target_entity_ids,
            )
            try:
                await self._async_apply_index(bounded_target_index, expanded_before)
            except Exception:
                self._ignore_state_changes_until = 0.0
                LOGGER.exception(
                    "Failed applying direct gesture (entry=%s title=%s gesture=%s target=%s steps=%s)",
                    self.entry.entry_id,
                    self.entry.title,
                    gesture_name,
                    bounded_target_index,
                    len(self._steps),
                )
                return
            else:
                self._resolved_index = bounded_target_index
                await self._async_reconcile_expanded_targets(
                    bounded_target_index, expanded_before
                )
                self._ignore_state_changes_until = max(
                    self._ignore_state_changes_until,
                    time.monotonic() + 0.5,
                )

    async def _async_apply_index(self, index: int, expanded_before: list[str]) -> None:
        """Apply one cycle index (Off or one configured On step)."""
        if index == 0:
            LOGGER.debug("Turning off %s", self._target_entity_ids)
            await self._async_call_light_service(
                "turn_off", {}, expanded_before
            )
            return

        # Step indexes are 1-based; list indexes are 0-based.
        step = self._steps[index - 1]
        label = step.get("label")
        mode = self._step_mode(step)
        brightness_pct = int(step[CONF_STEP_BRIGHTNESS_PCT])

        LOGGER.debug(
            "Turning on %s to %s%% (mode=%s label=%s)",
            self._target_entity_ids,
            brightness_pct,
            mode,
            label,
        )
        await self._async_apply_step_to_entities(step, expanded_before)

    async def _async_reconcile_expanded_targets(
        self, applied_index: int, previous_targets: list[str]
    ) -> None:
        """Re-apply the just-applied step to targets added after expansion changed."""
        refreshed_targets, changed = self._refresh_expanded_targets(force=True)
        if changed:
            self._resubscribe_state_listener()

        previous_set = set(previous_targets)
        added_targets = [entity_id for entity_id in refreshed_targets if entity_id not in previous_set]
        if not added_targets:
            return

        LOGGER.info(
            "Expanded target collection changed for entry %s; added=%s total=%s",
            self.entry.entry_id,
            len(added_targets),
            len(refreshed_targets),
        )
        if applied_index == 0:
            await self._async_call_light_service_best_effort("turn_off", added_targets, {})
            return

        # Reuse the exact applied step so newly discovered members stay in sync.
        step = self._steps[applied_index - 1]
        await self._async_apply_step_to_entities(step, added_targets)

    async def _async_call_light_service(
        self,
        service: str,
        service_data: dict[str, Any],
        fallback_targets: list[str],
    ) -> None:
        """Prefer calling the configured target, then fall back to expanded members.

        Calling the parent group first gives Home Assistant/integration-specific light
        platforms a chance to apply a consistent grouped brightness. If that fails, we
        retry best-effort over flattened member entities.
        """
        failures = await self._async_call_light_service_many(
            service, self._target_entity_ids, service_data
        )

        if not failures:
            return

        for entity_id, exc in failures:
            LOGGER.warning(
                "light.%s failed for %s: %s; retrying expanded targets",
                service,
                entity_id,
                exc,
            )

        if not fallback_targets:
            raise failures[0][1]

        await self._async_call_light_service_best_effort(
            service, fallback_targets, service_data
        )

    async def _async_apply_step_to_entities(
        self, step: dict[str, Any], entity_ids: list[str]
    ) -> None:
        """Apply one On step to a list of entities with per-entity payloads."""
        if not entity_ids:
            return

        payload_by_entity: dict[str, dict[str, Any]] = {}
        for entity_id in entity_ids:
            # Build payloads per entity to account for differing color capabilities.
            payload_by_entity[entity_id] = self._turn_on_payload_for_entity(entity_id, step)

        if LOGGER.isEnabledFor(logging.DEBUG):
            sample_entity = entity_ids[0]
            sample_payload = payload_by_entity[sample_entity]
            brightness_conflicts = [
                entity_id
                for entity_id, payload in payload_by_entity.items()
                if ATTR_BRIGHTNESS in payload and "brightness_pct" in payload
            ]
            LOGGER.debug(
                (
                    "Prepared step payloads (entry=%s label=%s mode=%s entities=%s "
                    "sample_entity=%s sample_keys=%s brightness_conflicts=%s)"
                ),
                self.entry.entry_id,
                step.get("label"),
                self._step_mode(step),
                len(entity_ids),
                sample_entity,
                sorted(sample_payload.keys()),
                len(brightness_conflicts),
            )

        failures = await self._async_call_light_service_many_with_payload(
            "turn_on", payload_by_entity
        )

        if not failures:
            return

        for entity_id, exc in failures:
            LOGGER.warning("light.turn_on failed for %s: %s", entity_id, exc)

        if len(failures) == len(entity_ids):
            raise failures[0][1]

    def _expanded_target_entity_ids(self) -> list[str]:
        """Return leaf `light.*` entity ids to call.

        If the configured target is a light group, recursively expand nested groups and
        de-duplicate entities. This avoids calling other group entities (which can hide
        partial failures and skew brightness classification).
        """
        expanded_targets, changed = self._refresh_expanded_targets(force=False)
        if changed:
            self._resubscribe_state_listener()
        return expanded_targets

    def _cached_expanded_target_entity_ids(self) -> list[str]:
        """Return expanded targets, building cache immediately when empty."""
        if self._expanded_targets_cache:
            return list(self._expanded_targets_cache)

        expanded_targets, _ = self._refresh_expanded_targets(force=True)
        self._resubscribe_state_listener()
        return expanded_targets

    def _refresh_expanded_targets(self, force: bool) -> tuple[list[str], bool]:
        """Rebuild expanded target cache when needed and report whether it changed."""
        if not force and self._expanded_targets_cache and not self._targets_cache_dirty:
            return list(self._expanded_targets_cache), False

        expanded = self._expanded_entity_ids(self._target_entity_ids)
        changed = expanded != self._expanded_targets_cache
        self._expanded_targets_cache = expanded
        self._targets_cache_dirty = False
        self._refresh_temp_range_cache(self._expanded_targets_cache)
        return list(self._expanded_targets_cache), changed

    def _expanded_entity_ids(self, root_entity_ids: list[str]) -> list[str]:
        """Expand nested light groups into leaf light entities."""
        visited: set[str] = set()
        leaves: list[str] = []
        stack: list[str] = list(root_entity_ids)

        while stack:
            entity_id = stack.pop()
            if not isinstance(entity_id, str):
                continue
            if entity_id in visited:
                continue
            visited.add(entity_id)

            state = self.hass.states.get(entity_id)
            members = state.attributes.get(ATTR_ENTITY_ID) if state is not None else None
            if isinstance(members, list) and members:
                for member in members:
                    if isinstance(member, str):
                        stack.append(member)
                continue

            if entity_id.startswith("light."):
                leaves.append(entity_id)

        # Keep order stable-ish and remove duplicates while preserving first occurrence.
        unique: list[str] = []
        seen: set[str] = set()
        for entity_id in leaves:
            if entity_id in seen:
                continue
            seen.add(entity_id)
            unique.append(entity_id)

        return unique or list(root_entity_ids)

    async def _async_call_light_service_best_effort(
        self,
        service: str,
        entity_ids: list[str],
        service_data: dict[str, Any],
    ) -> None:
        """Call a light service across many entities, tolerating partial failures."""
        failures = await self._async_call_light_service_many(
            service, entity_ids, service_data
        )

        if not failures:
            return

        for entity_id, exc in failures:
            LOGGER.warning("light.%s failed for %s: %s", service, entity_id, exc)

        if len(failures) == len(entity_ids):
            # Re-raise the first failure (preserves traceback) so the press handler
            # treats the step application as failed.
            raise failures[0][1]

    async def _async_call_light_service_single(
        self, service: str, entity_id: str, service_data: dict[str, Any]
    ) -> Exception | None:
        """Call one light service and return exception instead of raising."""
        try:
            await self.hass.services.async_call(
                LIGHT_DOMAIN,
                service,
                {ATTR_ENTITY_ID: entity_id, **service_data},
                blocking=True,
            )
        except Exception as exc:
            LOGGER.debug(
                "light.%s call exception (entry=%s entity=%s keys=%s payload=%s): %s",
                service,
                self.entry.entry_id,
                entity_id,
                sorted(service_data.keys()),
                service_data,
                exc,
            )
            return exc
        return None

    async def _async_call_light_service_many_with_payload(
        self, service: str, payload_by_entity: dict[str, dict[str, Any]]
    ) -> list[tuple[str, Exception]]:
        """Call one light service across entities where each has unique payload data."""
        if not payload_by_entity:
            return []

        ordered_entity_ids = self._ordered_entity_ids_for_dispatch(
            list(payload_by_entity.keys())
        )
        max_parallel_calls = self._max_parallel_calls()
        LOGGER.debug(
            "Dispatching light.%s with per-entity payloads (entry=%s entities=%s parallel=%s)",
            service,
            self.entry.entry_id,
            len(ordered_entity_ids),
            max_parallel_calls,
        )
        if max_parallel_calls <= 1 or len(ordered_entity_ids) == 1:
            failures: list[tuple[str, Exception]] = []
            for entity_id in ordered_entity_ids:
                exc = await self._async_call_light_service_single(
                    service, entity_id, payload_by_entity[entity_id]
                )
                if exc is not None:
                    failures.append((entity_id, exc))
            return failures

        semaphore = asyncio.Semaphore(max_parallel_calls)

        async def _call(entity_id: str) -> tuple[str, Exception | None]:
            # Semaphore bounds concurrent service calls for bridge/cloud stability.
            async with semaphore:
                exc = await self._async_call_light_service_single(
                    service, entity_id, payload_by_entity[entity_id]
                )
            return entity_id, exc

        results = await asyncio.gather(*[_call(entity_id) for entity_id in ordered_entity_ids])
        return [(entity_id, exc) for entity_id, exc in results if exc is not None]

    async def _async_call_light_service_many(
        self, service: str, entity_ids: list[str], service_data: dict[str, Any]
    ) -> list[tuple[str, Exception]]:
        """Call one light service across many entities with shared payload data."""
        if not entity_ids:
            return []

        ordered_entity_ids = self._ordered_entity_ids_for_dispatch(entity_ids)
        max_parallel_calls = self._max_parallel_calls()
        LOGGER.debug(
            "Dispatching light.%s with shared payload (entry=%s entities=%s parallel=%s keys=%s)",
            service,
            self.entry.entry_id,
            len(ordered_entity_ids),
            max_parallel_calls,
            sorted(service_data.keys()),
        )
        if max_parallel_calls <= 1 or len(ordered_entity_ids) == 1:
            failures: list[tuple[str, Exception]] = []
            for entity_id in ordered_entity_ids:
                exc = await self._async_call_light_service_single(
                    service, entity_id, service_data
                )
                if exc is not None:
                    failures.append((entity_id, exc))
            return failures

        semaphore = asyncio.Semaphore(max_parallel_calls)

        async def _call(entity_id: str) -> tuple[str, Exception | None]:
            # Semaphore bounds concurrent service calls for bridge/cloud stability.
            async with semaphore:
                exc = await self._async_call_light_service_single(
                    service, entity_id, service_data
                )
            return entity_id, exc

        results = await asyncio.gather(*[_call(entity_id) for entity_id in ordered_entity_ids])
        return [(entity_id, exc) for entity_id, exc in results if exc is not None]

    def _max_parallel_calls(self) -> int:
        """Return current integration-wide parallelism limit."""
        return get_max_parallel_calls(self.hass)

    def _ordered_entity_ids_for_dispatch(self, entity_ids: list[str]) -> list[str]:
        """Prioritize fast/local entities first and defer Tuya-backed entities."""
        if len(entity_ids) <= 1:
            return list(entity_ids)

        normal_entities: list[str] = []
        tuya_entities: list[str] = []
        for entity_id in entity_ids:
            if self._is_tuya_entity(entity_id):
                tuya_entities.append(entity_id)
            else:
                normal_entities.append(entity_id)

        if not tuya_entities or not normal_entities:
            return list(entity_ids)

        LOGGER.debug(
            "Dispatch order for entry %s: local/non-Tuya=%s Tuya=%s",
            self.entry.entry_id,
            len(normal_entities),
            len(tuya_entities),
        )
        return normal_entities + tuya_entities

    def _is_tuya_entity(self, entity_id: str) -> bool:
        """Return whether an entity is likely backed by Tuya integration/device."""
        cached = self._is_tuya_cache.get(entity_id)
        if cached is not None:
            return cached

        is_tuya = False
        entity_registry = er.async_get(self.hass)
        entity_entry = entity_registry.async_get(entity_id)
        if entity_entry is not None:
            platform = (entity_entry.platform or "").lower()
            is_tuya = platform in {"tuya", "localtuya", "tuya_local"}
            if not is_tuya and entity_entry.device_id:
                device_registry = dr.async_get(self.hass)
                device_entry = device_registry.async_get(entity_entry.device_id)
                if device_entry is not None:
                    manufacturer = (device_entry.manufacturer or "").lower()
                    model = (device_entry.model or "").lower()
                    is_tuya = "tuya" in manufacturer or model.startswith("tuya")

        self._is_tuya_cache[entity_id] = is_tuya
        return is_tuya
