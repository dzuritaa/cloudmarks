# Real Nautilus M2 acceptance harness

See [the M2 result and reproduction instructions](../../docs/dev/m2-nautilus-prototype.md).

`run_isolated.py` uses Bubblewrap, a private DBus session, extracted
nautilus-python/typelibs and Xvfb. It installs the actual CloudMarks package and
uses a separate, non-mutating fixture attribute observer. It never installs the
M0 spike, connects to the cloud or restarts the daily desktop.

Use a new short work directory; it holds only generated fixtures and evidence.
The test checks initial states, pin refresh, paused/dead helper behavior and
recovery, runtime disable, eight rendering combinations, 2,000 entries,
unrelated folders and uninstall. Unit tests separately stress 10,000 callback
requests and protocol/installation failure cases. The default test suite does
not run this opt-in desktop harness.

`run_account_readonly.py` additionally accepts an explicit private M3 `--config`
and a scoped disposable `--path`, including nested files. It keeps the file through the controller,
starts a credential-free offline FUSE view, explicitly verifies the file, and
starts the read-only status helper. It
mirrors that view read-only into the same isolated Files infrastructure. A
non-mutating observer checks the real account annotation/status. It removes its
user deployment and stops its own helper/view; the explicit pin is retained.
See M3's documentation for the original invocation and screenshot. M6 also
runs this scene with a 16 MiB file; the adapter stays read-only.
