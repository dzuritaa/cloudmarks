# M0.3 backend probes

These are disposable experiments, not the production backend. Read
[ADR 0001](../../docs/decisions/0001-offline-backend.md) for the decision,
results and limitations.

## Local-only origin failure

Requires the existing rclone binary, FUSE and `fusermount3`. These commands
create their own HTTP server and mounts. They never contact OneDrive, modify
global networking or touch an existing cache.

```bash
python3 spikes/backend-feasibility/rclone_cache_probe.py --work /tmp/cloudmarks-vfs-probe
python3 spikes/backend-feasibility/rclone_cache_probe.py --layout union --work /tmp/cloudmarks-union-probe
```

Keep each work directory separate. The first tests ordinary full VFS caching;
the second tests a complete local file in an ordered, read-only union.

## Isolated OneDrive account

This probe **creates a disposable folder and file in the explicitly configured
test account**. It requires the private settings already established for this
project, the personal-drive block record, and a dedicated test rclone config.
It refuses a mismatched account or drive. Do not run it with a personal account.

```bash
python3 spikes/backend-feasibility/onedrive_union_probe.py \
    --settings /private/test/settings.json \
    --blocked-drive-file /private/test/blocked-personal-drive.json \
    --config /private/test/rclone.conf \
    --work /selected-data-disk/private-new-experiment
```

Private settings contain `expected_drive_id` and `expected_email`; the block
record contains `blocked_drive_id`. The rclone remote defaults to
`cloudmarks-test`. The work directory must be new and private; use `--resume`
only to reuse that exact experiment's saved allowed-root record after a failure.
Never copy private run files into Git. Only `result.public.json` is intended as
sanitized evidence.

The fixture is provisioned directly by verified Graph folder ID. rclone reads
from a separately generated config whose root uses normalized drive/item IDs.
All local content is verified before use. The read-only experimental union is
unmounted between runs and before local eviction. A rejecting proxy simulates
cloud loss for the test processes only, and a direct remote-read canary must
fail before the offline restart test can pass.

The test leaves its cloud fixture in the disposable folder. It has no remote
delete command. All ordinary mounts are removed on success or controlled
failure; the existing desktop mount/service is not changed. If a process is
forcibly killed, inspect its private `view` path and unmount that experiment
with `fusermount3 -u` before removing local work files.
