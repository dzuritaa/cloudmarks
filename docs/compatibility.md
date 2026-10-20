# Compatibility and validation boundaries

The initial candidate is `0.1.0a1`. This is an experimental disposable-data viewer,
not a production replacement for Windows Files On-Demand. MIT licensing and publication
status are tracked in the [release checklist](release-checklist.md).

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

Content verification costs are bounded in the display helper: files over 8 MiB
show unknown; folder aggregates require their own complete fresh manifest and at
most 64 files / 8 MiB. Recursive intent is limited to 128 entries including the
folder. Larger real-file operational tests remain open. An offline view contains
retained local content, not every cloud placeholder; empty directory publication
is absent. Metadata freshness expires even while a snapshot remains readable.

Online mount preflight supports 1–128 retained files and checks them explicitly.
All local content controls require that experimental view unmounted. There is no
hot update/eviction, automatic watcher, upload, edit or merge support. Conflict
handling preserves and blocks; repair needs a separately reviewed workflow.

[M3 evidence](evidence/m3/validation.json), [M4 evidence](evidence/m4/validation.json)
and [M5 release validation](evidence/m5/validation.json) state their exact scope.
Proxy-blocked cloud loss, injected volume loss and process restart do not claim
physical unplug/replug or a full desktop logout/login. Those acceptance checks
remain required before daily deployment. No destructive full-disk test was run.
