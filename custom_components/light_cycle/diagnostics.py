"""Diagnostics support for Light Cycle Controller."""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from .const import CONF_REMOTE_IEEE, CONF_TARGET_ENTITY_ID, DOMAIN

_TO_REDACT = {CONF_REMOTE_IEEE}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    target_entity_id = entry.options.get(
        CONF_TARGET_ENTITY_ID, entry.data.get(CONF_TARGET_ENTITY_ID)
    )
    target_state = hass.states.get(target_entity_id) if target_entity_id else None

    controllers: dict[str, Any] = hass.data.get(DOMAIN, {}).get("controllers", {})
    controller = controllers.get(entry.entry_id)

    classified_index = None
    next_index = None
    expanded_targets: list[str] | None = None
    member_summary: dict[str, Any] | None = None
    if controller is not None:
        try:
            classified_index = controller._classify_state(target_state)
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

    data: dict[str, Any] = {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "data": async_redact_data(entry.data, _TO_REDACT),
            "options": async_redact_data(entry.options, _TO_REDACT),
        },
        "target": {
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
        data["controller"] = {
            "resolved_index": getattr(controller, "_resolved_index", None),
            "classified_index": classified_index,
            "next_index": next_index,
            "steps": getattr(controller, "_steps", None),
            "expanded_targets": expanded_targets,
            "member_summary": member_summary,
        }

    return data
