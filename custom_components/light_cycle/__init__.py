"""Light Cycle Controller integration."""

from __future__ import annotations

import asyncio
import logging
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

DATA_CONTROLLERS = "controllers"
DATA_SERVICES_REGISTERED = "services_registered"

SERVICE_DUMP = "dump"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Light Cycle Controller from a config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    controllers: dict[str, LightCycleController] = domain_data.setdefault(DATA_CONTROLLERS, {})

    if not domain_data.get(DATA_SERVICES_REGISTERED):
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

    controller = LightCycleController(hass, entry)
    await controller.async_start()
    controllers[entry.entry_id] = controller
    return True


async def _async_handle_dump_service(hass: HomeAssistant, call) -> None:
    """Dump controller/entry state to logs (for debugging)."""
    domain_data = hass.data.get(DOMAIN, {})
    controllers: dict[str, LightCycleController] = domain_data.get(DATA_CONTROLLERS, {})

    requested_entry_id = call.data.get("entry_id")
    entry_ids = [requested_entry_id] if requested_entry_id else list(controllers.keys())

    if not entry_ids:
        # Use WARNING so it shows up in Settings → System → Logs (which hides INFO).
        LOGGER.warning("Dump: no loaded controllers")
        return

    for entry_id in entry_ids:
        controller = controllers.get(entry_id)
        entry = hass.config_entries.async_get_entry(entry_id)

        if controller is None:
            LOGGER.warning("Dump: entry=%s not loaded", entry_id)
            continue

        merged = controller._merged_entry_data()
        steps = merged.get(CONF_STEPS, [])
        steps_list = steps if isinstance(steps, list) else []
        target_entity_id = merged.get(CONF_TARGET_ENTITY_ID)

        target_state = hass.states.get(target_entity_id) if target_entity_id else None
        target_state_str = None if target_state is None else target_state.state
        target_brightness = (
            None
            if target_state is None
            else target_state.attributes.get(ATTR_BRIGHTNESS)
        )
        try:
            classified = controller._classify_state(target_state)
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

        expanded_targets = (
            controller._expanded_target_entity_ids() if target_entity_id else []
        )
        member_summary: dict[str, Any] | None = None
        if expanded_targets:
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

                votes[controller._nearest_step_for_pct(pct)] += 1

            member_summary = {
                "total": len(expanded_targets),
                "counts": dict(counts),
                "step_votes": dict(votes),
                "sample": sample,
            }

        LOGGER.warning(
            "Dump: entry=%s title=%s target=%s state=%s brightness=%s controller_steps=%s entry_steps=%s resolved=%s classified=%s next(resolved)=%s next(classified)=%s targets=%s",
            controller.entry.entry_id,
            (entry.title if entry is not None else controller.entry.title),
            target_entity_id,
            target_state_str,
            target_brightness,
            len(controller._steps),
            len(steps_list),
            controller._resolved_index,
            classified,
            next_from_resolved,
            next_from_classified,
            len(expanded_targets),
        )
        if member_summary is not None:
            LOGGER.warning(
                "Dump: entry=%s members=%s",
                controller.entry.entry_id,
                member_summary,
            )
        LOGGER.warning(
            "Dump: entry=%s ieee=%s endpoint=%s command=%s cluster_id=%s args=%s",
            controller.entry.entry_id,
            merged.get(CONF_REMOTE_IEEE),
            merged.get(CONF_ENDPOINT_ID),
            merged.get(CONF_COMMAND),
            merged.get(CONF_CLUSTER_ID),
            merged.get(CONF_ARGS),
        )
        LOGGER.warning(
            "Dump: entry=%s steps=%s",
            controller.entry.entry_id,
            [
                {
                    "label": step.get("label"),
                    "brightness_pct": step.get(CONF_STEP_BRIGHTNESS_PCT),
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
        await controller.async_stop()

    if not controllers:
        if domain_data.get(DATA_SERVICES_REGISTERED):
            hass.services.async_remove(DOMAIN, SERVICE_DUMP)
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

        self._refresh_steps_from_entry()

        self._unsub_zha = self.hass.bus.async_listen(EVENT_ZHA_EVENT, self._on_zha_event)
        self._unsub_state = async_track_state_change_event(
            self.hass, [self._target_entity_id], self._on_state_change
        )

        self._resolved_index = self._classify_state(
            self.hass.states.get(self._target_entity_id)
        )

        LOGGER.info(
            "Started controller %s for %s (steps=%s ieee=%s endpoint=%s command=%s)",
            self.entry.entry_id,
            self._target_entity_id,
            len(self._steps),
            self._remote_ieee,
            self._endpoint_id,
            self._command,
        )
        LOGGER.info(
            "Controller %s steps for %s: %s",
            self.entry.entry_id,
            self._target_entity_id,
            [s.get(CONF_STEP_BRIGHTNESS_PCT) for s in self._steps],
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
        self._refresh_steps_from_entry()
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

        # For light groups (and some integrations that expose a member `entity_id` list),
        # determine the current step based on member states rather than the aggregated
        # group brightness. This avoids common cases where group brightness is `None` or
        # reflects an outlier member.
        members = state.attributes.get(ATTR_ENTITY_ID)
        if isinstance(members, list) and members:
            expanded = self._expanded_entity_ids(state.entity_id)
            return self._classify_expanded_members(expanded)

        brightness = state.attributes.get(ATTR_BRIGHTNESS)
        if brightness is None:
            if self._resolved_index > 0:
                return min(self._resolved_index, max(1, len(self._steps)))
            return 1

        try:
            brightness_int = int(brightness)
        except (TypeError, ValueError):
            if self._resolved_index > 0:
                return min(self._resolved_index, max(1, len(self._steps)))
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

    def _classify_expanded_members(self, entity_ids: list[str]) -> int:
        any_on = False
        on_step_votes: list[int] = []

        for entity_id in entity_ids:
            if not isinstance(entity_id, str) or not entity_id.startswith("light."):
                continue

            member_state = self.hass.states.get(entity_id)
            if member_state is None:
                continue

            if member_state.state in (STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue

            if member_state.state != STATE_ON:
                continue

            any_on = True
            brightness_pct = self._brightness_pct_from_state(member_state)
            if brightness_pct is None:
                continue

            on_step_votes.append(self._nearest_step_for_pct(brightness_pct))

        if not any_on:
            # The parent entity is ON (we only call this when the configured/parent
            # entity is ON). If we can't find any ON member states (e.g. states not
            # loaded yet), fall back to the last resolved index.
            if self._resolved_index > 0:
                return min(self._resolved_index, max(1, len(self._steps)))
            return 1

        if not on_step_votes:
            if self._resolved_index > 0:
                return min(self._resolved_index, max(1, len(self._steps)))
            return 1

        counts = Counter(on_step_votes)
        top = counts.most_common()
        if not top:
            return 1

        best_count = top[0][1]
        tied = [idx for idx, count in top if count == best_count]
        if len(tied) == 1:
            return tied[0]

        if self._resolved_index in tied:
            return int(self._resolved_index)

        if self._resolved_index > 0:
            resolved = int(self._resolved_index)
            tied.sort(key=lambda idx: abs(idx - resolved))
            return tied[0]

        return min(tied)

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
            new_steps = list(steps)
            if new_steps != self._steps:
                LOGGER.info(
                    "Refreshed steps for entry %s: %s -> %s",
                    self.entry.entry_id,
                    len(self._steps),
                    len(new_steps),
                )
                LOGGER.info(
                    "New steps for %s: %s",
                    self._target_entity_id,
                    [s.get(CONF_STEP_BRIGHTNESS_PCT) for s in new_steps],
                )
            self._steps = new_steps
            if self._resolved_index > len(self._steps):
                self._resolved_index = len(self._steps)

    async def _async_handle_press(self) -> None:
        async with self._press_lock:
            self._refresh_steps_from_entry()
            state = self.hass.states.get(self._target_entity_id)
            current_index = self._classify_state(state)
            self._resolved_index = current_index

            next_index = (current_index + 1) % (len(self._steps) + 1)
            LOGGER.debug(
                "Press: entry=%s title=%s target=%s current=%s next=%s steps=%s",
                self.entry.entry_id,
                self.entry.title,
                self._target_entity_id,
                current_index,
                next_index,
                len(self._steps),
            )
            try:
                await self._async_apply_index(next_index)
            except Exception:
                LOGGER.exception(
                    "Failed applying cycle step (entry=%s title=%s target=%s next=%s steps=%s)",
                    self.entry.entry_id,
                    self.entry.title,
                    self._target_entity_id,
                    next_index,
                    len(self._steps),
                )
                return
            else:
                self._resolved_index = next_index

    async def _async_apply_index(self, index: int) -> None:
        target_entity_ids = self._expanded_target_entity_ids()

        if index == 0:
            LOGGER.debug("Turning off %s", self._target_entity_id)
            await self._async_call_light_service_best_effort(
                "turn_off", target_entity_ids, {}
            )
            return

        step = self._steps[index - 1]
        brightness_pct = int(step[CONF_STEP_BRIGHTNESS_PCT])
        brightness = round((brightness_pct / 100) * 255)
        label = step.get("label")

        LOGGER.debug(
            "Turning on %s to %s%% (brightness=%s label=%s)",
            self._target_entity_id,
            brightness_pct,
            brightness,
            label,
        )
        await self._async_call_light_service_best_effort(
            "turn_on", target_entity_ids, {ATTR_BRIGHTNESS: brightness}
        )

    def _expanded_target_entity_ids(self) -> list[str]:
        """Return leaf `light.*` entity ids to call.

        If the configured target is a light group, recursively expand nested groups and
        de-duplicate entities. This avoids calling other group entities (which can hide
        partial failures and skew brightness classification).
        """
        return self._expanded_entity_ids(self._target_entity_id)

    def _expanded_entity_ids(self, root_entity_id: str) -> list[str]:
        visited: set[str] = set()
        leaves: list[str] = []
        stack: list[str] = [root_entity_id]

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

        return unique or [root_entity_id]

    async def _async_call_light_service_best_effort(
        self,
        service: str,
        entity_ids: list[str],
        service_data: dict[str, Any],
    ) -> None:
        failures: list[tuple[str, Exception]] = []
        for entity_id in entity_ids:
            exc = await self._async_call_light_service_single(
                service, entity_id, service_data
            )
            if exc is None:
                continue
            failures.append((entity_id, exc))

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
        try:
            await self.hass.services.async_call(
                LIGHT_DOMAIN,
                service,
                {ATTR_ENTITY_ID: entity_id, **service_data},
                blocking=True,
            )
        except Exception as exc:
            return exc
        return None
