# CloudMarks architecture

This is the current architecture of `main`. The milestone documents under
[`docs/dev`](dev/handoff.md) are historical records: where they disagree with
this page (for example, "Nautilus actions remain deferred" or "all controls
require the view unmounted"), this page is current. Decisions and their
evidence are in [ADR 0001](decisions/0001-offline-backend.md),
[ADR 0002](decisions/0002-live-view-updates.md) and
[ADR 0003](decisions/0003-durable-free-background-checks-and-cleanup.md) and
[ADR 0004](decisions/0004-bounded-status-reception-and-volume-proofs.md).

## Goal and limits

CloudMarks shows evidence-based OneDrive status in GNOME Files and lets a user
keep files offline or free their local copies. It is **not** a sync client:
the Offline folder is a read-only view, nothing is uploaded from it, and
CloudMarks never deletes or modifies cloud files. Uploads happen only through
the separately mounted, writable rclone Upload folder.

A badge is a claim. Every green badge requires fresh cloud metadata, complete
local bytes whose SHA-256 matches the recorded copy, and the expected
read-only view and disk. Missing or expired evidence is shown as
**Needs checking**, never as available.

## Processes

```
GNOME Files (Nautilus)
  └─ nautilus_extension.py      GI only; no file, socket or network I/O
       └─ desktop.ProcessBackground ── subprocess (desktop.pipe_worker)
            ├─ ipc.request ──────────── Unix socket, same UID, status only
            │                            └─ helper.serve  (cloudmarks serve)
            │                                 ├─ OfflineStatusService + StatusEngine
            │                                 │    └─ BackgroundHashMemo worker thread
            │                                 ├─ activity.Activity    (upload observer)
            │                                 └─ folders.FolderObserver (folder proofs)
            └─ control_gui.launch ──── GTK progress window (own process)
                                         └─ cloudmarks control  → controls.execute
user services: cloudmarks-*-view.service (rclone read-only union mount),
               upload mount, status helper
```

- **Nautilus extension** (`nautilus_extension.py`): emblems, status column and
  the Keep offline / Free local copy menu. It never blocks Files: all I/O is in
  a separate worker process, restarted with backoff if it exits.
- **Desktop worker** (`desktop.py`): maps Files URIs to relative paths, sends
  batches of at most 16 paths to the helper and caches each answer for its
  lease (`valid_for_ms`). Menu actions start the GTK window, which runs the
  `cloudmarks control` command and shows progress, cancellation and retry.
- **Status helper** (`helper.py`): one listener, up to 16 nonblocking peers with
  a 600 ms absolute framing/response deadline, 150 ms work budget per batch,
  same-UID peers only. Status evaluation stays on the SQLite owner thread. The socket protocol has no
  mutation operation. It opens the state database read-only (`ReadStore`).
  In account mode the same process runs upload and folder observer workers that hold
  read-only OneDrive transport for metadata; the socket thread never
  constructs transport or reads credentials.
- **Controls** (`controls.py`, `offline.py`, `tracking.py`, `recovery.py`): the
  only code that changes the store. A Free or Keep holds the exclusive state
  writer lock and a separate `control.lock` for its whole duration.
- **View** (`view.py`): the Offline folder is an rclone union mount: the
  local store tree first, the OneDrive root second, both read-only, no VFS
  data cache. An offline mode serves only the local tree at the same path.
- **Setup, install, doctor** (`setup.py`, `install.py`, `doctor.py`): guided
  profile creation, checksum-verified installation and a shareable health
  report.

## State

`StateStore` (`state.py`) is the only writer of a private SQLite database
(schema 6, `synchronous=FULL`, rollback journal). An exclusive `flock` allows
one writer per profile; `ReadStore` opens it read-only with a 100 ms busy
timeout for the helper and inspection commands.

| Table | Holds |
| --- | --- |
| `binding` | Account, root, disk volume, store path and case policy; any mismatch refuses to open. |
| `files` | Latest observation per stable `(drive_id, item_id)`: path, revision, size, server hash, pin, operation. |
| `copies`, `local_entries` | The verified immutable object and its visible publication path. |
| `pin_sources`, `folder_intents` | Who keeps a file offline (`direct` or `folder:<id>`) and each folder's recorded manifest. |
| `publications` | Durable filesystem intent for a publication, eviction or archive in progress. |
| `releases` | Pins a running Free released; restored if that Free does not finish. |
| `jobs`, `changes`, `issues`, `snapshots` | Queued downloads, observed cloud changes, conflicts and their retained candidates. |
| `archives`, `verifications` | Conflict archive receipts and explicit large-file proofs. |

Schema upgrades run forward only, on writer open, in numbered steps; tests
upgrade from every schema since 2. Job states change only through
`StateStore.set_job`, which accepts a fixed set of states. `check_integrity()`
is a read-only check of SQLite structure, foreign keys and CloudMarks
invariants (every copy has a publication, pin flags match their sources, no
interrupted operation or unfinished release); `offline doctor` reports it and
is not ready for use while it finds a problem.

## Offline store on disk

```
<storage_root>/
  .cloudmarks-volume   marker; must match the profile's volume ID
  objects/<uuid>.blob  immutable verified copy (0444); old versions remain
  objects/.partial-*   downloads in progress or interrupted
  local/<path>         visible tree served by the view (independent copy)
  recovery/*           rollback backups, aborted candidates, archived versions
```

All access goes through `paths.SecureRoot`: directory file descriptors,
`O_NOFOLLOW`, single-link regular files and a device check on every step.

## Operations and durability

- **Keep**: describe → reserve budget → download to a partial → server
  checksum and repeated revision checks → publish object → stage visible copy →
  journal (`publications`) → atomic `renameat2(RENAME_NOREPLACE)` → commit.
  Restart rolls back an uncommitted journal; ambiguous data is retained.
- **Free**: audit every retained copy against fresh metadata → record the pins
  to release in `releases` → unpin → evict each unpinned file through its own
  journaled rename → clear the release record. An exception restores the pins
  at once; process death leaves the record and the next writer restores it,
  so an interrupted Free never silently drops a selection.
- **Live updates** ([ADR 0002](decisions/0002-live-view-updates.md)): with
  `live_updates` the view stays mounted while renames happen underneath it;
  otherwise the view service is stopped and restarted around the operation.
- **Cleanup**: nothing is deleted from history automatically.
  `cloudmarks offline gc` reports unreferenced old versions, rollback backups
  and partials, and removes those older than a minimum age with `--apply`.

## Status evaluation

`StatusEngine` (`engine.py`) recomputes each answer from the database and the
disk: fresh observation (`max_age_seconds`), no issue, idle operation, copy
matching the observed revision, both local copies hashing to the recorded
SHA-256, view attested as the expected read-only mount, and state unchanged
during the check.

The helper keeps the socket thread cheap:

- Hash results are reused while the kernel fingerprint (device, inode, size,
  mtime, ctime, links) is unchanged, and re-hashed every 60 seconds.
- Files up to 1 MiB are hashed inline. Larger misses go to a background worker
  and answer **Needs checking** with a 250 ms lease until the result is ready.
- Files above 8 MiB are never hashed for a badge; they need an explicit
  `offline verify` proof, valid for 5 minutes.
- The storage-mount proof (`findmnt`) is reused for at most 10 seconds, measured
  from the start of its check. After 5 seconds, requests queue a refresh on one
  bounded background worker. Cold/expired proofs answer **Needs checking**; a
  failed refresh revokes the proof immediately. Every reuse checks device/inode
  and the private binding. Mutating callers always run `findmnt` synchronously.
- A database, disk or configuration error for one path answers
  **Needs checking** for that path (500 ms lease) and the helper keeps serving.

When evidence is unavailable the engine still fails closed, but keeps the
cause: database, storage, scope, configuration, permissions and unexpected
errors each have their own fixed reason, and the helper logs the category.

Folder badges come from `FolderObserver`, which enumerates the folder in the
cloud and checks members in its own thread; requests only read its cache. The
remote tree and derived manifest are reused for 15 seconds, without extending
the original metadata deadline. Trees above 10,000 entries remain **Needs
checking**. The legacy engine aggregate path retains its 128-member/64-file/8 MiB
budget; the desktop working set remains bounded at 512 items.

## Provider boundary

`backend.py` defines the contracts the rest of the code depends on
(`Metadata`, `Transfer`, `OfflineBackend`, `ActivityBackend`,
`TransportProof`) and `backend.create()` builds the configured provider.
`OneDrive` (`onedrive.py`, Microsoft Graph for metadata, rclone for bytes) is
the only implementation, and a contract test keeps its signatures aligned.
Observations, `Cancelled` and `RemoteMissing` are provider-neutral types in
`model.py`, and `Observation.hasher()` picks the server checksum from
`checksums.py`. Core modules (controls, offline, tracking, recovery, state,
storage, engine, verification, folders) do not import `onedrive`; a test
enforces it. The rclone configuration in `view.py` and setup's sign-in
(`signin.py`) are still OneDrive-specific.

## Code layout

| Area | Modules |
| --- | --- |
| Model and contracts | `model.py`, `backend.py`, `checksums.py`, `limits.py` |
| State and store | `state.py`, `paths.py`, `offline.py`, `tracking.py`, `recovery.py`, `storage.py` |
| Controls | `controls.py` (`ControlRun` stages), `offline_cli.py`, `control_gui.py` |
| Status | `engine.py`, `verification.py`, `helper.py`, `ipc.py`, `activity.py`, `folders.py` |
| Desktop | `nautilus_extension.py`, `desktop.py` |
| Provider | `onedrive.py`, `view.py` |
| Setup and operation | `setup.py`, `signin.py`, `units.py`, `install.py`, `doctor.py`, `diagnostics.py`, `telemetry.py` |

Core modules are type-checked with mypy (`check_untyped_defs`), configured in
`pyproject.toml` and run in CI.

## Security and privacy

- The socket, settings, state and store directories must be owned by the user
  and private (0700/0600); the helper rejects other peers.
- Paths are bounded relative paths; traversal and control characters are
  rejected before any lookup, and a case or Unicode alias never inherits
  another path's status.
- Cloud observations are checked against the expected drive, allowed root and
  ancestry; the account binding is stored and enforced.
- Diagnostics go to the user journal with allow-listed components, events and
  enum fields only: no paths, names, identifiers, tokens or error text.

## Validation

`python -m unittest discover -s tests` runs the local suite (no account,
network or desktop). CI runs it on Python 3.11–3.14 with coverage, lint and
package checks. Opt-in runners cover a real test account
([`tests/integration`](../tests/integration/README.md)), an isolated Files
session ([`tests/desktop`](../tests/desktop/README.md)) and physical
reconnect/logout acceptance. `scripts/bench_status.py` measures status-socket
latency with a cold page cache. `scripts/bench_scale.py` measures artificial
1k/10k/50k folder work, desktop queue admission, idle-peer isolation and cold
volume proofs; [results and limits](dev/status-performance.md) are recorded.
The installed-wheel gate runs install → enable → doctor → disable → uninstall
and reinstall in a temporary HOME with fake tools; generated units are checked
with `systemd-analyze --user verify` when available, without starting services.
