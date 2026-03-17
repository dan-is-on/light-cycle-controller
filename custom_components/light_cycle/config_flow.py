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
from homeassistant.core import Event, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ARGS,
    CONF_CLUSTER_ID,
    CONF_COMMAND,
    CONF_ENDPOINT_ID,
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
    MAX_ON_STEPS,
    MAX_MAX_PARALLEL_CALLS,
    MIN_MAX_PARALLEL_CALLS,
    MIN_ON_STEPS,
    STEP_MODE_COLOR,
    STEP_MODE_WHITE_TEMP,
)
from .settings import async_get_settings, async_set_max_parallel_calls

EVENT_ZHA_EVENT = "zha_event"
CONF_RECAPTURE = "recapture"

LOGGER = logging.getLogger(__name__)


def _step_label_key(step: int) -> str:
    return f"step_{step}_{CONF_STEP_LABEL}"


def _step_brightness_key(step: int) -> str:
    return f"step_{step}_{CONF_STEP_BRIGHTNESS_PCT}"


def _step_mode_key(step: int) -> str:
    return f"step_{step}_{CONF_STEP_MODE}"


def _step_temp_pct_key(step: int) -> str:
    return f"step_{step}_{CONF_STEP_TEMP_PCT}"


def _step_color_hex_key(step: int) -> str:
    return f"step_{step}_{CONF_STEP_COLOR_HEX}"


def _step_color_rgb_key(step: int) -> str:
    return f"step_{step}_{CONF_STEP_COLOR_RGB}"


def _default_brightness_pct(step: int, total_steps: int) -> int:
    if total_steps <= 0:
        return 100
    return max(1, min(100, round(step * 100 / total_steps)))


def _normalize_step_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode == STEP_MODE_COLOR:
        return STEP_MODE_COLOR
    return STEP_MODE_WHITE_TEMP


def _normalize_hex_color(value: Any) -> str | None:
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
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    normalized = _normalize_hex_color(value)
    if normalized is None:
        raise ValueError("Invalid hex color")
    raw = normalized[1:]
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def _coerce_rgb_channel(value: Any) -> int | None:
    try:
        channel = int(value)
    except (TypeError, ValueError):
        return None
    if not (0 <= channel <= 255):
        return None
    return channel


def _parse_rgb_color_value(value: Any) -> tuple[int, int, int] | None:
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
    ieee: str
    endpoint_id: int
    command: str
    cluster_id: int | None = None
    args: list[Any] | None = None


def _signature_from_zha_event(ieee: str, data: dict[str, Any]) -> _ZhaButtonSignature:
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
    for domain, identifier in device_entry.identifiers:
        if domain == "zha":
            return str(identifier)
    return None


def _entry_value(entry: ConfigEntry, key: str, default: Any | None = None) -> Any:
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


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
    targets = _entry_value(entry, CONF_TARGET_ENTITY_IDS, None)
    normalized = _normalize_target_entity_ids(targets)
    if normalized:
        return normalized

    legacy_target = _entry_value(entry, CONF_TARGET_ENTITY_ID, None)
    return _normalize_target_entity_ids(legacy_target)


def _light_entity_selector(*, multiple: bool) -> selector.EntitySelector:
    try:
        config = selector.EntitySelectorConfig(domain=LIGHT_DOMAIN, multiple=multiple)
    except TypeError:
        config = selector.EntitySelectorConfig(domain=LIGHT_DOMAIN)
    return selector.EntitySelector(config)


def _max_parallel_calls_selector() -> selector.NumberSelector:
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
            STEP_MODE_COLOR: "Color",
        }
    )


def _temp_pct_selector() -> selector.NumberSelector:
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


class LightCycleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Light Cycle Controller."""

    VERSION = 2

    def __init__(self) -> None:
        self._instance_name: str | None = None
        self._target_entity_ids: list[str] = []

        self._remote_device_id: str | None = None
        self._remote_device_name: str | None = None
        self._remote_ieee: str | None = None

        self._capture_future: asyncio.Future[dict[str, Any]] | None = None
        self._capture_unsub: Callable[[], None] | None = None

        self._signature: _ZhaButtonSignature | None = None
        self._on_steps: int | None = None
        self._pending_max_parallel_calls: int | None = None

    async def async_step_user(self, user_input: ConfigType | None = None):
        errors: dict[str, str] = {}
        global_settings = await async_get_settings(self.hass)
        default_max_parallel_calls = int(
            global_settings.get(CONF_MAX_PARALLEL_CALLS, DEFAULT_MAX_PARALLEL_CALLS)
        )
        is_first_entry = len(self.hass.config_entries.async_entries(DOMAIN)) == 0

        if user_input is not None:
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
                    self._remote_device_id = device_id
                    self._remote_device_name = device_entry.name_by_user or device_entry.name
                    self._remote_ieee = ieee
                    self._async_reset_capture()
                    return await self.async_step_capture()

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): selector.DeviceSelector(
                    selector.DeviceSelectorConfig(integration="zha")
                )
            }
        )
        return self.async_show_form(step_id="device", data_schema=schema, errors=errors)

    @callback
    def _async_reset_capture(self) -> None:
        if self._capture_unsub is not None:
            self._capture_unsub()
            self._capture_unsub = None
        self._capture_future = None

    @callback
    def _async_ensure_capture_listener(self) -> None:
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
        errors: dict[str, str] = {}

        if self._remote_ieee is None:
            errors["base"] = "not_zha_device"

        if user_input is not None and not errors:
            try:
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
        errors: dict[str, str] = {}

        assert self._on_steps is not None
        on_steps = self._on_steps

        if user_input is not None:
            steps: list[dict[str, Any]] = []
            for step_num in range(1, on_steps + 1):
                label_key = _step_label_key(step_num)
                brightness_key = _step_brightness_key(step_num)
                mode_key = _step_mode_key(step_num)
                temp_key = _step_temp_pct_key(step_num)
                color_hex_key = _step_color_hex_key(step_num)
                color_rgb_key = _step_color_rgb_key(step_num)

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
                step_data: dict[str, Any] = {
                    CONF_STEP_LABEL: label,
                    CONF_STEP_BRIGHTNESS_PCT: brightness_pct,
                    CONF_STEP_MODE: mode,
                }

                if mode == STEP_MODE_WHITE_TEMP:
                    try:
                        temp_pct = int(user_input[temp_key])
                    except (TypeError, ValueError, KeyError):
                        errors[temp_key] = "invalid_temp_pct"
                        continue

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
                        continue

                    step_data[CONF_STEP_TEMP_PCT] = DEFAULT_STEP_TEMP_PCT
                    step_data[CONF_STEP_COLOR_HEX] = color_hex
                    step_data[CONF_STEP_COLOR_RGB] = [rgb[0], rgb[1], rgb[2]]

                steps.append(step_data)

            if not errors:
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
                    CONF_STEPS: steps,
                }
                if self._pending_max_parallel_calls is not None:
                    await async_set_max_parallel_calls(
                        self.hass, self._pending_max_parallel_calls
                    )
                return self.async_create_entry(title=self._instance_name, data=data)

        schema_dict: dict[Any, Any] = {}
        color_rgb_field, rgb_field_is_text = _color_rgb_selector_field()
        for step_num in range(1, on_steps + 1):
            schema_dict[
                vol.Required(
                    _step_label_key(step_num), default=f"Step {step_num}"
                )
            ] = selector.TextSelector()
            schema_dict[
                vol.Required(
                    _step_brightness_key(step_num),
                    default=_default_brightness_pct(step_num, on_steps),
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
                    default=STEP_MODE_WHITE_TEMP,
                )
            ] = _step_mode_field()
            schema_dict[
                vol.Required(
                    _step_temp_pct_key(step_num),
                    default=DEFAULT_STEP_TEMP_PCT,
                )
            ] = _temp_pct_selector()
            schema_dict[
                vol.Required(
                    _step_color_rgb_key(step_num),
                    default=(
                        DEFAULT_STEP_COLOR_HEX
                        if rgb_field_is_text
                        else list(DEFAULT_STEP_COLOR_RGB)
                    ),
                )
            ] = color_rgb_field
            schema_dict[
                vol.Required(
                    _step_color_hex_key(step_num),
                    default=DEFAULT_STEP_COLOR_HEX,
                )
            ] = selector.TextSelector()

        return self.async_show_form(
            step_id="steps",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return LightCycleOptionsFlowHandler(config_entry)


class LightCycleOptionsFlowHandler(OptionsFlow):
    """Handle options for an existing Light Cycle Controller entry."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        try:
            super().__init__(config_entry)
        except TypeError:
            super().__init__()
        self._config_entry = config_entry

        self._target_entity_ids: list[str] = _entry_target_entity_ids(config_entry)
        self._remote_device_id: str | None = _entry_value(
            config_entry, CONF_REMOTE_DEVICE_ID, None
        )

        self._remote_device_name: str | None = None
        self._remote_ieee: str | None = None

        self._signature: _ZhaButtonSignature | None = None
        self._on_steps: int | None = None
        self._pending_max_parallel_calls: int | None = None

        self._existing_steps: list[dict[str, Any]] = list(
            _entry_value(config_entry, CONF_STEPS, [])
        )

    async def async_step_init(self, user_input: ConfigType | None = None):
        errors: dict[str, str] = {}
        global_settings = await async_get_settings(self.hass)
        default_max_parallel_calls = int(
            global_settings.get(CONF_MAX_PARALLEL_CALLS, DEFAULT_MAX_PARALLEL_CALLS)
        )

        if user_input is not None:
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
                if remote_device_id != self._remote_device_id:
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
                        self._target_entity_ids = target_entity_ids
                        self._remote_device_id = remote_device_id
                        self._remote_device_name = (
                            device_entry.name_by_user or device_entry.name
                        )
                        self._remote_ieee = ieee
                        self._on_steps = on_steps
                        self._pending_max_parallel_calls = max_parallel_calls

                        if recapture:
                            self._signature = None
                            return await self.async_step_capture()

                        endpoint_id = _entry_value(self._config_entry, CONF_ENDPOINT_ID)
                        command = _entry_value(self._config_entry, CONF_COMMAND)
                        if endpoint_id is None or command is None:
                            return await self.async_step_capture()

                        self._signature = _ZhaButtonSignature(
                            ieee=ieee,
                            endpoint_id=int(endpoint_id),
                            command=str(command),
                            cluster_id=_entry_value(self._config_entry, CONF_CLUSTER_ID),
                            args=_entry_value(self._config_entry, CONF_ARGS),
                        )
                        return await self.async_step_steps()

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

        schema = vol.Schema(
            {
                target_key: _light_entity_selector(multiple=True),
                device_key: selector.DeviceSelector(
                    selector.DeviceSelectorConfig(integration="zha")
                ),
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

    async def async_step_capture(self, user_input: ConfigType | None = None):
        errors: dict[str, str] = {}

        if self._remote_ieee is None:
            errors["base"] = "not_zha_device"

        if user_input is not None and not errors:
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
        errors: dict[str, str] = {}

        on_steps = self._on_steps or len(self._existing_steps) or 3

        if user_input is not None:
            steps: list[dict[str, Any]] = []
            for step_num in range(1, on_steps + 1):
                label_key = _step_label_key(step_num)
                brightness_key = _step_brightness_key(step_num)
                mode_key = _step_mode_key(step_num)
                temp_key = _step_temp_pct_key(step_num)
                color_hex_key = _step_color_hex_key(step_num)
                color_rgb_key = _step_color_rgb_key(step_num)

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
                step_data: dict[str, Any] = {
                    CONF_STEP_LABEL: label,
                    CONF_STEP_BRIGHTNESS_PCT: brightness_pct,
                    CONF_STEP_MODE: mode,
                }

                if mode == STEP_MODE_WHITE_TEMP:
                    try:
                        temp_pct = int(user_input[temp_key])
                    except (TypeError, ValueError, KeyError):
                        errors[temp_key] = "invalid_temp_pct"
                        continue

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
                        continue

                    step_data[CONF_STEP_TEMP_PCT] = DEFAULT_STEP_TEMP_PCT
                    step_data[CONF_STEP_COLOR_HEX] = color_hex
                    step_data[CONF_STEP_COLOR_RGB] = [rgb[0], rgb[1], rgb[2]]

                steps.append(step_data)

            if not errors:
                assert self._remote_ieee is not None
                assert self._signature is not None

                options = {
                    CONF_TARGET_ENTITY_IDS: self._target_entity_ids,
                    CONF_TARGET_ENTITY_ID: self._target_entity_ids[0],
                    CONF_REMOTE_DEVICE_ID: self._remote_device_id,
                    CONF_REMOTE_IEEE: self._remote_ieee,
                    CONF_ENDPOINT_ID: self._signature.endpoint_id,
                    CONF_COMMAND: self._signature.command,
                    CONF_CLUSTER_ID: self._signature.cluster_id,
                    CONF_ARGS: self._signature.args,
                    CONF_STEPS: steps,
                }
                if self._pending_max_parallel_calls is not None:
                    await async_set_max_parallel_calls(
                        self.hass, self._pending_max_parallel_calls
                    )
                LOGGER.info(
                    "Saving options for entry %s: steps=%s brightness=%s",
                    self._config_entry.entry_id,
                    len(steps),
                    [s.get(CONF_STEP_BRIGHTNESS_PCT) for s in steps],
                )
                return self.async_create_entry(title="", data=options)

        schema_dict: dict[Any, Any] = {}
        color_rgb_field, rgb_field_is_text = _color_rgb_selector_field()
        for step_num in range(1, on_steps + 1):
            existing = (
                self._existing_steps[step_num - 1]
                if step_num - 1 < len(self._existing_steps)
                else None
            )
            default_label = (
                str(existing.get(CONF_STEP_LABEL))
                if isinstance(existing, dict) and existing.get(CONF_STEP_LABEL)
                else f"Step {step_num}"
            )
            default_brightness = (
                int(existing.get(CONF_STEP_BRIGHTNESS_PCT))
                if isinstance(existing, dict) and existing.get(CONF_STEP_BRIGHTNESS_PCT)
                else _default_brightness_pct(step_num, on_steps)
            )
            default_mode = (
                _normalize_step_mode(existing.get(CONF_STEP_MODE))
                if isinstance(existing, dict)
                else DEFAULT_STEP_MODE
            )
            default_temp_pct = (
                int(existing.get(CONF_STEP_TEMP_PCT))
                if isinstance(existing, dict)
                and existing.get(CONF_STEP_TEMP_PCT) is not None
                else DEFAULT_STEP_TEMP_PCT
            )
            default_color_hex = (
                _normalize_hex_color(existing.get(CONF_STEP_COLOR_HEX))
                if isinstance(existing, dict)
                else None
            )
            if default_color_hex is None:
                default_color_hex = DEFAULT_STEP_COLOR_HEX
            default_rgb = (
                _parse_rgb_color_value(existing.get(CONF_STEP_COLOR_RGB))
                if isinstance(existing, dict)
                else None
            )
            if default_rgb is None:
                default_rgb = _parse_rgb_color_value(default_color_hex)
            if default_rgb is None:
                default_rgb = tuple(DEFAULT_STEP_COLOR_RGB)

            schema_dict[vol.Required(_step_label_key(step_num), default=default_label)] = (
                selector.TextSelector()
            )
            schema_dict[
                vol.Required(_step_brightness_key(step_num), default=default_brightness)
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
                    default=default_mode,
                )
            ] = _step_mode_field()
            schema_dict[
                vol.Required(
                    _step_temp_pct_key(step_num),
                    default=max(0, min(100, default_temp_pct)),
                )
            ] = _temp_pct_selector()
            schema_dict[
                vol.Required(
                    _step_color_rgb_key(step_num),
                    default=(
                        default_color_hex
                        if rgb_field_is_text
                        else [default_rgb[0], default_rgb[1], default_rgb[2]]
                    ),
                )
            ] = color_rgb_field
            schema_dict[
                vol.Required(
                    _step_color_hex_key(step_num),
                    default=default_color_hex,
                )
            ] = selector.TextSelector()

        return self.async_show_form(
            step_id="steps",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )
