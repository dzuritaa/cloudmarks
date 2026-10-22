# File/folder controls and larger libraries

## Outcome and acceptance

Add scoped Nautilus **Keep offline** and **Free local copy** actions for files and
folders, a separate progress window with cancellation, and truthful folder badges.
All work remains on the configured OneDrive test account/root. File and directory
names, credentials and response bodies stay out of diagnostic logging.

The current deployment has schema 4, two retained files and no unresolved issues.
The existing view is read-only and publication/eviction requires a normal unmount.
Status IPC remains read-only. The controls must preserve those boundaries.

- Menu construction and activation perform no filesystem, socket or cloud work in
  Nautilus callbacks. A separate process validates the private control profile.
- Controls offer JSON progress, explicit exit codes and `--dry-run`. They serialize
  operations, stop only the configured view service, refuse busy/foreign mounts,
  and restore the previous online/offline view after success, failure or cancel.
  A busy view produces an actionable retry message, never a forced unmount or
  automatic closure of unrelated Files windows.
- Keep enumerates a folder consistently, preserves pin intent, downloads/verifies
  current files and publishes supported empty directories. Cancellation retains
  completed work and previous versions. Applying Keep again resumes/rechecks it.
- Free removes only verified local copies after fresh matching cloud evidence.
  It never deletes cloud files or unresolved/dirty versions. Other direct/folder
  pin sources survive; results explain files retained by other selections.
- Folder progress distinguishes cloud-only, downloading, partially retained,
  verified complete, and error/checking. Green requires current remote membership,
  matching versions and verified local bytes/read paths for every member. Empty
  folders need a verified local directory. New/changed cloud members invalidate
  complete-folder evidence until Keep is applied again; there is no silent
  background publication through a mounted view.
- Replace the observer's whole-library 128-entry scan with bounded observations
  of requested paths. Use read-only Graph batches and bounded memory/backoff;
  a large unrelated directory must not invalidate visible file badges. Recursive
  controls support up to 10,000 entries per selection, with bounded depth/pages
  and cancellation. This is a resource bound per operation, not a library-wide
  128-file limit. Retained-path lookup and online restart checks must work beyond
  128 files without reading every retained row on each status request.

Automated acceptance covers >128 files, nested/empty folders, overlapping pins,
scope escapes, cancellation, busy/failed mount restoration, changed membership,
corruption, no-cloud-delete behavior, and immediate UI callback return. Validate
the installed package, native menu/badges and a real keep/free folder cycle in the
existing test profile. User reboot acceptance of the previous version is already
reported; these changes require their own startup smoke check.

## Impact and rollout

Keep account bindings and existing settings compatible. Opt-in control settings
name one managed user view service. A small state-schema upgrade may add an
indexed normalized retained path lookup; no existing copy or recovery version is
discarded. Back up stopped state **and offline store** before deploying a schema
change. Old code must refuse a newer schema rather than mutate it. Rollback uses
the paired stopped backup, preserving later data separately for review.

Background observers do not publish bytes or change pin intent. Folder proof work
runs outside the status request/UI path. Local-only mode remains supported; loss
of fresh cloud evidence can change badges to checking without removing bytes.
This work adds neither offline edits nor two-way synchronization and does not
connect the user's important OneDrive account.
