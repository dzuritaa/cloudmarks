# M1 — Fixture-backed status engine

Status: implemented and checked locally. This is the core for a future helper,
not an installed Nautilus extension or a real cloud-transfer service.

## Run it

The core and its tests use only the Python standard library. Linux with Python
3.11 or newer is required; the file operations deliberately use Linux directory
descriptors, `O_NOFOLLOW`, `fcntl` locks and `/proc`.

```bash
python3 -m cloudmarks demo
python3 -m cloudmarks check-config examples/config.toml
python3 -m unittest discover -s tests -v
```

The demo creates a temporary fake disk, private database and in-memory remote.
It shows cloud-only → transferring → saved locally → kept offline → unknown
after evidence expires. It discovers no credentials, contacts no network and
does not touch the desktop or existing mount. Its files are removed afterward.

The example TOML contains placeholders. `check-config` validates its structure
and identity settings; it does not create directories, verify a Microsoft
account, or make a disk ready. Real credentials and identities belong outside
the repository. No command enables real cloud operations in M1.

## Components

| Module | Responsibility |
| --- | --- |
| `model.py` | Stable drive/item identities, opaque revisions, file/folder distinction, local completion evidence and status values. |
| `config.py` | Version-1 TOML, required test-drive blocklist, allowed root, selected disk marker, private state location, path case policy and evidence lifetime. Unknown fields and wrong types are rejected. |
| `paths.py` | Directory-FD containment, no symlink traversal, regular-file and single-link checks, streaming verification, staged candidate writes and immutable-object publication. |
| `state.py` | Single-writer SQLite, binding to account/root/disk/path policy, ordered observations, pin intent, operation state and transactional completion references. |
| `engine.py` | Fail-closed availability decisions using live local bytes, current revision, freshness, disk identity and an explicit supported-read-path proof. |
| `fixture.py` | In-memory artificial transport and fixture-only controller/read-path proof. It is not the future rclone adapter. |

Only hexadecimal personal-drive identifiers are case-normalized. Business drive
IDs remain opaque. The initial disk policy refuses case-colliding paths instead
of conflating two remote identities. Renames retain identity but invalidate
an old read-path proof; recursive folder coverage remains unknown.

## What makes a green state possible

A completed manifest row is not enough. The engine requires fresh remote
evidence, an exact revision match, the expected disk marker, a regular complete
local file with the recorded checksum, and a positive read-path proof. That
proof defaults to false until an adapter supplies it. A partial filename or
unpublished object is not a valid local record.

The engine checks disk identity and file metadata again after the read-path
proof. It also rechecks state and evidence lifetime, so a concurrent edit,
revision update or expired observation during verification cannot produce a
green answer. Any provider failure falls back to unknown with a stable reason;
raw adapter errors and private paths are not returned as status text.

Hashing happens synchronously in this core API and **must run outside Nautilus**.
The M2 helper will need bounded work queues and result reuse. M1 makes no
large-folder latency claim.

## Failed transfers and interrupted writes

The artificial controller writes to a new candidate, verifies size/checksum,
rechecks source identity, path, revision and file flags, publishes a new immutable object and only then
commits its state reference. It keeps the previous complete object intact on
partial downloads, bad content, full-disk errors, remote revision changes and
metadata failures. Renaming a source during download invalidates completion
even if its content hash and revision remain unchanged. An orphaned published object or a partial candidate never
becomes a completed record automatically.

Metadata commits are transactional; file publication and SQLite are separate
transactions. Tests force an abort after file publication and kill a child
process inside a SQLite transaction to verify that the previous reference
survives. This is controlled crash testing, not a claim about every hardware
power-loss scenario. On startup, a transfer left active by a dead process becomes
an error instead of inventing completion.

Pin intent persists independently of content. There is deliberately no eviction
or upload API. M3 must add real checksum verification, a supported visible tree,
space budgets, safe cancellation/retry, orphan recovery and explicit local-only
eviction. Existing snapshots must remain recoverable when remote revisions or
deletions invalidate their current-copy state.

## Validation

The test suite covers complete/empty/partial/stale/dirty/missing files, transfer
states, checksums, disk absence/replacement, symlinks/hard links/FIFOs, traversal,
Unicode/spaces/case collisions, account/root mismatches, rename proof invalidation,
restart persistence, competing writers, future schemas and interrupted state
writes. It runs without Microsoft credentials or a desktop.

To repeat against a particular filesystem, create an empty private test directory
on that disk and set `CLOUDMARKS_TEST_TMPDIR` for the test process. Every test
creates and removes its own disposable subdirectory. The checked-in
[validation record](evidence/m1/validation.json) distinguishes the standard
temporary-filesystem, NTFS3 and Python compatibility runs.

The desktop remains at M0's isolated fixture spike. M2 still needs the real
helper interface, asynchronous adapter, performance checks, theme fallbacks,
installer and uninstaller. The production adapter must verify authenticated
account/root identity; M1's scope checks do not substitute for that network check.
