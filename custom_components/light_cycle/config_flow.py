"""Config flow for Light Cycle Controller."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

import voluptuous as vol

from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_DEVICE_ID, CONF_NAME
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ARGS,
    CONF_CLUSTER_ID,
    CONF_COMMAND,
    CONF_DOUBLE_PRESS_BINDING,
    CONF_ENDPOINT_ID,
    CONF_GESTURE_TARGET_INDEX,
    CONF_LONG_PRESS_BINDING,
    CONF_MAX_PARALLEL_CALLS,
    CONF_ON_STEPS,
    CONF_REMOTE_DEVICE_ID,
    CONF_REMOTE_IEEE,
    CONF_STEP_BRIGHTNESS_PCT,
    CONF_STEP_COLOR_HEX,
    CONF_STEP_COLOR_RGB,
    CONF_STEP_LABEL,
    CONF_STEP_MODE,
    CONF_STEP_TEMP_PCT,
    CONF_STEPS,
    CONF_TARGET_ENTITY_ID,
    CONF_TARGET_ENTITY_IDS,
    DEFAULT_STEP_COLOR_HEX,
    DEFAULT_STEP_COLOR_RGB,
    DEFAULT_STEP_MODE,
    DEFAULT_STEP_TEMP_PCT,
    DEFAULT_CAPTURE_TIMEOUT_SECONDS,
    DEFAULT_MAX_PARALLEL_CALLS,
    DOMAIN,
    GESTURE_DOUBLE_PRESS,
    GESTURE_LONG_PRESS,
    MAX_ON_STEPS,
    MAX_MAX_PARALLEL_CALLS,
    MIN_MAX_PARALLEL_CALLS,
    MIN_ON_STEPS,
    STEP_MODE_COLOR,
    STEP_MODE_WHITE_TEMP,
)
from .settings import (
    async_get_device_gesture_support,
    async_get_settings,
    async_set_device_gesture_support,
    async_set_max_parallel_calls,
)

EVENT_ZHA_EVENT = "zha_event"
CONF_RECAPTURE = "recapture"
CONF_GESTURE_NOT_SUPPORTED = "gesture_not_supported"
CONF_RECAPTURE_LONG_PRESS = "recapture_long_press"
CONF_RECAPTURE_DOUBLE_PRESS = "recapture_double_press"
CONF_LONG_PRESS_TARGET = "long_press_target"
CONF_DOUBLE_PRESS_TARGET = "double_press_target"
CONF_SKIP_CAPTURE = "skip_capture"
GESTURE_TARGET_NONE = "__none__"

LOGGER = logging.getLogger(__name__)

GESTURE_BINDING_KEYS = {
    GESTURE_LONG_PRESS: CONF_LONG_PRESS_BINDING,
    GESTURE_DOUBLE_PRESS: CONF_DOUBLE_PRESS_BINDING,
}

GESTURE_RECAPTURE_KEYS = {
    GESTURE_LONG_PRESS: CONF_RECAPTURE_LONG_PRESS,
    GESTURE_DOUBLE_PRESS: CONF_RECAPTURE_DOUBLE_PRESS,
}

GESTURE_TARGET_KEYS = {
    GESTURE_LONG_PRESS: CONF_LONG_PRESS_TARGET,
    GESTURE_DOUBLE_PRESS: CONF_DOUBLE_PRESS_TARGET,
}

REMOTE_TRIGGER_HINTS = (
    "button",
    "press",
    "hold",
    "rotate",
    "dial",
    "toggle",
    "dim",
    "scene",
)


def _gesture_label(gesture: str) -> str:
    """Return a user-facing label for an optional gesture."""
    if gesture == GESTURE_DOUBLE_PRESS:
        return "double press"
    return "long press"


def _signature_to_storage(signature: _ZhaButtonSignature, target_index: int) -> dict[str, Any]:
    """Serialize a captured optional gesture signature for config entry storage."""
    return {
        CONF_ENDPOINT_ID: signature.endpoint_id,
        CONF_COMMAND: signature.command,
        CONF_CLUSTER_ID: signature.cluster_id,
        CONF_ARGS: signature.args,
        CONF_GESTURE_TARGET_INDEX: target_index,
    }


def _binding_signature_from_storage(
    ieee: str | None, binding: Any
) -> _ZhaButtonSignature | None:
    """Deserialize one stored optional gesture binding to a signature object."""
    if ieee is None or not isinstance(binding, dict):
        return None

    endpoint_id = binding.get(CONF_ENDPOINT_ID)
    command = binding.get(CONF_COMMAND)
    if endpoint_id is None or command is None:
        return None

    try:
        return _ZhaButtonSignature(
            ieee=ieee,
            endpoint_id=int(endpoint_id),
            command=str(command),
            cluster_id=binding.get(CONF_CLUSTER_ID),
            args=list(binding.get(CONF_ARGS, []))
            if binding.get(CONF_ARGS) is not None
            else None,
        )
    except (TypeError, ValueError):
        return None


def _binding_target_index_from_storage(binding: Any, on_steps: int) -> int:
    """Return a bounded direct-jump target index from a stored binding."""
    if not isinstance(binding, dict):
        return 0

    try:
        target_index = int(binding.get(CONF_GESTURE_TARGET_INDEX, 0))
    except (TypeError, ValueError):
        return 0
    return max(0, min(on_steps, target_index))


def _step_target_selector(
    steps: list[dict[str, Any]], *, include_none: bool = False
) -> selector.SelectSelector:
    """Build a dropdown that lets users map a gesture to no action, Off, or a step."""
    options: list[dict[str, str]] = []
    if include_none:
        options.append({"value": GESTURE_TARGET_NONE, "label": "Do nothing"})
    options.append({"value": "0", "label": "Off"})
    for step_num, step in enumerate(steps, start=1):
        label = str(step.get(CONF_STEP_LABEL, f"Step {step_num}")).strip() or f"Step {step_num}"
        options.append(
            {
                "value": str(step_num),
                "label": f"Step {step_num}: {label}",
            }
        )

    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _step_label_key(step: int) -> str:
    """Return dynamic form key for a step label field."""
    return f"step_{step}_{CONF_STEP_LABEL}"


def _step_brightness_key(step: int) -> str:
    """Return dynamic form key for a step brightness field."""
    return f"step_{step}_{CONF_STEP_BRIGHTNESS_PCT}"


def _step_mode_key(step: int) -> str:
    """Return dynamic form key for a step mode field."""
    return f"step_{step}_{CONF_STEP_MODE}"


def _step_temp_pct_key(step: int) -> str:
    """Return dynamic form key for a step temperature-percentage field."""
    return f"step_{step}_{CONF_STEP_TEMP_PCT}"


def _step_color_hex_key(step: int) -> str:
    """Return dynamic form key for a step hex color field."""
    return f"step_{step}_{CONF_STEP_COLOR_HEX}"


def _step_color_rgb_key(step: int) -> str:
    """Return dynamic form key for a step RGB color selector field."""
    return f"step_{step}_{CONF_STEP_COLOR_RGB}"


def _default_brightness_pct(step: int, total_steps: int) -> int:
    """Return a simple evenly spaced default brightness for a step index."""
    if total_steps <= 0:
        return 100
    return max(1, min(100, round(step * 100 / total_steps)))


def _normalize_step_mode(value: Any) -> str:
    """Normalize user input to a supported step mode."""
    mode = str(value or "").strip().lower()
    if mode == STEP_MODE_COLOR:
        return STEP_MODE_COLOR
    return STEP_MODE_WHITE_TEMP


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
    """Convert RGB tuple to `#RRGGBB`."""
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Convert hex color to RGB tuple."""
    normalized = _normalize_hex_color(value)
    if normalized is None:
        raise ValueError("Invalid hex color")
    raw = normalized[1:]
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def _coerce_rgb_channel(value: Any) -> int | None:
    """Parse and validate one RGB channel (0..255)."""
    try:
        channel = int(value)
    except (TypeError, ValueError):
        return None
    if not (0 <= channel <= 255):
        return None
    return channel


def _parse_rgb_color_value(value: Any) -> tuple[int, int, int] | None:
    """Parse RGB values from selector payloads or hex text input."""
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

    if isinstance(value, str):
        normalized = _normalize_hex_color(value)
        if normalized is None:
            return None
        return _hex_to_rgb(normalized)

    return None


@dataclass(frozen=True)
class _ZhaButtonSignature:
    """Captured ZHA button signature used to match future events."""
    ieee: str
    endpoint_id: int
    command: str
    cluster_id: int | None = None
    args: list[Any] | None = None


def _signature_from_zha_event(ieee: str, data: dict[str, Any]) -> _ZhaButtonSignature:
    """Build a normalized button signature from one captured `zha_event` payload."""
    endpoint_id = data.get(CONF_ENDPOINT_ID)
    command = data.get(CONF_COMMAND)

    if endpoint_id is None or command is None:
        raise ValueError("Missing endpoint_id/command in zha_event")

    return _ZhaButtonSignature(
        ieee=ieee,
        endpoint_id=int(endpoint_id),
        command=str(command),
        cluster_id=data.get(CONF_CLUSTER_ID),
        args=list(data.get(CONF_ARGS, [])) if data.get(CONF_ARGS) is not None else None,
    )


def _zha_ieee_from_device_entry(device_entry: dr.DeviceEntry) -> str | None:
    """Extract ZHA IEEE identifier from a selected device registry entry."""
    for identifier in device_entry.identifiers:
        if not isinstance(identifier, (tuple, list)) or len(identifier) < 2:
            continue
        if identifier[0] == "zha":
            return str(identifier[1])
    return None


def _entry_value(entry: ConfigEntry, key: str, default: Any | None = None) -> Any:
    """Read a value from entry options first, then data."""
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


def _device_option_label(device_entry: dr.DeviceEntry) -> str:
    """Build a readable label for one candidate remote device."""
    name = device_entry.name_by_user or device_entry.name or device_entry.model or device_entry.id
    detail_parts = [
        part
        for part in (device_entry.manufacturer, device_entry.model)
        if isinstance(part, str) and part.strip()
    ]
    if not detail_parts:
        return str(name)

    detail = " / ".join(dict.fromkeys(detail_parts))
    if detail.lower() in str(name).lower():
        return str(name)
    return f"{name} ({detail})"


def _looks_like_remote_trigger(trigger: dict[str, Any]) -> bool:
    """Heuristically identify ZHA remote/button triggers from device automations."""
    if str(trigger.get("domain", "")).lower() != "zha":
        return False

    for key in ("type", "subtype"):
        value = trigger.get(key)
        if not isinstance(value, str):
            continue
        lowered = value.lower()
        if any(hint in lowered for hint in REMOTE_TRIGGER_HINTS):
            return True

    return False


async def _async_zha_remote_selector(
    hass: HomeAssistant, selected_device_id: str | None = None
) -> selector.SelectSelector | selector.DeviceSelector:
    """Return a filtered selector of likely ZHA remotes/buttons.

    We prefer a pre-filtered dropdown backed by device automations so users do not
    need to sift through every Zigbee device in the house. If device automation
    inspection is unavailable for any reason, fall back to the generic ZHA device
    selector so setup still works.
    """
    try:
        from homeassistant.components.device_automation import (
            DeviceAutomationType,
            async_get_device_automations,
        )
    except ImportError:
        LOGGER.debug(
            "Device automation helpers unavailable; falling back to generic ZHA device selector"
        )
        return selector.DeviceSelector(
            selector.DeviceSelectorConfig(integration="zha")
        )

    device_registry = dr.async_get(hass)
    zha_devices = [
        device_entry
        for device_entry in device_registry.devices.values()
        if _zha_ieee_from_device_entry(device_entry)
    ]
    if not zha_devices:
        return selector.DeviceSelector(
            selector.DeviceSelectorConfig(integration="zha")
        )

    device_ids = [device_entry.id for device_entry in zha_devices]
    try:
        triggers_by_device = await async_get_device_automations(
            hass,
            DeviceAutomationType.TRIGGER,
            device_ids,
        )
    except Exception as err:
        LOGGER.debug(
            "Unable to inspect ZHA device automations for remote filtering; falling back to generic selector: %s",
            err,
        )
        return selector.DeviceSelector(
            selector.DeviceSelectorConfig(integration="zha")
        )

    options: list[dict[str, str]] = []
    included_ids: set[str] = set()
    for device_entry in zha_devices:
        device_id = device_entry.id
        triggers = triggers_by_device.get(device_id, [])
        if not any(_looks_like_remote_trigger(trigger) for trigger in triggers):
            continue

        options.append(
            {
                "value": device_id,
                "label": _device_option_label(device_entry),
            }
        )
        included_ids.add(device_id)

    if (
        selected_device_id
        and selected_device_id not in included_ids
        and (selected_device := device_registry.async_get(selected_device_id)) is not None
        and _zha_ieee_from_device_entry(selected_device)
    ):
        options.append(
            {
                "value": selected_device_id,
                "label": _device_option_label(selected_device),
            }
        )

    if not options:
        return selector.DeviceSelector(
            selector.DeviceSelectorConfig(integration="zha")
        )

    options.sort(key=lambda option: option["label"].casefold())
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _infer_device_gesture_support_from_entries(
    hass: HomeAssistant, ieee: str | None
) -> dict[str, bool]:
    """Infer supported gestures from existing entries already using the remote.

    We only infer positive support here. If another entry on the same remote already
    has a long-press or double-press binding captured, we can safely skip the
    capability-probe pages for later entries and edits on that device.
    """
    if not ieee:
        return {}

    inferred: dict[str, bool] = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if _entry_value(entry, CONF_REMOTE_IEEE) != ieee:
            continue

        for gesture, binding_key in GESTURE_BINDING_KEYS.items():
            if inferred.get(gesture) is True:
                continue

            binding = _entry_value(entry, binding_key, None)
            if _binding_signature_from_storage(ieee, binding) is not None:
                inferred[gesture] = True

    return inferred


async def _async_load_known_device_gesture_support(
    hass: HomeAssistant,
    ieee: str | None,
    *,
    extra_supported_gestures: tuple[str, ...] = (),
) -> dict[str, bool]:
    """Return remembered gesture support, backfilling from existing entry data.

    Support is primarily persisted in the integration settings store, but we also
    reconstruct missing positive support from existing config entries so users are
    not forced through duplicate long/double capability checks while editing.
    """
    support = await async_get_device_gesture_support(hass, ieee)
    if not ieee:
        return support

    inferred = _infer_device_gesture_support_from_entries(hass, ieee)
    for gesture in extra_supported_gestures:
        if gesture in (GESTURE_LONG_PRESS, GESTURE_DOUBLE_PRESS):
            inferred[gesture] = True

    merged = dict(support)
    for gesture in (GESTURE_LONG_PRESS, GESTURE_DOUBLE_PRESS):
        if inferred.get(gesture) is not True:
            continue
        merged[gesture] = True
        if support.get(gesture) is True:
            continue
        support = await async_set_device_gesture_support(hass, ieee, gesture, True)

    return merged


def _normalize_target_entity_ids(value: Any) -> list[str]:
    """Normalize selector output to a de-duplicated list of light entity IDs."""
    values: list[Any]
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
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
    return normalized


def _entry_target_entity_ids(entry: ConfigEntry) -> list[str]:
    """Return normalized target entities from a config entry (with legacy fallback)."""
    targets = _entry_value(entry, CONF_TARGET_ENTITY_IDS, None)
    normalized = _normalize_target_entity_ids(targets)
    if normalized:
        return normalized

    legacy_target = _entry_value(entry, CONF_TARGET_ENTITY_ID, None)
    return _normalize_target_entity_ids(legacy_target)


def _light_entity_selector(*, multiple: bool) -> selector.EntitySelector:
    """Return a light entity selector with compatibility for older HA signatures."""
    try:
        config = selector.EntitySelectorConfig(domain=LIGHT_DOMAIN, multiple=multiple)
    except TypeError:
        config = selector.EntitySelectorConfig(domain=LIGHT_DOMAIN)
    return selector.EntitySelector(config)


def _max_parallel_calls_selector() -> selector.NumberSelector:
    """Return selector for integration-wide max parallel service calls."""
    mode = getattr(selector.NumberSelectorMode, "BOX", selector.NumberSelectorMode.SLIDER)
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=MIN_MAX_PARALLEL_CALLS,
            max=MAX_MAX_PARALLEL_CALLS,
            step=1,
            mode=mode,
        )
    )


def _step_mode_field() -> Any:
    """Return mode dropdown with broad Home Assistant compatibility."""
    return vol.In(
        {
            STEP_MODE_WHITE_TEMP: "White & temperature",
            STEP_MODE_COLOR: "Colour",
        }
    )


def _temp_pct_selector() -> selector.NumberSelector:
    """Return selector for white temperature percentage in 0..100 range."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=100,
            step=1,
            mode=selector.NumberSelectorMode.SLIDER,
            unit_of_measurement="%",
        )
    )


def _color_rgb_selector_field() -> tuple[Any, bool]:
    """Return (field, is_text_fallback)."""
    color_selector = getattr(selector, "ColorRGBSelector", None)
    if color_selector is None:
        return selector.TextSelector(), True

    try:
        return color_selector(), False
    except TypeError:
        config_cls = getattr(selector, "ColorRGBSelectorConfig", None)
        if config_cls is not None:
            try:
                return color_selector(config_cls()), False
            except TypeError:
                pass

    return selector.TextSelector(), True


def _boolean_field() -> Any:
    """Return a backwards-compatible boolean field for config flows."""
    boolean_selector = getattr(selector, "BooleanSelector", None)
    if boolean_selector is None:
        return bool
    try:
        return boolean_selector()
    except TypeError:
        return bool


def _step_defaults(step_num: int, total_steps: int, existing: Any | None = None) -> dict[str, Any]:
    """Return normalized default values for one step."""
    existing_step = existing if isinstance(existing, dict) else {}
    label = str(existing_step.get(CONF_STEP_LABEL) or f"Step {step_num}")

    try:
        brightness_pct = int(existing_step.get(CONF_STEP_BRIGHTNESS_PCT))
    except (TypeError, ValueError):
        brightness_pct = _default_brightness_pct(step_num, total_steps)
    brightness_pct = max(1, min(100, brightness_pct))

    mode = _normalize_step_mode(existing_step.get(CONF_STEP_MODE))

    try:
        temp_pct = int(existing_step.get(CONF_STEP_TEMP_PCT, DEFAULT_STEP_TEMP_PCT))
    except (TypeError, ValueError):
        temp_pct = DEFAULT_STEP_TEMP_PCT
    temp_pct = max(0, min(100, temp_pct))

    color_hex = _normalize_hex_color(existing_step.get(CONF_STEP_COLOR_HEX))
    if color_hex is None:
        color_hex = DEFAULT_STEP_COLOR_HEX

    rgb = _parse_rgb_color_value(existing_step.get(CONF_STEP_COLOR_RGB))
    if rgb is None:
        rgb = _parse_rgb_color_value(color_hex)
    if rgb is None:
        rgb = tuple(DEFAULT_STEP_COLOR_RGB)

    return {
        CONF_STEP_LABEL: label,
        CONF_STEP_BRIGHTNESS_PCT: brightness_pct,
        CONF_STEP_MODE: mode,
        CONF_STEP_TEMP_PCT: temp_pct,
        CONF_STEP_COLOR_HEX: color_hex,
        CONF_STEP_COLOR_RGB: [rgb[0], rgb[1], rgb[2]],
    }


def _build_step_defaults(existing_steps: list[dict[str, Any]], on_steps: int) -> list[dict[str, Any]]:
    """Return defaults list sized exactly to selected step count."""
    defaults: list[dict[str, Any]] = []
    for step_num in range(1, on_steps + 1):
        existing = existing_steps[step_num - 1] if step_num - 1 < len(existing_steps) else None
        defaults.append(_step_defaults(step_num, on_steps, existing))
    return defaults


class LightCycleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Light Cycle Controller."""

    VERSION = 3

    def __init__(self) -> None:
        # Step A fields (instance basics).
        self._instance_name: str | None = None
        self._target_entity_ids: list[str] = []

        # Step B/C fields (remote selection and captured device identity).
        self._remote_device_id: str | None = None
        self._remote_device_name: str | None = None
        self._remote_ieee: str | None = None

        # Capture-step listener lifecycle handles.
        self._capture_future: asyncio.Future[dict[str, Any]] | None = None
        self._capture_unsub: Callable[[], None] | None = None

        # Captured button signature and pending global setting updates.
        self._signature: _ZhaButtonSignature | None = None
        self._on_steps: int | None = None
        self._pending_max_parallel_calls: int | None = None
        self._draft_steps: list[dict[str, Any]] = []
        self._details_step_index: int = 1
        self._gesture_bindings: dict[str, dict[str, Any]] = {}
        self._supported_device_gestures: list[str] = []
        self._selected_gesture_targets: dict[str, int] = {}
        self._pending_device_gesture_probes: list[str] = []
        self._current_device_gesture_probe: str | None = None
        self._enabled_gestures: list[str] = []
        self._pending_gesture_captures: list[str] = []
        self._current_gesture_capture: str | None = None

    def _signature_in_use(self, signature: _ZhaButtonSignature) -> bool:
        """Return whether a captured signature is already assigned in this flow."""
        if self._signature == signature:
            return True

        for binding in self._gesture_bindings.values():
            existing = _binding_signature_from_storage(self._remote_ieee, binding)
            if existing == signature:
                return True
        return False

    async def _async_refresh_device_gesture_support(self) -> dict[str, bool]:
        """Load remembered gesture-support flags for the selected device."""
        support = await _async_load_known_device_gesture_support(
            self.hass, self._remote_ieee
        )
        self._supported_device_gestures = [
            gesture
            for gesture in (GESTURE_LONG_PRESS, GESTURE_DOUBLE_PRESS)
            if support.get(gesture) is True
        ]
        return support

    def _prepare_optional_gesture_capture_queue(self, selected_gestures: list[str]) -> None:
        """Reset per-entry gesture capture state for the chosen actions."""
        self._enabled_gestures = list(selected_gestures)
        self._pending_gesture_captures = list(selected_gestures)
        self._current_gesture_capture = None

        # Drop any disabled gesture bindings immediately so stale mappings are never saved.
        self._gesture_bindings = {
            gesture: binding
            for gesture, binding in self._gesture_bindings.items()
            if gesture in self._enabled_gestures
        }
        self._selected_gesture_targets = {
            gesture: target_index
            for gesture, target_index in self._selected_gesture_targets.items()
            if gesture in self._enabled_gestures
        }

    async def _async_next_device_probe_or_capture(self):
        """Continue probing unknown device gesture support, then capture the main press."""
        if self._pending_device_gesture_probes:
            self._current_device_gesture_probe = self._pending_device_gesture_probes.pop(0)
            return await self.async_step_probe_device_gesture()

        self._current_device_gesture_probe = None
        await self._async_refresh_device_gesture_support()
        return await self.async_step_capture()

    async def _async_start_device_support_flow(self):
        """Probe unknown long/double press support for the selected device once."""
        support = await self._async_refresh_device_gesture_support()
        self._pending_device_gesture_probes = [
            gesture
            for gesture in (GESTURE_LONG_PRESS, GESTURE_DOUBLE_PRESS)
            if gesture not in support
        ]
        return await self._async_next_device_probe_or_capture()

    async def _async_finish_create_entry(self):
        """Create the config entry with all captured gesture bindings included."""
        assert self._instance_name is not None
        assert self._target_entity_ids
        assert self._remote_device_id is not None
        assert self._remote_ieee is not None
        assert self._signature is not None

        target_signature = ",".join(sorted(self._target_entity_ids))
        unique_id = (
            f"{self._remote_ieee}:{self._signature.endpoint_id}:{self._signature.command}"
            f":{target_signature}"
        )
        # Unique ID prevents duplicate entries for same remote signature + target set.
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        data = {
            CONF_TARGET_ENTITY_IDS: self._target_entity_ids,
            CONF_TARGET_ENTITY_ID: self._target_entity_ids[0],
            CONF_REMOTE_DEVICE_ID: self._remote_device_id,
            CONF_REMOTE_IEEE: self._remote_ieee,
            CONF_ENDPOINT_ID: self._signature.endpoint_id,
            CONF_COMMAND: self._signature.command,
            CONF_CLUSTER_ID: self._signature.cluster_id,
            CONF_ARGS: self._signature.args,
            CONF_STEPS: self._draft_steps,
            CONF_LONG_PRESS_BINDING: self._gesture_bindings.get(GESTURE_LONG_PRESS),
            CONF_DOUBLE_PRESS_BINDING: self._gesture_bindings.get(GESTURE_DOUBLE_PRESS),
        }
        if self._pending_max_parallel_calls is not None:
            # Persist the integration-wide setting from first-entry setup step.
            await async_set_max_parallel_calls(
                self.hass, self._pending_max_parallel_calls
            )
        return self.async_create_entry(title=self._instance_name, data=data)

    async def _async_next_optional_gesture_step_or_finish(self):
        """Advance per-entry gesture capture flow or finish the entry."""
        if self._pending_gesture_captures:
            self._current_gesture_capture = self._pending_gesture_captures.pop(0)
            return await self.async_step_capture_optional()

        self._current_gesture_capture = None
        return await self._async_finish_create_entry()

    async def async_step_user(self, user_input: ConfigType | None = None):
        """Step A: capture instance name, targets, and initial global parallelism."""
        errors: dict[str, str] = {}
        global_settings = await async_get_settings(self.hass)
        default_max_parallel_calls = int(
            global_settings.get(CONF_MAX_PARALLEL_CALLS, DEFAULT_MAX_PARALLEL_CALLS)
        )
        is_first_entry = len(self.hass.config_entries.async_entries(DOMAIN)) == 0

        if user_input is not None:
            # Parse user input and normalize entity selection output.
            name = user_input[CONF_NAME].strip()
            target_entity_ids = _normalize_target_entity_ids(
                user_input.get(CONF_TARGET_ENTITY_IDS)
            )
            max_parallel_calls = None
            if is_first_entry:
                try:
                    max_parallel_calls = int(user_input[CONF_MAX_PARALLEL_CALLS])
                except (TypeError, ValueError, KeyError):
                    errors[CONF_MAX_PARALLEL_CALLS] = "invalid_max_parallel_calls"
                else:
                    if not (
                        MIN_MAX_PARALLEL_CALLS
                        <= max_parallel_calls
                        <= MAX_MAX_PARALLEL_CALLS
                    ):
                        errors[CONF_MAX_PARALLEL_CALLS] = "invalid_max_parallel_calls"

            if not name:
                errors[CONF_NAME] = "name_required"
            elif not target_entity_ids:
                errors[CONF_TARGET_ENTITY_IDS] = "target_required"
            else:
                # Persist validated values on this flow instance for next wizard steps.
                if is_first_entry and max_parallel_calls is not None:
                    self._pending_max_parallel_calls = max_parallel_calls
                self._instance_name = name
                self._target_entity_ids = target_entity_ids
                if not errors:
                    return await self.async_step_device()

        schema_dict: dict[Any, Any] = {
            vol.Required(CONF_NAME): selector.TextSelector(),
            vol.Required(CONF_TARGET_ENTITY_IDS): _light_entity_selector(multiple=True),
        }
        if is_first_entry:
            schema_dict[
                vol.Required(
                    CONF_MAX_PARALLEL_CALLS, default=default_max_parallel_calls
                )
            ] = _max_parallel_calls_selector()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={
                "default_max_parallel_calls": str(default_max_parallel_calls)
            },
        )

    async def async_step_device(self, user_input: ConfigType | None = None):
        """Step B: select the ZHA device to capture and react to."""
        errors: dict[str, str] = {}

        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID]
            device_registry = dr.async_get(self.hass)
            device_entry = device_registry.async_get(device_id)

            if device_entry is None:
                errors["base"] = "not_zha_device"
            else:
                ieee = _zha_ieee_from_device_entry(device_entry)
                if not ieee:
                    errors["base"] = "not_zha_device"
                else:
                    # Store both display name (UX text) and IEEE (event matching key).
                    self._remote_device_id = device_id
                    self._remote_device_name = device_entry.name_by_user or device_entry.name
                    self._remote_ieee = ieee
                    self._async_reset_capture()
                    return await self._async_start_device_support_flow()

        device_selector = await _async_zha_remote_selector(self.hass)
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): device_selector
            }
        )
        return self.async_show_form(step_id="device", data_schema=schema, errors=errors)

    async def async_step_probe_device_gesture(
        self, user_input: ConfigType | None = None
    ):
        """Probe whether the selected remote exposes one optional gesture in ZHA."""
        errors: dict[str, str] = {}
        gesture = self._current_device_gesture_probe

        if self._remote_ieee is None or gesture is None:
            return await self._async_next_device_probe_or_capture()

        if user_input is not None:
            if user_input.get(CONF_GESTURE_NOT_SUPPORTED):
                await async_set_device_gesture_support(
                    self.hass,
                    self._remote_ieee,
                    gesture,
                    False,
                )
                return await self._async_next_device_probe_or_capture()

            try:
                self._async_reset_capture()
                self._async_ensure_capture_listener()
                assert self._capture_future is not None
                await asyncio.wait_for(
                    asyncio.shield(self._capture_future),
                    timeout=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                errors["base"] = "no_press"
            except AssertionError:
                errors["base"] = "invalid_event"
            else:
                await async_set_device_gesture_support(
                    self.hass,
                    self._remote_ieee,
                    gesture,
                    True,
                )
                return await self._async_next_device_probe_or_capture()
            finally:
                if errors:
                    self._async_reset_capture()

        return self.async_show_form(
            step_id="probe_device_gesture",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_GESTURE_NOT_SUPPORTED,
                        default=False,
                    ): _boolean_field()
                }
            ),
            errors=errors,
            description_placeholders={
                "gesture": _gesture_label(gesture),
                "device": self._remote_device_name or (self._remote_ieee or "the device"),
            },
        )

    @callback
    def _async_reset_capture(self) -> None:
        """Clear capture listener/future so capture can be restarted safely."""
        if self._capture_unsub is not None:
            self._capture_unsub()
            self._capture_unsub = None
        self._capture_future = None

    @callback
    def _async_ensure_capture_listener(self) -> None:
        """Attach one-shot `zha_event` listener used by capture step."""
        if self._remote_ieee is None:
            return
        if self._capture_future is None or self._capture_future.done():
            self._capture_future = self.hass.loop.create_future()
        if self._capture_unsub is not None:
            return

        @callback
        def _handle_event(event: Event) -> None:
            if self._remote_ieee is None:
                return
            data = event.data
            if data.get(CONF_REMOTE_IEEE, data.get("device_ieee")) != self._remote_ieee:
                return

            if self._capture_future is None or self._capture_future.done():
                return

            self._capture_future.set_result(data)
            if self._capture_unsub is not None:
                self._capture_unsub()
                self._capture_unsub = None

        self._capture_unsub = self.hass.bus.async_listen(EVENT_ZHA_EVENT, _handle_event)

    async def async_step_capture(self, user_input: ConfigType | None = None):
        """Step C: after submit, wait for the next matching physical button press."""
        errors: dict[str, str] = {}

        if self._remote_ieee is None:
            errors["base"] = "not_zha_device"

        if user_input is not None and not errors:
            try:
                # Re-arm listener each attempt to ensure stale futures are not reused.
                self._async_reset_capture()
                self._async_ensure_capture_listener()
                assert self._capture_future is not None
                data = await asyncio.wait_for(
                    asyncio.shield(self._capture_future),
                    timeout=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
                )
                self._signature = _signature_from_zha_event(self._remote_ieee, data)
            except asyncio.TimeoutError:
                errors["base"] = "no_press"
            except (AssertionError, ValueError):
                errors["base"] = "invalid_event"
            else:
                return await self.async_step_steps_count()
            finally:
                # Keep listener active only while waiting for a capture result.
                if errors:
                    self._async_reset_capture()

        return self.async_show_form(
            step_id="capture",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "device": self._remote_device_name or (self._remote_ieee or "the device")
            },
        )

    async def async_step_steps_count(self, user_input: ConfigType | None = None):
        """Step D1: collect number of On steps for dynamic form generation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                on_steps = int(user_input[CONF_ON_STEPS])
            except (TypeError, ValueError):
                errors["base"] = "invalid_step_count"
            else:
                if not (MIN_ON_STEPS <= on_steps <= MAX_ON_STEPS):
                    errors["base"] = "invalid_step_count"
                else:
                    self._on_steps = on_steps
                    self._draft_steps = _build_step_defaults([], on_steps)
                    self._details_step_index = 1
                    return await self.async_step_steps()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ON_STEPS,
                    default=3,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_ON_STEPS,
                        max=MAX_ON_STEPS,
                        step=1,
                        mode=selector.NumberSelectorMode.SLIDER,
                    )
                )
            }
        )

        return self.async_show_form(step_id="steps_count", data_schema=schema, errors=errors)

    async def async_step_steps(self, user_input: ConfigType | None = None):
        """Step D2: collect per-step labels, brightness, and mode."""
        errors: dict[str, str] = {}

        assert self._on_steps is not None
        on_steps = self._on_steps
        if len(self._draft_steps) != on_steps:
            self._draft_steps = _build_step_defaults(self._draft_steps, on_steps)

        if user_input is not None:
            # Build the base step payload list from dynamic keys.
            steps: list[dict[str, Any]] = []
            for step_num in range(1, on_steps + 1):
                label_key = _step_label_key(step_num)
                brightness_key = _step_brightness_key(step_num)
                mode_key = _step_mode_key(step_num)
                existing = (
                    self._draft_steps[step_num - 1]
                    if step_num - 1 < len(self._draft_steps)
                    else _step_defaults(step_num, on_steps)
                )

                label = str(user_input.get(label_key, "")).strip()
                if not label:
                    errors[label_key] = "label_required"
                    continue

                try:
                    brightness_pct = int(user_input[brightness_key])
                except (TypeError, ValueError):
                    errors[brightness_key] = "invalid_brightness"
                    continue

                brightness_pct = max(1, min(100, brightness_pct))
                mode = _normalize_step_mode(user_input.get(mode_key))
                step_data: dict[str, Any] = dict(existing)
                step_data[CONF_STEP_LABEL] = label
                step_data[CONF_STEP_BRIGHTNESS_PCT] = brightness_pct
                step_data[CONF_STEP_MODE] = mode

                steps.append(step_data)

            if not errors:
                self._draft_steps = steps
                self._details_step_index = 1
                return await self.async_step_steps_details()

        schema_dict: dict[Any, Any] = {}
        for step_num in range(1, on_steps + 1):
            existing = (
                self._draft_steps[step_num - 1]
                if step_num - 1 < len(self._draft_steps)
                else _step_defaults(step_num, on_steps)
            )
            # Generate base fields for each configured On step.
            schema_dict[
                vol.Required(
                    _step_label_key(step_num),
                    default=str(existing.get(CONF_STEP_LABEL, f"Step {step_num}")),
                )
            ] = selector.TextSelector()
            schema_dict[
                vol.Required(
                    _step_brightness_key(step_num),
                    default=int(existing.get(CONF_STEP_BRIGHTNESS_PCT, _default_brightness_pct(step_num, on_steps))),
                )
            ] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=100,
                    step=1,
                    mode=selector.NumberSelectorMode.SLIDER,
                    unit_of_measurement="%",
                )
            )
            schema_dict[
                vol.Required(
                    _step_mode_key(step_num),
                    default=_normalize_step_mode(existing.get(CONF_STEP_MODE)),
                )
            ] = _step_mode_field()

        return self.async_show_form(
            step_id="steps",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={"on_steps": str(on_steps)},
        )

    async def async_step_steps_details(self, user_input: ConfigType | None = None):
        """Step D3: collect mode-specific fields one step at a time."""
        errors: dict[str, str] = {}

        assert self._on_steps is not None
        on_steps = self._on_steps
        if len(self._draft_steps) != on_steps:
            self._draft_steps = _build_step_defaults(self._draft_steps, on_steps)

        step_num = max(1, min(on_steps, self._details_step_index))
        step_index = step_num - 1
        step_data = dict(self._draft_steps[step_index])
        mode = _normalize_step_mode(step_data.get(CONF_STEP_MODE))

        temp_key = _step_temp_pct_key(step_num)
        color_hex_key = _step_color_hex_key(step_num)
        color_rgb_key = _step_color_rgb_key(step_num)

        if user_input is not None:
            if mode == STEP_MODE_WHITE_TEMP:
                try:
                    temp_pct = int(user_input[temp_key])
                except (TypeError, ValueError, KeyError):
                    errors[temp_key] = "invalid_temp_pct"
                else:
                    step_data[CONF_STEP_TEMP_PCT] = max(0, min(100, temp_pct))
                    step_data[CONF_STEP_COLOR_HEX] = DEFAULT_STEP_COLOR_HEX
                    step_data[CONF_STEP_COLOR_RGB] = list(DEFAULT_STEP_COLOR_RGB)
            else:
                color_hex = _normalize_hex_color(user_input.get(color_hex_key))
                rgb_value = _parse_rgb_color_value(user_input.get(color_rgb_key))

                if color_hex is not None:
                    rgb = _hex_to_rgb(color_hex)
                elif rgb_value is not None:
                    rgb = rgb_value
                    color_hex = _rgb_to_hex(rgb)
                else:
                    errors[color_hex_key] = "invalid_color"

                if not errors:
                    step_data[CONF_STEP_TEMP_PCT] = DEFAULT_STEP_TEMP_PCT
                    step_data[CONF_STEP_COLOR_HEX] = color_hex
                    step_data[CONF_STEP_COLOR_RGB] = [rgb[0], rgb[1], rgb[2]]

            if not errors:
                self._draft_steps[step_index] = step_data
                if step_num < on_steps:
                    self._details_step_index = step_num + 1
                    return await self.async_step_steps_details()

                return await self.async_step_gestures()

        schema_dict: dict[Any, Any] = {}
        if mode == STEP_MODE_WHITE_TEMP:
            schema_dict[
                vol.Required(
                    temp_key,
                    default=int(step_data.get(CONF_STEP_TEMP_PCT, DEFAULT_STEP_TEMP_PCT)),
                )
            ] = _temp_pct_selector()
        else:
            color_rgb_field, rgb_field_is_text = _color_rgb_selector_field()
            default_color_hex = _normalize_hex_color(step_data.get(CONF_STEP_COLOR_HEX))
            if default_color_hex is None:
                default_color_hex = DEFAULT_STEP_COLOR_HEX
            default_rgb = _parse_rgb_color_value(step_data.get(CONF_STEP_COLOR_RGB))
            if default_rgb is None:
                default_rgb = _parse_rgb_color_value(default_color_hex)
            if default_rgb is None:
                default_rgb = tuple(DEFAULT_STEP_COLOR_RGB)

            schema_dict[
                vol.Required(
                    color_rgb_key,
                    default=(
                        default_color_hex
                        if rgb_field_is_text
                        else [default_rgb[0], default_rgb[1], default_rgb[2]]
                    ),
                )
            ] = color_rgb_field
            schema_dict[
                vol.Required(
                    color_hex_key,
                    default=default_color_hex,
                )
            ] = selector.TextSelector()

        return self.async_show_form(
            step_id="steps_details",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={"step": str(step_num), "on_steps": str(on_steps)},
        )

    async def async_step_gestures(self, user_input: ConfigType | None = None):
        """Step E: choose what each supported optional gesture should do."""
        supported_gestures = list(self._supported_device_gestures)
        if not supported_gestures:
            return await self._async_finish_create_entry()

        errors: dict[str, str] = {}
        if user_input is not None:
            selected_gestures: list[str] = []
            selected_targets: dict[str, int] = {}

            for gesture in supported_gestures:
                raw_target = user_input.get(GESTURE_TARGET_KEYS[gesture], GESTURE_TARGET_NONE)
                if raw_target == GESTURE_TARGET_NONE:
                    self._gesture_bindings.pop(gesture, None)
                    continue

                try:
                    target_index = int(raw_target)
                except (TypeError, ValueError):
                    errors[GESTURE_TARGET_KEYS[gesture]] = "invalid_gesture_target"
                    continue

                selected_gestures.append(gesture)
                selected_targets[gesture] = max(0, min(len(self._draft_steps), target_index))

            if not errors:
                self._selected_gesture_targets = selected_targets
                self._prepare_optional_gesture_capture_queue(selected_gestures)
                return await self._async_next_optional_gesture_step_or_finish()

        selector_field = _step_target_selector(self._draft_steps, include_none=True)
        schema = vol.Schema(
            {
                vol.Required(
                    GESTURE_TARGET_KEYS[gesture],
                    default=(
                        GESTURE_TARGET_NONE
                        if gesture not in self._selected_gesture_targets
                        else str(
                            max(
                                0,
                                min(
                                    len(self._draft_steps),
                                    self._selected_gesture_targets[gesture],
                                ),
                            )
                        )
                    ),
                ): selector_field
                for gesture in supported_gestures
            }
        )
        return self.async_show_form(
            step_id="gestures",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "device": self._remote_device_name or (self._remote_ieee or "the device")
            },
        )

    async def async_step_capture_optional(self, user_input: ConfigType | None = None):
        """Step F: capture one optional long/double gesture for direct-jump actions."""
        errors: dict[str, str] = {}
        gesture = self._current_gesture_capture

        if self._remote_ieee is None or gesture is None:
            return await self._async_next_optional_gesture_step_or_finish()

        if user_input is not None:
            if user_input.get(CONF_SKIP_CAPTURE):
                self._gesture_bindings.pop(gesture, None)
                self._selected_gesture_targets.pop(gesture, None)
                return await self._async_next_optional_gesture_step_or_finish()

            try:
                self._async_reset_capture()
                self._async_ensure_capture_listener()
                assert self._capture_future is not None
                data = await asyncio.wait_for(
                    asyncio.shield(self._capture_future),
                    timeout=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
                )
                signature = _signature_from_zha_event(self._remote_ieee, data)
                if self._signature_in_use(signature):
                    errors["base"] = "duplicate_gesture"
                else:
                    existing_target = self._selected_gesture_targets.get(gesture, 0)
                    self._gesture_bindings[gesture] = _signature_to_storage(
                        signature,
                        existing_target,
                    )
            except asyncio.TimeoutError:
                errors["base"] = "no_press"
            except (AssertionError, ValueError):
                errors["base"] = "invalid_event"
            finally:
                if errors:
                    self._async_reset_capture()

            if not errors:
                return await self._async_next_optional_gesture_step_or_finish()

        schema = vol.Schema(
            {vol.Optional(CONF_SKIP_CAPTURE, default=False): _boolean_field()}
        )
        return self.async_show_form(
            step_id="capture_optional",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "gesture": _gesture_label(gesture),
                "device": self._remote_device_name or (self._remote_ieee or "the device"),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return options flow handler for an existing config entry."""
        return LightCycleOptionsFlowHandler(config_entry)


class LightCycleOptionsFlowHandler(OptionsFlow):
    """Handle options for an existing Light Cycle Controller entry."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        # Keep compatibility with HA versions where OptionsFlow init signatures differ.
        try:
            super().__init__(config_entry)
        except TypeError:
            super().__init__()
        self._config_entry = config_entry

        # Seed editable values from existing entry/options.
        self._instance_name: str = config_entry.title
        self._target_entity_ids: list[str] = _entry_target_entity_ids(config_entry)
        self._remote_device_id: str | None = _entry_value(
            config_entry, CONF_REMOTE_DEVICE_ID, None
        )

        self._remote_device_name: str | None = None
        self._remote_ieee: str | None = None

        self._signature: _ZhaButtonSignature | None = None
        self._on_steps: int | None = None
        self._pending_max_parallel_calls: int | None = None
        self._main_signature_recapture_required: bool = False

        self._existing_steps: list[dict[str, Any]] = list(
            _entry_value(config_entry, CONF_STEPS, [])
        )
        self._draft_steps: list[dict[str, Any]] = list(self._existing_steps)
        self._details_step_index: int = 1
        self._gesture_bindings: dict[str, dict[str, Any]] = {}
        for gesture, binding_key in GESTURE_BINDING_KEYS.items():
            binding = _entry_value(config_entry, binding_key, None)
            if isinstance(binding, dict):
                self._gesture_bindings[gesture] = dict(binding)
        self._supported_device_gestures: list[str] = []
        self._selected_gesture_targets: dict[str, int] = {
            gesture: _binding_target_index_from_storage(binding, len(self._draft_steps))
            for gesture, binding in self._gesture_bindings.items()
        }
        self._pending_device_gesture_probes: list[str] = []
        self._current_device_gesture_probe: str | None = None
        self._enabled_gestures: list[str] = list(self._gesture_bindings)
        self._pending_gesture_captures: list[str] = []
        self._current_gesture_capture: str | None = None

    def _signature_in_use(self, signature: _ZhaButtonSignature) -> bool:
        """Return whether a captured signature is already assigned in this options flow."""
        if self._signature == signature:
            return True

        for binding in self._gesture_bindings.values():
            existing = _binding_signature_from_storage(self._remote_ieee, binding)
            if existing == signature:
                return True
        return False

    async def _async_refresh_device_gesture_support(self) -> dict[str, bool]:
        """Load remembered gesture-support flags for the selected options-flow device."""
        support = await _async_load_known_device_gesture_support(
            self.hass,
            self._remote_ieee,
            extra_supported_gestures=tuple(self._gesture_bindings),
        )
        self._supported_device_gestures = [
            gesture
            for gesture in (GESTURE_LONG_PRESS, GESTURE_DOUBLE_PRESS)
            if support.get(gesture) is True
        ]
        return support

    def _prepare_optional_gesture_capture_queue(
        self, selected_gestures: list[str], recapture_gestures: list[str]
    ) -> None:
        """Apply enabled gesture list and queue only the gestures that need capture."""
        self._enabled_gestures = list(selected_gestures)
        self._pending_gesture_captures = list(recapture_gestures)
        self._current_gesture_capture = None
        self._gesture_bindings = {
            gesture: binding
            for gesture, binding in self._gesture_bindings.items()
            if gesture in self._enabled_gestures
        }
        self._selected_gesture_targets = {
            gesture: target_index
            for gesture, target_index in self._selected_gesture_targets.items()
            if gesture in self._enabled_gestures
        }

    async def _async_next_device_probe_or_continue(self):
        """Continue probing support for the selected device, then resume options flow."""
        if self._pending_device_gesture_probes:
            self._current_device_gesture_probe = self._pending_device_gesture_probes.pop(0)
            return await self.async_step_probe_device_gesture()

        self._current_device_gesture_probe = None
        await self._async_refresh_device_gesture_support()
        if self._main_signature_recapture_required:
            return await self.async_step_capture()
        return await self.async_step_steps()

    async def _async_start_device_support_flow(self):
        """Probe unknown long/double press support for the selected options-flow device."""
        support = await self._async_refresh_device_gesture_support()
        self._pending_device_gesture_probes = [
            gesture
            for gesture in (GESTURE_LONG_PRESS, GESTURE_DOUBLE_PRESS)
            if gesture not in support
        ]
        return await self._async_next_device_probe_or_continue()

    async def _async_finish_options_entry(self):
        """Persist edited controller options including optional gesture bindings."""
        assert self._remote_ieee is not None
        assert self._signature is not None

        updated_title = self._instance_name.strip()
        options = {
            CONF_TARGET_ENTITY_IDS: self._target_entity_ids,
            CONF_TARGET_ENTITY_ID: self._target_entity_ids[0],
            CONF_REMOTE_DEVICE_ID: self._remote_device_id,
            CONF_REMOTE_IEEE: self._remote_ieee,
            CONF_ENDPOINT_ID: self._signature.endpoint_id,
            CONF_COMMAND: self._signature.command,
            CONF_CLUSTER_ID: self._signature.cluster_id,
            CONF_ARGS: self._signature.args,
            CONF_STEPS: self._draft_steps,
            CONF_LONG_PRESS_BINDING: self._gesture_bindings.get(GESTURE_LONG_PRESS),
            CONF_DOUBLE_PRESS_BINDING: self._gesture_bindings.get(GESTURE_DOUBLE_PRESS),
        }
        if self._pending_max_parallel_calls is not None:
            # Save integration-wide parallelism when changed from options flow.
            await async_set_max_parallel_calls(
                self.hass, self._pending_max_parallel_calls
            )
        if updated_title != self._config_entry.title:
            # Keep the config-entry title aligned with the user-editable instance name.
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                title=updated_title,
            )
        LOGGER.info(
            "Saving options for entry %s: title=%s steps=%s brightness=%s gestures=%s",
            self._config_entry.entry_id,
            updated_title,
            len(self._draft_steps),
            [s.get(CONF_STEP_BRIGHTNESS_PCT) for s in self._draft_steps],
            sorted(self._gesture_bindings),
        )
        return self.async_create_entry(title="", data=options)

    async def _async_next_optional_gesture_step_or_finish(self):
        """Advance options gesture capture flow or save the edited entry."""
        if self._pending_gesture_captures:
            self._current_gesture_capture = self._pending_gesture_captures.pop(0)
            return await self.async_step_capture_optional()

        self._current_gesture_capture = None
        return await self._async_finish_options_entry()

    async def async_step_init(self, user_input: ConfigType | None = None):
        """Options step A: edit targets, remote, step-count, and global parallelism."""
        errors: dict[str, str] = {}
        global_settings = await async_get_settings(self.hass)
        default_max_parallel_calls = int(
            global_settings.get(CONF_MAX_PARALLEL_CALLS, DEFAULT_MAX_PARALLEL_CALLS)
        )

        if user_input is not None:
            # Parse and validate all editable options from the first options page.
            name = str(user_input.get(CONF_NAME, "")).strip()
            target_entity_ids = _normalize_target_entity_ids(
                user_input.get(CONF_TARGET_ENTITY_IDS)
            )
            remote_device_id = user_input[CONF_REMOTE_DEVICE_ID]
            recapture = bool(user_input.get(CONF_RECAPTURE, False))
            try:
                max_parallel_calls = int(user_input[CONF_MAX_PARALLEL_CALLS])
            except (TypeError, ValueError, KeyError):
                errors[CONF_MAX_PARALLEL_CALLS] = "invalid_max_parallel_calls"
                max_parallel_calls = default_max_parallel_calls
            else:
                if not (
                    MIN_MAX_PARALLEL_CALLS
                    <= max_parallel_calls
                    <= MAX_MAX_PARALLEL_CALLS
                ):
                    errors[CONF_MAX_PARALLEL_CALLS] = "invalid_max_parallel_calls"

            if not name:
                errors[CONF_NAME] = "name_required"

            try:
                on_steps = int(user_input[CONF_ON_STEPS])
            except (TypeError, ValueError):
                errors["base"] = "invalid_step_count"
            else:
                if not (MIN_ON_STEPS <= on_steps <= MAX_ON_STEPS):
                    errors["base"] = "invalid_step_count"

            if not errors:
                if not target_entity_ids:
                    errors[CONF_TARGET_ENTITY_IDS] = "target_required"

            if not errors:
                LOGGER.info(
                    "Options init for entry %s: targets=%s device=%s on_steps=%s recapture=%s max_parallel_calls=%s",
                    self._config_entry.entry_id,
                    target_entity_ids,
                    remote_device_id,
                    on_steps,
                    recapture,
                    max_parallel_calls,
                )
                device_changed = remote_device_id != self._remote_device_id
                if device_changed:
                    # Device change always requires a fresh button capture.
                    recapture = True

                device_registry = dr.async_get(self.hass)
                device_entry = device_registry.async_get(remote_device_id)
                if device_entry is None:
                    errors["base"] = "not_zha_device"
                else:
                    ieee = _zha_ieee_from_device_entry(device_entry)
                    if not ieee:
                        errors["base"] = "not_zha_device"
                    else:
                        # Persist normalized values for follow-up capture/steps pages.
                        self._target_entity_ids = target_entity_ids
                        self._remote_device_id = remote_device_id
                        self._remote_device_name = (
                            device_entry.name_by_user or device_entry.name
                        )
                        self._instance_name = name
                        self._remote_ieee = ieee
                        self._on_steps = on_steps
                        self._pending_max_parallel_calls = max_parallel_calls
                        self._draft_steps = _build_step_defaults(self._existing_steps, on_steps)
                        self._details_step_index = 1
                        self._main_signature_recapture_required = False

                        if device_changed:
                            # Extra gesture captures belong to the old remote and must not
                            # be reused after switching devices.
                            self._gesture_bindings = {}
                            self._selected_gesture_targets = {}

                        if recapture:
                            # Clear prior capture and proceed to capture page.
                            self._signature = None
                            self._main_signature_recapture_required = True
                            return await self._async_start_device_support_flow()

                        endpoint_id = _entry_value(self._config_entry, CONF_ENDPOINT_ID)
                        command = _entry_value(self._config_entry, CONF_COMMAND)
                        if endpoint_id is None or command is None:
                            # Missing historical signature means we must capture again.
                            self._main_signature_recapture_required = True
                            return await self._async_start_device_support_flow()

                        self._signature = _ZhaButtonSignature(
                            ieee=ieee,
                            endpoint_id=int(endpoint_id),
                            command=str(command),
                            cluster_id=_entry_value(self._config_entry, CONF_CLUSTER_ID),
                            args=_entry_value(self._config_entry, CONF_ARGS),
                        )
                        return await self._async_start_device_support_flow()

        target_key = (
            vol.Required(CONF_TARGET_ENTITY_IDS, default=self._target_entity_ids)
            if self._target_entity_ids
            else vol.Required(CONF_TARGET_ENTITY_IDS)
        )
        device_key = (
            vol.Required(CONF_REMOTE_DEVICE_ID, default=self._remote_device_id)
            if self._remote_device_id
            else vol.Required(CONF_REMOTE_DEVICE_ID)
        )

        remote_selector = await _async_zha_remote_selector(
            self.hass, self._remote_device_id
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=self._instance_name): selector.TextSelector(),
                target_key: _light_entity_selector(multiple=True),
                device_key: remote_selector,
                vol.Optional(CONF_RECAPTURE, default=False): _boolean_field(),
                vol.Required(
                    CONF_MAX_PARALLEL_CALLS, default=default_max_parallel_calls
                ): _max_parallel_calls_selector(),
                vol.Required(
                    CONF_ON_STEPS, default=len(self._existing_steps) or 3
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_ON_STEPS,
                        max=MAX_ON_STEPS,
                        step=1,
                        mode=selector.NumberSelectorMode.SLIDER,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "default_max_parallel_calls": str(default_max_parallel_calls)
            },
        )

    async def async_step_probe_device_gesture(
        self, user_input: ConfigType | None = None
    ):
        """Probe whether the selected options-flow device exposes one gesture in ZHA."""
        errors: dict[str, str] = {}
        gesture = self._current_device_gesture_probe

        if self._remote_ieee is None or gesture is None:
            return await self._async_next_device_probe_or_continue()

        if user_input is not None:
            if user_input.get(CONF_GESTURE_NOT_SUPPORTED):
                await async_set_device_gesture_support(
                    self.hass,
                    self._remote_ieee,
                    gesture,
                    False,
                )
                return await self._async_next_device_probe_or_continue()

            future: asyncio.Future[dict[str, Any]] = self.hass.loop.create_future()

            @callback
            def _handle_event(event: Event) -> None:
                if self._remote_ieee is None:
                    return
                data = event.data
                if data.get("device_ieee") != self._remote_ieee:
                    return
                if not future.done():
                    future.set_result(data)

            unsub = self.hass.bus.async_listen(EVENT_ZHA_EVENT, _handle_event)
            try:
                await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                errors["base"] = "no_press"
            finally:
                unsub()

            if not errors:
                await async_set_device_gesture_support(
                    self.hass,
                    self._remote_ieee,
                    gesture,
                    True,
                )
                return await self._async_next_device_probe_or_continue()

        return self.async_show_form(
            step_id="probe_device_gesture",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_GESTURE_NOT_SUPPORTED,
                        default=False,
                    ): _boolean_field()
                }
            ),
            errors=errors,
            description_placeholders={
                "gesture": _gesture_label(gesture),
                "device": self._remote_device_name or (self._remote_ieee or "the device"),
            },
        )

    async def async_step_capture(self, user_input: ConfigType | None = None):
        """Options step B: capture replacement button signature for this entry."""
        errors: dict[str, str] = {}

        if self._remote_ieee is None:
            errors["base"] = "not_zha_device"

        if user_input is not None and not errors:
            # Create one-shot listener for the next matching event after submit.
            future: asyncio.Future[dict[str, Any]] = self.hass.loop.create_future()

            @callback
            def _handle_event(event: Event) -> None:
                if self._remote_ieee is None:
                    return
                data = event.data
                if data.get("device_ieee") != self._remote_ieee:
                    return
                if not future.done():
                    future.set_result(data)

            unsub = self.hass.bus.async_listen(EVENT_ZHA_EVENT, _handle_event)
            try:
                data = await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
                )
                self._signature = _signature_from_zha_event(self._remote_ieee, data)
            except asyncio.TimeoutError:
                errors["base"] = "no_press"
            except ValueError:
                errors["base"] = "invalid_event"
            finally:
                unsub()

            if not errors:
                return await self.async_step_steps()

        return self.async_show_form(
            step_id="capture",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "device": self._remote_device_name or (self._remote_ieee or "the device")
            },
        )

    async def async_step_steps(self, user_input: ConfigType | None = None):
        """Options step C: edit step labels, brightness, and mode only."""
        errors: dict[str, str] = {}

        on_steps = self._on_steps or len(self._existing_steps) or 3
        if len(self._draft_steps) != on_steps:
            self._draft_steps = _build_step_defaults(self._draft_steps or self._existing_steps, on_steps)

        if user_input is not None:
            # Rebuild base step fields only; mode-specific values come in next page.
            steps: list[dict[str, Any]] = []
            for step_num in range(1, on_steps + 1):
                label_key = _step_label_key(step_num)
                brightness_key = _step_brightness_key(step_num)
                mode_key = _step_mode_key(step_num)
                existing = (
                    self._draft_steps[step_num - 1]
                    if step_num - 1 < len(self._draft_steps)
                    else _step_defaults(step_num, on_steps)
                )

                label = str(user_input.get(label_key, "")).strip()
                if not label:
                    errors[label_key] = "label_required"
                    continue

                try:
                    brightness_pct = int(user_input[brightness_key])
                except (TypeError, ValueError):
                    errors[brightness_key] = "invalid_brightness"
                    continue

                brightness_pct = max(1, min(100, brightness_pct))
                mode = _normalize_step_mode(user_input.get(mode_key))
                step_data: dict[str, Any] = dict(existing)
                step_data[CONF_STEP_LABEL] = label
                step_data[CONF_STEP_BRIGHTNESS_PCT] = brightness_pct
                step_data[CONF_STEP_MODE] = mode

                steps.append(step_data)

            if not errors:
                self._draft_steps = steps
                self._details_step_index = 1
                return await self.async_step_steps_details()

        schema_dict: dict[Any, Any] = {}
        for step_num in range(1, on_steps + 1):
            existing = (
                self._draft_steps[step_num - 1]
                if step_num - 1 < len(self._draft_steps)
                else _step_defaults(step_num, on_steps)
            )
            schema_dict[vol.Required(_step_label_key(step_num), default=str(existing.get(CONF_STEP_LABEL, f"Step {step_num}")))] = (
                selector.TextSelector()
            )
            schema_dict[
                vol.Required(
                    _step_brightness_key(step_num),
                    default=int(existing.get(CONF_STEP_BRIGHTNESS_PCT, _default_brightness_pct(step_num, on_steps))),
                )
            ] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=100,
                    step=1,
                    mode=selector.NumberSelectorMode.SLIDER,
                    unit_of_measurement="%",
                )
            )
            schema_dict[
                vol.Required(
                    _step_mode_key(step_num),
                    default=_normalize_step_mode(existing.get(CONF_STEP_MODE)),
                )
            ] = _step_mode_field()

        return self.async_show_form(
            step_id="steps",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={"on_steps": str(on_steps)},
        )

    async def async_step_steps_details(self, user_input: ConfigType | None = None):
        """Options step D: edit mode-specific fields one step at a time."""
        errors: dict[str, str] = {}

        on_steps = self._on_steps or len(self._existing_steps) or 3
        if len(self._draft_steps) != on_steps:
            self._draft_steps = _build_step_defaults(self._draft_steps or self._existing_steps, on_steps)

        step_num = max(1, min(on_steps, self._details_step_index))
        step_index = step_num - 1
        step_data = dict(self._draft_steps[step_index])
        mode = _normalize_step_mode(step_data.get(CONF_STEP_MODE))

        temp_key = _step_temp_pct_key(step_num)
        color_hex_key = _step_color_hex_key(step_num)
        color_rgb_key = _step_color_rgb_key(step_num)

        if user_input is not None:
            if mode == STEP_MODE_WHITE_TEMP:
                try:
                    temp_pct = int(user_input[temp_key])
                except (TypeError, ValueError, KeyError):
                    errors[temp_key] = "invalid_temp_pct"
                else:
                    step_data[CONF_STEP_TEMP_PCT] = max(0, min(100, temp_pct))
                    step_data[CONF_STEP_COLOR_HEX] = DEFAULT_STEP_COLOR_HEX
                    step_data[CONF_STEP_COLOR_RGB] = list(DEFAULT_STEP_COLOR_RGB)
            else:
                color_hex = _normalize_hex_color(user_input.get(color_hex_key))
                rgb_value = _parse_rgb_color_value(user_input.get(color_rgb_key))

                if color_hex is not None:
                    rgb = _hex_to_rgb(color_hex)
                elif rgb_value is not None:
                    rgb = rgb_value
                    color_hex = _rgb_to_hex(rgb)
                else:
                    errors[color_hex_key] = "invalid_color"

                if not errors:
                    step_data[CONF_STEP_TEMP_PCT] = DEFAULT_STEP_TEMP_PCT
                    step_data[CONF_STEP_COLOR_HEX] = color_hex
                    step_data[CONF_STEP_COLOR_RGB] = [rgb[0], rgb[1], rgb[2]]

            if not errors:
                self._draft_steps[step_index] = step_data
                if step_num < on_steps:
                    self._details_step_index = step_num + 1
                    return await self.async_step_steps_details()

                return await self.async_step_gestures()

        schema_dict: dict[Any, Any] = {}
        if mode == STEP_MODE_WHITE_TEMP:
            schema_dict[
                vol.Required(
                    temp_key,
                    default=int(step_data.get(CONF_STEP_TEMP_PCT, DEFAULT_STEP_TEMP_PCT)),
                )
            ] = _temp_pct_selector()
        else:
            color_rgb_field, rgb_field_is_text = _color_rgb_selector_field()
            default_color_hex = _normalize_hex_color(step_data.get(CONF_STEP_COLOR_HEX))
            if default_color_hex is None:
                default_color_hex = DEFAULT_STEP_COLOR_HEX
            default_rgb = _parse_rgb_color_value(step_data.get(CONF_STEP_COLOR_RGB))
            if default_rgb is None:
                default_rgb = _parse_rgb_color_value(default_color_hex)
            if default_rgb is None:
                default_rgb = tuple(DEFAULT_STEP_COLOR_RGB)

            schema_dict[
                vol.Required(
                    color_rgb_key,
                    default=(
                        default_color_hex
                        if rgb_field_is_text
                        else [default_rgb[0], default_rgb[1], default_rgb[2]]
                    ),
                )
            ] = color_rgb_field
            schema_dict[
                vol.Required(
                    color_hex_key,
                    default=default_color_hex,
                )
            ] = selector.TextSelector()

        return self.async_show_form(
            step_id="steps_details",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={"step": str(step_num), "on_steps": str(on_steps)},
        )

    async def async_step_gestures(self, user_input: ConfigType | None = None):
        """Options step E: choose what each supported gesture should do."""
        supported_gestures = list(self._supported_device_gestures)
        if not supported_gestures:
            return await self._async_finish_options_entry()

        errors: dict[str, str] = {}
        if user_input is not None:
            selected_gestures: list[str] = []
            recapture_gestures: list[str] = []
            selected_targets: dict[str, int] = {}

            for gesture in supported_gestures:
                raw_target = user_input.get(GESTURE_TARGET_KEYS[gesture], GESTURE_TARGET_NONE)
                if raw_target == GESTURE_TARGET_NONE:
                    self._gesture_bindings.pop(gesture, None)
                    self._selected_gesture_targets.pop(gesture, None)
                    continue

                try:
                    target_index = int(raw_target)
                except (TypeError, ValueError):
                    errors[GESTURE_TARGET_KEYS[gesture]] = "invalid_gesture_target"
                    continue

                selected_gestures.append(gesture)
                selected_targets[gesture] = max(0, min(len(self._draft_steps), target_index))
                binding = self._gesture_bindings.get(gesture)
                if binding is None or user_input.get(GESTURE_RECAPTURE_KEYS[gesture]):
                    recapture_gestures.append(gesture)

            if not errors:
                self._selected_gesture_targets = selected_targets
                self._prepare_optional_gesture_capture_queue(
                    selected_gestures, recapture_gestures
                )

                for gesture in selected_gestures:
                    if gesture in recapture_gestures:
                        continue
                    binding = dict(self._gesture_bindings.get(gesture, {}))
                    binding[CONF_GESTURE_TARGET_INDEX] = self._selected_gesture_targets[gesture]
                    self._gesture_bindings[gesture] = binding

                return await self._async_next_optional_gesture_step_or_finish()

        selector_field = _step_target_selector(self._draft_steps, include_none=True)
        schema = vol.Schema(
            {
                **{
                    vol.Required(
                        GESTURE_TARGET_KEYS[gesture],
                        default=(
                            GESTURE_TARGET_NONE
                            if gesture not in self._selected_gesture_targets
                            else str(
                                max(
                                    0,
                                    min(
                                        len(self._draft_steps),
                                        self._selected_gesture_targets[gesture],
                                    ),
                                )
                            )
                        ),
                    ): selector_field
                    for gesture in supported_gestures
                },
                **{
                    vol.Optional(
                        GESTURE_RECAPTURE_KEYS[gesture],
                        default=False,
                    ): _boolean_field()
                    for gesture in supported_gestures
                },
            }
        )
        return self.async_show_form(
            step_id="gestures",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "device": self._remote_device_name or (self._remote_ieee or "the device")
            },
        )

    async def async_step_capture_optional(self, user_input: ConfigType | None = None):
        """Options step F: capture one optional long/double gesture."""
        errors: dict[str, str] = {}
        gesture = self._current_gesture_capture

        if self._remote_ieee is None or gesture is None:
            return await self._async_next_optional_gesture_step_or_finish()

        if user_input is not None:
            if user_input.get(CONF_SKIP_CAPTURE):
                self._gesture_bindings.pop(gesture, None)
                self._selected_gesture_targets.pop(gesture, None)
                return await self._async_next_optional_gesture_step_or_finish()

            future: asyncio.Future[dict[str, Any]] = self.hass.loop.create_future()

            @callback
            def _handle_event(event: Event) -> None:
                if self._remote_ieee is None:
                    return
                data = event.data
                if data.get("device_ieee") != self._remote_ieee:
                    return
                if not future.done():
                    future.set_result(data)

            unsub = self.hass.bus.async_listen(EVENT_ZHA_EVENT, _handle_event)
            try:
                data = await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
                )
                signature = _signature_from_zha_event(self._remote_ieee, data)
                if self._signature_in_use(signature):
                    errors["base"] = "duplicate_gesture"
                else:
                    existing_target = self._selected_gesture_targets.get(gesture, 0)
                    self._gesture_bindings[gesture] = _signature_to_storage(
                        signature,
                        existing_target,
                    )
            except asyncio.TimeoutError:
                errors["base"] = "no_press"
            except ValueError:
                errors["base"] = "invalid_event"
            finally:
                unsub()

            if not errors:
                return await self._async_next_optional_gesture_step_or_finish()

        schema = vol.Schema(
            {vol.Optional(CONF_SKIP_CAPTURE, default=False): _boolean_field()}
        )
        return self.async_show_form(
            step_id="capture_optional",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "gesture": _gesture_label(gesture),
                "device": self._remote_device_name or (self._remote_ieee or "the device"),
            },
        )
