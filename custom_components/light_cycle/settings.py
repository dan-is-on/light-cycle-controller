"""Global settings storage for Light Cycle Controller."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_MAX_PARALLEL_CALLS,
    DEFAULT_MAX_PARALLEL_CALLS,
    DOMAIN,
    MAX_MAX_PARALLEL_CALLS,
    MIN_MAX_PARALLEL_CALLS,
)

SETTINGS_STORE_VERSION = 1
SETTINGS_STORE_KEY = f"{DOMAIN}.settings"
DATA_SETTINGS = "settings"


def _clamp_max_parallel_calls(value: Any) -> int:
    """Parse and bound max parallel call values to supported limits."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PARALLEL_CALLS
    return max(MIN_MAX_PARALLEL_CALLS, min(MAX_MAX_PARALLEL_CALLS, parsed))


def _settings_store(hass: HomeAssistant) -> Store[dict[str, Any]]:
    """Return storage helper used for integration-wide settings persistence."""
    return Store(hass, SETTINGS_STORE_VERSION, SETTINGS_STORE_KEY)


async def async_get_settings(hass: HomeAssistant) -> dict[str, Any]:
    """Return global settings, loading from storage if needed."""
    # Reuse an in-memory cache in hass.data to avoid disk reads on each access.
    domain_data = hass.data.setdefault(DOMAIN, {})
    cached = domain_data.get(DATA_SETTINGS)
    if isinstance(cached, dict):
        # Backfill defaults when older cache shapes are encountered.
        if CONF_MAX_PARALLEL_CALLS not in cached:
            cached[CONF_MAX_PARALLEL_CALLS] = DEFAULT_MAX_PARALLEL_CALLS
        return cached

    # Load persisted settings and normalize values before caching.
    stored = await _settings_store(hass).async_load()
    settings: dict[str, Any] = {
        CONF_MAX_PARALLEL_CALLS: _clamp_max_parallel_calls(
            (stored or {}).get(CONF_MAX_PARALLEL_CALLS, DEFAULT_MAX_PARALLEL_CALLS)
        )
    }
    domain_data[DATA_SETTINGS] = settings
    return settings


def get_max_parallel_calls(hass: HomeAssistant) -> int:
    """Return global max parallel calls from cache, with a safe default fallback."""
    domain_data = hass.data.get(DOMAIN, {})
    settings = domain_data.get(DATA_SETTINGS)
    if not isinstance(settings, dict):
        # Fallback protects runtime behavior even before settings are loaded.
        return DEFAULT_MAX_PARALLEL_CALLS
    return _clamp_max_parallel_calls(settings.get(CONF_MAX_PARALLEL_CALLS))


async def async_set_max_parallel_calls(hass: HomeAssistant, value: Any) -> int:
    """Persist and cache the global max parallel calls setting."""
    # Normalize before saving so persisted values are always valid.
    max_parallel_calls = _clamp_max_parallel_calls(value)
    settings = await async_get_settings(hass)
    settings[CONF_MAX_PARALLEL_CALLS] = max_parallel_calls
    await _settings_store(hass).async_save(settings)
    return max_parallel_calls
