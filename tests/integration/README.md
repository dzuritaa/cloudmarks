# Opt-in dedicated-account integration

See [M3 setup, results and constraints](../../docs/dev/m3-offline-controls.md).
`run_offline.py` requires explicit private configuration and an existing disposable
file inside the independently verified allowed folder in the dedicated account.
It implements no cloud provisioning, upload or deletion. It initializes a new
store by default; `--reuse-owned-store` only reuses an already bound store after
normal volume/account/root checks. It downloads, verifies, mounts online and
through two credential-free offline processes, checks EROFS, unpins/frees, then
independently rereads remote content and metadata. All views are stopped normally.

The test's result contains only fixture hashes, byte counts and assertions.
Do not publish private configuration, raw rclone/Graph diagnostics or account
identifiers. Use a small disposable fixture first. It does not replace physical
network/disk, logout/login, larger-file or daily-desktop acceptance checks.


## M4 change/recovery matrix

`run_tracking.py --config /private/new-offline.json --result /private/result.json`
requires a new separate store/view and the same verified disposable allowed root.
It refuses adoption of an existing store. This runner **does mutate cloud test
data**: it creates one uniquely named folder and three files, updates two files,
renames one and deletes exactly its own `delete.txt` fixture with an eTag
precondition. It never deletes the folder, other fixtures or an arbitrary item.
The production `OneDrive` class still implements GET/download only; mutation
code lives exclusively in this opt-in test harness. Use only the dedicated test
account and explicit scoped private configuration.

The runner verifies every created identity and its ancestry/effective transport
identity, retries jobs after a rejecting proxy in a fresh process, preserves an
old revision, restarts the credential-free offline view twice, checks folder
status/EROFS and then tests local/remote conflict, rename and remote disappearance.
The conflicted snapshots and remaining cloud fixtures are deliberately retained
for review. The result JSON is sanitized; the companion fixture record and
configuration stay private. It does not simulate full logout or physically unplug
the data disk/network, and it does not install anything in the daily desktop.

## M6 archive and larger-file trial

Create a separate M6 profile and run `run_tracking.py` there first; do not use a
historical M3/M4 store. `run_hardening.py --config /private/offline.json --result
/private/result.json` requires that new `.cloudmarks-m6-*` profile, its owned
fixture record and private `baseline.private.json` mapping original-store file
paths to SHA-256 digests. The runner deliberately refuses unrelated profiles.

It archives/exports all three recorded conflicts, checks every exported checksum,
queues the two live identities and retires the unavailable file locally. The
only additional cloud mutation is a new 16 MiB `large.bin` in the prerequisite's
owned folder. It validates scoped identity/ancestry and the server checksum,
checks two offline mounts, read-only enforcement and 40 helper lookups with byte
hashing disabled. It confirms helper DB bytes and the original profile baselines
are unchanged. Archives, exports, cloud fixtures and the clean trial store remain
available for review. The result is sanitized; logs/config/receipts remain private.

See [M6 hardening](../../docs/dev/m6-hardening.md) for reproduction, private handoff
and the remaining supervised physical/user acceptance checks. The test-only
writer is never imported by the production CLI or helper.

Fixture-creation PUTs request `@microsoft.graph.conflictBehavior=fail` in the URL
to refuse an unexpected occupant, following the [Graph driveItem instance attribute
contract](https://learn.microsoft.com/en-us/graph/api/resources/driveitem?view=graph-rest-1.0#instance-attributes).
