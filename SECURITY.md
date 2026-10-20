# Security and data-preservation reports

CloudMarks is an experimental read-only viewer, not a backup or production sync
client. Stop testing if data preservation, root containment or status truth fails.
Preserve the stopped private store/state/config bundle and all candidate versions;
do not delete evidence or reconnect another account to work around a failure.

Do not post tokens, emails, drive/root IDs, private names/paths, configurations,
SQLite databases, raw Graph/rclone responses or service logs in public issues.
Use synthetic names and locally redact diagnostics. A useful ordinary report has
CloudMarks/Python/OS/Nautilus/rclone versions, a minimal artificial reproduction,
expected/actual behavior and whether the view was mounted. The status helper is
read-only; installation or badges do not authorize cloud writes or transfers.

For a sensitive vulnerability, use GitHub's private vulnerability reporting on
this repository if that feature is available. If it is unavailable, contact the
repository owner through an existing private channel; no monitored security email
or response-time guarantee has been established. Do not create a public issue
containing an exploit or private data just to reach the maintainer. Public reports
should be limited to nonsensitive information.

Dependencies are installed only in development environments. CI requires no
account credentials, cloud access or privileged desktop setup. M5 does not enable
a daily service, replace the user's mount, install system packages or reconnect
the personal account. Review [operating limits](docs/operations.md) and the
[pending acceptance checks](docs/release-checklist.md) before deployment.
