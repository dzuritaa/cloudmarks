# Configuration reference

`cloudmarks setup` creates every file below. This page describes them for
review, troubleshooting and manual setups. All settings files are private:
folders `0700`, files `0600`, owned by you. CloudMarks refuses files with wider
permissions, symlinks or hard links.

## Locations

| Path (defaults) | Contents |
| --- | --- |
| `~/.config/cloudmarks/desktop.json` | Installed desktop settings read by Files and the status helper |
| `~/.config/cloudmarks/install.json` | Checksums of installed files, used by `doctor` and `uninstall` |
| `~/.config/cloudmarks/profile/` | Private profile written by setup (files below) |
| `~/.local/state/cloudmarks/state.sqlite3` | State database: identities, revisions, pins, jobs, issues |
| `~/.local/share/cloudmarks/m2/` | Installed Files extension and helper code |
| `~/.local/share/nautilus-python/extensions/cloudmarks_nautilus.py` | Extension loader |
| `~/.config/systemd/user/cloudmarks-*.service` | The three user services |
| Chosen store parent `/cloudmarks-store` | Offline store: verified copies, recovery data, disk marker |
| `/run/user/UID/cloudmarks/status.sock` | Status helper socket |
| `/run/user/UID/cloudmarks-upload/rc.sock` | rclone upload-queue socket (local only) |

`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME` and
`XDG_RUNTIME_DIR` change these defaults. Keep settings, state and credentials
outside the store and both view folders; CloudMarks enforces this.

## `core.toml`

```toml
version = 1

[account]
expected_drive_id = "..."   # OneDrive drive ID
allowed_root_id = "..."     # Graph item ID of the chosen folder
blocked_drive_ids = []      # Optional: other drives to refuse explicitly

[storage]
root = "/absolute/path/cloudmarks-store"
volume_id = "cloudmarks-..."  # Random marker written into the store
state_path = "/absolute/path/state.sqlite3"
case_sensitive = false

[status]
max_age_seconds = 300         # Upper bound on cloud evidence age
```

Every file must belong to `expected_drive_id` and sit under `allowed_root_id`.
`blocked_drive_ids` adds an extra refusal for known drives, such as a personal
account kept separate from a test account. Validate with
`cloudmarks check-config core.toml`.

## `offline.json`

| Field | Meaning |
| --- | --- |
| `version` | Always `1` |
| `core_config` | Absolute path of `core.toml` |
| `rclone_config` | Absolute path of `transport.conf` |
| `rclone` | Absolute path of the rclone executable |
| `visible_root` | The Offline folder |
| `expected_email` | Account email; sign-in must match it |
| `budget_bytes` | Maximum space for offline copies |
| `min_free_bytes` | Free space to always leave on the store's disk |
| `storage_mount_uuid`, `storage_mount_target` | Filesystem UUID and mount point of the store's disk, from `findmnt` |

## `transport.conf` and `upload.conf`

rclone configuration files holding a private copy of the OneDrive credential.
`transport.conf` has exactly two remotes:

```ini
[origin]
type = onedrive
drive_id = DRIVE
drive_type = personal
root_folder_id = DRIVE#FOLDER
token = {...}

[view]
type = union
upstreams = "/absolute/path/cloudmarks-store/local:ro" origin::ro
search_policy = epall
```

`upload.conf` has one remote named `upload` with the same `type`, `drive_id`,
`drive_type`, `root_folder_id` and its own token copy. CloudMarks refuses any
other remote layout, and `root_folder_id` must equal `expected_drive_id#allowed_root_id`.
Never share or commit these files.

## `desktop.json`

| Field | Versions | Meaning |
| --- | --- | --- |
| `version` | all | `1` fixture demo, `2` account status, `3` adds upload observation, `4` adds menu controls |
| `mode` | all | `fixture` (version 1) or `onedrive` |
| `enabled` | all | Show badges; changed by `cloudmarks enable`/`disable` |
| `visible_root` | all | The Offline folder |
| `core_config` | all | `offline.json` (account versions) |
| `socket` | all | Status helper socket |
| `upload_root`, `upload_config`, `upload_socket` | 3, 4 | Upload folder, `upload.conf`, rclone queue socket |
| `control_config`, `control_binding` | 4 | `controls.json` and a SHA-256 fingerprint of the drive, folder, store, state, views and view service |

The binding stops menu actions if the profile changes underneath them. Setup
computes it; after a manual change to those values, recompute it with
`cloudmarks.controls.binding`.

## `controls.json`

```json
{"version": 1, "view_service": "cloudmarks-view.service", "live_updates": false}
```

| Field | Meaning |
| --- | --- |
| `view_service` | User service that mounts the Offline folder; must match `cloudmarks-[NAME-]view.service` |
| `live_updates` | Optional. `true` keeps the Offline folder mounted during Keep offline and Free local copy ([ADR 0002](../decisions/0002-live-view-updates.md)) |

## `mount.env`

Read by `cloudmarks-view.service`. `CLOUDMARKS_VIEW_FLAGS=--offline` makes the
Offline folder show only retained copies, with no network access. Leave it empty
for the normal online view.

## Services

| Unit | Runs | Notes |
| --- | --- | --- |
| `cloudmarks-upload.service` | `rclone mount upload:` on the Upload folder | Write cache, local-only queue socket, mount guard before start and after stop |
| `cloudmarks-status.service` | The installed status helper | Starts after the upload service |
| `cloudmarks-view.service` | `cloudmarks offline ... mount` on the Offline folder | Read-only; starts after the helper; mount guard before start and after stop |

The mount guard refuses to mount over a folder that contains files, so files
copied into a stopped Upload folder are never hidden. After stopping, it makes
the empty folder read-only.
