# CloudMarks

Cloud and offline status badges for GNOME Files, starting with OneDrive through rclone.

## Project status

M0–M4 are implemented experimentally for read-only offline viewing. M4 adds explicit change checks, durable retryable downloads, preserved conflict versions and bounded folder pins. M2 remains a status-only adapter. [M5’s experimental `0.1.0a1` release](https://github.com/dzuritaa/cloudmarks/releases/tag/v0.1.0a1) is published under MIT with passing hosted CI, package validation and operating guides; nothing is installed in the daily desktop.

See the [project plan](PLAN.md) for milestones and acceptance criteria, and
[your setup checklist](YOUR_CHECKLIST.md) for the steps needed before desktop testing.

## Planned first version

- A Nautilus extension that displays cloud-only, saved locally, transferring, and error states.
- A separate helper that manages file state without blocking Files.
- Explicit download and offline controls, with local data stored on a user-selected disk.
- Offline viewing first; offline editing and two-way synchronization are deferred.
- Installation for the current user and a documented uninstall path.

M0 answered two feasibility questions: whether GNOME Files can show readable status badges, and whether the backend can keep verified offline copies. Its outcome selected the backend. Status indicators now run against disposable local fixtures; individual-file CLI controls now pass scoped test-account checks. Editing, uploads and two-way sync remain deferred.

The chosen experimental backend is an explicit verified offline store with rclone transport and a read-only union view. See [ADR 0001](docs/decisions/0001-offline-backend.md) for the tools comparison, experiments and limitations, and [M0 badge screenshots](docs/m0-presentation.md) for the Files presentation result.

See the [installation and operating guide](docs/operations.md),
[tested compatibility](docs/compatibility.md), [changelog](CHANGELOG.md) and
[release gates](docs/release-checklist.md). Use [GitHub Releases](https://github.com/dzuritaa/cloudmarks/releases) for published
builds and [Actions](https://github.com/dzuritaa/cloudmarks/actions) for hosted
validation. The project uses the [MIT license](LICENSE). Physical reconnect/logout
acceptance remains open before daily use.

Use `cloudmarks logs --follow` for privacy-filtered operational diagnostics;
see [logging and retention](docs/operations.md#diagnostic-logging).

## Try the local status engine

Requires Linux and Python 3.11+. No dependencies or account credentials are needed.

```bash
python3 -m cloudmarks demo
python3 -m unittest discover -s tests -v
```

The demo uses only temporary local fixtures. See [M1 implementation and checks](docs/m1-status-engine.md) for the state model, failure handling, configuration and remaining integration work.

See [M2 implementation, screenshots, limits and reversible installation](docs/m2-nautilus-prototype.md) for the helper and Files prototype. The opt-in desktop harness is [here](tests/desktop/README.md).

See [M3 controls, real-account evidence, setup and limits](docs/m3-offline-controls.md) for the current workflow. Publishing or evicting a file requires its experimental view unmounted. The first version supports read-only viewing.

See [M4 change tracking, durable jobs, conflict review and folder intent](docs/m4-change-tracking.md) for the current controls and limits.

The unreleased [M6 hardening workflow](docs/m6-hardening.md) adds explicit conflict archival/export, same-identity retry, larger-file verification and a read-only health report. It prepares a separate supervised trial; physical and user acceptance remain open.

## Development principles

- Never label a file available offline unless a complete local copy exists.
- Keep credentials, personal files, and machine-specific configuration out of this repository.
- Test against disposable data before connecting experimental actions to real files.
- Keep cloud deletion separate from removing a local cached copy.

## Target environment

GNOME Files (Nautilus), with GNOME 50 as the initial development environment. Broader compatibility will be documented as it is tested.
