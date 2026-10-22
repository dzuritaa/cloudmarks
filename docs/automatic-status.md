# Automatic status observation — experimental

The opt-in version 3 desktop settings add a real upload observer to the existing
read-only offline viewer. See [the settings example](../examples/desktop-automatic.json).
Version 1 fixtures and version 2 account settings retain their previous behavior.
Use a dedicated test OneDrive account and folder. Account-wide daily use is not
validated.

The writable upload mount and the read-only view must point to the same verified
OneDrive drive and subfolder. Its private rclone configuration must have exactly
one remote named `upload`, with `type=onedrive`, the expected `drive_id` and
`root_folder_id=DRIVE#FOLDER`. Credentials stay outside the repository. The helper
checks that binding before accepting queue observations. The existing read-only
transport still independently verifies the account and allowed-root identity.

The upload mount needs `--vfs-cache-mode=writes --rc` and
`--rc-addr=unix:///run/user/UID/cloudmarks-upload/rc.sock`. Create that socket's
parent with mode 0700, set the service's `UMask=0077`, and do not expose the RC
server on a network port. This API has broad privileges; CloudMarks only calls
the read-only `vfs/queue` and `vfs/stats` methods with `fs=upload:`.
The socket and peer must belong to the current user.
See [rclone's RC documentation](https://rclone.org/rc/#vfsqueue-queue-info-for-a-vfs)
and [Unix socket configuration](https://rclone.org/commands/rclone_rc/).

For a systemd helper, the optional observer also needs network address families:
use `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`. The version 2 status-only
service can retain its existing AF_UNIX-only restriction. Services remain manual
unless explicitly enabled by the operator; these settings alone do not enable
login startup.

The status helper polls the local upload queue every half second and observes
cache changes through Linux notifications. It scans the cache at startup and
rebuilds watches after directory changes, rather than walking every idle cache
file repeatedly. A separate worker authenticates the configured OneDrive account
and observes requested paths in read-only Graph batches about every ten seconds.
A queue transition requests an earlier check, with a minimum three-second interval
and failure backoff up to sixty seconds. Network latency never blocks a Nautilus
callback or the local status request. Version 4 also adds separate background
[folder verification and menu controls](folder-controls.md).

Presentation:

| State | Meaning |
| --- | --- |
| Uploading | The local cache has an unfinished write or rclone has queued/active upload work. |
| Needs attention | An upload failed and rclone has scheduled a retry. |
| Needs checking | Awaiting fresh cloud confirmation, observer unavailable, or evidence expired. |
| Cloud only | OneDrive confirms the file after any observed local write; no offline guarantee is made for the upload cache. |
| Kept offline / Saved locally | In the read-only view, the existing offline engine verified a durable copy and supported read path. |

Queue disappearance alone never proves completion. A cloud observation must be
newer than the last observed local write/queue transition and match the completed
cache file's size when that file is present. Remote evidence expires
after thirty seconds or the configured evidence limit, whichever is shorter.
Stalled local queue observations expire after three seconds. Status leases remain
at most one second and never outlive their evidence. Small, fast uploads can
finish between UI refreshes, so an uploading badge may be too brief to see.

The automatic observer keeps a bounded working set of 1,024 requested/recent paths;
it no longer enumerates the whole library or applies a 128-entry library limit.
Each Graph batch contains at most 20 metadata GETs. Unsafe links/shortcuts,
duplicate paths, wrong ancestry and malformed responses fail closed. Missing or
failed items mean unknown, never deletion or eviction. Recursive folder controls
have separate [per-selection limits](folder-controls.md#badges-and-limits).

Observations live only in the helper's bounded memory. They overlay fresh remote
metadata for status calculation without changing the durable state database,
pin intent or file contents. A changed cloud revision cannot turn an older local
copy green. An occupied local path with a different remote identity remains
unknown. Existing issues, checksum/read-path checks and large-file explicit proof
requirements remain authoritative. Restarting the helper begins unknown until
both workers establish new evidence.

For a test, copy a disposable file into the upload mount and keep its CloudMarks
column visible. It should transition through uploading/checking to cloud-only;
the corresponding file should appear in OneDrive's configured subfolder. To test
offline availability, use the existing explicit `offline keep` control with the
read-only view unmounted, then remount and verify. No download happens merely by
requesting a badge. Offline mode still makes the read-only mount local-only; the
optional observer independently attempts metadata reads while network access is
available. Loss of fresh cloud evidence changes the badge to checking while
retained local bytes remain readable.

Diagnostics use the existing privacy-filtered journal: scan success/failure,
queue failures and desktop state transitions. No file names, IDs, email addresses
or tokens are logged. To disable the observer, restore a version 2 settings file
and restart only the status helper; clear/reload the adapter settings as usual.
Stop the upload mount only after writes complete, using a normal unmount.

## Startup and stopped-mount protection

A bookmark alone does not start a stopped mount. Files copied into an ordinary
unmounted directory have not been sent to OneDrive. Preserve those files in a
separate private directory before mounting, then restore them through the live
upload mount with no-overwrite checks. Never use `--allow-non-empty` to mount over
pending files.

User services can use `cloudmarks mount-guard` before startup and after stopping:

```ini
ExecStartPre=%h/.local/bin/cloudmarks mount-guard --settings %h/.config/cloudmarks/desktop.json --view upload --operation prepare
ExecStopPost=%h/.local/bin/cloudmarks mount-guard --settings %h/.config/cloudmarks/desktop.json --view upload --operation protect
```

Use `--view offline` on the read-only view's service. The guard validates the
explicit private account settings, rejects symlinks/shared mountpoints and refuses
to prepare any nonempty or already mounted directory. FUSE requires owner write
access while mounting, so prepare sets an empty directory to 0700. Stop/failure
protection sets an unmounted directory to 0500, preventing normal accidental local
writes while stopped. If a normal unmount remains busy, protection leaves the
mounted filesystem untouched. Files arriving during startup make the normal
nonempty mount check fail; retained local files are never removed by the guard.

For a supervised profile that should resume after login, add an `[Install]`
section with `WantedBy=default.target` to its user units and explicitly enable the
upload, helper and read-only view services. Start the upload unit before the
helper (`Wants=` and `After=`); use bounded restart delays for transient startup
network failures. This is an opt-in operational change for that configured test
profile. Launchers should also start their services before opening a bookmark.
