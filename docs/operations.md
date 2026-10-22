# Install and operate the experimental viewer

The published `0.1.0a1` prerelease provides experimental read-only offline viewing
and explicit CLI controls for disposable test data. The current `main` branch
(`0.1.0a2.dev0`) also adds an opt-in [automatic upload/status observer](automatic-status.md)
and [file/folder context-menu controls](folder-controls.md), plus
[verification and recovery commands](m6-hardening.md).

The base setup below establishes the account, store, and read-only adapter. For
the current desktop workflow, configure automatic observation next, then enable
version 4 menu controls using the linked guides. An external rclone mount performs
uploads; CloudMarks' offline view remains read-only. Offline edits, two-way sync,
and automatic conflict merging are not implemented. Start with a dedicated test
account and review [compatibility](compatibility.md) before connecting data.

## Install the CLI

Use Python 3.11–3.14 on Linux and a separate virtual environment. For the current
README's upload observation and context-menu features, install from `main`:

```bash
git clone https://github.com/dzuritaa/cloudmarks.git
cd cloudmarks
python3 -m venv "$HOME/.local/share/cloudmarks/venv"
"$HOME/.local/share/cloudmarks/venv/bin/python" -m pip install --no-deps .
"$HOME/.local/share/cloudmarks/venv/bin/cloudmarks" --version
"$HOME/.local/share/cloudmarks/venv/bin/cloudmarks" demo
```

For an existing installation, follow [upgrade and rollback](#upgrade-and-rollback)
first. Installing the CLI alone does not configure an account or load the Nautilus
extension; continue with the separate test profile below.

### Install the earlier published prerelease instead

For `0.1.0a1`, download its wheel and
`SHA256SUMS` from the project's release page into a new directory. Verify the
checksums there; they detect accidental changes, not the identity of the publisher.

```bash
sha256sum --check SHA256SUMS
python3 -m venv "$HOME/.local/share/cloudmarks/venv"
"$HOME/.local/share/cloudmarks/venv/bin/python" -m pip install --no-deps ./cloudmarks-0.1.0a1-py3-none-any.whl
"$HOME/.local/share/cloudmarks/venv/bin/cloudmarks" --version
"$HOME/.local/share/cloudmarks/venv/bin/cloudmarks" demo
```

The CLI runtime has no third-party Python dependencies. `demo` uses temporary
artificial data only. The package does not install a service, start a mount,
authenticate an account or install a Nautilus extension automatically. In the
commands below, `cloudmarks` means this environment's executable; use its full
path or add that `bin` directory to your shell PATH yourself.

## Configure a separate test profile

Start from the templates in the source archive's `examples` directory:

| File | Purpose |
| --- | --- |
| `config.toml` | Expected/blocked drive IDs, allowed subfolder ID, storage marker, private state path and freshness policy. |
| `offline.json` | Links the core and transport files, rclone executable, expected test email, visible root, disk UUID/target and limits. |
| `transport.conf` | Exact scoped `origin` plus read-only local-first `view`; substitute private credentials locally. |
| `desktop-account.json` | Version 2 status-helper settings pointing at `offline.json`, visible root and private socket. |

Keep these files in a new private directory (0700), with private files (0600).
Do not overwrite an existing profile or copy private examples into Git. Use only
the dedicated test account and a disposable subfolder; the allowed root cannot
be the drive root or a shared shortcut. Configure the blocked personal drive ID.
Account IDs/email, tokens, storage UUIDs and paths must be your private values.
Authenticate that dedicated rclone `origin` locally; never put tokens in chat,
command-line arguments, reports or issues. CloudMarks validates account/root
identity rather than discovering or adopting your existing mount.

Keep core settings, credentials and SQLite state outside both the managed store
and visible root. Store and visible root must be disjoint; state/config must also
stay outside the installed code. `storage_mount_uuid`/`storage_mount_target` refer
to the real selected filesystem from `findmnt`; stacked autofs triggers are not
the disk. The store has an independent marker and account/state/view binding.
Changing paths or making a new marker is not a supported migration method.

The detailed [M3 setup](m3-offline-controls.md) explains root-ID normalization,
transport policy and initialization. Choose an empty, new store path for:

```bash
cloudmarks offline --config /private/offline.json init
```

For an existing bound store, stop its view and run `recover` instead of `init`.
The development writer upgrades schemas 1–4 to 5; the published 0.1.0a1 writer
upgrades schemas 1/2 to 3. Back up a consistent store/state/
configuration bundle before upgrading; read-only helper startup does not migrate.
The M4 conflict-test store deliberately contains issues and modified raw files;
use a clean independent profile for viewing tests.

## Download and open

With the experimental view unmounted:

```bash
cloudmarks offline --config /private/offline.json queue folder/file.txt
cloudmarks offline --config /private/offline.json resume
cloudmarks offline --config /private/offline.json jobs
cloudmarks offline --config /private/offline.json mount --offline
```

`mount --offline` runs in the foreground and exposes retained local files at the
configured visible root without a cloud remote in its derived mount config.
Open that root in Files. Keep the terminal running; Ctrl-C normally unmounts only
this experimental view. If busy, close its files before trying a normal unmount.
Never use forced/lazy unmount or remove mounted files as an eviction method.
A positive offline badge requires the supported mounted read path plus fresh
revision evidence; readable snapshots can still show unknown after evidence expires.

Online `mount` uses the scoped read-only union. It first checks all retained
identities and refuses stale/conflicted local shadows. It does not download or
repair them automatically. Unmount first, then explicitly `refresh`/`resume` or
review `conflicts`. Every byte-changing control requires the view unmounted.

[M4](m4-change-tracking.md) documents `queue-tree`, aggregate limits, separate pin
sources and conflict preservation. `unpin`/`unpin-tree` change intent only;
`free` is a separate individual-file eviction command requiring fresh matching
cloud evidence and no remaining pins, issue or dirty state. It never deletes a
cloud file. Cancellation uses Ctrl-C; after restart/reconnection run `recover`
and explicitly `resume`. Do not delete partial files or edit SQLite rows to
force progress.

## Install the read-only Files adapter

The desktop requires the matching Nautilus Python loader and typelib. The tested
Ubuntu 26.04 packages are `python3-nautilus` 4.1.0 and
`gir1.2-nautilus-4.1` for Nautilus 50.2.2. Installing system packages is a separate
administrator action; CloudMarks never does it for you. Other Nautilus APIs are
untested. Review [the isolated desktop evidence](m2-nautilus-prototype.md) first.

Prepare a private `desktop-account.json` with an absolute account settings path,
absolute visible root, `enabled: false` and socket
`/run/user/YOUR_NUMERIC_UID/cloudmarks/status.sock` (substitute the actual UID,
not `$UID` or `%t` inside JSON). Then:

```bash
cloudmarks install --settings /private/desktop-account.json
cloudmarks serve --settings "$HOME/.config/cloudmarks/desktop.json"
```

Installation copies owned code/assets to `~/.local/share/cloudmarks/m2` and a
launcher to `~/.local/share/nautilus-python/extensions/cloudmarks_nautilus.py`.
The historical `m2` directory name is retained across versions. Desktop settings
and the ownership manifest are private under `~/.config/cloudmarks`. The command
installs disabled and never restarts Files. `serve` is foreground; start it in a
separate terminal. Its status socket exposes no transfer or credential API. The
base version 2 helper reads account state with SQLite mode=ro; opting into version
3/4 automatic observation also gives separate background workers scoped metadata
access through the private transport configuration. Use a fresh Files process in
your chosen test session, after saving work; an already loaded extension keeps its imported code until exit.

```bash
cloudmarks enable
cloudmarks disable
```

Enable/disable is picked up live and never changes mounts. An unavailable helper
or expired lease clears positive status. Unrelated roots stay unbadged.
Do not install the disposable M0 spike beside the real adapter.

## Optional helper startup at login

This section describes the base version 2 helper. For version 3/4 observation and
mount startup, also follow [automatic status setup](automatic-status.md#startup-and-stopped-mount-protection).

The source archive contains `examples/cloudmarks-status.service`. It starts only
the installed account status helper, with a private runtime socket directory and
AF_UNIX restriction. It does not start rclone, a view, downloads or a sync worker.
It assumes default XDG installation paths and `/usr/bin/python3` version 3.11+;
customize the unit if you deliberately used different locations. Configure the
socket above and initialize/recover the account DB before starting it.

In the chosen test session, after reviewing the unit:

```bash
mkdir -p "$HOME/.config/systemd/user"
install -m 600 examples/cloudmarks-status.service "$HOME/.config/systemd/user/cloudmarks-status.service"
systemctl --user daemon-reload
systemctl --user enable --now cloudmarks-status.service
systemctl --user status cloudmarks-status.service
```

These are optional user-run steps. M5 validates the unit syntax, not a full login
cycle; it was not installed or enabled on the daily desktop. Never install this
under the existing test mount service's name. To stop automatic helper startup:

```bash
systemctl --user disable --now cloudmarks-status.service
```

The helper may start while the data disk/view is absent; it reports unknown and
never downloads. Mounting remains a deliberate foreground action after login.

## Upgrade and rollback

1. Disable badges. Stop the optional status unit (or the foreground helper).
   Close experimental-view files and stop that foreground mount normally. Leave
   unrelated/daily mounts alone.
2. With all CloudMarks writers/views stopped, make a private consistent backup of
   core/desktop/offline/transport settings, the SQLite state directory and the
   entire bound store including marker, binding, local, objects and recovery.
   Keep credential-bearing backups private and outside the managed store. Copying
   the DB alone does not back up file content or filesystem intents.
3. Retain the previous wheel and original source desktop settings. Run the old
   CLI's `uninstall` to remove its owned adapter snapshot; it preserves state,
   credentials, all offline data and unrelated extensions.
4. Install the new wheel into the CLI venv with `python -m pip install --no-deps
   --upgrade /download/new.whl`. Run `--version`, then `offline ... recover` while
   unmounted. Reinstall the adapter from the new CLI with the original private
   source settings. It starts disabled.
5. Use a fresh Files process in the selected session, start the helper, mount the
   view explicitly and verify statuses before enabling badges. Enable the helper
   unit again only if you use that optional unit.

A modified installed file stops uninstall before removing the deployment. Preserve
and review it against the old wheel/ownership manifest; do not use recursive
removal or force an upgrade over changed files. This conservative uninstall and
reinstall workflow is the supported upgrade procedure; there is no in-place
updater.

Rollback code by uninstalling the new adapter, reinstalling the retained old
wheel and reinstalling its snapshot. Do not point an older writer at an unsupported
new schema: it refuses unknown schemas. There is no automatic schema downgrade.
If a schema rollback is necessary, restore the whole stopped backup bundle at its
original bound paths after separately preserving any newer local bytes, pin/job
intent and conflict candidates. Never restore an old DB over a newer live store.
The published 0.1.0a1 supports schema 3, but refuses schema 4. Development
0.1.0a2.dev0 supports schemas 1–5 for reading and upgrades them to 5 when writing.

The unreleased version 4 desktop profile adds opt-in [file/folder menu controls](folder-controls.md).

## Uninstall

Disable badges; stop/disable the optional helper unit or foreground helper. Close
files and normally stop the experimental view. Then:

```bash
cloudmarks uninstall
```

Only manifest-owned adapter/settings/code/assets and known generated bytecode are
removed. Offline snapshots, recovery candidates, queued intent, SQLite state,
credentials, account data, cloud files, other extensions and existing mounts stay.
The loaded extension clears presentation when settings disappear; exit that Files
process when convenient to unload its code fully. The CLI venv remains usable for
recovery. To remove just its Python package, use that venv's `python -m pip uninstall
cloudmarks`. Remove the optional unit file you explicitly installed and run
`systemctl --user daemon-reload`; never remove another service.

## Diagnostic logging

CloudMarks writes structured diagnostics to the local systemd journal under
`cloudmarks-diagnostics`. Logging is on at the normal `info` level. Read or watch
it with:

```bash
cloudmarks logs --since "1 hour ago"
cloudmarks logs --follow
```

The messages cover command outcomes, helper/worker lifecycle, status changes,
refresh latency, late/expired leases, IPC failures, mount lifecycle and transport
error categories/HTTP status codes. Normal healthy status summaries occur at
most every 30 seconds unless their state changes. Repeated identical event types
are capped at ten per ten seconds per process. Journald manages rotation, disk
limits and retention using the computer's existing settings; CloudMarks creates
no separate growing log files. Availability after reboot depends on the host's
journal persistence settings.

Diagnostic messages allow only known event names, enum values and bounded
numbers/booleans. They exclude file names/paths, account IDs/email, configuration,
tokens, response bodies, file contents and raw exception/rclone text. Journal
metadata still identifies the local process, service and machine. Existing raw
service/terminal messages and historical private test reports are separate and
can contain private paths; do not publish those without review.

For extra request timing while reproducing a CLI/helper issue, start that process
with `CLOUDMARKS_LOG_LEVEL=debug`. `CLOUDMARKS_LOG_LEVEL=off` disables diagnostics
for that process and its children. The same privacy filter applies at every
level. Logging uses nonblocking local journal sends; a missing/full/unavailable
journal drops diagnostic events without changing file operations or status.
Nautilus callbacks only queue bounded diagnostic notes for its isolated worker.
Raw transport stderr remains suppressed rather than copied into these logs.

`cloudmarks offline --config /private/offline.json doctor` is also available for
a sanitized current health snapshot; it is not a historical log.

## Troubleshooting and recovery

| Symptom | Action |
| --- | --- |
| No badges | Check enabled settings, exact visible root, matching loader/API and a fresh Files process. Check helper status/socket ownership; do not restart unrelated mounts. |
| Unknown after time passes | Remote evidence expired. Unmount; explicitly refresh/resume or rescan the folder. A retained snapshot can still be readable. |
| Helper absent or hung | Status expires to unknown. Stop only this helper, check the disk, restart it. Browsing never triggers transfers. |
| Unsupported state schema | Use the matching/newer CLI; run matching recover unmounted to migrate supported older schemas. Do not edit the schema version or downgrade the DB manually. |
| Writer already active | Stop/wait for that CloudMarks control process. Do not delete the lock to bypass a live writer. |
| Mount refuses online start | A retained revision/path/checksum changed. Inspect jobs/conflicts, preserve versions and update or review explicitly. |
| Disk absent/wrong marker/binding | Reconnect the expected filesystem at its original mountpoint; verify UUID/device/marker. Do not initialize over existing data or manufacture a new binding. |
| Interrupted publication | Stop view/writers, then recover. It rolls uncommitted names back and retains ambiguous candidates; do not promote them by hand. |
| Blocked job/conflict | Inspect private conflicts/snapshots/changes reports and copy desired versions to independent backup. There is no in-place merge/resolve command in this alpha. |
| Quota/free-space error | Retained history and partials count. Unpin and free only verified clean individual files. No automatic history GC exists; do not delete raw objects as cleanup. |
| Authentication expired | Reauthenticate the dedicated rclone origin locally. Check expected account/drive/root again. Never reconnect the personal account as a fallback. |
| Busy unmount | Close applications using the experimental view and use its normal shutdown again. Never force/lazy-unmount it. |

Use `--version`, `offline ... jobs` and the supported compatibility versions when
reporting problems. Reports and service logs can contain private paths, names or
identifiers: redact locally. Do not post configs, tokens, raw Graph/rclone logs,
SQLite databases or private recovery reports. See [SECURITY](../SECURITY.md).
