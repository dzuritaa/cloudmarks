# Physical acceptance tests

Three checks remain before CloudMarks can be recommended for everyday use with
important files: real network loss, disconnecting the store's disk, and a full
logout and login. Software tests cannot simulate them faithfully, so a person
runs them. Use the dedicated test account and disposable files only.

Each test uses [`run_physical.py`](../../tests/acceptance/run_physical.py) to
record sanitized snapshots before and after: doctor results, and re-verification
of every retained copy against its recorded checksum. The output contains only
counts and outcomes. Save it outside the repository, review it, then add it under
`docs/evidence/` with the date.

## Before any test

- Back up anything important. Save open work; the session test logs you out.
- Run `cloudmarks doctor` and fix any failure.
- Keep at least two disposable files offline, one of them 50 MB or larger so a
  download takes long enough to interrupt.

## 1. Network loss

```bash
python3 tests/acceptance/run_physical.py --test network --phase before --output ~/cm-network.json
```

1. In the Offline folder, choose **Keep offline** on a disposable folder or large
   file that is not offline yet.
2. While it downloads, disconnect the network (turn off Wi-Fi or unplug the cable).
3. Expect: the progress window reports an error or stops making progress within
   about a minute; no badge turns positive for the interrupted file.
4. Open a file that was already kept offline. Expect: it opens normally.
5. Wait one minute. Expect: kept files show **Needs checking**, not an error.
6. Reconnect. Click **Retry** in the progress window. Expect: the download
   completes and the file shows **Kept offline**.

```bash
python3 tests/acceptance/run_physical.py --test network --phase after --output ~/cm-network.json
```

## 2. Disk disconnect and reconnect

This needs the offline store on a **removable disk** that holds nothing else you
depend on. If the store shares a disk with the system or this repository, run
`cloudmarks setup` for a separate profile on a spare USB disk first, or skip this
test and record why.

```bash
python3 tests/acceptance/run_physical.py --test disk --phase before --output ~/cm-disk.json
```

1. Make sure no Keep offline or Free action is running.
2. Close Files windows showing the Offline folder. Safely remove the store's disk
   from the Files sidebar if possible; otherwise unplug it.
3. Expect: badges change to **Needs checking**; nothing claims a file is offline;
   `cloudmarks doctor` reports the store as unavailable.
4. Reconnect the disk and let it mount at the same place.
5. Restart the view: `systemctl --user restart cloudmarks-view.service`.
6. Expect: `cloudmarks doctor` verifies the disk marker again; kept files show
   **Kept offline** and open normally.

```bash
python3 tests/acceptance/run_physical.py --test disk --phase after --output ~/cm-disk.json
```

## 3. Logout and login

```bash
python3 tests/acceptance/run_physical.py --test session --phase before --output ~/cm-session.json
```

1. Make sure no transfer is running and the services are enabled
   (`systemctl --user is-enabled cloudmarks-view.service`).
2. Log out completely, then log back in.
3. Expect: the three services start by themselves; badges reappear in a new Files
   window within about a minute; kept files open normally.

```bash
python3 tests/acceptance/run_physical.py --test session --phase after --output ~/cm-session.json
```

## Recording results

The `after` phase prints PASS or FAIL findings and asks whether the observed
behaviour matched the expectations above. Any FAIL, or an unexpected observation,
blocks the everyday-use recommendation until it is understood and fixed. Update
the [release checklist](release-checklist.md) when all three pass.
