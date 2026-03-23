"""Global settings storage for Light Cycle Controller."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_MAX_PARALLEL_CALLS,
    DEFAULT_MAX_PARALLEL_CALLS,
    DOMAIN,
    GESTURE_DOUBLE_PRESS,
    GESTURE_LONG_PRESS,
    MAX_MAX_PARALLEL_CALLS,
    MIN_MAX_PARALLEL_CALLS,
)

SETTINGS_STORE_VERSION = 1
SETTINGS_STORE_KEY = f"{DOMAIN}.settings"
DATA_SETTINGS = "settings"
SETTINGS_DEVICE_GESTURE_SUPPORT = "device_gesture_support"
KNOWN_DEVICE_GESTURES = (GESTURE_LONG_PRESS, GESTURE_DOUBLE_PRESS)


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


def _normalize_device_gesture_support(
    value: Any,
) -> dict[str, dict[str, bool]]:
    """Normalize cached or persisted per-device gesture capability values."""
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, dict[str, bool]] = {}
    for ieee, raw_support in value.items():
        if not isinstance(ieee, str) or not isinstance(raw_support, dict):
            continue

        per_device: dict[str, bool] = {}
        for gesture in KNOWN_DEVICE_GESTURES:
            gesture_value = raw_support.get(gesture)
            if isinstance(gesture_value, bool):
                per_device[gesture] = gesture_value

        if per_device:
            normalized[ieee] = per_device

    return normalized


async def async_get_settings(hass: HomeAssistant) -> dict[str, Any]:
    """Return global settings, loading from storage if needed."""
    # Reuse an in-memory cache in hass.data to avoid disk reads on each access.
    domain_data = hass.data.setdefault(DOMAIN, {})
    cached = domain_data.get(DATA_SETTINGS)
    if isinstance(cached, dict):
        # Backfill defaults when older cache shapes are encountered.
        if CONF_MAX_PARALLEL_CALLS not in cached:
            cached[CONF_MAX_PARALLEL_CALLS] = DEFAULT_MAX_PARALLEL_CALLS
        cached[SETTINGS_DEVICE_GESTURE_SUPPORT] = _normalize_device_gesture_support(
            cached.get(SETTINGS_DEVICE_GESTURE_SUPPORT)
        )
        return cached

    # Load persisted settings and normalize values before caching.
    stored = await _settings_store(hass).async_load()
    settings: dict[str, Any] = {
        CONF_MAX_PARALLEL_CALLS: _clamp_max_parallel_calls(
            (stored or {}).get(CONF_MAX_PARALLEL_CALLS, DEFAULT_MAX_PARALLEL_CALLS)
        ),
        SETTINGS_DEVICE_GESTURE_SUPPORT: _normalize_device_gesture_support(
            (stored or {}).get(SETTINGS_DEVICE_GESTURE_SUPPORT)
        ),
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


async def async_get_device_gesture_support(
    hass: HomeAssistant, ieee: str | None
) -> dict[str, bool]:
    """Return remembered gesture support flags for one remote IEEE."""
    if not isinstance(ieee, str) or not ieee:
        return {}

    settings = await async_get_settings(hass)
    by_device = _normalize_device_gesture_support(
        settings.get(SETTINGS_DEVICE_GESTURE_SUPPORT)
    )
    settings[SETTINGS_DEVICE_GESTURE_SUPPORT] = by_device
    return dict(by_device.get(ieee, {}))


async def async_set_device_gesture_support(
    hass: HomeAssistant, ieee: str, gesture: str, supported: bool
) -> dict[str, bool]:
    """Persist one per-device gesture support verdict and return the device map."""
    if gesture not in KNOWN_DEVICE_GESTURES:
        raise ValueError(f"Unsupported gesture capability key: {gesture}")

    settings = await async_get_settings(hass)
    by_device = _normalize_device_gesture_support(
        settings.get(SETTINGS_DEVICE_GESTURE_SUPPORT)
    )
    per_device = dict(by_device.get(ieee, {}))
    per_device[gesture] = bool(supported)
    by_device[ieee] = per_device
    settings[SETTINGS_DEVICE_GESTURE_SUPPORT] = by_device
    await _settings_store(hass).async_save(settings)
    return dict(per_device)
