# ADR 0002 — Optional live updates of the mounted offline view

Date: 2026-10-26. Status: **accepted as an opt-in setting**.

## Context

Keep offline and Free local copy stopped the read-only view service, changed
the store, and started the view again. A normal unmount fails while any Files
tab or application uses the view, so users had to close windows and click Retry.
This was the main daily-use friction reported during the test-account trial.

Publication and eviction are already atomic renames inside the store, and the
view mounts with a one-second directory cache and no VFS data cache.

## Evidence

The [live view probe](../../spikes/live-view/README.md) published and evicted
8 MiB files under mounted online (union) and offline (local-only) views on NTFS3
and tmpfs. Changes appeared within about one second. Readers that already had a
file open finished with complete, unmixed bytes of the version they opened, and
normal unmounts stayed clean.

## Decision

Control profiles accept `"live_updates": true`. With it, controls keep the view
mounted. They still refuse to run when anything other than the attested
CloudMarks read-only mount occupies the view folder, and every existing
integrity check remains in place. Without the flag, the unmount workflow is
unchanged.

The unmount workflow's busy message now names the applications that hold files
in the view, so users know what to close.

The flag stays opt-in for one release. `cloudmarks setup` offers it as an
experimental choice.

## Consequences

- With live updates, a freed file in the online view falls back to streaming
  from OneDrive on the next open; in the offline view it disappears.
- The probe used a local stand-in for OneDrive. Before making live updates the
  default, run Keep and Free on the real test account with the view open, and
  record the result.
- With live updates, the automatic recovery step at the start of each control
  also runs under the mounted view; it uses the same atomic renames. The separate
  `cloudmarks offline recover` command still requires an unmounted view.
