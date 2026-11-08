# Status performance and local validation — 2026-11-07

This pass addresses status latency, testability, offline operational verification
and current documentation. It does not validate physical disk reconnects, a real
account, GTK rendering or Files scrolling. All fixtures are artificial.

## Reproduce the measurements

```bash
python scripts/bench_scale.py --entries 1000 10000 50000 --rounds 5 --json
python scripts/bench_status.py --files 64 --size-mib 4 --rounds 3 --json
```

The first command measures real SQLite reads and secure directory checks,
synthetic remote metadata, real bounded queue admission and real Unix sockets.
Counts include the root, files are remote-only, and there is no FUSE or network.
Five samples per size include one cold tree and four reused trees. p95/p99 are
sample maxima at this small sample size, not production tail estimates.
The volume fixture substitutes a deterministic 100 ms `findmnt`; it measures
request-thread blocking, not a physical mount or real `findmnt` latency. The
second command is the separate cold-content/hash benchmark from ADR 0003.

The same `bench_scale.py` was run against baseline code from commit
`c3ea1f51833b03428f1cb855fce939161ada7e3c` and the changed code, using disposable
package copies under `/tmp`, Python 3.14.4, on the same Linux host and temporary
filesystem. To repeat a baseline, copy the baseline `cloudmarks/` tree plus the
current `scripts/bench_scale.py` and `scripts/bench_status.py` into a temporary
directory and run there. Do not run a real account profile for this comparison.
Full samples are in [the evidence JSON](../evidence/status-performance.json).

| Work, p50 milliseconds | Baseline | Changed |
| --- | ---: | ---: |
| FolderObserver, 1,000 entries | 23.00 | 11.87 |
| FolderObserver, 10,000 entries | 230.13 | 119.73 |
| FolderObserver, 50,000 entries, rejected as unknown | 0.02 | 0.02 |
| Ordinary socket request | 0.17 | 0.16 |
| Socket request behind an incomplete peer | 581.04 | 0.49 |
| Cold status volume proof, simulated 100 ms findmnt | 100.29 | 0.23 |
| Synchronous volume proof, same fixture | 100.54 | 100.35 |

The incomplete-peer socket maximum falls from 581.28 to 0.52 ms. This measured
head-of-line delay justified multiplexing framing/response writes while keeping
status evaluation on the SQLite owner thread. Volume mutation checks remain
synchronous. Cold or expired status volume evidence fails closed while the
worker runs, with no stale grace.

The engine's unchanged 128-member/64-file/8 MiB aggregate guard returns unknown
at all three sizes. Its manifest parse still costs 0.65 / 8.17 / 47.54 ms at
1k / 10k / 50k entries after the change; the last value is 43.23 ms at baseline.
This path is not used by the current account FolderObserver aggregate route.
A future core change could reject oversized manifests before parsing them.

The desktop's actual frozen-consumer admission burst accepts 512 items at each
size and defers 488 / 9,488 / 49,488. Submission takes 0.64 / 6.92 / 34.98 ms
for the full artificial burst after the change. Memory is bounded; deferred
items require later Files callbacks. These are queue measurements, not a claim
that every item in a large real Files window receives a badge. No limit was
raised to improve the benchmark. The largest supported observer scan was the
largest measured cost and now avoids unused per-file reads and repeated
manifest construction without dropping any validation.

## Test and release gates

The initial suite ran **383 tests**, with **77%** combined statement/branch
coverage. The changed suite runs **433 tests**; combined coverage is **85.69%**
(86% rounded), with an enforced **85%** CI floor. This is coverage.py's combined
metric with branch measurement enabled, not 85.69% branch coverage alone.

Meaningful new seams:

- `test_control_gui.GuiTests`: progress, retained pins, cancel, close, error,
  retry and button reset against a minimal GTK fake; `OutputTests` uses a real
  child with stderr larger than a pipe buffer and malformed/oversized stdout.
- `test_view_mount.ViewTests`: exact read-only attestation, local-only proof,
  offline credential-free configuration, online authentication sequencing,
  mount failures, foreign mounts, busy unmount and process termination.
- `test_helper_loop`: idle/fragmented peers, EOF, malformed/oversized frames,
  admission/deadlines, wrong UID, SQLite failure, stale/live sockets, lock
  collisions and cleanup of all workers after startup/serve failures.
- `test_status_cache.VolumeReuseTests`: background thread identity, deduplication,
  expiration, early-refresh failure, binding mismatch and synchronous revocation.
- `test_observer_workers` and `test_desktop_protocol`: deterministic worker
  refresh/failure/shutdown, bounded queues, progress process identity, protocol
  limits and no renewal of expired evidence.
- `test_operation` and `test_doctor_operation`: temporary-HOME installation,
  enable/disable, offline doctor, uninstall/reinstall, preserved state/unrelated
  files, generated user units and actionable warning/failure hints.
- `test_bench_scale`: help, invalid input, JSON shape and safe scale limits.

The installed-wheel gate repeats the deployment round trip outside the checkout
using fake rclone/systemctl. `systemd-analyze --user verify` validates generated
units without starting them (explicit skip if systemd/fuse3 is absent locally;
CI installs these tools). The doctor fixture intentionally skips account checks.
Tests do not replace real GTK, service startup or physical acceptance.

Run the CI equivalents in an external venv with the pinned development tools:

```bash
python -m ruff check cloudmarks tests spikes scripts
python -m ruff format --check cloudmarks tests spikes scripts
python -W error::ResourceWarning -m coverage run -m unittest discover -s tests -q
python -m coverage report --fail-under=85
python -m compileall -q cloudmarks tests spikes scripts
python scripts/check_docs.py
python -m build
python -m twine check --strict dist/*.whl dist/*.tar.gz
python scripts/check_dist.py dist
python scripts/test_installed.py dist/*.whl
python -m pip wheel --no-deps --wheel-dir rebuilt dist/*.tar.gz
python scripts/compare_wheels.py dist/*.whl rebuilt/*.whl
```

All gates above passed locally, including all 433 tests against the installed
wheel, the deployment/doctor round trip, generated-unit verification, strict
distribution checks and matching rebuilt wheel payloads.

CI runs Python 3.11–3.14; this local pass uses Python 3.14.4. Artifacts may be
built outside the checkout by passing `build --outdir` and corresponding paths
to the remaining commands. Set `SOURCE_DATE_EPOCH=1792454400`, as in CI, when
comparing rebuilt wheel payloads.
