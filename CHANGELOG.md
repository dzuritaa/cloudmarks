# Changelog

## 0.1.0a3.dev0 — unreleased

- Folder badges now share single-file evidence: 60-second fingerprint hash
  reuse and failure memory, and explicit five-minute proofs above 8 MiB.
  Each folder check limits hashing to 16 MiB and checks a 150 ms time budget
  between bounded reads; incomplete checks remain Needs checking.
- Status clients split batches by encoded request and worst-case response size;
  UTF-8 replies also preserve valid Unicode paths near the 16 KiB frame limit.
- Both doctors share state-health classification, including consistency problems;
  completed Free operations no longer report cancelled jobs as unfinished.
- Mypy now checks 21 core modules, including folder proofs, verification, IPC,
  offline configuration, the helper and both diagnostics commands.
- Configuration documentation distinguishes core TOML and store-root permission
  requirements from JSON settings and other installed artifacts.

- Status answers and resumed jobs keep the cause of a failure (database,
  disk, scope, configuration, permissions or unexpected) in a fixed message,
  and the helper logs its category, instead of one generic message.
- `cloudmarks offline doctor` reports state consistency problems
  (`state_problems`) and is not ready for use while any exists.
- Internal: Keep/Free run as named stages (`ControlRun`); core modules no
  longer import the OneDrive module (`checksums.py`, provider-neutral
  `Observation`); setup split into `units.py` and `signin.py`; job state
  changes go through `StateStore.set_job`; mypy checks core modules in CI;
  tests upgrade every state schema since 2.
- Storage-volume proof refreshes run on a bounded background worker; cold or
  expired evidence stays **Needs checking**. Mutations still verify synchronously.
- Up to 16 socket peers can send/receive independently, so an incomplete request
  no longer delays other badges (artificial p50: 581 ms → 0.49 ms). SQLite status
  evaluation remains on one owner thread.
- Folder observation reuses its derived manifest and avoids unused per-file
  reads: 10,000 artificial entries take 120 ms instead of 230 ms (p50).
- The progress window drains long error output without deadlocking and restores
  Cancel on retry. Malformed progress frames are ignored within fixed limits.
- Offline doctor warnings/failures retain their suggested next action.
- CI enforces 85% combined coverage and runs installed deployment/doctor and
  generated-unit checks in a temporary HOME, without an account or live services.
  Current/historical handoff sections, cleanup and one-way schema 6 rollback
  instructions are aligned; reproducible measurements are in
  [the validation record](docs/dev/status-performance.md).

- An interrupted **Free local copy** no longer loses the offline selection.
  The pins it releases are recorded first (state schema 6) and restored if
  the operation fails or the process is killed; run Free again to finish.
- The status helper keeps running when the state database is briefly locked
  or a disk check fails: the affected file shows **Needs checking** for half a
  second instead of every badge disappearing.
- Badge requests no longer hash files above 1 MiB on the request thread. A
  background worker checks them and the file shows **Needs checking** until
  it finishes. On a cold NTFS3 disk with 8 MiB files, per-request latency fell
  from 214 ms (p50) and up to 462 ms to 5 ms and at most 12 ms.
- New `cloudmarks offline gc`: reports old versions, rollback backups and
  interrupted downloads that no record uses, and removes those older than 7
  days with `--apply`.
- Internal: provider contracts in `cloudmarks/backend.py` with a single
  factory; more state SQL moved into `StateStore`; `scripts/bench_status.py`;
  [architecture overview](docs/architecture.md) and ADR 0003.
- Badge requests no longer hash each visible offline file (up to 8 MiB) twice
  per second. The status helper reuses a full SHA-256 result while the file's
  kernel fingerprint is unchanged and hashes again every 60 seconds; it reuses
  the storage-mount proof (`findmnt`) for 10 seconds. Keep, Free and explicit
  verification still hash and check the mount every time. A status lookup for
  an 8 MiB file dropped from about 11 ms plus `findmnt` to about 0.5 ms.
- QuickXor folds 160-byte lanes with integer XOR instead of looping per byte:
  8 MiB takes about 19 ms instead of about 1 s. Digests are unchanged.
- Keep offline audits store usage once per operation and keeps a conservative
  running bound, instead of walking the whole store for every file.
- Keeping a folder or file again reuses retained copies that still match fresh
  OneDrive metadata and checksums instead of downloading them again.
- The Files extension restarts its status worker with backoff if it exits.
- Status text and messages no longer mention a test account or a
  `CloudMarks-Test` window.
- Internal: status-path SQL moved into `StateStore` query methods; the fixture
  status service is separate from the account status service; socket tests no
  longer fail when `TMPDIR` is long.

## 0.1.0a2 — 2026-10-26 — experimental prerelease

- `cloudmarks setup` guides a new user from sign-in to working folders: rclone
  handles the browser sign-in, setup confirms the account, picks a regular
  OneDrive folder, writes the private profile, creates the store, installs the
  Files extension and generates the three user services. It resumes after an
  interruption and never overwrites existing files. Sign-in uses
  `rclone authorize` and the account's own drive, so accounts that list
  inaccessible extra drives work.
- `cloudmarks doctor` checks requirements, installed files, the profile, mounts,
  sockets, the helper, control binding, store health and services. Its report
  contains no paths, IDs, emails or credentials.
- Optional live updates (`"live_updates": true` in the control profile) let Keep
  offline and Free local copy run while the offline view stays mounted. A busy
  view now names the applications holding it open.
- `blocked_drive_ids` is optional; the expected drive and allowed root remain
  enforced.
- A plain-language user guide plus configuration and command references.
  Development records moved to `docs/dev`. CI reports test coverage.
- Opt-in Nautilus file/folder Keep offline and Free local copy controls run in a
  separate progress/retry/cancel window. Normal unmounts, overlapping pins,
  corruption checks and intent recovery preserve local/cloud data.
- Background aggregate badges verify current folder membership, local bytes and
  read paths, including nested/empty folders. Partial coverage stays distinct.
- Demand-driven read-only metadata batches replace the 128-entry library scan;
  recursive selections support 10,000 entries. Cache change notifications avoid
  repeated idle directory walks. Schema 5 adds retained-path and pin-owner indexes.

- Service mount guards refuse startup over pending local files and protect
  stopped mountpoints against accidental local writes. Optional login startup
  can resume a supervised test profile without relying on bookmark navigation.
- Opt-in automatic status observation follows a scoped rclone upload queue and
  bounded OneDrive working set in background workers. Both test views receive badges;
  pending uploads, fresh cloud confirmation and verified offline copies remain
  distinct. Existing metadata-only desktop configurations remain supported.
- Structured, privacy-filtered journal diagnostics cover desktop refresh/expiry,
  helper/worker lifecycle, commands, mounts and transport failures. `cloudmarks
  logs` reads or follows these events without exposing raw paths or credentials.
- Nautilus performs status configuration/socket work in a separate Python
  process, preventing embedded-interpreter scheduling from consuming healthy
  status leases and flickering badges and column widths.
- Explicit conflict retirement archives measured local/original/remote versions;
  optional same-identity retry and private no-overwrite export preserve recovery.
- Explicit full verification gives larger files short-lived read-only helper
  evidence, avoiding repeated byte hashing in status requests.
- Read-only sanitized health reports, schema 4 migration and an isolated M6 trial
  extend operational coverage. Physical reconnect/logout and user UI acceptance
  remain pending; the published 0.1.0a1 release is unchanged.

## 0.1.0a1 — 2026-10-20 — experimental prerelease

Initial experimental read-only offline viewer for a scoped dedicated OneDrive
test account. See [the release checklist](docs/dev/release-checklist.md) for build gates and
[GitHub Releases](https://github.com/dzuritaa/cloudmarks/releases) for publication
availability.

- Evidence-based cloud/local/offline/transfer/error/unknown statuses.
- Read-only Nautilus badges/column and a bounded status-only local helper.
- Verified complete-file storage, read-only local-first/explicit offline views,
  individual download/cancellation/retry and local-only eviction.
- Durable publication recovery, explicit stable-ID change checks, retryable jobs,
  retained conflict candidates and bounded recursive folder pin intent.
- MIT license selected by the owner.
- User-only reversible adapter deployment, explicit upgrade/rollback instructions,
  optional status-helper unit, CI and installed-package/source-rebuild checks.

Offline edits, uploads, automatic sync, mounted updates, context-menu controls,
in-place conflict repair and automatic history cleanup are absent. Hardware
reconnect/logout, larger-file and daily desktop acceptance remain open.
