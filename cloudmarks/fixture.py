"""Artificial transport and read-path proofs for M1; never contacts a cloud.

No credentials/config discovery or subprocess calls are present in this module.
It is deliberately separate from the production status calculation.
"""

import hashlib
import time
from dataclasses import replace

from .model import IntegrityError, LocalCopy, RemoteFile, ScopeError
from .paths import SecureRoot


class FixtureBackend:
    def __init__(self, config, *, clock=time.time):
        self.config = config
        self.clock = clock
        self.files = {}
        self.read_paths = {}
        self.chunk_hook = None
        self.fail_after = None
        self.read_path_available = True

    def put(self, identity, relative_path, content, revision="fixture-v1"):
        remote = RemoteFile(
            identity,
            self.config.allowed_root_id,
            relative_path,
            revision,
            len(content),
            self.clock(),
            hashlib.sha256(content).hexdigest(),
        )
        self.config.validate_remote(remote)
        self.files[identity] = (remote, content)
        return remote

    def describe(self, identity):
        self.config.validate_identity(identity)
        remote, _ = self.files[identity]
        return replace(remote, observed_at=self.clock())

    def chunks(self, identity):
        remote, content = self.files[identity]
        for index, offset in enumerate(range(0, len(content), 8)):
            if self.chunk_hook:
                self.chunk_hook(index)
            if self.fail_after is not None and index >= self.fail_after:
                raise OSError("Artificial interrupted transfer")
            yield content[offset : offset + 8]

    def prove_read_path(self, remote, copy):
        # This is a fixture proof, not an assertion about a real rclone mount.
        return self.read_path_available and self.read_paths.get(remote.identity) == (
            remote.relative_path,
            copy.object_path,
        )


class FixtureController:
    def __init__(self, config, store, backend):
        if type(backend) is not FixtureBackend:
            raise ScopeError("M1 controller accepts only the artificial backend")
        self.config, self.store, self.backend = config, store, backend

    def download(self, identity):
        remote = self.backend.describe(identity)
        self.config.validate_remote(remote)
        self.store.observe(remote)
        self.store.begin_transfer(identity)
        try:
            if remote.sha256 is None or remote.deleted:
                raise IntegrityError("Fixture lacks a verifiable remote checksum")
            with SecureRoot(self.config.storage_root, self.config.volume_id) as root:
                candidate = root.stage(self.backend.chunks(identity), remote.size, remote.sha256)
                latest = self.backend.describe(identity)
                self.config.validate_remote(latest)
                if latest.identity != identity:
                    raise IntegrityError("Transport returned a different file identity")
                if replace(latest, observed_at=remote.observed_at) != remote:
                    self.store.observe(latest)
                    raise IntegrityError("Remote identity, path or version changed during download")
                # Reject a swapped/modified candidate before and after publication.
                root.verify(candidate, remote.size, remote.sha256)
                published = root.publish(candidate)
                root.verify(published, remote.size, remote.sha256)
                complete = LocalCopy(identity, remote.revision, published, remote.size, remote.sha256)
                self.store.record_complete(complete)
                self.backend.read_paths[identity] = (remote.relative_path, published)
                return complete
        except Exception:
            self.store.fail_transfer(identity, "Download did not complete; previous data was retained")
            raise
