# Compatibility and validation boundaries

The published prerelease is `0.1.0a1`; `main` develops `0.1.0a2.dev0`.
CloudMarks remains experimental. MIT licensing and publication status are tracked
in the [release checklist](dev/release-checklist.md).

## Current main validation — 2026-10-22

The current development build was installed in the owner's native GNOME Files
session on the Ubuntu/GNOME/rclone versions listed below. The dedicated OneDrive
test-account trial passed personal acceptance, including uploads, badges, Keep
offline, Free local copy, automatic startup after reboot, and busy-view retry.
Both the source and installed-wheel suites passed 296 tests.

Version 4 settings add file/folder menu controls and background aggregate proofs,
including empty directories. Recursive selections are bounded to 10,000 entries;
file observation uses a bounded requested-path working set rather than a
128-entry library scan. Online preflight checks every retained file without the
old 128-file cap. It can still be slow for large retained libraries.

These current behaviors supersede the initial release's feature/size limits below.
See [folder controls](folder-controls.md), [automatic status](automatic-status.md),
and the [current validation record](evidence/m6/folder-controls-local.json).
Large real-account use, business/shared accounts, and physical storage reconnect
remain unvalidated. No general desktop or filesystem compatibility claim follows
from the test-account trial.

## Published 0.1.0a1 baseline and historical M3–M5 evidence

The following table and limits describe the earlier release validation, before
the development desktop trial and version 4 controls above.

| Component | Tested scope |
| --- | --- |
| OS/desktop | Ubuntu 26.04.1 LTS, x86_64; native GNOME Files 50.2.2 in an isolated Xvfb/Bubblewrap session. No daily-desktop deployment. |
| Kernel/storage implementation | Linux 7.0.0-38-generic; Linux `renameat2(RENAME_NOREPLACE)`, `/proc`, `flock` and FUSE are required. |
| Nautilus Python API | Nautilus 4.1 typelib and python3-nautilus 4.1.0-1build1, locally extracted for isolated tests. Other APIs/desktops untested. |
| Python runtime | Core supports 3.11–3.14. CI tests all four; local M5 results are recorded separately in the validation record. System Python also loads the optional adapter/helper. |
| Cloud transport | rclone 1.75.2; explicit private OneDrive origin rooted at a verified dedicated test subfolder. Graph v1.0 checks stable identity/ancestry/checksum. |
| Account | One personal-type OneDrive test drive, separate from the blocked personal-use account. Business/shared accounts and shortcuts are untested and not advertised as supported. |
| Managed content filesystem | NTFS3 on the selected data volume, including stacked autofs mount handling. M3/M4 controller/publication/recovery cases passed there. |
| Temporary/core fixtures | tmpfs on the development machine; Ubuntu CI runner filesystems are CI core coverage, not proof of FUSE/NTFS3 desktop behavior. |
| UI | Eight icon/list, zoom and light/dark cases, 2,000 entries, helper pause/exit/recovery, disable and uninstall passed. Readability/accessibility user review remains open. |

No general compatibility claim is made for Ubuntu 24.04's desktop, older Files,
macOS, Windows, removable FAT/exFAT, network filesystems or other rclone versions.
CI uses Ubuntu 24.04 for credential-free Python core/packaging only. The native
adapter needs matching system GI packages; a successful CLI wheel install does
not supply them.

Content verification costs are bounded in the display helper. In 0.1.0a1, files
over 8 MiB show unknown. M6 development adds explicit full hashing and short-lived
fingerprint evidence for larger account files; see [the workflow](dev/m6-hardening.md).
Folder aggregates require their own complete fresh manifest and at
most 64 files / 8 MiB. Recursive intent is limited to 128 entries including the
folder. M6 larger-file operational results are recorded separately. An offline view contains
retained local content, not every cloud placeholder; empty directory publication
is absent. Metadata freshness expires even while a snapshot remains readable.

Online mount preflight supports 1–128 retained files and checks them explicitly.
All local content controls require that experimental view unmounted. There is no
hot update/eviction, automatic watcher, upload, edit or merge support. Conflict
handling preserves and blocks; development M6 adds explicit archive/export and
same-identity retry, with no merge or cloud writes.

[M3 evidence](evidence/m3/validation.json), [M4 evidence](evidence/m4/validation.json)
and [M5 release validation](evidence/m5/validation.json) state their exact scope.
Proxy-blocked cloud loss, injected volume loss and process restart do not claim
physical unplug/replug or a full desktop logout/login. Those acceptance checks
remain required before daily deployment. No destructive full-disk test was run.
