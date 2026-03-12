"""Light Cycle Controller integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

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
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_ARGS,
    CONF_CLUSTER_ID,
    CONF_COMMAND,
    CONF_ENDPOINT_ID,
    CONF_REMOTE_IEEE,
    CONF_STEP_BRIGHTNESS_PCT,
    CONF_STEPS,
    CONF_TARGET_ENTITY_ID,
    DOMAIN,
)

LOGGER = logging.getLogger(__name__)

EVENT_ZHA_EVENT = "zha_event"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Light Cycle Controller from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    controller = LightCycleController(hass, entry)
    await controller.async_start()
    hass.data[DOMAIN][entry.entry_id] = controller
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry updates (options/data) by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    controller: LightCycleController | None = hass.data.get(DOMAIN, {}).pop(
        entry.entry_id, None
    )
    if controller is not None:
        await controller.async_stop()
    if not hass.data.get(DOMAIN):
        hass.data.pop(DOMAIN, None)
    return True


class LightCycleController:
    """Runtime controller for a single config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

        data: dict[str, Any] = {**entry.data, **entry.options}

        self._target_entity_id: str = data[CONF_TARGET_ENTITY_ID]
        self._remote_ieee: str = data[CONF_REMOTE_IEEE]
        self._endpoint_id: int = int(data[CONF_ENDPOINT_ID])
        self._command: str = str(data[CONF_COMMAND])
        self._cluster_id: int | None = data.get(CONF_CLUSTER_ID)
        self._args: list[Any] | None = data.get(CONF_ARGS)

        self._steps: list[dict[str, Any]] = list(data[CONF_STEPS])

        self._unsub_zha: Callable[[], None] | None = None
        self._unsub_state: Callable[[], None] | None = None

        self._press_lock = asyncio.Lock()
        self._resolved_index: int = 0

    async def async_start(self) -> None:
        """Start listening for button presses and light state changes."""
        if self._unsub_zha is not None or self._unsub_state is not None:
            return

        self._unsub_zha = self.hass.bus.async_listen(EVENT_ZHA_EVENT, self._on_zha_event)
        self._unsub_state = async_track_state_change_event(
            self.hass, [self._target_entity_id], self._on_state_change
        )

        self._resolved_index = self._classify_state(
            self.hass.states.get(self._target_entity_id)
        )

        LOGGER.debug(
            "Started controller %s for %s (ieee=%s endpoint=%s command=%s)",
            self.entry.entry_id,
            self._target_entity_id,
            self._remote_ieee,
            self._endpoint_id,
            self._command,
        )

    async def async_stop(self) -> None:
        """Stop listening."""
        if self._unsub_zha is not None:
            self._unsub_zha()
            self._unsub_zha = None
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None

    @callback
    def _on_state_change(self, event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        self._resolved_index = self._classify_state(new_state)

    @callback
    def _on_zha_event(self, event: Event) -> None:
        if not self._matches_zha_event(event.data):
            return

        self.hass.async_create_task(self._async_handle_press())

    def _matches_zha_event(self, data: dict[str, Any]) -> bool:
        device_ieee = data.get("device_ieee")
        if device_ieee != self._remote_ieee:
            return False

        endpoint_id = data.get(CONF_ENDPOINT_ID)
        if endpoint_id is None or int(endpoint_id) != self._endpoint_id:
            return False

        command = data.get(CONF_COMMAND)
        if command is None or str(command) != self._command:
            return False

        if self._cluster_id is not None:
            cluster_id = data.get(CONF_CLUSTER_ID)
            if cluster_id is None or int(cluster_id) != int(self._cluster_id):
                return False

        if self._args is not None:
            args = data.get(CONF_ARGS, [])
            if list(args) != list(self._args):
                return False

        return True

    def _classify_state(self, state: State | None) -> int:
        """Return current cycle index derived from the light's state.

        Index 0 is Off; 1..N are the configured On steps.
        """
        if state is None:
            return 0

        if state.state in (STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN):
            return 0

        if state.state != STATE_ON:
            return 0

        brightness = state.attributes.get(ATTR_BRIGHTNESS)
        if brightness is None:
            return 1

        try:
            brightness_int = int(brightness)
        except (TypeError, ValueError):
            return 1

        if brightness_int <= 0:
            return 0

        brightness_pct = round((brightness_int / 255) * 100)
        best_step: int = 1
        best_delta: int = 999

        for step_num, step in enumerate(self._steps, start=1):
            try:
                step_pct = int(step[CONF_STEP_BRIGHTNESS_PCT])
            except (KeyError, TypeError, ValueError):
                continue

            delta = abs(step_pct - brightness_pct)
            if delta < best_delta:
                best_delta = delta
                best_step = step_num

        return best_step

    async def _async_handle_press(self) -> None:
        async with self._press_lock:
            state = self.hass.states.get(self._target_entity_id)
            current_index = self._classify_state(state)
            self._resolved_index = current_index

            next_index = (current_index + 1) % (len(self._steps) + 1)
            await self._async_apply_index(next_index)
            self._resolved_index = next_index

    async def _async_apply_index(self, index: int) -> None:
        if index == 0:
            LOGGER.debug("Turning off %s", self._target_entity_id)
            await self.hass.services.async_call(
                LIGHT_DOMAIN,
                "turn_off",
                {ATTR_ENTITY_ID: self._target_entity_id},
                blocking=True,
            )
            return

        step = self._steps[index - 1]
        brightness_pct = int(step[CONF_STEP_BRIGHTNESS_PCT])
        brightness = round((brightness_pct / 100) * 255)

        LOGGER.debug(
            "Turning on %s to %s%% (brightness=%s)",
            self._target_entity_id,
            brightness_pct,
            brightness,
        )
        await self.hass.services.async_call(
            LIGHT_DOMAIN,
            "turn_on",
            {ATTR_ENTITY_ID: self._target_entity_id, ATTR_BRIGHTNESS: brightness},
            blocking=True,
        )
