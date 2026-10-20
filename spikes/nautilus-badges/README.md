# M0.1: disposable Nautilus presentation spike

This is hard-coded fixture code, not the CloudMarks extension. Do not reuse it
in M2 or load it into your daily Files process.

The runner uses Bubblewrap, a new DBus session and Xvfb. It hides the real home
and runtime sockets, disables networking, and mounts the host read-only except
for the selected work directory and a private temporary filesystem. Python extension installation and
removal happen under that sandbox's `XDG_DATA_HOME`, never the daily desktop.

On Ubuntu 26.04 amd64, download and extract these packages into a temporary
sysroot (no package installation or sudo):

```bash
mkdir -p /tmp/cloudmarks-m0/packages /tmp/cloudmarks-m0/sysroot
cd /tmp/cloudmarks-m0/packages
apt-get download python3-nautilus gir1.2-nautilus-4.1 xvfb xserver-common
for package in ./*.deb; do
    dpkg-deb -x "$package" /tmp/cloudmarks-m0/sysroot
done
```

From the repository root:

```bash
python3 spikes/nautilus-badges/run_isolated.py \
    --sysroot /tmp/cloudmarks-m0/sysroot \
    --work /tmp/cloudmarks-m0/desktop
```

Prerequisites already provided by the development desktop: `bwrap`,
`dbus-run-session`, `xauth`, system Python with PyGObject and Pillow, GTK4,
Nautilus 50, and Yaru icons. Extracted package dependencies must match the host;
the runner intentionally does not change system packages.

Outputs: eight content-pane screenshots (icon/list × small/large × light/dark), a baseline
after removing the extension, callback events, icon-name availability, and
result JSON. Review screenshots and logs together; a live process alone does
not establish that badges were visible. Logs contain fixture basenames only.

The private DBus instance disables only its own AppArmor mediation because the
read-only sandbox cannot perform securityfs policy queries. System AppArmor
policy stays in place. GLX is disabled for Xvfb so it does not initialize the
host's GPU driver; Nautilus uses the Cairo renderer. No desktop services are
activated, so missing Tracker/volume-monitor warnings are expected.

The `06-refresh.txt` badge changes after an asynchronous invalidation. The
unmanaged fixture must remain unbadged. All labels explicitly identify fixture
state. Delete the temporary work directory to remove the spike environment.
