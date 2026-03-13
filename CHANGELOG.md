# Changelog

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
