# Live view updates probe

Question: can CloudMarks publish or evict verified copies while the read-only
offline view stays mounted, instead of unmounting it for every Keep offline or
Free local copy? See [ADR 0002](../../docs/decisions/0002-live-view-updates.md)
for the decision.

The probe needs rclone, FUSE and `fusermount3`. A local directory stands in for
the OneDrive origin, so no account, network or existing mount is involved. The
mounts use the same flags as `cloudmarks/view.py` (`--read-only`,
`--vfs-cache-mode=off`, `--dir-cache-time=1s`, `--poll-interval=0`).

```bash
python3 spikes/live-view/live_view_probe.py --work /path/on/the/store/disk/new-dir
python3 spikes/live-view/live_view_probe.py --work /tmp/new-dir
```

The work directory must not exist; it is removed afterwards.

## Results (2026-10-26, rclone v1.75.2, Linux 7.0)

Identical outcomes on NTFS3 (the reference store disk) and tmpfs. Files are
8 MiB with different bytes for the "cloud" and "local" versions, so mixed reads
would be detected.

| Scenario | Online view (union) | Offline view (local only) |
| --- | --- | --- |
| Publish a new file | Visible with correct bytes in 0.7–1.0 s | Visible in 0.8–1.0 s |
| Publish over a cloud-only file | Local copy served in about 1.0 s | — |
| Evict a closed file | Falls back to the cloud copy in about 1.0 s | Disappears in about 1.0 s |
| Evict while a reader has it open | Reader finishes with the complete local bytes; new opens get the cloud copy | Reader finishes with the local bytes; the file then disappears |
| Publish while a reader has the cloud copy open | Reader finishes with the complete cloud bytes; new opens get the local copy | — |
| Publish a folder tree (nested and empty folders) | Visible in about 1.0 s | Visible in about 1.0 s |
| Normal unmount afterwards | Clean | Clean |

No scenario produced mixed or truncated content. Unlinking an open file on
NTFS3 also behaves like other Linux filesystems: the open handle keeps reading.

## Limits

- A local directory replaces the OneDrive backend; the real union combines the
  store with the scoped Graph remote. rclone's union backend has no cache of its
  own, so the VFS directory cache governs both cases, but this was not measured
  against the real account.
- Badge truth does not depend on the view: status proofs verify stored bytes
  directly and require the attested CloudMarks mount.
- The probe exercised one reader at a time.
