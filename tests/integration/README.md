# Opt-in dedicated-account integration

See [M3 setup, results and constraints](../../docs/m3-offline-controls.md).
`run_offline.py` requires explicit private configuration and an existing disposable
file inside the independently verified allowed folder in the dedicated account.
It implements no cloud provisioning, upload or deletion. It initializes a new
store by default; `--reuse-owned-store` only reuses an already bound store after
normal volume/account/root checks. It downloads, verifies, mounts online and
through two credential-free offline processes, checks EROFS, unpins/frees, then
independently rereads remote content and metadata. All views are stopped normally.

The test's result contains only fixture hashes, byte counts and assertions.
Do not publish private configuration, raw rclone/Graph diagnostics or account
identifiers. Use a small disposable fixture first. It does not replace physical
network/disk, logout/login, larger-file or daily-desktop acceptance checks.


## M4 change/recovery matrix

`run_tracking.py --config /private/new-offline.json --result /private/result.json`
requires a new separate store/view and the same verified disposable allowed root.
It refuses adoption of an existing store. This runner **does mutate cloud test
data**: it creates one uniquely named folder and three files, updates two files,
renames one and deletes exactly its own `delete.txt` fixture with an eTag
precondition. It never deletes the folder, other fixtures or an arbitrary item.
The production `OneDrive` class still implements GET/download only; mutation
code lives exclusively in this opt-in test harness. Use only the dedicated test
account and explicit scoped private configuration.

The runner verifies every created identity and its ancestry/effective transport
identity, retries jobs after a rejecting proxy in a fresh process, preserves an
old revision, restarts the credential-free offline view twice, checks folder
status/EROFS and then tests local/remote conflict, rename and remote disappearance.
The conflicted snapshots and remaining cloud fixtures are deliberately retained
for review. The result JSON is sanitized; the companion fixture record and
configuration stay private. It does not simulate full logout or physically unplug
the data disk/network, and it does not install anything in the daily desktop.
