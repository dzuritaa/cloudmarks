# M4 — Explicit change tracking and recovery

M4 extends the read-only offline viewer. It adds durable download jobs, stable-ID
remote checks, separately retained conflict candidates and bounded recursive
folder intent. Nautilus and its IPC stay status-only. Production code implements
no cloud writes, uploads, cloud deletion, automatic merge or two-way sync.

All state/content controls still require the experimental view unmounted. M4
checks every retained item before an online union mount so an old local file
cannot silently shadow a newly observed cloud revision or renamed item. Offline
mounts may expose clean retained snapshots at their original paths; expired
metadata continues to show unknown.

## Workflow

Use the same explicit private offline configuration described in
[M3](m3-offline-controls.md). Keep credentials and actual account/root identifiers
outside the repository. Schema 1/2 stores migrate to schema 3 under the writer
lock, preserving copies and existing direct pins. The read-only helper accepts
old schemas without migrating them; `recover` performs the upgrade explicitly.

```bash
# Persist individual intent; known items can be queued without contacting Graph.
cloudmarks offline --config /private/offline.json queue folder/file.txt
# Attempt at most 128 pending/retryable jobs, once each.
cloudmarks offline --config /private/offline.json resume
cloudmarks offline --config /private/offline.json jobs

# Check a known stable ID, including a retained old path after a rename.
cloudmarks offline --config /private/offline.json refresh folder/file.txt
cloudmarks offline --config /private/offline.json resume
cloudmarks offline --config /private/offline.json conflicts
cloudmarks offline --config /private/offline.json snapshots
cloudmarks offline --config /private/offline.json changes

# Enumerate twice and atomically save a matching, bounded recursive manifest.
cloudmarks offline --config /private/offline.json queue-tree folder
cloudmarks offline --config /private/offline.json resume
cloudmarks offline --config /private/offline.json status folder
cloudmarks offline --config /private/offline.json unpin-tree folder
```

These commands are explicit; there is no background watcher, installed service or
automatic network retry. `refresh` queues a pinned file for verification/download;
it does not publish bytes. Reissue `queue-tree` to discover additions and update
folder coverage. Scanning never infers deletion from a missing listing entry.

Jobs move from pending to running to done, error or blocked. Restart resets a
running job to pending. Network/disk failures retain intent and previous bytes;
`resume` retries error jobs. Cancellation leaves a pending job. An unavailable
physical volume stops work before consuming jobs. Publication/eviction still use
M3's durable filesystem journal and rollback. Partial/orphan/old files count
against quota; retries restart downloads rather than resuming partial byte ranges.

A blocked job needs review. `resume` does not automatically retry blocked jobs;
reissuing `queue` explicitly requests another check. An unresolved issue still
prevents publication and eviction, including direct `keep`/`retry` commands.
`resume` exits unsuccessfully when jobs remain blocked or failed; inspect its JSON
and the read-only reports for details.

## Preservation and review

Fresh metadata is fetched by verified drive/item ID, with allowed-root ancestry.
Renames change tracked metadata while leaving the committed visible path intact.
A clean revision at the same path can be replaced by an explicit worker attempt;
the previous immutable object and publication backup remain retained.

A modified, missing or externally renamed raw-store publication is never silently
replaced. The viewer itself enforces read-only access, but raw store files can be
changed by the owning user. M4 records dirty/error state and, when the cloud item
is readable and capacity allows, downloads a separately verified remote candidate.
It preserves the current visible bytes, the original immutable object and the
remote candidate. Candidate hashes and repeated metadata checks must pass before
indexing a complete snapshot. Every indexed candidate remains listed in
`snapshots`, including candidates superseded by a later remote revision.

`conflicts` reports the retained relative path, original object and latest remote
candidate. `snapshots` lists verified remote candidate object paths and hashes;
`changes` records metadata transitions. These are private local reports and may
contain file names and item identifiers. Do not publish their raw output. Copy
versions you wish to retain to a separate backup before manual repair. There is
no in-place resolve/merge/discard command in this milestone: unresolved stores
remain blocked until a separately reviewed repair or replacement-store workflow.
Never delete private recovery files to clear an error.

A stable-ID 404 after successful account/root checks means **unavailable**, which
can include deletion or lost access. It is not proof of deletion. M4 retains the
snapshot and reserves its path; failed requests, missing root, 403, throttling and
listing omissions do not become deletion evidence. A move outside the allowed
root fails scope verification; CloudMarks does not follow it. No absence case
permits local eviction or cloud deletion.

## Folder coverage and pins

Recursive enumeration is limited to 128 entries including the requested folder,
16 levels, 32 pages and a 120-second enumeration budget checked between enumeration operations.
Individual Graph calls retain their own timeout. Both complete listings must
match identity, path, revision, size, type and service checksum. Duplicate paths,
links/shortcuts, ancestry escapes, failed pages and unsafe continuation URLs abort
the scan. Continuations must use the exact verified Graph children endpoint.
Metadata, manifest, pin membership and queued jobs commit atomically.

Folder status describes the recorded manifest, not a perpetual cloud snapshot.
Evidence expires with the configured freshness interval. A positive aggregate
requires a matching fresh manifest and verified current bytes/read paths for
every recorded file. Child changes or incomplete work invalidate it. Empty folders
and aggregates above 64 files or 8 MiB remain unknown to bound status-helper work.
Only a folder with its own explicit `queue-tree` manifest receives aggregate
coverage; a nested folder stays unknown until explicitly scanned itself.
Empty-directory publication and a full offline placeholder catalog remain absent.

Direct and folder pin sources are separate. Overlapping recursive intents retain
one another's pins. `unpin-tree` removes that folder's source, cancels jobs with
no remaining pin source and leaves all downloaded bytes intact. A direct pin
survives recursive unpinning. Individual `unpin` removes only the direct source;
a folder source can still keep the file pinned. `free` remains an individual,
explicit operation requiring no pin sources, no issue/dirty state and fresh
matching cloud evidence. There is no recursive eviction or automatic history GC.

## Validation

All **184 automated tests** pass on Python 3.14 and 3.11, including 35 new M4
checks. The 29 storage/controller cases also passed on the selected NTFS3 disk;
six Graph-only cases use ordinary temporary directories. The isolated native
Files regression passed again. All 184 tests also pass against the installed
wheel, with the checkout excluded from imports.

The dedicated OneDrive run created three files in one new unique folder. It
passed two remote updates, one rename and deletion of its own deletion fixture,
proxy-blocked cloud loss with fresh-process recovery, three-version conflict
preservation and two offline view restarts with folder state and EROFS. Three
deliberate issues and their local versions remain retained for explicit review.
The separate M3 pin/config/store and daily test-account mount were preserved.

The acceptance matrix and final run scope are recorded in
[the M4 validation record](../evidence/m4/validation.json). The opt-in cloud runner
and its mutation boundaries are documented [here](../../tests/integration/README.md).

Physical cable/Wi-Fi loss, hardware disk unplug/replug and full desktop
logout/login are still separate operational acceptance checks. Worker restart,
rejecting-proxy cloud loss and injected missing/reconnected-volume checks do not
claim those physical scenarios. Daily desktop deployment and release work remain
M5. Offline editing/uploads/two-way sync need a separate scope decision.

## Protocol references

Microsoft documents [stable IDs across renames](https://learn.microsoft.com/en-us/graph/onedrive-addressing-driveitems),
[bounded paged children listing](https://learn.microsoft.com/en-us/graph/api/driveitem-list-children?view=graph-rest-1.0)
and [folder eTag limitations](https://learn.microsoft.com/en-us/graph/api/resources/driveitem?view=graph-rest-1.0).
Those distinctions inform the ID-based checks and repeated full manifest design;
folder eTags alone are not descendant coverage evidence.
