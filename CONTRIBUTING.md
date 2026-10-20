# Contributing

Start with [the plan](PLAN.md), [handoff](docs/HANDOFF.md),
[operating boundaries](docs/operations.md) and [compatibility](docs/compatibility.md).
The project uses the [MIT license](LICENSE), selected by the owner. The project does not yet accept a claim of production safety.

Create a development virtual environment with Python 3.11–3.14:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m ruff check cloudmarks tests spikes scripts
.venv/bin/python -m ruff format --check cloudmarks tests spikes scripts
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -q
.venv/bin/python scripts/check_docs.py
.venv/bin/python -m build
.venv/bin/python -m twine check --strict dist/*.whl dist/*.tar.gz
.venv/bin/python scripts/check_dist.py dist
.venv/bin/python scripts/test_installed.py dist/*.whl
.venv/bin/python -m pip wheel --no-deps --wheel-dir rebuilt dist/*.tar.gz
.venv/bin/python scripts/compare_wheels.py dist/*.whl rebuilt/*.whl
```

Use a fresh output directory or remove only your own previous build artifacts
before a different version's build. Build tools are development dependencies;
the installed CLI runtime is standard-library only. The wheel suite runs outside
the checkout so missing package assets cannot be masked by source imports.

Default tests are credential-free and never mount OneDrive or install an adapter
in the real desktop. Optional independent rclone QuickXor vectors use no account.
Do not put cloud integration tests or desktop restarts into default CI.
[Desktop](tests/desktop/README.md) and [account integration](tests/integration/README.md)
runners are opt-in. The M4 cloud runner mutates only new disposable fixtures it
creates; it is not a production upload/delete API. Restrict it to the dedicated
verified account/root and keep configs, IDs, credentials and private logs outside
Git. Do not run it against personal/shared data.

Preserve existing working-tree changes. Keep network, hashing and directory walks
out of Nautilus callbacks. A missing disk, failed listing, partial download or
stale observation must not become a positive offline claim. Preserve ambiguous
bytes and do not invent automatic conflict merges or delete private data to make
checks pass. Tests should exercise real failure boundaries rather than mirror
implementation details. Record manual checks separately from automated CI.

CI has read-only repository permissions and requires no secrets. The tag-only
release workflow publishes an experimental prerelease from a deliberate matching
version tag after validation and licensing checks; ordinary branch pushes never
publish.
Do not send private configs or raw Graph/rclone output in issues/PRs. See
[SECURITY](SECURITY.md) for reporting guidance.
