# Command reference

Run `cloudmarks COMMAND --help` for every option. Commands print plain text for
people or JSON where noted. Errors exit with status 1 and a message that omits
credentials and private paths.

## Everyday

| Command | What it does |
| --- | --- |
| `cloudmarks setup` | Guided setup ([user guide](../user-guide.md#set-up)). `--dry-run` shows the plan; `--answers FILE` runs without questions. |
| `cloudmarks doctor` | Read-only health report. `--online` also confirms the account; `--json` for scripts. Exit 0 when nothing failed, 1 otherwise. |
| `cloudmarks enable` / `disable` | Show or hide badges. Mounts and services are unchanged. |
| `cloudmarks logs` | Privacy-filtered diagnostics from the user journal. `--since`, `--lines`, `--follow`. |
| `cloudmarks control --settings FILE --operation keep\|free --path PATH` | What the Files menu runs. Prints JSON progress. `--dry-run` describes the selection without changes. Exit 0 success, 1 expected failure, 2 invalid arguments, 130 cancelled. |
| `cloudmarks demo` | A temporary, artificial demonstration of the status engine. No account or desktop involved. |

### Setup answers file

Every key is optional except `accept_experimental` and `folder` for a fully
non-interactive run. Sign-in still opens the browser through rclone.

```toml
accept_experimental = true
account_email = "you@example.com"   # setup stops if the signed-in account differs
folder = "Documents/CloudMarks"
store_parent = "/home/you/.local/share/cloudmarks-offline"
budget_gib = 10
reserve_gib = 20
upload_root = "/home/you/CloudMarks/Upload"
offline_root = "/home/you/CloudMarks/Offline"
live_updates = false
enable_services = true
enable_badges = true
```

## Installation and services

| Command | What it does |
| --- | --- |
| `cloudmarks install --settings FILE` | Install the Files extension and helper for this user, disabled. Setup runs this for you. |
| `cloudmarks uninstall` | Remove installed files after verifying their checksums. Settings, state, offline copies and mounts are kept. |
| `cloudmarks serve --settings FILE` | Run the status helper in the foreground (what `cloudmarks-status.service` runs). |
| `cloudmarks mount-guard --settings FILE --view upload\|offline --operation prepare\|protect` | Used by the services to protect mount folders. |
| `cloudmarks check-config FILE` | Validate a `core.toml` without contacting OneDrive. |
| `cloudmarks fixture-session --directory DIR` | Serve artificial fixtures for desktop testing. |

## Offline store (`cloudmarks offline --config offline.json ACTION`)

Lower-level controls used by the menu and by recovery. Actions that change
offline copies require the Offline folder to be unmounted unless noted.

| Action | What it does |
| --- | --- |
| `init` | Create a new, empty store bound to the account, folder, disk and state database. |
| `recover` | Roll back or finish an interrupted operation after a crash or restart. |
| `doctor` | JSON store health summary. Read-only. |
| `status PATH` | JSON status of one file. Read-only. |
| `keep PATH`, `free PATH`, `unpin PATH`, `retry PATH` | Single-file controls. |
| `queue`, `queue-tree`, `unpin-tree`, `refresh`, `resume`, `jobs` | Queued and recursive work. |
| `changes`, `conflicts`, `snapshots` | Review cloud changes and conflicts. Read-only. |
| `verify PATH` | Full checksum verification of a retained copy. |
| `resolve PATH`, `archives`, `export-archive ID DIR` | Archive conflicting versions, list archives and export one. |
| `mount [--offline]` | Mount the Offline folder in the foreground. |

## Environment

| Variable | Effect |
| --- | --- |
| `CLOUDMARKS_LOG_LEVEL` | `info` (default), `debug` or `off` for journal diagnostics |
| `CLOUDMARKS_VIEW_FLAGS` | Read from `mount.env` by the view service; `--offline` for a no-network view |
