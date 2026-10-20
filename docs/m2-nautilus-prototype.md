# M2 — Read-only helper and Nautilus prototype

Historical M2 result. [M3 now adds explicit CLI controls and version-2 test-account status](m3-offline-controls.md), while keeping this adapter read-only. The fixture-only guard below describes the original M2/version-1 configuration.

Date: 2026-10-20. M2 is implemented and validated against artificial local
fixtures in an isolated GNOME Files session. Real OneDrive integration and
individual-file offline actions remain M3 work. Nothing was installed into the
daily desktop, and the existing test-account mount was not changed.

## What runs

`cloudmarks.helper` serves a version 1, same-user Unix socket. Its only operation
is `status`. Indexed path lookups feed the M1 status engine, including live
object checks and a separate checksum/volume-marker check of the artificial
visible copy. No download, upload, eviction, directory enumeration, cloud
listing or credential discovery is exposed by the helper protocol.

The M2 account/root guard accepts only `fixture-test-drive` / `fixture-root`.
The desktop settings must explicitly say `mode: "fixture"`. This development
build cannot be pointed at the existing OneDrive mount and treated as truthful
production integration. The fixture's materialized files and permissions are
not a proof of M3's planned read-only union or publication/recovery contract.

`cloudmarks.nautilus_extension` provides emblems and an optional
**CloudMarks (fixture)** list column. Callbacks read memory and queue work;
configuration and socket I/O run on one bounded background thread. The callback
returns immediately. The worker delivers evidence through a queue, and a GLib
timer invalidates changed attributes on the Files main thread. Unchanged evidence
renews without clearing the displayed status. There are no async closures or
ambiguous NULL cancellation handles to retain in the Nautilus Python binding.

Bundled SVG emblems distinguish cloud, local check, offline pin, downloading,
error and unknown by shape. They include a light interior and dark outline for
contrast in both appearances. All six resolve under Yaru and Adwaita. If an icon
cannot be resolved, the extension uses a generic warning if available and keeps
the explicit text status. These are not color-only indicators; screen-reader
behavior and user readability approval have not been established.

## Bounds and freshness

| Boundary | M2 policy |
| --- | --- |
| Socket/configuration | Private user-owned directories, no symlink traversal; socket/config files 0600; Linux peer UID checked in both directions. |
| Protocol | Version 1; exact schema; status only; at most 16 unique relative paths and a 16 KiB frame per request. |
| Connections | One request at a time; backlog 8; incomplete frames have a 600 ms total receive deadline. |
| Helper work | 150 ms batch budget checked between files; files over 8 MiB return unknown before hashing. |
| UI work | At most 512 retained file entries, 512 queued requests and 1,024 queued results; one background worker. |
| Evidence | Maximum one-second lease, shortened to remote-observation expiry; response age includes time spent waiting on the helper. |
| Refresh | 100 ms main-thread drain; renew before expiry; visible/recent entries only, with inactive entries released after their evidence expires. |
| Helper failure | Unknown on missing, malformed, stalled or unavailable evidence; expired responses cannot restore green. Automatic retry supports recovery without navigation. |
| Overload | Unknown for entries beyond tracking/work limits; Files remains usable. Reloading after capacity is released requests fresh evidence. |
| Directories | Unknown; existence or listing is never aggregate offline proof. |

A kernel-blocked filesystem call can stall the helper process; it cannot be
cancelled by the 150 ms budget. The UI deadline and evidence lease remain
independent of that process. This is a bounded prototype, not a large-file
verification scheduler. A brief unknown state during slow refresh is deliberate.
No green result is retained indefinitely while waiting for the helper.

## Desktop evidence

The acceptance harness installs the actual M2 package snapshot and launcher,
runs a foreground helper, and observes the optional status attribute through a
separate test-only extension. The observer never assigns badges or invalidates
files. It also measures main-loop progress. Tests run Nautilus 50.2.2,
nautilus-python 4.1.0, Python 3.14.4, GTK4 and Yaru under Xvfb with a private bus,
no network and a hidden real home/runtime. No administrator privileges or daily
Files restart were needed. Missing Tracker/volume services and software-renderer
warnings are expected in this deliberately minimal session.

The 2,000-entry run's largest measured main-loop gap was **217.7 ms** (100 ms
heartbeat interval; the acceptance limit was one second).

The test uninstalls once **before the first enablement**, then installs again.
It verifies all six engine states, an artificial local-to-pin transition without
navigation, a suspended helper, automatic recovery after resume, helper exit,
disabling in the same Files process, unrelated files, and a fresh launch after
uninstall. After helper restart, the fixture that was actively transferring
correctly becomes an interrupted-transfer error rather than remaining active.

- [Initial state column, including downloading](evidence/m2/initial.png).
- [Live pin transition](evidence/m2/refreshed.png).
- [Unresponsive helper](evidence/m2/helper-unresponsive.png),
  [recovered helper](evidence/m2/helper-recovered.png),
  [unavailable helper](evidence/m2/helper-unavailable.png).
- [Disabled while Files stays open](evidence/m2/disabled.png).
- [2,000-entry folder](evidence/m2/large-folder.png).
- [Unrelated folder](evidence/m2/unrelated.png) and
  [uninstalled baseline](evidence/m2/uninstalled.png).
- [Acceptance results](evidence/m2/results.json),
  [sanitized observed attributes and heartbeat samples](evidence/m2/observer.jsonl),
  [theme availability](evidence/m2/icon-themes.json),
  [validation record](evidence/m2/validation.json).

| View | Small/light | Small/dark | Large/light | Large/dark |
| --- | --- | --- | --- | --- |
| Icons | [PNG](evidence/m2/icon-small-light.png) | [PNG](evidence/m2/icon-small-dark.png) | [PNG](evidence/m2/icon-large-light.png) | [PNG](evidence/m2/icon-large-dark.png) |
| List + status | [PNG](evidence/m2/list-small-light.png) | [PNG](evidence/m2/list-small-dark.png) | [PNG](evidence/m2/list-large-light.png) | [PNG](evidence/m2/list-large-dark.png) |

Nautilus adds its own lock mark to the fixture files with read-only permissions.
That lock is distinct from the CloudMarks availability symbol. Screenshots crop
out host sidebar volume names and contain only disposable fixture paths/content.
The large icon view extends below the window; other views show the full fixture
list. These checks establish rendering and responsiveness, not real cloud truth
or physical disk/network recovery.

## Reproduce without changing the daily desktop

Run the automated suite (no GI package or credentials needed):

```bash
python3 -m unittest discover -s tests -v
```

Run the real desktop harness using the locally extracted Ubuntu dependencies
from the [M0 runner instructions](../spikes/nautilus-badges/README.md):

```bash
python3 tests/desktop/run_isolated.py \
  --sysroot /tmp/cloudmarks-m0/sysroot \
  --work /tmp/cm-m2-review
```

Use a new, short work path for every run. `bwrap`, `dbus-run-session`, Xvfb,
`xauth`, GTK/GI, Pillow, `xwininfo`, Nautilus and nautilus-python are needed for
this harness. The extracted extension loader is mounted over its fixed system
path **inside the namespace only**. The host home, runtime sockets and system
extension directory are hidden. Only the temporary work directory is writable.
The private bus's AppArmor mediation setting does not change host policy.
A desktop security policy that forbids this isolation is a prerequisite failure,
not a reason to relax the host's security settings.

## User-only installation, disable and removal

These commands are implemented and tested in isolation. They are optional steps
for a future deliberate desktop test; they were not run against the daily Files
session. The target session needs the matching system nautilus-python loader and
Nautilus typelib. The installer checks neither system package compatibility nor
cloud connectivity and installs no system packages.

In one terminal, create a new private fixture directory and keep its helper in
the foreground:

```bash
python3 -m cloudmarks fixture-session --directory /tmp/cm-demo --cycle-seconds 12
```

`--count 2000` adds a large test folder. `--cycle-seconds` is an explicit
artificial pin transition, not a transfer command. Creation refuses an existing
directory. The helper never reads Microsoft credentials. Stop it with Ctrl-C.
For an existing fixture store, `serve --settings /tmp/cm-demo/desktop.json`
reopens persisted evidence; it does not recreate the artificial active transfer.

In another terminal, using the same user's XDG environment:

```bash
python3 -m cloudmarks install --settings /tmp/cm-demo/desktop.json
python3 -m cloudmarks enable
python3 -m cloudmarks disable
python3 -m cloudmarks uninstall
```

Installation starts **disabled**, snapshots the Python package and icons under
`$XDG_DATA_HOME/cloudmarks/m2`, and installs only
`$XDG_DATA_HOME/nautilus-python/extensions/cloudmarks_nautilus.py` plus private
settings/ownership manifest under `$XDG_CONFIG_HOME/cloudmarks`. Defaults are
`~/.local/share` and `~/.config`. It does not create or enable a service, start a
helper, restart Files, mount a filesystem, or change GNOME preferences. A fresh
Files process in the chosen test session loads a newly installed extension.
The optional column is **CloudMarks (fixture)** in Files' column chooser.

Enable/disable is picked up by an already-loaded adapter; disabling clears its
attributes without touching the OneDrive mount. Uninstall preflights the owned
file hashes and ownership, preserves modified/unrelated files, removes known
generated package bytecode, and permits clean reinstall. If an owned deployment
file was modified, removal stops before deleting the deployment and identifies
that condition. Restore/review that file instead of deleting directories blindly.
A failed install rolls back its newly written files. Changing only the enabled
setting does not invalidate the ownership manifest.

The extension launcher can remain imported until that Files process exits, but
removing its configuration clears presentation in the running process; a fresh
process returns to baseline. No fixture files, offline objects, account data,
mount definitions or rclone cache are removed. To upgrade the snapshot, uninstall
and reinstall from the desired source, then use a fresh Files process in the
chosen test session. Do not copy the disposable M0 spike into the user extension
directory alongside M2.

## Next boundary

M3 must implement the real transport and read-path proof, verified publication
into the visible read-only view, individual-file controls, budgets, cancellation,
recovery and safe local-only eviction. M2 does not establish those guarantees.
Retain the account/root isolation and never infer deletion from a failed listing.
No context-menu actions or writable IPC commands were added here.

API references: [Nautilus InfoProvider](https://gnome.pages.gitlab.gnome.org/nautilus-python/class-nautilus-python-info-provider.html),
[ColumnProvider](https://gnome.pages.gitlab.gnome.org/nautilus-python/class-nautilus-python-column-provider.html).
