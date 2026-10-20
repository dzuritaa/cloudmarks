# CloudMarks handoff — M0 through M5 candidate

M0 feasibility, M1 status engine, M2 read-only Nautilus adapter and M3
experimental individual-file controls are implemented. M2 remains read-only;
M3 actions are separate explicit CLI commands. Nothing is installed in the daily
desktop. M4 now adds explicit read-only change checks, durable download jobs, conflict
snapshots and bounded recursive intent. M5 now prepares the 0.1.0a1 candidate, CI, package checks and operating guides.
Hosted CI/publication and physical
reconnect/logout acceptance remain open.

## Start here

- [M5 release gates and publication status](release-checklist.md).
- [Installation, upgrades, rollback and recovery](operations.md).
- [Tested compatibility and limits](compatibility.md).

- [M4 commands, conflict review, folder intent and remaining acceptance](m4-change-tracking.md).

- [M3 workflow, real-account evidence and limits](m3-offline-controls.md).
- [M2 historical desktop evidence](m2-nautilus-prototype.md).
- [Backend decision](decisions/0001-offline-backend.md) and
  [storage design / M3 review](offline-store-design.md).
- [M1 state model](m1-status-engine.md).

```bash
python3 -m cloudmarks demo
python3 -m unittest discover -s tests -v
python3 -m cloudmarks offline --help
```

The default tests are local. Optional independent QuickXor vectors use the local
rclone binary, without an account. Real-account and desktop runners are opt-in
under `tests/integration` and `tests/desktop`.

## M3 behavior

Explicit private account/root settings, Graph ancestry, rclone effective item
identity, server checksum, local SHA-256 and repeated revision checks guard every
completed download. Transfers use bounded blocks and emit byte progress. Pin
intent survives cancellation/failure/restart. Retry starts verification anew.

The store has independent immutable objects and visible files. Physical device,
UUID, marker and a private account/state/view binding prevent wrong-disk and
alternate-configuration adoption. Allocated/logical bytes, retained candidates
and both new copies count against the quota; writes check the free-space floor.

Schema 2 adds publication records and durable filesystem intent. Failed or
abrupt publication/eviction is rolled back before a new view starts; old data is
not promoted or deleted on ambiguity. Old versions and uncommitted candidates
remain recoverable and count against the budget. There is no automatic history
or orphan garbage collection.

All controls require the experimental view unmounted. The online mount is a
local-first read-only union; explicit offline mode is a credential-free local
alias at the same path. Both use kernel `ro` and rclone read-only policy.
Unpinning does not evict. Free requires an unpinned, idle, clean copy with fresh
matching remote evidence; it removes only private local paths, never a mounted
path or remote file. The adapter/socket expose only status. A read-only SQLite
connection can observe a separate control process without taking its writer
lock or mutating recovery state.

## M0–M3 historical validation

All **149 automated tests** pass on Python 3.14.4 and 3.11.16. M3's new checks also
pass on the selected NTFS3 disk. They include cancellation at final metadata
checks, checksum/revision races, budget/free-space failures, injected ENOSPC,
link/dirty-file protection, scope checks, safe credential diagnostics, abrupt
process death during publication/eviction and SQLite commit failure. See
[the validation record](evidence/m3/validation.json) for run scope/timings.

The existing real 100 KiB test file matched its server QuickXor and local SHA-256,
opened after two offline mount restarts with pin recovery, and rejected modify,
create, rename and delete through the view. Local eviction preserved cloud item,
revision and content. M3 made no cloud writes or deletes.

M2's eight view/zoom/style cases, 2,000-entry responsiveness, helper pause/exit and
recovery, disable and uninstall passed again. A second isolated Files scene
showed the real retained file as **Kept offline (test account, read-only)** through
the unchanged status-only protocol. Source/wheel packaging, installed CLI/assets,
lint/format/compilation, documentation links and sanitized evidence were checked.
No release was published.

## Private state and next steps

The current M3 config/result paths are recorded in
`~/.config/cloudmarks-test/m3-work.json` (private, outside Git). That record points
to separate credential/config/state/store/view directories. Backups of the prior
test config/service are private. One verified 100 KiB pin plus prior recoverable
fixture snapshots remain locally. All experimental views/helpers were stopped.

The existing dedicated test-account mount/service remains active; it was not
replaced or reconfigured. No daily extension/settings, CloudMarks service, system
package installation, personal-account reconnection or daily Files restart
occurred. The M0 disposable cloud fixtures remain unchanged.

Before daily use, finish physical network loss, disk unplug/replug, logout/login,
larger real-file, readability and accessibility checks. The controlled offline
view has no origin configured; the host remains online. No physical full-disk
test was run. Mounted updates/context menus, placeholder catalogs, automatic watching, editing,
uploads and two-way sync remain outside this implementation. M4 adds bounded
manifest coverage with explicit refresh; it does not make a perpetual folder
completeness claim. Stale evidence stays unknown even when the last
verified offline snapshot remains readable.

Changes, including the prior documentation work, remain in the working tree.
No commit or push was made.


## M4 continuation

All **184 automated tests** pass on Python 3.14.4 and 3.11.16. The 29 new
store/controller cases also passed on the selected NTFS3 disk; six Graph-only
cases use ordinary temporary directories. The native M2 eight-case/2,000-entry
regression passed again. Real OneDrive tests passed proxy-loss/fresh-process
recovery, revision replacement, conflict preservation, rename and disappearance,
and two credential-free offline mount restarts with folder state and EROFS.
Three deliberately unresolved conflicts remain preserved in the independent M4
store for review; all experimental views/helpers were stopped. Physical network/
disk unplug and full logout/login are still pending.

Schema 3 adds jobs, metadata history, issues, candidate snapshots, folder
manifests and independent direct/folder pin sources. Existing stores migrate
under the writer lock. The M2 helper opens state read-only and never migrates or
recovers jobs. `offline jobs`, `conflicts`, `snapshots` and `changes` are read-only
inspection commands; all other M4 controls retain the unmounted gate.

Use `queue`/`resume` for durable individual work, `refresh` to check stable IDs,
`queue-tree` to atomically replace a bounded complete manifest, and `unpin-tree`
to remove only that recursive pin source. See the M4 document before operating a
conflicted store. There is no automatic conflict resolution or garbage collection.

M4 private work/config/result locations are recorded separately in
`~/.config/cloudmarks-test/m4-work.json`. The M3 retained store/config and daily
test-account mount were preserved. The cloud matrix uses a new unique disposable
folder; its harness alone provisions/updates/renames/deletes its own test files.
Production still contains no remote write/delete feature. Review the sanitized
[M4 validation record](evidence/m4/validation.json) for exact run results and
operational exclusions. Prior M0–M3 evidence remains historical.


## M5 candidate

All 190 tests pass locally on Python 3.11.16, 3.12.14, 3.13.15 and 3.14.4.
The isolated installed wheel also passes all 190 tests. Strict package metadata,
archive/source inventory, source-distribution rebuild payload, workflow lint,
compilation, documentation links and a real isolated upgrade/rollback cycle pass.
The release gate rejects unlicensed candidates; the owner has now selected MIT and the candidate is licensed. No
hosted CI or publication has run; MIT is applied; hosted publication remains pending.

Version 0.1.0a1 adds --version, alpha metadata, pinned development/build tools,
credential-free Python 3.11–3.14 CI, wheel/source inventory validation, installed
wheel tests outside the checkout, source rebuild comparison and local docs checks.
GitHub actions are pinned to verified commit IDs and use read-only permissions by
default. A tag-only workflow validates licensing/version and publishes an experimental
prerelease only for a deliberate version tag; ordinary branch pushes never publish. No hosted workflow has run yet.

The optional cloudmarks-status.service template starts only the status helper.
It does not mount, hydrate or install itself. Operations/compatibility/recovery
and upgrade/rollback instructions are in the new guides. A real isolated XDG
old-snapshot → alpha → old-snapshot cycle preserved fixture bytes/state and an
unrelated extension, then removed its deployments. Schema downgrade is untested
and unsupported; rollback requires compatible code or a stopped consistent bundle.

The owner selected MIT; LICENSE and SPDX metadata are applied. The current GitHub API connection reports
pull=true, push=false. Separately, Git transport passed a push dry-run with no
ref created. Actual source/workflow push and release publication have not been
attempted; the API and Git transports may use different credentials. Source/CI/notes/artifacts are prepared locally; nothing was
committed, pushed, tagged or published. The repository's current remote HEAD
tracks only historical docs, so publishing a tag at that HEAD would omit the
implementation. A release must include the reviewed implementation and M5 files.
See [M5 validation](evidence/m5/validation.json) for local run results and the
remaining gates. No real account/daily desktop was altered for M5.


## MIT decision and concrete publication blocker

The owner selected MIT. LICENSE and SPDX metadata are applied. The licensed
candidate passed all 190 local tests, installed-wheel tests, strict package
metadata and the release-content gate. Commit e167f04 contains the complete
implementation and CI/release files. A normal push of main was rejected by GitHub
because the OAuth credential lacks workflow scope for .github/workflows/ci.yml.
The earlier successful dry-run did not establish workflow write authorization.
No remote ref/tag/release was created and no hosted CI ran. Reauthorize the local
GitHub connection with workflow write permission, then push the existing commits;
do not strip the workflows to bypass the permission requirement. M3/M4 stores,
the daily mount and desktop were not changed. The release checklist and M5 record
now capture this actual failure rather than treating publication as complete.
