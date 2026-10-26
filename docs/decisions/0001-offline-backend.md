# ADR 0001 — Own a read-only offline store; use rclone for transport and presentation

Date: 2026-10-19. Status: **accepted for M1 implementation**. Direction **C** in
PLAN.md. This is an experimental architecture, not permission to replace the
daily test mount or ship offline controls.

## Decision

CloudMarks will own the evidence for complete offline files and persistent pin
intent. It will use supported rclone operations to obtain content and a
**read-only, local-first union view** to open complete copies through the same
experimental path online and offline. The offline store is separate from the
rclone VFS cache. Both union upstreams are read-only and VFS data caching is off.

This is more than a badge extension, but substantially less than a two-way sync
client. The initial product supports offline viewing. It has no upload, remote
delete, automatic merge, or offline editing feature. The storage design and its
review are recorded separately in [the offline-store design](../dev/offline-store-design.md).

## Existing tools review (M0.2)

This review checked official documentation and tagged source. Only the rclone
experiments below were executed against the test account. “Not established”
means no supported capability was verified, rather than proof it cannot exist.

| Tool | On-demand / offline behavior | Pinning, eviction, and state | Files integration | Maintenance / license / decision |
| --- | --- | --- | --- | --- |
| rclone 1.75.2 | Streaming and full VFS caching; ordinary cache fails the restart test below. | Supported RC has aggregate stats and upload queue, not a verified per-file pin/evict contract. | FUSE mount; no built-in CloudMarks badges. | Installed release and tagged source reviewed; MIT. Use transport + read-only union, not cache internals. |
| jstaf/onedriver 0.15.0 | Documented on-demand filesystem; cached files available read-only offline. | Tagged CLI/source has no established external pin/evict/status service. Internal `HasContent` includes incomplete/open cache files. | Ordinary FUSE paths; no status-provider integration established. | Release includes authentication/WebKit updates; GPLv3. Learn from it; reconsider upstream work if a supported state/control interface emerges. |
| GNOME Online Accounts / GVfs 1.60.0 | Native OneDrive access; tagged `open_for_read` calls the Graph download service. | Durable offline pin/evict/state contract not established in backend source. | Native account and Files locations, not proof of offline support. | GNOME tagged source; LGPL-2.0-or-later. Keep as an online-access reference. |
| abraunegg/onedrive 2.5.11 | Download-only and selective sync; released README explicitly lists Files On-Demand as missing. | `sync_list` chooses a local subset; pin/evict semantics for a placeholder view and a supported badge-state API not established. | Local sync folder; equivalent on-demand Files experience not established. | Current release/source reviewed; GPLv3. Useful alternative if selective sync becomes the goal. |
| Insync | Vendor documents selective sync, one-way/two-way modes and external-disk destinations. | Supported placeholder/pin/evict/status API for CloudMarks not established. | Desktop/file-manager workflows are a behavior reference; GNOME 50 extension compatibility untested. | Commercial per-account licensing; full terms could not be loaded. Current vendor site/downloads reviewed. Learn from selection/progress UX; no purchase or adoption. |

Primary sources:

- rclone: [VFS caching](https://rclone.org/commands/rclone_mount/#vfs-file-caching),
  [RC API](https://rclone.org/rc/), [per-file status request](https://github.com/rclone/rclone/issues/8779),
  [tagged union source](https://github.com/rclone/rclone/blob/v1.75.2/backend/union/union.go),
  [license](https://github.com/rclone/rclone/blob/v1.75.2/COPYING).
- onedriver: [README](https://github.com/jstaf/onedriver/tree/v0.15.0),
  [CLI manual](https://github.com/jstaf/onedriver/blob/v0.15.0/pkg/resources/onedriver.1),
  [content cache](https://github.com/jstaf/onedriver/blob/v0.15.0/fs/content_cache.go),
  [release](https://github.com/jstaf/onedriver/releases/tag/v0.15.0),
  [license](https://github.com/jstaf/onedriver/blob/v0.15.0/LICENSE).
- GVfs: [read path](https://github.com/GNOME/gvfs/blob/1.60.0/daemon/gvfsbackendonedrive.c),
  [version/license](https://github.com/GNOME/gvfs/blob/1.60.0/meson.build).
- abraunegg: [released README](https://github.com/abraunegg/onedrive/blob/v2.5.11/readme.md),
  [usage](https://github.com/abraunegg/onedrive/blob/v2.5.11/docs/usage.md),
  [release](https://github.com/abraunegg/onedrive/releases/tag/v2.5.11),
  [license](https://github.com/abraunegg/onedrive/blob/v2.5.11/LICENSE).
- Insync: [features](https://www.insynchq.com/syncing-superpowers),
  [Linux downloads](https://www.insynchq.com/downloads/linux),
  [licensing](https://www.insynchq.com/pricing).

## Experiments (M0.3)

The fixture scripts are under [spikes/backend-feasibility](../../spikes/backend-feasibility).
Private credentials, identifiers, raw diagnostics, and machine paths remain
outside Git. Public results contain fixture hashes and assertions only.

1. **Ordinary VFS cache is insufficient.** A disposable HTTP origin served a
   complete file through `--vfs-cache-mode=full`. The complete file opened after
   the origin was stopped, but failed after restarting the mount with that
   origin still unavailable. Cache age was extended for this test. See
   [result](../evidence/m0/rclone-vfs-cache.json). This reproduces a restart gap;
   it is not a universal assertion about every possible rclone configuration.
2. **Explicit store + union works in a local fixture.** A complete local file
   took priority in a read-only union and remained readable across restart
   without the HTTP origin. See [result](../evidence/m0/rclone-http-union.json).
3. **Actual OneDrive test account.** Authenticated profile and drive matched
   the dedicated account; the recorded personal drive was rejected. A unique
   disposable folder was created, and its identity/parent were checked. The
   clean run provisioned its fixture directly by Graph folder ID, downloaded
   and checked all 102,400 bytes against the known SHA-256, then opened it
   through the experimental union mount. Two new mount processes opened the
   same path with HTTP(S) access denied. Four write operations returned EROFS:
   modification, creation, rename, and deletion. Local eviction occurred only
   after unmount; a fresh remote read and metadata check confirmed identical
   content, item identity, and revision. See [result](../evidence/m0/onedrive-union.json).

The OneDrive failure simulation used a loopback proxy that rejects requests,
set only in the test processes' environments. A direct remote-read canary
failed and the proxy recorded rejected requests. The host stayed online.
Starting FUSE inside a network namespace was prevented by the host's AppArmor
policy; that policy was not disabled or bypassed. Physical network
disconnection, logout/login, and disk unplug/replug remain manual integration
checks before everyday use.

### Setup error and correction

An initial experiment used a raw Graph folder ID as rclone's `root_folder_id`.
rclone uses a normalized **driveID#itemID** identity; a raw ID can cause path
operations to fall back to the drive root. One disposable fixture was written
at the dedicated test account's root. Its content was independently verified,
then it was moved into its allowed disposable folder with an ETag precondition.
No personal account was used and no cloud file was deleted.

The experiment was corrected to use normalized IDs, with provisioning addressed
directly to the verified Graph folder. A separate clean end-to-end run passed
without that setup error. Neither a config string nor a visible mount root is
sufficient root-containment enforcement: the implementation must validate
identity, relative paths, and operation destinations itself.

## Why this direction

- **A rejected:** ordinary rclone VFS controls do not provide the verified
  durable per-file state/control contract required by this project.
- **B not selected:** no reviewed replacement supplied all required state,
  pinning, eviction, and integration controls without further backend work.
  onedriver remains the most relevant comparison, not a proven replacement.
- **C selected:** an owned complete-file store passes the controlled
  same-path/restart/eviction experiment using documented rclone building blocks.
  Read-only scope avoids an implicit two-way sync project.
- **D unnecessary for now:** offline viewing is feasible in the tested scope.

## Limits and follow-up

This was a small-file feasibility test on the selected NTFS3 disk. It does not
prove large-file behavior, storage-budget enforcement, path safety, concurrent
updates, helper responsiveness, or recovery under disk failure. M1 must test
state truth and containment with artificial data. M3 must implement verified
publication, atomic updates to the visible tree, space accounting and recovery
before enabling any real menu action. Stale local copies can shadow newer
remote content in a local-first union; the helper must detect that condition
and never present it as a verified current copy.

A separate [allocation probe](../evidence/m0/filesystem-allocation.json) confirmed
the filesystem constraint: a truncated 64 MiB fixture allocated 64 MiB on the
selected NTFS3 disk, compared with zero allocated bytes on tmpfs. A complete
1 MiB file allocated 1 MiB on both. All allocation fixtures were removed. This
supports complete-file storage with explicit accounting; it does not justify
assuming VFS sparse-cache efficiency on this disk.

The local union probe also lists only the retained local fixture after offline
restart: its remote-only fixture is absent. A failed remote listing must not be
interpreted as cloud deletion. Preserving a complete cloud-only placeholder
catalog while offline is not provided by this initial union design.

Existing test mount/service remain in place. No daily-desktop extension or
CloudMarks service is enabled. The disposable cloud fixtures were retained in
their experimental folders; private run records identify them for later cleanup.
