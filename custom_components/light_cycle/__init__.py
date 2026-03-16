"""Light Cycle Controller integration."""

from __future__ import annotations

import asyncio
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
    CONF_ENDPOINT_ID,
    CONF_REMOTE_IEEE,
    CONF_STEP_BRIGHTNESS_PCT,
    CONF_STEPS,
    CONF_TARGET_ENTITY_ID,
    CONF_TARGET_ENTITY_IDS,
    DOMAIN,
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Light Cycle Controller from a config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    controllers: dict[str, LightCycleController] = domain_data.setdefault(DATA_CONTROLLERS, {})
    await async_get_settings(hass)

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
                "average_pct": round(getattr(controller, "_last_average_pct", 0.0), 2),
            }

        LOGGER.warning(
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

        self._target_entity_ids: list[str] = _coerce_target_entity_ids(data)
        self._remote_ieee: str = data[CONF_REMOTE_IEEE]
        self._endpoint_id: int = int(data[CONF_ENDPOINT_ID])
        self._command: str = str(data[CONF_COMMAND])
        self._cluster_id: int | None = data.get(CONF_CLUSTER_ID)
        self._args: list[Any] | None = data.get(CONF_ARGS)

        self._steps: list[dict[str, Any]] = list(data[CONF_STEPS])
        self._expanded_targets_cache: list[str] = []
        self._targets_cache_dirty: bool = True
        self._watched_state_entity_ids: list[str] = []
        self._is_tuya_cache: dict[str, bool] = {}

        self._unsub_zha: Callable[[], None] | None = None
        self._unsub_state: Callable[[], None] | None = None

        self._press_lock = asyncio.Lock()
        self._resolved_index: int = 0
        self._ignore_state_changes_until: float = 0.0
        self._last_average_pct: float = 0.0
        self._last_sample_counts: dict[str, int] = {}

    async def async_start(self) -> None:
        """Start listening for button presses and light state changes."""
        if self._unsub_zha is not None or self._unsub_state is not None:
            return

        self._refresh_targets_from_entry()
        self._refresh_steps_from_entry()
        self._refresh_expanded_targets(force=True)
        self._resubscribe_state_listener()

        self._unsub_zha = self.hass.bus.async_listen(EVENT_ZHA_EVENT, self._on_zha_event)
        self._resolved_index = self._classify_state()

        LOGGER.info(
            "Started controller %s for %s (expanded=%s steps=%s ieee=%s endpoint=%s command=%s)",
            self.entry.entry_id,
            self._target_entity_ids,
            len(self._expanded_targets_cache),
            len(self._steps),
            self._remote_ieee,
            self._endpoint_id,
            self._command,
        )
        LOGGER.info(
            "Controller %s steps for %s: %s",
            self.entry.entry_id,
            self._target_entity_ids,
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
        if time.monotonic() < self._ignore_state_changes_until:
            return

        old_state: State | None = event.data.get("old_state")
        new_state: State | None = event.data.get("new_state")

        if self._group_membership_changed(old_state, new_state):
            self._targets_cache_dirty = True

        self._refresh_targets_from_entry()
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

    def _classify_state(self, _state: State | None = None) -> int:
        """Classify cycle index from averaged brightness across the collection."""
        expanded = self._cached_expanded_target_entity_ids()
        return self._classify_expanded_members(expanded)

    def _classify_expanded_members(self, entity_ids: list[str]) -> int:
        average_pct, counts = self._average_collection_brightness_pct(entity_ids)
        self._last_average_pct = average_pct
        self._last_sample_counts = counts
        return self._classify_average_pct(average_pct)

    def _average_collection_brightness_pct(self, entity_ids: list[str]) -> tuple[float, dict[str, int]]:
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

    def _step_pct(self, index: int) -> int:
        if index <= 0:
            return 0
        try:
            return int(self._steps[index - 1][CONF_STEP_BRIGHTNESS_PCT])
        except (IndexError, KeyError, TypeError, ValueError):
            return 100

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
                    self._target_entity_ids,
                    [s.get(CONF_STEP_BRIGHTNESS_PCT) for s in new_steps],
                )
            self._steps = new_steps
            if self._resolved_index > len(self._steps):
                self._resolved_index = len(self._steps)

    def _refresh_targets_from_entry(self) -> None:
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
        if old_state is None and new_state is None:
            return False
        old_members = None if old_state is None else old_state.attributes.get(ATTR_ENTITY_ID)
        new_members = None if new_state is None else new_state.attributes.get(ATTR_ENTITY_ID)
        if isinstance(old_members, list) or isinstance(new_members, list):
            return list(old_members or []) != list(new_members or [])
        return False

    def _state_subscription_entities(self) -> list[str]:
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
        async with self._press_lock:
            self._refresh_targets_from_entry()
            self._refresh_steps_from_entry()

            expanded_before = self._cached_expanded_target_entity_ids()
            current_index = self._classify_expanded_members(expanded_before)
            self._resolved_index = current_index

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
                self._resolved_index = next_index
                await self._async_reconcile_expanded_targets(next_index, expanded_before)
                self._ignore_state_changes_until = max(
                    self._ignore_state_changes_until,
                    time.monotonic() + 0.5,
                )

    async def _async_apply_index(self, index: int, expanded_before: list[str]) -> None:
        if index == 0:
            LOGGER.debug("Turning off %s", self._target_entity_ids)
            await self._async_call_light_service(
                "turn_off", {}, expanded_before
            )
            return

        step = self._steps[index - 1]
        brightness_pct = int(step[CONF_STEP_BRIGHTNESS_PCT])
        brightness = round((brightness_pct / 100) * 255)
        label = step.get("label")

        LOGGER.debug(
            "Turning on %s to %s%% (brightness=%s label=%s)",
            self._target_entity_ids,
            brightness_pct,
            brightness,
            label,
        )
        await self._async_call_light_service(
            "turn_on",
            {
                ATTR_BRIGHTNESS: brightness,
                "brightness_pct": brightness_pct,
            },
            expanded_before,
        )

    async def _async_reconcile_expanded_targets(
        self, applied_index: int, previous_targets: list[str]
    ) -> None:
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

        step = self._steps[applied_index - 1]
        brightness_pct = int(step[CONF_STEP_BRIGHTNESS_PCT])
        brightness = round((brightness_pct / 100) * 255)
        await self._async_call_light_service_best_effort(
            "turn_on",
            added_targets,
            {ATTR_BRIGHTNESS: brightness, "brightness_pct": brightness_pct},
        )

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
        if self._expanded_targets_cache:
            return list(self._expanded_targets_cache)

        expanded_targets, _ = self._refresh_expanded_targets(force=True)
        self._resubscribe_state_listener()
        return expanded_targets

    def _refresh_expanded_targets(self, force: bool) -> tuple[list[str], bool]:
        if not force and self._expanded_targets_cache and not self._targets_cache_dirty:
            return list(self._expanded_targets_cache), False

        expanded = self._expanded_entity_ids(self._target_entity_ids)
        changed = expanded != self._expanded_targets_cache
        self._expanded_targets_cache = expanded
        self._targets_cache_dirty = False
        return list(self._expanded_targets_cache), changed

    def _expanded_entity_ids(self, root_entity_ids: list[str]) -> list[str]:
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

    async def _async_call_light_service_many(
        self, service: str, entity_ids: list[str], service_data: dict[str, Any]
    ) -> list[tuple[str, Exception]]:
        if not entity_ids:
            return []

        ordered_entity_ids = self._ordered_entity_ids_for_dispatch(entity_ids)
        max_parallel_calls = self._max_parallel_calls()
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
            async with semaphore:
                exc = await self._async_call_light_service_single(
                    service, entity_id, service_data
                )
            return entity_id, exc

        results = await asyncio.gather(*[_call(entity_id) for entity_id in ordered_entity_ids])
        return [(entity_id, exc) for entity_id, exc in results if exc is not None]

    def _max_parallel_calls(self) -> int:
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
