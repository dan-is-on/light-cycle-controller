# Changelog

## 0.1.8

- Reload the config entry when options are saved (more reliable edits + ensures old listeners are cleaned up).
- Add `services.yaml` documentation for `light_cycle.dump`.
- Add config entry diagnostics (downloadable from the integration entry) to capture active steps/controller state for debugging.
- Update README debugging instructions for Home Assistant 2026.3+ (Developer Tools → Actions).

## 0.1.7

- Log config edits and controller restarts at `INFO` level (so it shows up in standard system logs).
- Add a `light_cycle.dump` service to print the active controller configuration to logs.
- Apply light group member changes sequentially (best-effort) to reduce API concurrency issues.

## 0.1.6

- Add extra debug logging to troubleshoot step edits not applying.
- Refresh the controller’s step list from the latest config entry (so edits apply even if the entry object is stale).
- Catch and log failures when calling `light.turn_on`/`light.turn_off` to avoid “Task exception was never retrieved”.
- If the target is a light group that exposes member `entity_id`s, apply changes best-effort per member (one failing light won’t always block the whole step).

## 0.1.5

- Apply edited options immediately by restarting the controller on entry updates.
- Refresh steps from the config entry on press/state changes (so step edits take effect even if the controller wasn’t restarted).
- Add debug logging for press handling (current/next step and step count).
- Add light/dark + @2x brand image variants for more reliable icon rendering.

## 0.1.4

- Improve cycling for lights that don’t report a brightness attribute.

## 0.1.3

- Fixed “Configure” (options) flow crash on newer Home Assistant versions.
- Added `icon.png`/`logo.png` at the repo root for HACS UI.

## 0.1.2

- Added local brand images for the integration UI.

## 0.1.1

- Capture step UX: instruct “Submit, then press” and only start listening after Submit.
- Added an options flow to edit an existing entry (target light, ZHA device/button capture, steps).
- Reload config entries on updates to ensure old listeners are cleaned up.

## 0.1.0

- Initial release: wizard-led setup + ZHA button cycling + sync-by-brightness.
