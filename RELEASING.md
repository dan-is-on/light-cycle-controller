# Releasing (for HACS versions)

HACS uses **GitHub Releases** to determine semantic versions. If there are no GitHub releases, HACS falls back to showing short commit SHAs as the “version”.

## Steps

1. Update `custom_components/light_cycle/manifest.json` `"version"` (e.g. `0.1.12`).
2. Add/update the matching section in `CHANGELOG.md`:
   - `## 0.1.12`
   - Bullet notes under that heading
3. Commit and push to `main`.

That’s it. The Release workflow now auto-creates:
- tag: `v0.1.12`
- GitHub Release named `v0.1.12`
- release notes from the `## 0.1.12` changelog section

If the tag already exists, the workflow exits cleanly with no duplicate release.

> Note: If you installed a **dev/branch** version in HACS, it may continue showing commit SHAs for that install.
> Re-install from the Release (tag) once you’ve published GitHub Releases.

## Automated releases (GitHub Actions)

Workflow: `.github/workflows/release.yml`

- Triggered automatically on pushes to `main` when `CHANGELOG.md` changes.
- Reads the top changelog release heading (or optional manual `version` input).
- Requires a non-empty matching changelog section.
- Skips if `vX.Y.Z` already exists.
- Otherwise publishes the release and creates the tag.

### Manual run (optional)

GitHub → Actions → **Release** → **Run workflow**
- Leave `version` empty to use the top changelog heading.
- Or set `version` (e.g. `0.1.12`) to force a specific changelog section.

This workflow does **not** publish to the HACS default repository (“official store”) — it only creates GitHub Releases in this repo, which HACS then uses to show semantic versions instead of commit SHAs.
