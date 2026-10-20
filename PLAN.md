# CloudMarks project plan

Status: M0–M5 experimental implementation and MIT prerelease v0.1.0a1 are delivered. Hosted CI and published-asset verification pass. Physical reconnect/logout and daily-use acceptance remain open; no daily-desktop extension or CloudMarks service is installed.

## Recorded M0 outcome

- [Badge presentation](docs/m0-presentation.md): real Nautilus 50 rendered fixture emblems in icon/list views, both appearances and two zoom levels; asynchronous completion, refresh and removal were exercised in an isolated desktop.
- [ADR 0001](docs/decisions/0001-offline-backend.md): select direction **C**, an owned verified offline store with rclone transport and a read-only local-first union view. Ordinary VFS caching failed the tested offline-restart scenario.
- [Storage design review](docs/offline-store-design.md): M1 may implement transactional state, stable identity and evidence checks against an artificial backend. Real transfers, visible-tree publication and offline controls remain M3 work.
- The clean OneDrive test passed same-path reads after two process restarts under a rejecting proxy, read-only operations and local eviction without remote change. Physical network loss, logout and disk reconnection still need integration checks before daily use. See the ADR for the initial setup error, its correction, and experiment limits.

## Validation account setup

Account isolation is configured and the dedicated test OneDrive mount is active. All CloudMarks cloud validation and integration tests will use the user's separate Microsoft test account, initially empty and reserved for disposable test data.

- The test account email and verified drive identifier are stored in private local configuration, outside this public repository.
- The personal account has been disconnected from Ubuntu's OneDrive mounts and Online Accounts; its saved local connection credentials were removed without deleting cloud files.
- Test credentials, mount, cache, and service configuration are separate from the retired personal connection. The test mount checks its configured drive identity and rejects the recorded personal drive identifier.
- The test mount currently uses files on demand with a writes cache. It does not download the whole account or provide the project's planned permanent offline controls.
- Cloud tests must use disposable files in the allowed test area. The M3 transport verifies the dedicated account, Graph ancestry, effective rclone file identity and checksums. The M2 adapter remains display-only; actions use the separate CLI. An active mount is not evidence of verified CloudMarks offline availability.

Do not reconnect the personal account for project validation. Any later account switch requires an explicit configuration change and renewed identity verification.

## Outcome

In GNOME Files, show whether a OneDrive file is cloud-only, saved locally, transferring, or in an error state. Eventually offer **Keep offline** and **Free up local space** without confusing removal of a local copy with deletion from OneDrive.

The first target is GNOME Files 50 on Ubuntu, using one OneDrive account through rclone and an explicit complete-file offline store. Account names, mount paths, credentials, and disk identifiers must be configuration, never hard-coded project defaults.

### Confirmed starting scope

- Offline viewing is an acceptable first usable version. Offline editing, queued uploads, and two-way synchronization are deferred; they are not prerequisites for the first release.
- The offline open path must enforce read-only behavior and be tested as such. An ordinary application must not be able to silently edit a managed offline copy that CloudMarks cannot synchronize or safely preserve.
- Backend selection is an engineering investigation. If rclone fails the feasibility gate, evaluate replacements and recommend one based on evidence; the user does not need to choose a backend in advance. Building a new sync client is not the automatic fallback.
- [Preliminary backend research](docs/backend-research.md) records documentation findings and outstanding experiments. It does not select the M0 backend or complete M0.2/M0.3.

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
3. **Storage adapter:** use the owned-store/read-only-union mechanism selected in ADR 0001. Mock transport and supported-read-path evidence for M1. Do not reuse the disposable spikes as production adapters.
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

### M0 — Feasibility spikes and backend decision

M0 decides what kind of project CloudMarks is. If rclone cannot support verified offline copies, evaluate existing backends before considering an owned offline store, and revise the plan before M1 begins. The initial offline-viewing scope does not require building a two-way sync client. Run the three parts in order; the first two are cheap and can change the scope of the third. Preliminary documentation research may inform those spikes without counting as a completed milestone.

#### M0.1 — Badge rendering spike

Timebox: one working session. Disposable code; do not reuse it in M2.

- Write a minimal `InfoProvider` that assigns hard-coded emblems to files in a disposable local folder. No helper, no cloud, no OneDrive mount.
- Check readability in icon view and list view, at different zoom levels, and in light and dark styles. Record which emblem names the installed icon theme provides.
- Confirm that `update_file_info_full` can complete asynchronously and that `invalidate_extension_info` refreshes visible items.
- If emblems are unreadable or absent in a view, evaluate a `ColumnProvider` status column or another documented API before committing to the M2 design.
- Install under the user's Nautilus Python extension directory. Removal is deleting that file and restarting Files with `nautilus -q`.

**Output:** a short note listing the presentation that works on GNOME Files 50, with screenshots taken against disposable files only.

#### M0.2 — Existing tools review

Timebox: half a day. Verify each capability directly; this plan does not assume any of them.

- Review onedriver, the GNOME Online Accounts OneDrive backend, the abraunegg `onedrive` client, rclone's own per-file status work, and at least one commercial client as a behavior reference.
- For each, record on-demand download, pinning, local eviction, how state is exposed, Nautilus integration, maintenance activity, and license.
- Decide whether CloudMarks should build on, contribute to, or only learn from each tool.

**Output:** a comparison table in the design decision document.

#### M0.3 — Backend feasibility

- Create a local fixture tree and a disposable remote test area in a dedicated, otherwise empty Microsoft account.
- Give the test account its own credential configuration, remote name, mount, cache, state store, and user service. Do not reuse the personal account's tokens or default rclone configuration.
- Before any cloud test, verify the authenticated drive identity against the explicitly configured test drive and enforce the allowed test root. Refuse a mismatch or a missing identity; never fall back to the personal account. Keep real account and drive identifiers out of Git.
- Investigate supported rclone status/retention controls and the actual read path for complete cached files.
- Compare possible offline storage approaches, including filesystem limitations and local-space accounting. Check that complete files open through the same user-visible path with networking unavailable.
- Verify that the supported offline path rejects writes for the initial viewing-only scope, including application save/replace operations on the selected filesystem. A backend that automatically enables uploads on reconnect needs an explicit integration design before use.
- Specify how remote edits, local edits, deletions, and disk removal affect each state.
- Record the chosen mechanism, supported operations, and limitations in a design decision document.

**Gate:** a backend experiment demonstrates complete download, offline read through the intended path, restart persistence, and local eviction without remote deletion. A separate “open offline copy” action must be labelled as such, not presented as Windows-equivalent behavior.

#### M0 decision

The design decision document selects one direction and states why the others were rejected:

| Direction | When it applies | Effect on the plan |
| --- | --- | --- |
| A. rclone-backed extension | Supported rclone controls pass the M0.3 gate. | Continue with M1–M5 as written. |
| B. Build on an existing tool | M0.2 finds a maintained tool that already manages offline state. | CloudMarks provides Files integration for that tool, or the work goes upstream. Rewrite M1–M4 around its state interface. |
| C. Own offline store | No existing backend passes, and offline controls remain the goal. | CloudMarks owns durable content and revision tracking. Re-scope M3 with a separate design review covering read-only access, updates, and recovery. Two-way sync remains deferred and requires a further decision. |
| D. Read-only status | Offline controls are not feasible at acceptable risk. | Ship only badges for state the backend reports authoritatively. Drop M3–M4 and document the limitation. |

An inconclusive result is recorded as such and is not treated as direction A.

### M1 — Status engine and automated checks

Requires the M0 decision. The state store and file identity model depend on the chosen backend.

Implemented: see [the M1 result](docs/m1-status-engine.md) and its validation record. The adapter and read-path proof are artificial; no real cloud actions are enabled by this engine.

- Implement configuration validation, root containment, a stable file identity model, and the status contract.
- Test state transitions with fixture data and an artificial transfer backend; no Microsoft credentials required.
- Cover partial downloads, stale revisions, errors, missing disks, symlink/path escapes, and interrupted state writes.

**Gate:** partial or stale files never become “saved locally”; operations cannot escape the configured test root; failed transfers preserve existing data.

### M2 — Read-only Nautilus prototype

Implemented: see [the M2 result, screenshots and installation instructions](docs/m2-nautilus-prototype.md). The actual helper/adapter passed isolated desktop checks; real OneDrive state and actions remain disabled.

- Add cloud, local, transfer, error, and unknown badges using the presentation chosen in M0.1, with text descriptions where the API permits.
- Read state from the helper; do not perform transfers or automatically hydrate files when browsing.
- Refresh visible badges after state changes without scanning the entire drive.
- Provide a user-only installer, a working uninstaller, and a way to disable the extension independently of the OneDrive mount.

**Gate:** badges match fixture state; large folders and an unavailable helper do not freeze Files; unrelated folders receive no badges; uninstall restores baseline Files behavior. Validate visually in a separate desktop session or VM before the daily desktop.

While M0.3 is in progress, only backend-independent work may start: configuration validation, root containment, and path-escape tests. Do not build the state store, file identity model, or helper interface until the M0 decision is recorded. Fixture-backed badges are not proof that real offline storage works.

### M3 — Individual-file offline controls

Requires the M0 backend decision and successful offline tests.

Implemented as [experimental CLI controls with the M2 adapter kept read-only](docs/m3-offline-controls.md). Real test-account checks passed checksum verification, two offline restart reads, read-only operations and safe local eviction. Publication/eviction require the experimental view unmounted; mounted actions and context menus are deferred.

The first usable offline version targets viewing only. Do not add offline editing or automatic uploads as part of this milestone.

- Add **Keep offline**, cancellation, retry, and explicit progress for individual files.
- Download to temporary files, validate content, then atomically publish the complete copy.
- Add **Free up local space** only for a verified clean copy already present remotely. It must never call a remote delete operation or remove the mounted file to achieve eviction.
- Refuse eviction of unsent edits, active transfers, or unverified copies. Handle open-file behavior explicitly.
- Enforce a configurable storage budget and a minimum free-space reserve. Refuse new work cleanly when the disk is unavailable or full.
- Recover completed offline copies and pin intent after restart, and mark missing disks or stale/unverified content accurately. These viewing-only recovery requirements belong to M3 even though broader synchronization recovery is deferred to M4.

**Gate:** a pinned file opens offline after restart; clearing its local copy leaves the cloud file intact; cancellation and disk-full scenarios retain recoverable data.

### M4 — Change tracking and recovery

Implemented for the read-only viewing scope at the user’s request: see [M4 behavior, matrix and limits](docs/m4-change-tracking.md). M2 remains status-only. Explicit retries and repeated bounded manifests replace an automatic sync daemon. Physical network/disk reconnection and desktop logout/login acceptance remain open. Offline editing, uploads, and two-way synchronization still need a separate scope decision before implementation.

- Detect remote revisions and deletions without silently overwriting unsent local work.
- Preserve both versions on conflicts; show an actionable state instead of inventing automatic merge behavior.
- Recover queued work after network loss, process restart, logout, and disk reconnection.
- Define unpinning separately from cache eviction and remote deletion.
- Add recursive folder pinning and aggregate folder states only after individual-file behavior passes.

**Gate:** a documented matrix of local/remote edits, renames, deletions, and reconnects passes on disposable data. Automated fault/restart and scoped cloud results are recorded separately from the pending physical reconnect/logout checks; do not treat those as completed.

### M5 — Shareable release

[Experimental MIT prerelease v0.1.0a1](https://github.com/dzuritaa/cloudmarks/releases/tag/v0.1.0a1) is published. It includes
credential-free CI, package/source-rebuild checks, compatibility and operating/
upgrade/recovery guides, an optional status-helper unit and a tag-only release
workflow. Main and exact-tag hosted CI passed; downloaded release checksums,
license and source contents were independently verified. See [M5 evidence](docs/evidence/m5/validation.json).
Physical daily-use acceptance remains separate and open.

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

See [YOUR_CHECKLIST.md](YOUR_CHECKLIST.md). The M0.1/M2 desktop tests used locally extracted `python3-nautilus`, typelibs and Xvfb inside an isolated session; no system package installation or daily Files restart occurred. The working OneDrive connection was not changed by M1–M2. Backup steps are tied to the milestone that introduces the corresponding risk.

## References

- [GNOME Nautilus InfoProvider](https://gnome.pages.gitlab.gnome.org/nautilus-python/class-nautilus-python-info-provider.html)
- [GNOME Nautilus MenuProvider](https://gnome.pages.gitlab.gnome.org/nautilus-python/class-nautilus-python-menu-provider.html)
- [rclone VFS cache modes](https://rclone.org/commands/rclone_mount/#vfs-file-caching)
- [rclone per-file cache status feature request](https://github.com/rclone/rclone/issues/8779)
- [Timeshift scope and exclusions](https://github.com/linuxmint/timeshift#user-data-is-excluded-by-default)
