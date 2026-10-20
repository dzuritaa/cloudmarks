# Preliminary backend research

Historical first pass. Superseded by the completed source review and experiments
in [ADR 0001](decisions/0001-offline-backend.md). The final M0 decision selects an
owned offline store with rclone transport/read-only union, rather than adopting
onedriver without a supported state/control interface.

Reviewed: 2026-10-19. Documentation review only; no candidate was installed or connected to an account during this review. M0.1, the complete M0.2 comparison, and M0.3 experiments remain outstanding. This is not the final backend decision document.

## Working recommendation

Keep rclone as the baseline and investigate **jstaf/onedriver** as the first replacement candidate. Its documented on-demand access and read-only offline behavior fit the accepted initial scope. That is a reason to test it, not evidence it passes our pinning, eviction, status, or restart requirements.

The user accepts offline viewing as the first usable version and delegates replacement research to implementation work. Offline editing and two-way synchronization are deferred. No backend has been selected or replaced.

## rclone: baseline, feasibility unproven

The [mount documentation](https://rclone.org/commands/rclone_mount/#vfs-file-caching) describes read caching in `full` mode, eviction based on cache age/space, and sparse files. The current `writes` mount is not a durable read cache. Global cache retention settings do not establish per-file pinning. The existing NTFS3 allocation observation remains a reason to test actual disk usage before changing cache mode.

The [RC documentation](https://rclone.org/rc/#vfs-stats-stats-for-a-vfs) provides aggregate cache statistics and upload queue information. `vfs/forget` invalidates directory metadata; it is not a local-content eviction control. The [per-file cache status request](https://github.com/rclone/rclone/issues/8779) remains open at review time. No documented per-file pin/evict interface was established in this review.

Next: check the installed version's supported controls, then demonstrate complete download, offline reads after process restart, persistent retention, and clean local eviction. Do not infer completion from file size or manipulate a running cache directly. Failure means investigating another backend, not fabricating a green badge.

## jstaf/onedriver: first replacement candidate

The [project README](https://github.com/jstaf/onedriver) describes a FUSE filesystem that downloads files on access, caches content and metadata on disk, and makes previously opened files available read-only when offline. Write access returns when networking returns, so read-only offline behavior alone does not prevent online uploads. It also warns that file-browser thumbnails can trigger downloads.

The [release history](https://github.com/jstaf/onedriver/releases) lists v0.15.0 as the latest release, including authentication and WebKit updates; this provides evidence of maintenance, not Ubuntu 26.04 compatibility. The repository identifies GPLv3 licensing.

Not established: persistent per-file pins, safe individual eviction, a supported external status interface, restart behavior without networking, disk-budget enforcement, and compatibility with this machine/filesystem. The [cache FAQ](https://github.com/jstaf/onedriver/wiki/Troubleshooting-and-FAQs) describes resetting a mount to reclaim space rather than an individual-file eviction API and refers to an older cache layout, so it cannot settle current behavior.

Next: inspect current tagged source and packaging; test with isolated disposable data only after the M0.1 presentation check. Do not adopt it until all required controls are demonstrated.

## abraunegg/onedrive: selective-download alternative

The [README](https://github.com/abraunegg/onedrive) documents download-only operation and explicitly lists Windows-style Files On-Demand as missing. The [usage guide](https://github.com/abraunegg/onedrive/blob/master/docs/usage.md) documents filtering with `sync_list`. This could support a selected local subset for offline viewing, but does not establish the intended cloud-only placeholders in the same Files folder.

The [releases page](https://github.com/abraunegg/onedrive/releases) lists v2.5.11 as latest at review time. It also mentions work supporting on-demand development; that is not evidence the released feature is available.

Next: consider it if a selective-download experience proves preferable to a mounted on-demand filesystem. Verify per-file selection, state exposure, and local-removal semantics before integrating. Do not treat ordinary synchronization deletion as cache eviction.

## GNOME Online Accounts / GVfs: review incomplete

The upstream GitLab source page failed to load during this review; the [GNOME GitHub mirror](https://github.com/GNOME/gvfs/blob/master/daemon/gvfsbackendonedrive.c) is available. No durable offline pinning, eviction, or authoritative per-file offline-state contract was established. Native desktop account integration alone is insufficient evidence. Finish the source/API review before ranking this backend.

## Insync: commercial behavior reference

The vendor's [feature documentation](https://www.insynchq.com/syncing-superpowers) describes selective synchronization, one-way/two-way modes, external-disk destinations, and conflict handling. It does not establish an on-demand placeholder filesystem or a supported interface for CloudMarks badges and pin/evict controls. Use its selection and progress workflows as references; this review does not recommend purchasing or adopting it.

## Required evidence before choosing

For rclone and each serious replacement candidate, record the exact version and demonstrate verified complete content, read-only offline opening through the intended Files path, restart persistence, persistent pin intent, local-only eviction, and disk-full/removal behavior. Also establish how CloudMarks obtains authoritative state without blocking Nautilus. Preserve the account-identity and disposable-root boundaries from PLAN.md.

If none passes, document the remaining gaps and recommend a revised scope. A separate local-copy action must be labeled accurately. Owning an offline store or adding two-way sync requires its own design decision rather than an implicit expansion of this first version.
