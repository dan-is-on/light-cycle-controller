# Changelog

## 0.1.23

- Reuse learned long-press and double-press command templates across buttons on the same remote, so after the first captured button you usually do not have to re-capture those gestures for every other button on that controller.

## 0.1.22

- Fix a Configure-flow 500 caused by some device registry identifiers not being simple 2-tuples while building the filtered ZHA remote picker.

## 0.1.21

- Filter the ZHA device picker down to likely remotes/buttons by inspecting device automations, while still falling back safely if Home Assistant cannot provide that metadata.

## 0.1.20

- Add instance-name editing to the Configure flow, so existing Light Cycle Controller entries can be renamed without deleting and recreating them.
- Reconstruct remembered long/double gesture support from existing entries on the same remote, preventing duplicate gesture-support checks during normal edits when support is already known.

## 0.1.19

- Learn long-press and double-press support once per ZHA remote, cache that capability in integration settings, and skip repeating those support-check screens on later entries for the same device.
- Change the wizard so supported gestures always get an explicit action choice (`Do nothing`, `Off`, or a direct jump to a configured step), instead of first asking whether to enable them.
- Keep per-entry gesture capture only for the supported gestures you actually map, and include remembered device gesture support in diagnostics.

## 0.1.18

- Add optional long-press and double-press capture to the config flow and options flow, with each captured gesture mappable to `Off` or any configured step.
- React to those optional gesture bindings at runtime, so long/double press can jump directly to a target step instead of advancing the normal cycle.
- Add config-entry migration to v3 plus richer dump/diagnostics output for optional gesture bindings, making gesture debugging much easier.

## 0.1.17

- Fix duplicate-brightness step progression by using the last resolved step as a tie-breaker during brightness classification; this prevents getting stuck on the same colour step when multiple steps share the same brightness.
- Keep deterministic fallback to the lowest index when no resolved tied step exists (for example, cold-start classification).

## 0.1.16

- Fix `light.turn_on` payload validation by sending only one brightness key (`brightness`) per call, resolving button presses that previously failed with `MultipleInvalid` and appeared to do nothing.
- Add extra debug logging around per-entity payload building and service dispatch/failures so future runtime issues are easier to diagnose from Home Assistant logs.

## 0.1.15

- Replace `en-AU` translation pack with `en-GB` for broader Home Assistant language support while keeping UK spelling in UI labels.

## 0.1.14

- Refine step configuration UX to a two-phase flow: first set label/brightness/mode for all steps, then configure mode-specific fields one step at a time.
- Show only relevant controls per step detail page (white/temperature slider **or** colour picker + hex), reducing config-flow clutter.
- Add Australian English translation support with `en-AU` locale strings (UI uses “colour” when HA language is set to Australian English).
- Standardize dump output to `INFO` level and keep `WARNING`/`ERROR` focused on actual runtime issues.
- Expand inline comments/docstrings across runtime, config flow, diagnostics, settings, and constants for maintainability.

## 0.1.13

- Add integration-wide `max_parallel_calls` setting (persisted in storage), asked during first entry setup and editable later in the entry Configure flow.
- Apply light service calls with capped async fan-out (instead of strictly sequential calls) for better responsiveness on large collections.
- Prioritize non-Tuya entities before Tuya-backed entities during service dispatch so mixed collections show faster visible response.
- Keep collection expansion cached in memory for the press hot path; re-expand after apply and patch newly discovered lights to the same level.
- Include concurrency and average collection metrics in dump/diagnostics for easier tuning and debugging.
- Automate GitHub Releases from `CHANGELOG.md` on `main` pushes (tag + release creation when a new version heading appears).
- Add per-step mode support: `white_temp` (0–100% warmth over each light’s Kelvin range) or `color` (picker + hex).
- Keep sync classification brightness-only when lights are changed externally (voice/UI), so cycle inference remains stable.
- Add config-entry migration to v2: existing steps default to white mode at warmest `1%` temp.
- Apply per-entity turn_on payloads for mixed collections, with Tuya-preferring `hs_color` mapping to avoid hue skew.

## 0.1.11

- Add multi-target collections in config/options flow (select multiple lights and/or light groups for one controller entry).
- Classify current step from the average brightness across the expanded collection (`off`/`unavailable`/`unknown` count as `0%`).
- Cache expanded collection members in memory for fast button response; re-expand after each apply and patch newly added lights to the just-applied step.
- Subscribe/unsubscribe state listeners for expanded collections so entry unload/remove stays clean.
- Extend diagnostics and dump output with collection-level metrics (`average_pct`, expanded targets, and sync suppression status).

## 0.1.10

- Keep cycle progression deterministic during large group updates by temporarily suppressing transient state re-classification while a press is being applied.
- Prefer applying `light.turn_on`/`light.turn_off` to the configured target first, then fall back to flattened member entities if the target call fails.
- Send both `brightness` and `brightness_pct` on On steps for broader light platform compatibility.
- Add a `state_sync_suppressed` flag to diagnostics so it’s obvious when transient state updates are being ignored after a press.

## 0.1.9

- Make `light_cycle.dump` visible in the System Logs UI (log level `WARNING`) and include member vote summaries for light groups.
- Fix `services.yaml` schema so the `light_cycle.dump` action loads cleanly.
- Improve sync for light groups by classifying the current step from member states (mode vote) instead of relying on the group’s aggregated brightness.
- Add expanded target + member vote summaries to config entry diagnostics to help troubleshoot “step count not applying” reports.

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
