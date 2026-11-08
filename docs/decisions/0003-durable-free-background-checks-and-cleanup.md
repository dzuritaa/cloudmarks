# ADR 0003 — Durable Free intent, background status checks and explicit store cleanup

Date: 2026-11-07. Status: **accepted**.

An architecture review found three gaps, each confirmed in code:

1. **Free could lose the user's selection.** `release_intent` kept the
   previous pins only in memory and restored them in an `except` block.
   `unpin_tree` commits before any eviction, so a process killed between the
   two left the copies on disk, the folder unmarked and no record to recover.
2. **One locked SQLite read could stop the status helper.** The socket loop
   caught `OSError`, `ValueError` and CloudMarks errors, but not
   `sqlite3.Error`. A busy database during `lookup` ended `serve`, and badges
   disappeared until the service restarted.
3. **The 150 ms request budget did not bound a single check.** The budget is
   tested between paths. One cold 8 MiB file needs two full SHA-256 passes on
   the socket thread, which serves one request at a time and whose client
   gives up after 600 ms.

The review also noted that old versions, rollback backups and interrupted
downloads are retained forever and count against the storage budget.

## Decision

- **Release journal (schema 6).** Free records the pins it is about to
  release in a `releases` row, in its own committed transaction, before
  removing any pin. Completion deletes the row. An exception restores the
  pins immediately. Because the writer lock is exclusive, a row found when a
  writer opens belongs to a Free that died, and `StateStore` restores it on
  open. Restoring is idempotent. Files already evicted stay evicted; their
  pins come back, so the folder shows **Partly offline** and Free can simply
  be run again.
- **Per-path containment in the helper.** `sqlite3.Error`, `OSError` and
  CloudMarks errors raised while answering one path return **Needs checking**
  for that path with a 500 ms lease, and are logged as an allow-listed
  `lookup_failed` event with an error category (`database`, `storage`, …).
  The socket loop also catches `sqlite3.Error` as a last resort.
- **Background content checks.** The helper's hash memo hashes files up to
  1 MiB inline and queues larger misses to one worker thread, answering
  **Needs checking** with a 250 ms lease until the result is in the memo. When
  a file's 60-second rehash is due and its fingerprint is unchanged, the old
  result is kept for up to one more interval while the worker rehashes, so
  badges do not flicker. A failed hash is remembered by fingerprint, so a
  corrupt copy shows **Needs attention**, not **Needs checking** forever. CLI
  status and every mutating operation still hash inline.
- **Explicit cleanup, never automatic.** `cloudmarks offline gc` lists
  unreferenced files in `objects/` and `recovery/` by category (partial,
  superseded, recovery) and removes those untouched for 7 days (configurable)
  only with `--apply`. It runs under the writer lock, refuses while a
  publication intent is unrecovered, keeps every path any state record
  mentions (copies, snapshots, conflict candidates, archives, proofs,
  publications), never touches `local/`, and skips files whose stat
  fingerprint changed since the inventory.
- **Provider contracts.** `backend.py` names the methods each caller group
  uses and `backend.create()` is the single construction point for controls
  and observers.

## Evidence

`scripts/bench_status.py`, 64 artificial 8 MiB files on the NTFS3 data disk,
page cache evicted with `posix_fadvise(DONTNEED)` before each of 3 rounds,
16-path batches:

| Mode | Per-request p50 | p95 | p99 | max | All badges final |
| --- | ---: | ---: | ---: | ---: | ---: |
| Inline hashing (before) | 214 ms | 289 ms | 300 ms | 462 ms | 10.6 s |
| Background hashing | 5.2 ms | 9.6 ms | 9.9 ms | 11.9 ms | 11.3 s |

Background hashing does not make the disk faster: total time to final badges
is the same. It keeps every request far below the 600 ms client timeout, so
other paths and folders keep getting answers while a cold disk is read.

The new tests include a Free killed with `os._exit` after one eviction. Without
the journal no record survived; with it the next writer restored the pins and
folder intent.

## Consequences

- Large files that were not checked recently briefly show **Needs checking**
  before their final badge. Folder badges already behaved this way.
- `findmnt` still runs on the socket thread when its 10-second proof expires;
  it has a 5-second timeout. Moving it behind the same worker is possible
  future work.
- Cleanup deletes only copies of cloud content: the view is read-only, so no
  retained file is the only copy of a local edit. Conflict material is kept.
