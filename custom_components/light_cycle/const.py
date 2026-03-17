"""Constants for the Light Cycle Controller integration."""

from __future__ import annotations

DOMAIN = "light_cycle"

# Config entry target settings.
CONF_TARGET_ENTITY_ID = "target_entity_id"
CONF_TARGET_ENTITY_IDS = "target_entity_ids"
CONF_MAX_PARALLEL_CALLS = "max_parallel_calls"
CONF_REMOTE_DEVICE_ID = "remote_device_id"
CONF_REMOTE_IEEE = "remote_ieee"

# Captured ZHA button signature fields.
CONF_ENDPOINT_ID = "endpoint_id"
CONF_COMMAND = "command"
CONF_CLUSTER_ID = "cluster_id"
CONF_ARGS = "args"

# Step configuration fields.
CONF_ON_STEPS = "on_steps"
CONF_STEPS = "steps"
CONF_STEP_LABEL = "label"
CONF_STEP_BRIGHTNESS_PCT = "brightness_pct"
CONF_STEP_MODE = "mode"
CONF_STEP_TEMP_PCT = "temp_pct"
CONF_STEP_COLOR_HEX = "color_hex"
CONF_STEP_COLOR_RGB = "color_rgb"

# Supported step rendering modes.
STEP_MODE_WHITE_TEMP = "white_temp"
STEP_MODE_COLOR = "color"

# Per-step defaults used for migration and new form entries.
DEFAULT_STEP_MODE = STEP_MODE_WHITE_TEMP
DEFAULT_STEP_TEMP_PCT = 1
DEFAULT_STEP_COLOR_HEX = "#FF0000"
DEFAULT_STEP_COLOR_RGB = [255, 0, 0]

# Fallback white temperature range when a light does not expose capabilities.
DEFAULT_TEMP_MIN_KELVIN = 2000
DEFAULT_TEMP_MAX_KELVIN = 6500

# Bounds for step count and global concurrency.
MIN_ON_STEPS = 1
MAX_ON_STEPS = 8

DEFAULT_CAPTURE_TIMEOUT_SECONDS = 60
DEFAULT_MAX_PARALLEL_CALLS = 6
MIN_MAX_PARALLEL_CALLS = 1
MAX_MAX_PARALLEL_CALLS = 20
