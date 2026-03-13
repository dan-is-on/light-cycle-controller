# Releasing (for HACS versions)

HACS uses **GitHub Releases** to determine semantic versions. If there are no GitHub releases, HACS falls back to showing short commit SHAs as the “version”.

## Steps

1. Update `custom_components/light_cycle/manifest.json` `"version"` (e.g. `0.1.2`).
2. Update `CHANGELOG.md`.
3. Commit and push to `main`.
4. Create a GitHub Release:
   - Tag: `v0.1.2` (or `0.1.2` if you prefer)
   - Release title: `0.1.2`
   - Release notes: paste the `CHANGELOG.md` section for that version

After that, HACS should show `0.1.2` as the installed/latest version instead of a commit SHA.

> Note: If you installed a **dev/branch** version in HACS, it may continue showing commit SHAs for that install.
> Re-install from the Release (tag) once you’ve published GitHub Releases.
