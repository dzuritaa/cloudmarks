# What you need to do

## Current account setup

The separate Microsoft test account is now authenticated, its drive identity is verified, and its test mount is active. The account email and drive identifier are kept in private local configuration rather than this public checklist.

Your personal OneDrive account is disconnected from Ubuntu. Use only the test account and disposable files for CloudMarks validation. No additional sign-in is needed unless the test connection expires or is deliberately reset.

The test folder uses files on demand; it is not downloading the whole account. This account setup does not install CloudMarks or complete its offline-storage milestones.

Each step below is tied to the milestone that introduces the risk it protects against. Nothing here needs to be done earlier than its section says.

## M0–M4 implementation

M0 ran in isolated test environments; the daily Files process was not restarted.
Review [the badge screenshots](docs/m0-presentation.md) and
[the backend decision](docs/decisions/0001-offline-backend.md). M1's local
status engine and tests are described [here](docs/m1-status-engine.md).
M2's actual helper and extension now pass isolated desktop tests. Review
[its screenshots and results](docs/m2-nautilus-prototype.md). No CloudMarks
service or daily-desktop extension has been enabled. M3 now adds separate
CLI controls with [real-account evidence and operating limits](docs/m3-offline-controls.md).

The controlled OneDrive experiments left two small fixtures in uniquely named
disposable folders in the test account. Their locations and run configuration
are recorded privately on this machine. M0–M3 made no cloud deletion; M4’s
separate opt-in harness mutates only files it provisions for its acceptance matrix.
Physical network disconnection, logout/login and disk reconnection tests are
still needed before everyday use; the experiments simulated cloud loss only
for their own processes.

## Right now

Keep using the dedicated test connection normally. The M2 adapter stays read-only. M3 uses independent private configuration and a separate experimental view/store for the same verified disposable test area. The personal account stays disconnected.

- Review [the M2 result](docs/m2-nautilus-prototype.md) and [the plan](PLAN.md). The chosen backend is an owned verified offline store with rclone transport and a read-only union view. The existing mount streams content; a badge alone cannot create offline availability.
- Keep access to the connected test Microsoft account. If reauthentication is required later, sign in yourself through Microsoft's browser page and select that test account explicitly. Passwords, tokens, and recovery codes must not be sent in chat or committed to Git.
- Have a few disposable documents or pictures available for later testing. A test folder will be configured inside that separate account; do not provide important files or shared folders from your personal account as test data.

## Desktop and backend checks already run

M0.1 and M2 used real Nautilus in an isolated desktop with locally extracted
Python integration packages. The daily Files process was not restarted and no
system packages were installed. Do not install the disposable M0 spike alongside
the actual M2 extension.

M0.3 performed controlled download/offline-read/local-eviction experiments using
separate storage and the verified test account/root. Its design decision and
limits are [recorded here](docs/decisions/0001-offline-backend.md).

## Reviewing the read-only prototype (M2)

No setup is required to review [the saved screenshots](docs/m2-nautilus-prototype.md#desktop-evidence).
The isolated tests already checked accurate fixture states, live refresh,
2,000 entries, stalled/dead helper recovery, runtime disable, unrelated folders
and uninstall. User readability feedback is still useful:

- Are cloud, local check, offline pin, download, error and question symbols clear?
- Is the status column useful at your normal zoom and appearance?

For a later deliberate daily-desktop test, the matching Nautilus Python loader
and typelib must be installed system-wide. That administrator step is separate
from the user-only installer. Follow [M2's installation instructions](docs/m2-nautilus-prototype.md#user-only-installation-disable-and-removal)
only in the chosen test session. A new Files process is needed to load a newly
installed extension; save work before manually restarting it. Once loaded,
enable/disable is picked up without a restart and never changes the mount.

M2 reads artificial fixture evidence and displays badges. Browsing triggers no
transfers. It has no download, eviction, cloud-delete or upload action. Removal
was tested before first enablement, and fixture data is preserved by uninstall.
Your usual backups are sufficient for this read-only prototype.

## Individual-file offline controls (M3)

The CLI now implements keep, retry/cancel, unpin, local-only free and recovery.
The [M3 result](docs/m3-offline-controls.md) records real checksum verification,
two offline mount restarts, EROFS write rejection and cloud-preserving local
eviction. The actual read-only M2 adapter also displayed the retained file in an
isolated Files session. No cloud file was created, uploaded or deleted by M3.

No additional sign-in, administrator package installation or daily Files restart
is required to review this evidence. A small verified pin remains in private
experimental storage; all experimental views/helpers were stopped.

Before a deliberate everyday test, keep separate backups of important files,
read the unmount-before-control workflow, and finish physical network/disk and
logout/login checks. A system restore point is relevant only if a later backend
setup installs system packages; none were installed here. A snapshot does not
replace cloud-file/configuration backups.

The current controls accept only explicit private test configuration. Keep using
disposable files. Close files and stop only the experimental foreground view
before changing its local content. Unpin and eviction are separate commands;
free requires fresh matching remote evidence. Never point this at important or
personal files as an unverified cleanup tool.

## Change tracking and recovery (M4)

[The M4 workflow](docs/m4-change-tracking.md) adds explicit stable-ID refresh,
durable download jobs and bounded recursive folder pins. M2 remains read-only.
Unpinning a folder retains its files and independent direct/other-folder pins.
Renames, unavailable items and changed raw-store files stop automatic replacement;
verified remote candidates are retained separately for review. Production has no
upload or cloud-delete action. There is no merge or in-place resolve command.

The opt-in real-cloud test uses a fresh unique folder in the existing disposable
allowed root, including its own deliberate revision, rename and deletion fixture.
Its local conflict store is retained independently of the M3 working snapshot.
Physical disk/network unplug/replug and full logout/login checks remain pending;
no daily desktop install is implied by M4 completion.

## Implementation boundaries

- Save a private backup of the current OneDrive mount service, configuration, and relevant Files settings before integration changes. Credential-bearing backups stay outside this public repository with restrictive permissions.
- Build tests, installation, disable, and uninstall procedures before enabling the extension.
- Use fixture data first, then restrict cloud tests to the disposable area in the separate Microsoft account. Use independent credentials, mount, cache, state, and service configuration, and verify the test drive identity before enabling cloud operations.
- Keep file content on the selected data disk and verify storage limits before enabling offline downloads.
- Leave the current connection in place until a tested replacement or integration is ready.

## M5 release preparation

The `0.1.0a1` candidate now has CI, package checks, compatibility information and
[install/upgrade/rollback/recovery instructions](docs/operations.md). The optional
status-helper service is only a template; it was not installed or enabled. M2
remains read-only and your working mount is preserved.

MIT is selected and applied. Personal account dzuritaa is verified for both Git
and the CLI, workflow authorization is complete, and main is pushed with hosted
CI passing. Follow [the release checklist](docs/release-checklist.md) for the
version-tag publication outcome. No work repository was changed.
Physical reconnect/logout and daily-use acceptance
are still pending; they were not replaced by package/CI checks.
