# CloudMarks project plan

Status: proposed implementation plan; no extension or transfer helper is installed.

## Outcome

In GNOME Files, show whether a OneDrive file is cloud-only, saved locally, transferring, or in an error state. Eventually offer **Keep offline** and **Free up local space** without confusing removal of a local copy with deletion from OneDrive.

The first target is GNOME Files 50 on Ubuntu, using one OneDrive account through rclone. Account names, mount paths, credentials, and disk identifiers must be configuration, never hard-coded project defaults.

## Current constraints

- The existing connection is an rclone mount using `--vfs-cache-mode=writes`. Read-only access streams file content; it does not guarantee a retained offline copy.
- A directory listing, a cache entry, or a previously opened file is not sufficient evidence of offline availability.
- The reference data disk uses NTFS3. A temporary file expanded with `truncate` did not remain sparse in the observed configuration. Do not switch it to full VFS caching and assume that large cloud files will use only their downloaded space.
- File status support must be verified against the installed rclone version. Do not depend on undocumented cache internals as a stable API or modify an active rclone cache directly.
- Nautilus loads its extension in the Files process. Network calls, hashing, directory walks, and transfers must run outside the UI callbacks.

These are engineering constraints, not reasons to change partitions, reformat disks, or replace the user's working mount during development.

## Architecture

1. **Nautilus adapter:** a small Python extension using `InfoProvider` for badges and later `MenuProvider` for actions. Restrict it to configured roots. Use asynchronous, bounded status requests, reuse results, and invalidate visible items when state changes.
2. **User service:** maintain file state and run background work. Expose a versioned, per-user local interface with owner-only access. The adapter must remain responsive when this service is absent or slow.
3. **Storage adapter:** encapsulate rclone and local storage behavior. Mock this boundary for development. Decide the real offline mechanism only after the feasibility tests below.
4. **State store:** track remote identity/revision, completed local version, pending edits, transfer state, and pin intent. Store content on the configured data disk; keep credentials outside the repository and out of the Nautilus adapter.

Use the backend's authoritative state when available. A local-copy manifest alone must not override actual missing files, partial downloads, or remote changes.

## Status contract

| Display | Required evidence |
| --- | --- |
| Cloud only | Known remote file, no verified complete local copy, no pending local edit. |
| Saved locally | Complete local copy and metadata are verified, and the supported open path can use it without internet. |
| Kept offline | Saved locally, plus persistent pin intent that prevents ordinary cache eviction. |
| Downloading / uploading | A tracked operation is active; never show success before completion and validation. |
| Error / conflict | A failed operation or competing edits need attention; preserve recoverable data. |
| Unknown / needs checking | Helper unavailable, stale state, missing disk, or insufficient evidence. No green check. |

Folders must not receive a green check merely because the folder itself exists. Recursive folder status is deferred until coverage can be calculated accurately.

## Milestones and acceptance criteria

### M0 — Offline feasibility and design decision

- Create a local fixture tree and a disposable remote test area in a dedicated, otherwise empty Microsoft account.
- Give the test account its own credential configuration, remote name, mount, cache, state store, and user service. Do not reuse the personal account's tokens or default rclone configuration.
- Before any cloud test, verify the authenticated drive identity against the explicitly configured test drive and enforce the allowed test root. Refuse a mismatch or a missing identity; never fall back to the personal account. Keep real account and drive identifiers out of Git.
- Investigate supported rclone status/retention controls and the actual read path for complete cached files.
- Compare possible offline storage approaches, including filesystem limitations and local-space accounting. Check that complete files open through the same user-visible path with networking unavailable.
- Specify how remote edits, local edits, deletions, and disk removal affect each state.
- Record the chosen mechanism, supported operations, and limitations in a design decision document.

**Gate:** a backend experiment demonstrates complete download, offline read through the intended path, restart persistence, and local eviction without remote deletion. If the existing mount cannot support this, document the additional backend work before implementing production offline controls. A separate “open offline copy” action must be labelled as such, not presented as Windows-equivalent behavior.

### M1 — Status engine and automated checks

- Implement configuration validation, root containment, a stable file identity model, and the status contract.
- Test state transitions with fixture data and an artificial transfer backend; no Microsoft credentials required.
- Cover partial downloads, stale revisions, errors, missing disks, symlink/path escapes, and interrupted state writes.

**Gate:** partial or stale files never become “saved locally”; operations cannot escape the configured test root; failed transfers preserve existing data.

### M2 — Read-only Nautilus prototype

- Add cloud, local, transfer, error, and unknown badges with text descriptions where the API permits.
- Read state from the helper; do not perform transfers or automatically hydrate files when browsing.
- Refresh visible badges after state changes without scanning the entire drive.
- Provide a user-only installer, a working uninstaller, and a way to disable the extension independently of the OneDrive mount.

**Gate:** badges match fixture state; large folders and an unavailable helper do not freeze Files; unrelated folders receive no badges; uninstall restores baseline Files behavior. Validate visually in a separate desktop session or VM before the daily desktop.

M1/M2 may proceed against fixtures while the M0 storage investigation continues. Their UI is not proof that real offline storage works.

### M3 — Individual-file offline controls

Requires the M0 backend decision and successful offline tests.

- Add **Keep offline**, cancellation, retry, and explicit progress for individual files.
- Download to temporary files, validate content, then atomically publish the complete copy.
- Add **Free up local space** only for a verified clean copy already present remotely. It must never call a remote delete operation or remove the mounted file to achieve eviction.
- Refuse eviction of unsent edits, active transfers, or unverified copies. Handle open-file behavior explicitly.
- Enforce a configurable storage budget and a minimum free-space reserve. Refuse new work cleanly when the disk is unavailable or full.

**Gate:** a pinned file opens offline after restart; clearing its local copy leaves the cloud file intact; cancellation and disk-full scenarios retain recoverable data.

### M4 — Change tracking and recovery

- Detect remote revisions and deletions without silently overwriting unsent local work.
- Preserve both versions on conflicts; show an actionable state instead of inventing automatic merge behavior.
- Recover queued work after network loss, process restart, logout, and disk reconnection.
- Define unpinning separately from cache eviction and remote deletion.
- Add recursive folder pinning and aggregate folder states only after individual-file behavior passes.

**Gate:** a documented matrix of local/remote edits, renames, deletions, and reconnects passes on disposable data.

### M5 — Shareable release

- Document supported Ubuntu/Nautilus/rclone versions and tested filesystems.
- Supply install, upgrade, disable, uninstall, troubleshooting, and recovery instructions.
- Add CI for status/backend tests and package validation; record manual desktop checks separately.
- Select a license before describing the project as open source or distributing a release.
- Publish an initial experimental release with clear limitations. Do not claim full Windows feature parity.

## Development and deployment boundaries

- Start with fixture-only work and read-only badges. Keep production OneDrive integration disabled by default in development builds.
- Use the dedicated empty Microsoft account, a test remote/root, and disposable files for integration tests. Enforce both the expected drive identity and the allowed root in the helper, not just in menu visibility. Keep test services and storage separate from the working personal OneDrive connection.
- Do not use a general destructive sync command for cache management. Do not add a cloud-delete feature in the initial release.
- An unprivileged extension still has access to the user's files; running without root does not eliminate data-loss risk.
- Install extension code for the current user. System packages may require a separate administrator step; never replace GNOME, the kernel, boot settings, or filesystem drivers for this project.
- Separate background work from Nautilus callbacks. Bound requests, handle cancellation, and fall back to unknown state on failure.
- Keep tokens, real file listings, private paths, personal logs, and machine configuration out of commits and CI artifacts.
- Build and verify uninstall before enabling the first desktop prototype. Save the pre-test mount configuration privately outside Git.

## Validation matrix

| Area | Minimum check |
| --- | --- |
| Status truth | Complete, partial, empty, stale, pinned, dirty, and missing local files. |
| Local storage | NTFS3 behavior, allocated bytes, large files, full disk, disk absent, and reconnection. |
| Network | Offline before opening, connection loss during download/upload, retry, cancellation. |
| File identity | Unicode, spaces, case differences, renames, symlinks, and path traversal attempts. |
| Data protection | Cloud copy unchanged by local eviction; unsent edits retained; conflicts preserve both copies. |
| Desktop | GNOME Files 50 badges, state refresh, large folders, helper failure, and reinstall/uninstall. |

## User participation

See [YOUR_CHECKLIST.md](YOUR_CHECKLIST.md). No software installation or changes to the working OneDrive connection are needed to begin fixture-based development.

## References

- [GNOME Nautilus InfoProvider](https://gnome.pages.gitlab.gnome.org/nautilus-python/class-nautilus-python-info-provider.html)
- [GNOME Nautilus MenuProvider](https://gnome.pages.gitlab.gnome.org/nautilus-python/class-nautilus-python-menu-provider.html)
- [rclone VFS cache modes](https://rclone.org/commands/rclone_mount/#vfs-file-caching)
- [rclone per-file cache status feature request](https://github.com/rclone/rclone/issues/8779)
- [Timeshift scope and exclusions](https://github.com/linuxmint/timeshift#user-data-is-excluded-by-default)
