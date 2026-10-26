# File and folder controls — experimental

Version 4 desktop settings add **Keep offline** and **Free local copy** to the
Nautilus context menu for one selected file or folder in either configured test
view. A separate GTK 4 window shows progress, Cancel and Retry. Nautilus callbacks
only enqueue work; the status socket remains read-only.

Use the existing test OneDrive profile. Copy/upload in `CloudMarks-Upload`; use
`CloudMarks-Test` for verified offline reads. Keep offline downloads the selection,
including nested files and empty folders. Apply Keep again to resume an interrupted
selection or include new cloud members. It does not continuously download changes.

Free local copy releases this selection's pin intent and evicts only clean copies
whose current cloud revision/checksum is verified. It never deletes OneDrive
files. Files pinned directly or by another folder selection stay local, and the
result reports that count. Failure/cancellation preserves remaining files and
restores the selection's pin intent; already completed evictions remain completed.
Modified files, conflicting identities and changed folder manifests require review
or another Keep before Free. No cloud delete API is part of these controls.

With `"live_updates": true` in the control profile, publication and eviction
run while the view stays mounted ([ADR 0002](decisions/0002-live-view-updates.md));
CloudMarks still refuses any mount other than its attested read-only view.
Otherwise, publication and eviction require a normal unmount of the controlled
read-only view. The control stops only its configured user view service, restores
its previous online/offline mode, and leaves the upload mount running. If an open
file or Files window makes it busy, close the `CloudMarks-Test` window and files
opened from it, then click Retry; the message names the applications holding
files in the view. There is no forced/lazy unmount. A retained file
is readable in the test view after the control restores that view.

## Badges and limits

- Downloading / Freeing local copy: a live local control reports progress for the
  selection, its children and displayed ancestors.
- Partly offline: only some current members are verified, membership changed, or
  a complete recursive pin is absent. Apply Keep offline to refresh it.
- Kept offline: fresh remote membership matches the selected manifest and every
  file's revision, complete object/local bytes and supported read path are verified.
  Empty directories must exist on the bound offline volume too.
- Cloud only: OneDrive confirms the selection without a complete offline selection.
  The upload view never claims offline availability from its VFS cache.
- Needs checking / attention: expired/unavailable evidence or an unresolved error.

Folder cloud scans and local verification run in a background thread, outside IPC
requests and Nautilus. A scan starts at most every 15 seconds per visible folder;
its evidence expires after at most 30 seconds. Local checks cache SHA-256 results
only while the copy record and both kernel fingerprints remain identical. Folder
status leases are at most one second, and expire during unavailable/slow checks.
The helper bounds active aggregate work to 16 folder/view requests. Hashing a new
large folder can still take time; no large-library latency guarantee is made.

Recursive selections support up to 10,000 total entries, depth 64, 512 pages and
600 seconds between Graph calls. The offline budget/free-space rules still apply.
These are per-selection limits. Automatic file observations use batches of 20
read-only Graph requests within a 1,024-path working set, independent of total
library size. Batch paths use the allowed root’s verified absolute path because
consumer OneDrive can ignore item-relative suffixes inside a batch; every returned
item still requires an exact path and ancestry match to the configured root ID.
See Microsoft’s [batch API](https://learn.microsoft.com/en-us/graph/json-batching)
and [path addressing](https://learn.microsoft.com/en-us/graph/onedrive-addressing-driveitems). Nautilus still tracks at most 512 visible/recent files. Upload cache
notifications avoid repeatedly walking an idle cache; initial scans and directory
changes rebuild bounded watches. Queue/cache bounds fail closed instead of
claiming completion. Account/root attestation is reused for at most 20 seconds in one transport
instance and invalidated by credential/account configuration changes; per-file
identity, ancestry, revision and checksum checks remain fresh. Online remount
checks cover every retained file; they can
remain slow for a large retained library because integrity checks are preserved.

## Configure and operate

Upgrade the CLI and adapter with the [paired-backup procedure](operations.md#upgrade-and-rollback).
The new writer upgrades schemas 1–4 to 5, adding a normalized retained-path index
and pin-owner index without removing data. Readers support schemas 1–5. Old writers
refuse schema 5. Never restore a database alone after changing offline-store bytes.

`cloudmarks setup` creates the control profile and binding automatically. The
steps below are for manual profiles. Start with a working version 3 desktop
settings file. Create a private JSON control profile beside it with these fields:

```json
{"version": 1, "view_service": "cloudmarks-trial-view.service"}
```

The service must be the operator's managed read-only test view, with the existing
mount guard and online/offline environment. Protect its directory with 0700 and
file with 0600. Set desktop `version` to 4, add absolute `control_config`, then
compute `control_binding` using the installed package. This binds queued selections
to the exact drive, root, volume, storage/state paths, views and service:

```python
from cloudmarks.controls import binding
from cloudmarks.ipc import read_private_json, validate_settings
from cloudmarks.offline_config import load_offline
from cloudmarks.install import atomic_json

path = "/home/YOU/.config/cloudmarks/desktop.json"
settings = read_private_json(path)
settings.update(version=4, control_config="/home/YOU/.config/cloudmarks/controls.json")
config, offline = load_offline(settings["core_config"])
settings["control_binding"] = binding(config, settings, read_private_json(settings["control_config"]))
from pathlib import Path
atomic_json(Path(path), validate_settings(settings))
```

Reload the installed adapter in a fresh Files process and restart the helper.
GTK 4 Python introspection is required for the separate progress window. Settings
versions 1–3 remain supported and do not expose menu controls.

CLI automation uses the same controls and JSON progress:

```sh
cloudmarks control --settings /private/desktop.json --operation keep --path test --dry-run
cloudmarks control --settings /private/desktop.json --operation keep --path test
cloudmarks control --settings /private/desktop.json --operation free --path test
```

Dry-run reads remote/local metadata but does not change state, pins, mounts or
bytes. Exit 0 means success, 1 an expected operational failure, 2 invalid arguments,
and 130 cancellation. SIGINT cancels safely and restores the previous view before
returning. The progress window uses those same commands. Private progress state
contains the selected path; the privacy-filtered journal does not log it.

## Local validation

The [2026-10-22 validation record](evidence/m6/folder-controls-local.json) covers
296 source and installed-wheel tests, a 140-file artificial recursive selection,
native GI menu activation/GTK progress, the actual Nautilus completed-folder
column and a real one-file test-account keep/free cycle. A busy open test window
refused eviction before content changes. Free released the local selection while
the cloud manifest, identities, revisions, sizes and authoritative checksums stayed
identical. That real Free plus cloud comparison took 33.1 seconds on this machine.
The owner subsequently confirmed successful automatic startup after reboot and
Keep offline after closing a busy test-view tab, completing personal acceptance
for this test-account trial. Large real-account performance and physical storage
reconnection remain unvalidated.
