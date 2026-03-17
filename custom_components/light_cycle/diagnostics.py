"""Diagnostics support for Light Cycle Controller."""

from __future__ import annotations

from collections import Counter
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from .const import CONF_REMOTE_IEEE, CONF_TARGET_ENTITY_ID, CONF_TARGET_ENTITY_IDS, DOMAIN
from .settings import get_max_parallel_calls

_TO_REDACT = {CONF_REMOTE_IEEE}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    # Resolve target entity IDs from modern multi-select field with legacy fallback.
    raw_targets = entry.options.get(
        CONF_TARGET_ENTITY_IDS, entry.data.get(CONF_TARGET_ENTITY_IDS)
    )
    target_entity_ids = []
    if isinstance(raw_targets, str):
        target_entity_ids = [raw_targets]
    elif isinstance(raw_targets, list):
        target_entity_ids = [value for value in raw_targets if isinstance(value, str)]
    elif isinstance(raw_targets, tuple):
        target_entity_ids = [value for value in raw_targets if isinstance(value, str)]

    if not target_entity_ids:
        legacy_target = entry.options.get(
            CONF_TARGET_ENTITY_ID, entry.data.get(CONF_TARGET_ENTITY_ID)
        )
        if isinstance(legacy_target, str):
            target_entity_ids = [legacy_target]

    # Snapshot primary target state for quick top-level diagnostics context.
    target_entity_id = target_entity_ids[0] if target_entity_ids else None
    target_state = hass.states.get(target_entity_id) if target_entity_id else None

    # Pull live controller runtime object if currently loaded.
    controllers: dict[str, Any] = hass.data.get(DOMAIN, {}).get("controllers", {})
    controller = controllers.get(entry.entry_id)

    classified_index = None
    next_index = None
    expanded_targets: list[str] | None = None
    member_summary: dict[str, Any] | None = None
    if controller is not None:
        # Classify current position and compute expected next index when possible.
        try:
            classified_index = controller._classify_state()
        except Exception:
            classified_index = None

        try:
            next_index = (int(controller._resolved_index) + 1) % (len(controller._steps) + 1)
        except Exception:
            next_index = None

        try:
            expanded_targets = list(controller._expanded_target_entity_ids())
        except Exception:
            expanded_targets = None

        if expanded_targets:
            # Build collection-level counters to explain step classification behavior.
            votes: Counter[int] = Counter()
            counts: Counter[str] = Counter()

            for entity_id in expanded_targets:
                st = hass.states.get(entity_id)
                if st is None:
                    counts["missing"] += 1
                    continue

                counts[f"state_{st.state}"] += 1
                if st.state != "on":
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
            }

    # Include entry payloads (redacted) and primary target state in every diagnostics file.
    data: dict[str, Any] = {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "data": async_redact_data(entry.data, _TO_REDACT),
            "options": async_redact_data(entry.options, _TO_REDACT),
        },
        "target": {
            "entity_ids": target_entity_ids,
            "entity_id": target_entity_id,
            "state": None if target_state is None else target_state.state,
            "brightness": None
            if target_state is None
            else target_state.attributes.get("brightness"),
            "supported_color_modes": None
            if target_state is None
            else target_state.attributes.get("supported_color_modes"),
            "color_mode": None if target_state is None else target_state.attributes.get("color_mode"),
            "members": None if target_state is None else target_state.attributes.get("entity_id"),
        },
    }

    if controller is not None:
        # Serialize tuple cache values into JSON-friendly list values.
        temp_range_cache = {
            entity_id: [bounds[0], bounds[1]]
            for entity_id, bounds in getattr(controller, "_temp_range_cache", {}).items()
            if isinstance(entity_id, str)
            and isinstance(bounds, tuple)
            and len(bounds) == 2
        }
        data["controller"] = {
            "resolved_index": getattr(controller, "_resolved_index", None),
            "classified_index": classified_index,
            "next_index": next_index,
            "steps": getattr(controller, "_steps", None),
            "expanded_targets": expanded_targets,
            "member_summary": member_summary,
            "average_pct": getattr(controller, "_last_average_pct", None),
            "sample_counts": getattr(controller, "_last_sample_counts", None),
            "max_parallel_calls": get_max_parallel_calls(hass),
            "temp_range_cache": temp_range_cache,
            "state_sync_suppressed": bool(
                time.monotonic()
                < float(getattr(controller, "_ignore_state_changes_until", 0.0))
            ),
        }

    return data
