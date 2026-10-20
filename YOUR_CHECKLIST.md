# What you need to do

## Right now

Nothing needs to be installed to begin development. The first work uses fake files and a test backend. Keep using your existing OneDrive connection normally.

- Read [the plan](PLAN.md), especially the offline-storage feasibility gate. The existing mount streams content; a badge alone cannot create Windows-style offline behavior.
- Have a few disposable documents or pictures available for later testing. A separate cloud test folder will be configured when cloud integration is ready; do not provide important files as test data.

## Before testing the extension on your desktop

1. **Save work and close active file transfers.** Enabling or disabling an extension can require restarting Files. A separate test session or VM is preferable for the first desktop test.
2. **Back up important personal files separately.** A system snapshot does not protect the contents of your cloud account. Back up only the important subset if a full cloud copy will not fit, and keep that backup outside the experimental mount/cache.
3. **Create a system restore point if practical.** Timeshift is a suitable option. For an ext4 Ubuntu system, use its RSYNC mode, check the estimated space, and choose a supported Linux filesystem for the snapshot destination. Do not reformat an existing data disk. Timeshift normally excludes home directories, so it is not a replacement for step 2 or the private configuration backup below.
4. **Install the Nautilus Python integration when the prototype is ready.** On the initial Ubuntu development system, `python3-nautilus` is available but not installed. We will verify the exact dependency command at that stage. An administrator password may be needed; enter it locally in Ubuntu, never in chat.

Timeshift and other backup programs are not installed or configured by this planning step. There is no need to take repeated snapshots while we are only editing project files and running isolated tests.

## What the implementation work will handle

- Save a private backup of the current OneDrive mount service, configuration, and relevant Files settings before integration changes. Credential-bearing backups stay outside this public repository with restrictive permissions.
- Build tests, installation, disable, and uninstall procedures before enabling the extension.
- Use fixture data first, then restrict any cloud tests to the disposable area.
- Keep file content on the selected data disk and verify storage limits before enabling offline downloads.
- Leave the current connection in place until a tested replacement or integration is ready.

## When the prototype is ready

You will be asked to check visible behavior in Files:

- Are badges readable in icon and list views?
- Does an explicitly downloaded test file open offline?
- Does **Free up local space** preserve the copy on the OneDrive website?
- Does Files remain responsive, and does disabling the extension remove its badges?

We will provide the exact steps and disposable filenames with that build. Do not test an unverified **Free up local space** action on important files.

## Before a public release

Choose the project's license. The repository does not currently contain one. No additional GitHub account setup is required for the existing personal repository.
