# Changelog

## 0.1.0a1 — 2026-10-20 — experimental prerelease

Initial experimental read-only offline viewer for a scoped dedicated OneDrive
test account. See [the release checklist](docs/release-checklist.md) for build gates and
[GitHub Releases](https://github.com/dzuritaa/cloudmarks/releases) for publication
availability.

- Evidence-based cloud/local/offline/transfer/error/unknown statuses.
- Read-only Nautilus badges/column and a bounded status-only local helper.
- Verified complete-file storage, read-only local-first/explicit offline views,
  individual download/cancellation/retry and local-only eviction.
- Durable publication recovery, explicit stable-ID change checks, retryable jobs,
  retained conflict candidates and bounded recursive folder pin intent.
- MIT license selected by the owner.
- User-only reversible adapter deployment, explicit upgrade/rollback instructions,
  optional status-helper unit, CI and installed-package/source-rebuild checks.

Offline edits, uploads, automatic sync, mounted updates, context-menu controls,
in-place conflict repair and automatic history cleanup are absent. Hardware
reconnect/logout, larger-file and daily desktop acceptance remain open.
