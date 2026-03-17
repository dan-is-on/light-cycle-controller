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
- **Runtime behaviour:**
  - Matching press advances to the next step
  - External light changes reconcile internal cycle position (nearest brightness step wins)
  - Multiple instances supported without collisions

## Guided setup (config flow)

1. **Instance basics**
   - Instance Name (e.g., “Dining Pendant Cycle”)
   - Target Lights (`light.*`, supports selecting multiple items including light groups)
   - On first setup only: **Parallel service calls (integration-wide)** (default `6`)
     - Why `6`: good speedup for medium/large collections without overwhelming common bridges/cloud APIs
     - If you see intermittent failures or lag (especially Tuya throttling), reduce this value
2. **Select Zigbee remote (ZHA)**
   - Pick the ZHA device (button/remote)
3. **Capture button press**
   - Click **Submit**, then press the desired physical button once
   - Integration stores a “signature” (device IEEE + endpoint + command; optionally cluster/args if needed)
4. **Configure brightness steps**
   - Choose number of “On” steps (1–8)
   - For each step:
     - label + brightness % (1–100)
     - mode: **White & temperature** or **Color**
     - White mode: temperature slider (0–100% of each light’s min/max Kelvin range)
     - Color mode: color picker + `#RRGGBB` hex
   - Off is always included as the first state

## How cycling + sync works

- **Cycle order:** Off → Step 1 → Step 2 → … → Off
- **On press:** integration classifies the *current* light state, then advances one step and calls:
  - `light.turn_off` for Off
  - `light.turn_on` with both `brightness` (percent → 0–255) and `brightness_pct` for On steps
  - Service dispatch prioritizes non-Tuya entities first and defers Tuya-backed entities, so local lights tend to respond sooner in mixed collections
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

- Double press / long press
- Zigbee2MQTT support (ZHA only for v1)
- Per-step colour temperature / colour (brightness-only for v1)
- Auto-creating HA helpers/scenes/automations as persistent artefacts

## Why we don’t create helpers/scenes/automations (v1)

Home Assistant integrations generally should not create/maintain UI-managed artefacts (helpers/scenes/automations) unless there’s a strong, version-resilient idempotency strategy. For v1, the integration focuses on the behavioural outcome (cycling + sync) directly in runtime code, keeping setup repeatable and upgrades safer.

## Installation (HACS)

This repository is intended to be HACS-compatible.

1. HACS → **Integrations** → menu → **Custom repositories**
2. Add this repo URL and select category **Integration**
3. Install, then restart Home Assistant
4. Settings → Devices & Services → **Add Integration** → “Light Cycle Controller”

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

After setup, you can edit an entry (target light, ZHA device/button capture, and steps):

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
4. Check Settings → System → Logs for `Dump:` lines (dump output is logged at `WARNING` so it shows up there)

Or download diagnostics for a specific entry:

1. Settings → Devices & Services → “Light Cycle Controller”
2. Open the entry’s menu (⋮) → **Download diagnostics**

### Debug logging (button presses)

Enable debug logging to see per-press logs:

1. Settings → System → Logs → ⋮ → **Configure logging**
2. Add: `custom_components.light_cycle: debug`
3. Edit an entry (e.g. change 2 → 3 steps), press the button once, then check logs for lines like:
   - `Started controller ... (steps=...)`
   - `Refreshed steps ...`
   - `Press: ... steps=...`

## License

MIT — see `LICENSE`.

## Release history

See `CHANGELOG.md`.
