"""Shared test helpers."""

import tempfile

# CloudMarks bounds Unix socket paths at 100 bytes (the kernel limit is 108).
# Sandboxes and agent runners often set a long TMPDIR, which would make every
# socket test fail for reasons unrelated to the code under test.
SOCKET_TMP = "/tmp" if len(tempfile.gettempdir()) > 40 else None
