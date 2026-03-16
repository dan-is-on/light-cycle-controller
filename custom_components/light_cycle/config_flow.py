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
    CONF_ON_STEPS,
    CONF_REMOTE_DEVICE_ID,
    CONF_REMOTE_IEEE,
    CONF_STEP_BRIGHTNESS_PCT,
    CONF_STEP_LABEL,
    CONF_STEPS,
    CONF_TARGET_ENTITY_ID,
    DEFAULT_CAPTURE_TIMEOUT_SECONDS,
    DOMAIN,
    MAX_ON_STEPS,
    MIN_ON_STEPS,
)

EVENT_ZHA_EVENT = "zha_event"
CONF_RECAPTURE = "recapture"

LOGGER = logging.getLogger(__name__)


def _step_label_key(step: int) -> str:
    return f"step_{step}_{CONF_STEP_LABEL}"


def _step_brightness_key(step: int) -> str:
    return f"step_{step}_{CONF_STEP_BRIGHTNESS_PCT}"


def _default_brightness_pct(step: int, total_steps: int) -> int:
    if total_steps <= 0:
        return 100
    return max(1, min(100, round(step * 100 / total_steps)))


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

    VERSION = 1

    def __init__(self) -> None:
        self._instance_name: str | None = None
        self._target_entity_id: str | None = None

        self._remote_device_id: str | None = None
        self._remote_device_name: str | None = None
        self._remote_ieee: str | None = None

        self._capture_future: asyncio.Future[dict[str, Any]] | None = None
        self._capture_unsub: Callable[[], None] | None = None

        self._signature: _ZhaButtonSignature | None = None
        self._on_steps: int | None = None

    async def async_step_user(self, user_input: ConfigType | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            if not name:
                errors[CONF_NAME] = "name_required"
            else:
                self._instance_name = name
                self._target_entity_id = user_input[CONF_TARGET_ENTITY_ID]
                return await self.async_step_device()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): selector.TextSelector(),
                vol.Required(CONF_TARGET_ENTITY_ID): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=LIGHT_DOMAIN)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

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
                steps.append(
                    {CONF_STEP_LABEL: label, CONF_STEP_BRIGHTNESS_PCT: brightness_pct}
                )

            if not errors:
                assert self._instance_name is not None
                assert self._target_entity_id is not None
                assert self._remote_device_id is not None
                assert self._remote_ieee is not None
                assert self._signature is not None

                unique_id = (
                    f"{self._remote_ieee}:{self._signature.endpoint_id}:{self._signature.command}"
                    f":{self._target_entity_id}"
                )
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                data = {
                    CONF_TARGET_ENTITY_ID: self._target_entity_id,
                    CONF_REMOTE_DEVICE_ID: self._remote_device_id,
                    CONF_REMOTE_IEEE: self._remote_ieee,
                    CONF_ENDPOINT_ID: self._signature.endpoint_id,
                    CONF_COMMAND: self._signature.command,
                    CONF_CLUSTER_ID: self._signature.cluster_id,
                    CONF_ARGS: self._signature.args,
                    CONF_STEPS: steps,
                }
                return self.async_create_entry(title=self._instance_name, data=data)

        schema_dict: dict[Any, Any] = {}
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

        self._target_entity_id: str | None = _entry_value(
            config_entry, CONF_TARGET_ENTITY_ID, None
        )
        self._remote_device_id: str | None = _entry_value(
            config_entry, CONF_REMOTE_DEVICE_ID, None
        )

        self._remote_device_name: str | None = None
        self._remote_ieee: str | None = None

        self._signature: _ZhaButtonSignature | None = None
        self._on_steps: int | None = None

        self._existing_steps: list[dict[str, Any]] = list(
            _entry_value(config_entry, CONF_STEPS, [])
        )

    async def async_step_init(self, user_input: ConfigType | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            target_entity_id = user_input[CONF_TARGET_ENTITY_ID]
            remote_device_id = user_input[CONF_REMOTE_DEVICE_ID]
            recapture = bool(user_input.get(CONF_RECAPTURE, False))

            try:
                on_steps = int(user_input[CONF_ON_STEPS])
            except (TypeError, ValueError):
                errors["base"] = "invalid_step_count"
            else:
                if not (MIN_ON_STEPS <= on_steps <= MAX_ON_STEPS):
                    errors["base"] = "invalid_step_count"

            if not errors:
                LOGGER.info(
                    "Options init for entry %s: target=%s device=%s on_steps=%s recapture=%s",
                    self._config_entry.entry_id,
                    target_entity_id,
                    remote_device_id,
                    on_steps,
                    recapture,
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
                        self._target_entity_id = target_entity_id
                        self._remote_device_id = remote_device_id
                        self._remote_device_name = (
                            device_entry.name_by_user or device_entry.name
                        )
                        self._remote_ieee = ieee
                        self._on_steps = on_steps

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
            vol.Required(CONF_TARGET_ENTITY_ID, default=self._target_entity_id)
            if self._target_entity_id
            else vol.Required(CONF_TARGET_ENTITY_ID)
        )
        device_key = (
            vol.Required(CONF_REMOTE_DEVICE_ID, default=self._remote_device_id)
            if self._remote_device_id
            else vol.Required(CONF_REMOTE_DEVICE_ID)
        )

        schema = vol.Schema(
            {
                target_key: selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=LIGHT_DOMAIN)
                ),
                device_key: selector.DeviceSelector(
                    selector.DeviceSelectorConfig(integration="zha")
                ),
                vol.Optional(CONF_RECAPTURE, default=False): _boolean_field(),
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

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

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
                steps.append(
                    {CONF_STEP_LABEL: label, CONF_STEP_BRIGHTNESS_PCT: brightness_pct}
                )

            if not errors:
                assert self._remote_ieee is not None
                assert self._signature is not None

                options = {
                    CONF_TARGET_ENTITY_ID: self._target_entity_id,
                    CONF_REMOTE_DEVICE_ID: self._remote_device_id,
                    CONF_REMOTE_IEEE: self._remote_ieee,
                    CONF_ENDPOINT_ID: self._signature.endpoint_id,
                    CONF_COMMAND: self._signature.command,
                    CONF_CLUSTER_ID: self._signature.cluster_id,
                    CONF_ARGS: self._signature.args,
                    CONF_STEPS: steps,
                }
                LOGGER.info(
                    "Saving options for entry %s: steps=%s brightness=%s",
                    self._config_entry.entry_id,
                    len(steps),
                    [s.get(CONF_STEP_BRIGHTNESS_PCT) for s in steps],
                )
                return self.async_create_entry(title="", data=options)

        schema_dict: dict[Any, Any] = {}
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

        return self.async_show_form(
            step_id="steps",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )
