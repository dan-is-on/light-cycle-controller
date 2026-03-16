"""Diagnostics support for Light Cycle Controller."""

from __future__ import annotations

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
        },
    }

    controllers: dict[str, Any] = hass.data.get(DOMAIN, {}).get("controllers", {})
    controller = controllers.get(entry.entry_id)
    if controller is not None:
        data["controller"] = {
            "resolved_index": getattr(controller, "_resolved_index", None),
            "steps": getattr(controller, "_steps", None),
        }

    return data
