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

## Automated releases (GitHub Actions)

This repo includes a workflow at `.github/workflows/release.yml` that can publish a GitHub Release:

- **Tag push:** push a tag like `v0.1.6` and the workflow publishes a release for it.
- **Manual:** GitHub → Actions → **Release** → “Run workflow” and enter the version (e.g. `0.1.6`).

The workflow extracts the matching `## 0.1.6` section from `CHANGELOG.md` and uses it as the release notes.
If the section is missing, it falls back to GitHub’s auto-generated release notes.

This workflow does **not** publish to the HACS default repository (“official store”) — it only creates GitHub Releases in this repo, which HACS then uses to show semantic versions instead of commit SHAs.
