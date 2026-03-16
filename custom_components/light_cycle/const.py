"""Constants for the Light Cycle Controller integration."""

from __future__ import annotations

DOMAIN = "light_cycle"

CONF_TARGET_ENTITY_ID = "target_entity_id"
CONF_TARGET_ENTITY_IDS = "target_entity_ids"
CONF_MAX_PARALLEL_CALLS = "max_parallel_calls"
CONF_REMOTE_DEVICE_ID = "remote_device_id"
CONF_REMOTE_IEEE = "remote_ieee"

CONF_ENDPOINT_ID = "endpoint_id"
CONF_COMMAND = "command"
CONF_CLUSTER_ID = "cluster_id"
CONF_ARGS = "args"

CONF_ON_STEPS = "on_steps"
CONF_STEPS = "steps"
CONF_STEP_LABEL = "label"
CONF_STEP_BRIGHTNESS_PCT = "brightness_pct"

MIN_ON_STEPS = 1
MAX_ON_STEPS = 8

DEFAULT_CAPTURE_TIMEOUT_SECONDS = 60
DEFAULT_MAX_PARALLEL_CALLS = 6
MIN_MAX_PARALLEL_CALLS = 1
MAX_MAX_PARALLEL_CALLS = 20
