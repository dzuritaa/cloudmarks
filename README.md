# CloudMarks

Cloud and offline status badges for GNOME Files, starting with OneDrive through rclone.

## Project status

Initial project setup. The extension is not implemented yet.

## Planned first version

- A Nautilus extension that displays cloud-only, saved locally, transferring, and error states.
- A separate helper that manages file state without blocking Files.
- Explicit download and offline controls, with local data stored on a user-selected disk.
- Installation for the current user and a documented uninstall path.

The first milestone will focus on accurate status indicators and disposable test files. Transfer and space-reclamation actions will follow once their behavior is verified.

## Development principles

- Never label a file available offline unless a complete local copy exists.
- Keep credentials, personal files, and machine-specific configuration out of this repository.
- Test against disposable data before connecting experimental actions to real files.
- Keep cloud deletion separate from removing a local cached copy.

## Target environment

GNOME Files (Nautilus), with GNOME 50 as the initial development environment. Broader compatibility will be documented as it is tested.
