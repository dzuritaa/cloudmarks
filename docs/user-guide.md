# CloudMarks user guide

CloudMarks shows OneDrive status badges in GNOME Files (Nautilus) and lets you
keep files or folders available offline. This guide covers installing, setting
up, everyday use and removal. CloudMarks is **experimental**: start with a test
account or a folder of disposable files, and keep backups of anything important.

## What you get

CloudMarks gives you two folders:

| Folder | What it is for |
| --- | --- |
| **Upload** (default `~/CloudMarks/Upload`) | Copy files here to upload them to your chosen OneDrive folder. Badges show the upload progress and confirmation. |
| **Offline** (default `~/CloudMarks/Offline`) | A read-only view of the same OneDrive folder. Files you keep offline open from your disk, even without a connection. |

Both folders work inside **one OneDrive folder you choose** during setup, not
your whole OneDrive.

## Requirements

- Linux with GNOME Files (Nautilus) 43 or newer. Tested on Ubuntu 26.04 with
  Files 50.2.2; other versions and desktops are untested.
- Python 3.11 or newer.
- [rclone](https://rclone.org/install/) 1.75 or newer.
- These system packages (names for Ubuntu and Debian):

  ```bash
  sudo apt install python3-nautilus gir1.2-nautilus-4.1 gir1.2-gtk-4.0 fuse3 pipx
  ```

  `python3-nautilus` lets Files load the CloudMarks badges and menu. `fuse3`
  lets rclone show OneDrive as a folder. GTK 4 draws the progress window.

## Install

```bash
pipx install git+https://github.com/dzuritaa/cloudmarks.git
```

To install a published release instead, download its wheel from
[GitHub Releases](https://github.com/dzuritaa/cloudmarks/releases) and run
`pipx install ./cloudmarks-VERSION-py3-none-any.whl`.

## Set up

```bash
cloudmarks setup
```

Setup checks the requirements, then walks you through these steps:

1. **Sign in.** rclone opens your browser so you can sign in to Microsoft. Your
   password and sign-in token stay with rclone; CloudMarks never shows them.
   Setup then shows the account email and asks you to confirm it.
2. **Choose a OneDrive folder.** Pick a top-level folder by number or type a path
   such as `Documents/CloudMarks`. Create new folders on OneDrive first. Shared
   folders and the whole drive are not allowed.
3. **Choose storage.** Pick where offline copies are stored, how much space they
   may use, and how much free space to always leave on the disk.
4. **Choose the two folders.** The defaults are `~/CloudMarks/Upload` and
   `~/CloudMarks/Offline`. They must be new or empty.
5. **Live updates (optional).** Choose whether Keep offline and Free local copy
   may run while the offline folder is open. This is new and off by default; see
   [Keep offline and free space](#keep-offline-and-free-space).
6. **Finish.** Setup writes your private settings, creates the offline store,
   installs the Files extension, and creates three background services. It asks
   before starting them and before turning badges on.

Setup ends by running `cloudmarks doctor`. If setup stops part way (for example,
the sign-in window was closed), run `cloudmarks setup` again; it continues from
the last completed step.

Close all Files windows and open Files again so it loads the extension.

To preview the plan without changing anything, run `cloudmarks setup --dry-run`.

## Everyday use

### Upload files

Copy files into the Upload folder. Their badge shows **Uploading**, then
**Needs checking** while CloudMarks confirms the upload with OneDrive, then
**Cloud only** once OneDrive has it. Small files can finish too quickly to see
the Uploading badge.

### Keep offline and free space

In the Offline folder, right-click a file or folder:

- **Keep offline** downloads it, verifies every byte, and keeps it on your disk.
  A progress window shows the download and lets you cancel or retry.
- **Free local copy** removes the local copy to save space. The file stays in
  OneDrive. Files kept offline by another selection stay on your disk.

Without live updates, CloudMarks briefly closes the Offline folder's view while
it changes your offline copies. If an application still has a file open there,
the progress window names it. Close it and click **Retry**. The Upload folder
can stay open.

With live updates turned on, the Offline folder stays open during these actions.
New copies appear within about a second; an application that already has a file
open keeps reading it normally. To turn live updates on or off later, set
`"live_updates": true` or `false` in `~/.config/cloudmarks/profile/controls.json`.

### What the badges mean

| Badge | Meaning |
| --- | --- |
| Cloud only | OneDrive has the file; there is no verified copy on this computer. |
| Uploading / Downloading | A transfer is in progress. |
| Saved locally | A complete, verified copy is on this computer. |
| Kept offline | A verified copy is on this computer and selected to stay there. |
| Partly offline | Some files in the folder are offline, or the folder changed. Choose Keep offline again to complete it. |
| Needs checking | CloudMarks cannot confirm the state right now, for example while offline or while the helper restarts. |
| Needs attention | A transfer or integrity problem needs review. |

CloudMarks only shows a positive badge when it has fresh evidence. When OneDrive
cannot be reached, a kept file stays readable but its badge changes to
**Needs checking**.

### Work offline

Without a connection, files you kept offline still open from the Offline folder.
To make the Offline folder show only your offline copies, set
`CLOUDMARKS_VIEW_FLAGS=--offline` in `~/.config/cloudmarks/profile/mount.env`
and restart the view:

```bash
systemctl --user restart cloudmarks-view.service
```

## Check health

```bash
cloudmarks doctor            # system, installation, folders, services
cloudmarks doctor --online   # also confirms the OneDrive account and folder
```

Each line says PASS, WARN or FAIL, with a suggested fix. The report contains no
paths, account names, IDs or passwords, so you can paste it into a bug report.

For recent diagnostic events, run `cloudmarks logs` (`--follow` to watch).
These logs also exclude file names, paths, IDs and tokens.

## Start, stop and restart

CloudMarks runs as three user services:

| Service | Role |
| --- | --- |
| `cloudmarks-upload.service` | The Upload folder |
| `cloudmarks-status.service` | The status helper behind the badges |
| `cloudmarks-view.service` | The Offline folder |

```bash
systemctl --user stop cloudmarks-view.service cloudmarks-status.service cloudmarks-upload.service
systemctl --user start cloudmarks-upload.service cloudmarks-status.service cloudmarks-view.service
cloudmarks disable   # hide badges without stopping anything
cloudmarks enable    # show badges again
```

Stop the upload service only after uploads have finished.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| No badges or menu items | Run `cloudmarks doctor`. Install the missing Nautilus packages if listed, run `cloudmarks enable`, then close all Files windows and reopen Files. |
| Every badge says Needs checking | The status helper or network is unavailable. Check `cloudmarks doctor` and start any stopped service. |
| Keep offline asks you to close an application | Close it (or its Files tab in the Offline folder) and click Retry, or turn on live updates. |
| The sign-in expired | Run `rclone config reconnect upload: --config ~/.config/cloudmarks/profile/upload.conf` and the same for `origin:` with `transport.conf`, choosing the same account. |
| Files copied to the Upload folder while it was stopped | They are on your disk, not in OneDrive. Move them somewhere safe, start the upload service, then copy them in again. |

## Remove CloudMarks

This keeps your offline copies and settings until you delete them yourself.
Nothing is deleted from OneDrive.

```bash
systemctl --user disable --now cloudmarks-view.service cloudmarks-status.service cloudmarks-upload.service
cloudmarks uninstall
rm ~/.config/systemd/user/cloudmarks-{upload,status,view}.service
systemctl --user daemon-reload
pipx uninstall cloudmarks
```

After checking you no longer need them, you can delete the private settings in
`~/.config/cloudmarks/profile`, the state in `~/.local/state/cloudmarks`, and the
offline store folder you chose during setup.

## Glossary

- **Offline view**: the read-only Offline folder, which combines OneDrive with your
  verified local copies.
- **Offline store**: the private folder where CloudMarks keeps verified copies.
  Do not edit it by hand.
- **Status helper**: a small background program that answers badge requests from
  Files without slowing it down.

For the configuration files and every command, see the
[configuration reference](reference/configuration.md) and
[command reference](reference/cli.md). Advanced manual setup is described in the
[operations guide](operations.md).
