"""Cloud provider contracts. OneDrive (``cloudmarks.onedrive``) is the only implementation.

Callers depend on these protocols and obtain an implementation from
``create()``, so a second provider needs a new class and a factory branch,
not changes in every control and observer. Each contract is the smallest set
of methods one group of callers uses:

- ``Metadata``: fresh, scoped observations. Never infers deletion from a
  failed lookup; a missing item raises ``RemoteMissing``.
- ``Transfer``: bounded byte blocks for one observed version. The caller
  verifies length and checksum; the backend does not decide completeness.
- ``OfflineBackend``: Metadata + Transfer, for Keep offline / Free / tracking.
- ``ActivityBackend``: Metadata plus bounded batch and root listings, for the
  upload observer in the status helper.
- ``TransportProof``: checks that the separately mounted transport (rclone)
  serves the same account, root and item that metadata described.

Observations, ``Cancelled`` and ``RemoteMissing`` are provider-neutral types
in ``model``; ``Observation.hasher()`` returns the hasher for the provider's
checksum ``kind`` from ``checksums``. Core modules do not import ``onedrive``.
"""

from collections.abc import Callable, Iterator, Sequence
from typing import Protocol, runtime_checkable

from .limits import TREE_ENTRIES
from .model import FileIdentity


@runtime_checkable
class Metadata(Protocol):
    def describe(self, path: str, *, allow_directory: bool = False): ...

    def describe_identity(self, identity: FileIdentity): ...

    def tree(self, path: str, *, limit: int = TREE_ENTRIES, cancelled: Callable[[], bool] = ...): ...


@runtime_checkable
class Transfer(Protocol):
    def chunks(self, observation, *, cancelled: Callable[[], bool] = ...) -> Iterator[bytes]: ...


@runtime_checkable
class OfflineBackend(Metadata, Transfer, Protocol):
    pass


@runtime_checkable
class ActivityBackend(Metadata, Protocol):
    def describe_many(self, paths: Sequence[str]): ...

    def catalog(self, *, limit: int = 128, cancelled: Callable[[], bool] = ...): ...


@runtime_checkable
class TransportProof(Protocol):
    def authenticate(self): ...

    def verify_transport_root(self): ...

    def verify_transport_item(self, observation): ...


def create(config, settings):
    """The configured provider for one offline profile. Today always OneDrive."""
    from . import onedrive

    return onedrive.OneDrive(config, settings)
