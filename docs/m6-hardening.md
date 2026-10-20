# M6 hardening and supervised trial

M6 develops `0.1.0a2.dev0`; the published `0.1.0a1` release is unchanged. The
viewer remains read-only. The Nautilus adapter and status socket expose no
mutating commands. New controls are explicit CLI operations on one dedicated
profile. See the [validation record](evidence/m6/validation.json) for measured
results and the remaining human acceptance work. The [real 16 MiB Files scene](evidence/m6/account-readonly.png) shows the verified status; the [account trial](evidence/m6/account.public.json) and [desktop regression](evidence/m6/desktop-regression.json) record their exact scope.

## Review and repair a recorded conflict

Stop the experimental view normally before repair. Inspect the private reports:

```bash
cloudmarks offline --config /private/offline.json conflicts
cloudmarks offline --config /private/offline.json snapshots
```

These reports contain private names, item IDs and paths. Do not paste them into
public issues. Review the local publication, original object and remote candidate.
CloudMarks never chooses a merge or sends a local change to OneDrive.

To retire the retained entry from the active catalog, preserving its bytes:

```bash
cloudmarks offline --config /private/offline.json resolve folder/file.txt --archive
```

The command moves the indexed local publication into private recovery storage,
retains the original object and any indexed remote candidate, and commits an
archive receipt before retiring the entry. It measures actual bytes, including
modified/corrupt versions, rather than asserting that they match the original
index. Missing versions are marked missing. Unexpected links, changed content,
wrong disk, active mount or ambiguous recovery prevent completion.

Retirement removes that file's active pin sources and job, allowing its old path
to be explicitly reused. The receipt records the prior intent and issue. A
remaining folder intent can rediscover the file during a later explicit scan.
Directory manifests become unknown until rechecked; retirement never changes
cloud files. Unmanaged/raw files outside the indexed publication are retained
in place and are not automatically discovered or included in an archive.

To archive and request a clean copy of the **same live cloud identity**:

```bash
cloudmarks offline --config /private/offline.json resolve folder/file.txt --archive --retry-remote
cloudmarks offline --config /private/offline.json resume
```

The retry checks fresh scoped metadata twice, preserves existing pin intent,
clears the resolved local issue and queues a download at the current remote path.
For an unpinned file, explicit retry adds direct pin intent. It never adopts an
unrelated identity at the old path. The archive is committed before a download
can replace the active entry. A deleted/inaccessible identity cannot be retried;
archive-only retirement still works offline. If a new cloud change occurs after
review, ordinary download validation can block the new job again.

## Export a private archive

```bash
cloudmarks offline --config /private/offline.json archives
cloudmarks offline --config /private/offline.json export-archive ARCHIVE_ID /private/new-export-directory
```

The view must be unmounted. Export reads the catalog without recovery, migration
or a cloud transport. Its destination must be a new absolute directory outside
the managed store, state and view, with no symlink ancestors. Directories are
0700 and files 0600. It never overwrites an existing destination. It copies and
checks each available version and writes `manifest.json` after all copies pass.
A missing version remains marked missing. A failed export retains its incomplete
directory for inspection; retry into another new directory. Exported bundles
contain private metadata and should be kept private.

Archive receipts and all retained bytes count toward the storage burden. There
is no archive garbage collection. Keep receipts/state/config and managed data
together in a stopped consistent backup. Do not delete blobs or edit SQLite to
make room. Archive publication uses the existing durable journal, no-replace
rename and atomic SQLite commit. After interruption, unmount and run `recover`;
it restores the measured modified bytes without overwriting a new occupant.
Ambiguity preserves both names and the intent for manual inspection.

## Verify a larger file without repeated helper hashing

Files above 8 MiB can now receive verified account badges after an explicit
full-byte check. With the controlled view mounted, use another terminal:

```bash
cloudmarks offline --config /private/offline.json verify folder/large.bin
cloudmarks offline --config /private/offline.json status folder/large.bin
```

Verification requires fresh remote metadata, a current clean complete copy, no
issue and an attested read-only mount. It hashes both the object and durable
local publication with cancellation checks and verifies that neither changed.
It stores a short-lived proof in private state. This command reads local bytes
and writes proof metadata; it does not construct a cloud transport or alter files.

For large files, the display helper checks current metadata, copy identity,
revision, account/disk binding and mount attestation plus both file fingerprints.
A fingerprint includes device, inode, size, mtime, ctime and link count. Changing
bytes or replacing a file invalidates proof, even when mtime is restored.
Unchanged proof can survive a separately attested remount. Proof expires after
300 seconds; the status lease cannot extend past it. Remote freshness can expire
earlier. Missing/expired proof shows unknown and suggests `offline verify`;
changed content shows error. Readable retained bytes remain available when a
badge is unknown. There is no background full-file verifier.

This evidence trusts kernel timestamps and the user's private state; it is not
protection against a privileged local attacker. Small files still receive full
hash checks. Folder aggregates remain bounded at 64 files / 8 MiB, with recursive
intent limited to 128 entries. Large-file proof does not raise those limits.

## Read-only health report and upgrade

```bash
cloudmarks offline --config /private/offline.json doctor
```

The report includes schema, verified disk/view state, free/reserve space and
counts of copies, jobs, issues, archives and interrupted operations. It omits
account names, IDs, paths and credentials. It does not walk/hydrate content,
construct a transport, migrate or recover state. `ready_for_trial` means these
operational preconditions passed; it does not certify every file or daily use.

The first writer upgrades schemas 1–3 to 4 transactionally. Read-only inspection
accepts schemas 1–4 and never migrates them. Stop and back up the whole consistent
profile before upgrade. The published 0.1.0a1 writer does not support schema 4;
rollback requires restoring the stopped matching bundle, or compatible newer
code. A read-only older helper may refuse a schema it does not understand.

## Supervised physical acceptance session

Use the separate M6 test profile. Keep the original M3/M4 evidence stores and
daily mount unchanged. No daily extension or service is installed by this work.
The private handoff is `~/.config/cloudmarks-test/m6-work.json`; it contains the
profile and result locations, without placing private values in Git.

1. Run `doctor`; review zero issues and unfinished jobs. Mount offline, run
   `verify` for the 16 MiB fixture, and open/hash it through the mounted path.
   Review status text, badge meaning, readability at normal/large zoom, light/dark
   contrast and screen-reader access with the actual user.
2. With the user present, disable networking through the desktop controls. Open
   the retained file again, confirm the checksum and verify no cloud transport is
   required. After metadata expires, expect unknown while reads still work.
   Reconnect, stop the view, explicitly refresh/resume, remount and reverify.
3. Close applications using the experimental view and stop its helper and mount
   normally. Arrange a disk maintenance window or a separate removable test
   volume before disk-disconnect tests. **The current selected disk also contains
   the repository and other data. Do not unplug it during this development
   session.** Test missing/wrong volume refusal, reconnect the original volume,
   `recover`, and verify all retained/exported checksums before any resume.
4. Save other desktop work. Stop this foreground trial normally and log out/in
   with the user. Confirm CloudMarks has not enabled any mount/helper/extension
   automatically. Restart the explicit trial, check the bound disk/profile,
   `recover`, mount offline and read/reverify the retained file. This tests the
   manual lifecycle; automatic startup is not implemented or claimed.
5. Record timestamps, pass/fail, versions and sanitized observations. Preserve
   private diagnostics locally. Stop the trial normally and confirm the daily
   mount is unchanged. Do not enable a daily deployment until physical and UI
   acceptance have been reviewed.

Proxy-blocked requests, process death, injected I/O failure and isolated Xvfb
sessions are useful evidence. They do not stand in for physical unplug/replug,
a full desktop logout/login or the user's accessibility judgment.
