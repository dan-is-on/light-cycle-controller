# Light Cycle Controller (Home Assistant)

Wizard-led Home Assistant custom integration that lets a **ZHA Zigbee button** cycle a target collection of `light.*` entities through a user-defined set of **discrete brightness steps** (Off → Low → Medium → High → Off), while staying in sync when the lights are controlled elsewhere (UI, voice, wall switch).

- **Integration name:** Light Cycle Controller
- **Domain:** `light_cycle`
- **Config:** UI config flow only (no YAML)

## What it does (v1 scope)

- **Setup wizard is the product:** create repeatable “cycle controllers” without hand-built helpers/automations.
- **Per-instance configuration:**
  - Instance name
  - Target light collection (single lights and/or light groups exposed as `light`)
  - ZHA remote device + captured button signature from an actual press (`zha_event`)
  - Dynamic brightness steps: Off + N “On” steps (each step has a label + brightness %)
  - Optional long press / double press bindings that jump directly to Off or a chosen step
- **Runtime behaviour:**
  - Matching press advances to the next step
  - Optional long press / double press can jump straight to Off or a specific step
  - External light changes reconcile internal cycle position (nearest brightness step wins)
  - Multiple instances supported without collisions
  - Existing entries can be renamed later from the **Configure** flow

## Guided setup (config flow)

1. **Instance basics**
   - Instance Name (e.g., “Dining Pendant Cycle”)
   - Target Lights (`light.*`, supports selecting multiple items including light groups)
   - On first setup only: **Parallel service calls (integration-wide)** (default `6`)
     - Why `6`: good speedup for medium/large collections without overwhelming common bridges/cloud APIs
     - If you see intermittent failures or lag (especially Tuya throttling), reduce this value
2. **Select Zigbee remote (ZHA)**
   - Pick the ZHA device (button/remote)
   - The picker is filtered to likely remotes/buttons so you do not have to scroll past every Zigbee light and sensor
3. **Check gesture support (first time per remote)**
   - If this remote has never been checked before, the wizard asks you to try a **Long press** and then a **Double press**
   - Support is remembered per remote, so later entries and normal edits on the same device skip these checks
   - If the remembered support cache is missing, the integration rebuilds it from any existing entries that already captured long/double gestures for that remote
   - If a gesture is not supported, mark it as unsupported and continue
4. **Capture button press**
   - Click **Submit**, then press the desired physical button once
   - Integration stores a “signature” (device IEEE + endpoint + command; optionally cluster/args if needed)
5. **Configure step basics**
   - Choose number of “On” steps (1–8)
   - For each step: label + brightness % + mode (**White & temperature** or **Colour**)
6. **Configure step details**
   - Mode-specific details are then shown **one step at a time** (clear visual separation):
     - White & temperature: only temperature slider (0–100% of each light’s min/max Kelvin range)
     - Colour: only colour picker + `#RRGGBB` hex
   - Off is always included as the first state
7. **Gesture actions**
   - For any gestures the remote is known to support, choose what each one should do:
     - **Do nothing**
     - jump to **Off**
     - jump to any configured step
   - If you choose an action for long press or double press, the wizard then captures that gesture for the selected button on this entry

## How cycling + sync works

- **Cycle order:** Off → Step 1 → Step 2 → … → Off
- **On press:** integration classifies the *current* light state, then advances one step and calls:
  - `light.turn_off` for Off
  - `light.turn_on` with per-entity payloads (brightness plus colour or white-temperature when configured)
- **Optional gesture shortcuts:**
  - Long press and double press support is learned once per remote and remembered in integration storage
  - For supported gestures, each entry can map long press / double press to **Do nothing**, `Off`, or any On step
  - If a gesture is mapped for an entry, the integration captures that gesture from the chosen button before saving
- **Dispatch behaviour:** service dispatch prioritizes non-Tuya entities first and defers Tuya-backed entities, so local lights tend to respond sooner in mixed collections
- **Sync rules (deterministic):**
  - Light turns Off → cycle state becomes Off
  - Light becomes `unavailable` → treated as Off for cycling
  - Compute average **brightness only** across the expanded collection (including nested groups)
    - `off` / `unavailable` / `unknown` contribute `0%`
    - `on` with brightness contributes its converted `%`
    - `on` without brightness falls back to the last resolved step
  - Choose the configured step whose brightness % is nearest to that average
    - If the entity does not report a brightness attribute, the controller keeps cycling using its last known step (sync is limited to Off vs On)
  - During a button-triggered apply, transient intermediate group states are briefly ignored so large groups don’t collapse the cycle back to fewer steps mid-update
  - After apply, the controller re-expands the collection; if new lights appeared, it applies the same step to those added lights

## Explicit non-goals (deferred)

- Zigbee2MQTT support (ZHA only for v1)
- Triple press / arbitrary multi-gesture automation logic
- Auto-creating HA helpers/scenes/automations as persistent artefacts

## Why we don’t create helpers/scenes/automations (v1)

Home Assistant integrations generally should not create/maintain UI-managed artefacts (helpers/scenes/automations) unless there’s a strong, version-resilient idempotency strategy. For v1, the integration focuses on the behavioural outcome (cycling + sync) directly in runtime code, keeping setup repeatable and upgrades safer.

## Installation (HACS)

This repository is intended to be HACS-compatible.

1. HACS → **Integrations** → menu → **Custom repositories**
2. Add this repo URL and select category **Integration**
3. Install, then restart Home Assistant
4. Settings → Devices & Services → **Add Integration** → “Light Cycle Controller”

### British English UI text

This integration includes an `en-GB` translation file.  
If your Home Assistant language is set to English (UK), UI labels use local spelling (for example, “Colour”).

### Integration icon (“icon not available”)

Home Assistant shows integration icons via the Brands system. This integration ships local brand images (including light/dark + @2x variants) and requires **Home Assistant Core 2026.3+**; on older versions you may still see “icon not available”.

If you’re on 2026.3+ and still see the placeholder, restart Home Assistant after updating and hard-refresh your browser (the frontend can cache brand images).

### Version numbers in the update UI

If you see versions like `bc31cf4` in Home Assistant’s update dialog, that’s a commit SHA. HACS uses **GitHub Releases** (not just tags) to determine semantic versions; without releases it falls back to commit SHAs.

This repo now auto-publishes GitHub Releases from `CHANGELOG.md` on pushes to `main` (see `RELEASING.md`).

If you installed the integration from the `main` branch (dev install), HACS will continue showing SHAs until you reinstall/switch to a GitHub Release.

### Do I need to restart Home Assistant after updating?

Yes — when HACS updates a custom integration’s Python code, Home Assistant needs a restart to load the new code. (Editing an entry via **Configure** does not require a restart.)

## Editing an existing controller

After setup, you can edit an entry (target lights, ZHA device/button capture, optional gesture bindings, and steps):

1. Settings → Devices & Services → “Light Cycle Controller”
2. Open the entry’s menu (⋮) → **Configure**

### Integration-wide performance setting

You can update the integration-wide **Parallel service calls** value in that same Configure flow (first page, “Edit controller”).  
This value applies to all `light_cycle` entries.

## Debugging

### What shows in the System Logs UI

Home Assistant’s **Settings → System → Logs** UI primarily shows warnings/errors. `INFO`/`DEBUG` logs are typically only visible in the downloaded log file unless you change logging configuration.

You can also dump the currently loaded configuration via an action/service:

1. Developer Tools → **Actions**
2. Choose **Dump controller state** (`light_cycle.dump`)
3. Optional data: `{"entry_id": "..."}`
4. Check logs for `Dump:` lines (`light_cycle.dump` output is logged at `INFO` level)

Or download diagnostics for a specific entry:

1. Settings → Devices & Services → “Light Cycle Controller”
2. Open the entry’s menu (⋮) → **Download diagnostics**

### Debug logging (button presses)

Enable debug logging to see per-press logs:

1. Developer Tools → **Actions**
2. Run this action to enable full debug logs at runtime:
   ```yaml
   action: logger.set_level
   data:
     custom_components.light_cycle: debug
   ```
   Or use `info` instead of `debug` for lighter logs (still includes `Dump:` lines).
3. Edit an entry (e.g. change 2 → 3 steps), press the button once, then check logs for lines like:
   - `Started controller ... (steps=...)`
   - `Refreshed steps ...`
   - `Press: ... steps=...`
   - `Direct gesture: ... gesture=long_press target=...`
4. In Logs, use **⋮ → Show raw logs** to see INFO/DEBUG output (the condensed view is warnings/errors only).

#### Persistent logger config (`configuration.yaml`)

If you want logging levels to survive restarts, add:

```yaml
logger:
  default: warning
  logs:
    custom_components.light_cycle: debug
```

Use `info` instead of `debug` when you only want operational logs and dump output with less noise.

## License

MIT — see `LICENSE`.

## Release history

See `CHANGELOG.md`.
