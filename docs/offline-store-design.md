# Read-only offline-store design and M1 review

Status: accepted for the fixture-backed M1 engine after
[ADR 0001](decisions/0001-offline-backend.md). M3 now implements real CLI controls and a read-only view; see
[the M3 review and evidence](m3-offline-controls.md). M4 adds [explicit change tracking and bounded recursive intent](m4-change-tracking.md). Nautilus actions remain deferred. The M0 spike is not production code.

## Responsibilities

CloudMarks owns persistent pin intent, complete-file verification and revision
evidence. rclone transports bytes and exposes a read-only union view with the
managed local tree first and the isolated OneDrive root second. Use ordered
`epall` search, both upstreams marked `:ro`, mount `--read-only`, and
`--vfs-cache-mode=off`. Do not alter or infer state from rclone's VFS cache.

The helper must enforce drive identity and configured Graph root on every
operation. Normalize native rclone folder identities deliberately as
`driveID#itemID`; never infer containment from that option alone. Keep credentials
only in private adapter configuration. The M1 backend is artificial and cannot
read credentials or perform network requests.

## Identity and revision

Use `(drive_id, item_id)` as the stable file identity. Keep the allowed parent
root identity separately. Paths are display/open locations, not identity. Keep
opaque IDs and revisions intact; only hexadecimal personal-drive IDs have an
explicit case-insensitive canonicalization rule. A rename must not create a
second file identity or silently adopt an unrelated path.

Each completed local record includes remote revision, byte length, local
SHA-256, storage-relative object path, and completion evidence. A local hash
alone is not proof of the corresponding remote revision. A real adapter must
validate a remote checksum (for example OneDrive QuickXorHash), or independently
verify the remote content, and recheck revision before publication. M1 fixtures
provide a known expected SHA-256 to exercise this boundary.

## Files and state writes

The engine opens its configured store through directory file descriptors. Reject
absolute paths, `..`, empty components, symlinks, non-regular files and hard-linked
content; resolve descendants without following links. A disk marker identifies
the expected store, so an empty mountpoint on the wrong filesystem cannot become
a valid offline disk. M3 adds the production device/mount lifecycle checks.

Write candidates to unique temporary files on the selected disk. Verify length,
checksum and revision, fsync, and publish immutable version objects. Commit the
state reference transactionally only after the object is complete. Do not replace
the previous complete object on a failed transfer or a failed state write. An
uncommitted orphan is not a completed file and may be collected later by a
separate, narrowly scoped recovery operation.

SQLite provides transactional metadata in M1. Its transaction is not atomic
with the filesystem: object publication precedes metadata commit, so failure
leaves an orphan rather than a record pointing at partial data. Tests must
inject a failure between those two steps and verify the previous state survives.

The visible union tree and immutable version objects are separate concerns.
M3 must atomically publish the intended file into the visible tree and confirm
that the read path serves that same version before issuing an offline badge.
M1 models this proof through its fixture backend; it does not publish a real
cloud view or claim that a raw stored object makes the cloud path available offline.

## State truth

Unknown is the default when the disk, helper evidence, revision or read-path
verification is missing/stale. A partial download, a file's apparent size, a
manifest row, or pin intent by itself cannot produce a green check. Rehash
completed fixture objects in the helper before returning local state and
compare pre/post file identity and timestamps to catch concurrent writes.
Nautilus never performs that work.

Tracked operations produce transferring state; failed operations remain
actionable errors and retain old complete data. Modified local objects are
errors/conflicts, never eviction candidates. Confirmed remote deletion or a
new revision invalidates current-copy success; retain old recoverable data.
Without fresh remote evidence, an offline snapshot can remain readable while
the badge says it needs checking. Explain the last verified revision rather
than promising it is the latest cloud version.

Read-only behavior applies online as well as offline in the first experimental
view. Application save/create/rename/delete operations must fail, rather than
silently enqueue an upload. Explicit “Save a copy” outside that view is an
application operation, not synchronization.

When the remote listing is unavailable, the initial union can list only its
local tree after restart. Missing remote-only entries are not evidence of
deletion. The helper must retain its last observations as stale metadata;
offline placeholder browsing of the entire cloud tree remains outside this
first design.

## Recovery and eviction

Missing marker/disk, mismatched root/account, failed integrity checks or unknown
schema are fail-closed conditions. Reopen state after process restart and
revalidate bytes; do not promote a pending transfer to complete during recovery.
Retain both old and candidate data on ambiguous failures.

M3 eviction targets only a verified clean local object/tree entry after a fresh
remote-revision check, with open-file behavior explicitly defined. Unpinning is
separate from eviction. Neither operation deletes a mounted path or invokes a
remote delete/sync command. The M1 engine deliberately has no eviction API.

## Review outcome

The design keeps the initial read path read-only, makes local content evidence
explicit, and preserves previous data across interrupted transfers and metadata
writes. Its primary risks are stale local content shadowing remote content,
the filesystem/SQLite transaction gap, unsafe paths, and disk replacement. M1
acceptance tests target those risks. M3 stays gated until visible-tree
publication, quotas, cancellation, real checksum support and desktop recovery
are implemented and tested. This review authorizes the status engine and
artificial-backend tests only; it does not enable real transfers on the desktop.

## M3 implementation review

The M3 result records the implemented journal/rollback protocol, independent object and visible copies, allocated-byte accounting, physical UUID/device and state/view binding, effective transport identity checks, and normal-unmount requirement. Real test-account and failure-injection gates passed. Controls stay in the CLI; the M2 status socket and adapter remain read-only. This implements the narrow individual-file viewing scope, with offline mode using a credential-free local alias at the same view path. Mounted updates, automatic history cleanup, broader lifecycle checks and synchronization remain outside this implementation.
