"""Server checksum algorithms, by the name a provider reports for them.

Pure computation, no I/O. ``Observation.hasher()`` selects one for an
observed version; transfers and evictions compare its digest with the
provider's. QuickXor is OneDrive's algorithm.
"""

import hashlib

from .model import IntegrityError

_MASK_160 = (1 << 160) - 1


class QuickXor:
    def __init__(self):
        self.data = bytearray(20)
        self.length = 0
        self.shift = 0

    def update(self, content):
        # Byte j is XORed into the 160-bit little-endian register rotated left
        # by (shift + 11*j) mod 160. Because gcd(11, 160) == 1, bytes 160
        # positions apart use the same rotation, and XOR is linear: fold all
        # 160-byte lanes together with big-integer XOR (C speed), then apply
        # the 160 per-lane rotations once. Equivalent to the byte-wise loop.
        content = memoryview(content).cast("B")
        count = len(content)
        if not count:
            return
        lanes = int.from_bytes(content, "little")
        lane_bits = 160 * 8
        width = -(-count // 160) * lane_bits
        while width > lane_bits:
            chunks = width // lane_bits
            half = (chunks // 2) * lane_bits
            lanes = (lanes >> half) ^ (lanes & ((1 << half) - 1))
            width -= half
        folded = lanes.to_bytes(160, "little")
        register = int.from_bytes(self.data, "little")
        shift = self.shift
        for offset, value in enumerate(folded):
            if value:
                rotated = value << ((shift + 11 * offset) % 160)
                register ^= (rotated & _MASK_160) | (rotated >> 160)
        self.data = bytearray(register.to_bytes(20, "little"))
        self.shift = (shift + 11 * count) % 160
        self.length += count

    def digest(self):
        result = bytearray(self.data)
        for index, value in enumerate(self.length.to_bytes(8, "little")):
            result[12 + index] ^= value
        return bytes(result)


def checksum(kind):
    if kind == "quickXorHash":
        return QuickXor()
    if kind == "sha1Hash":
        return hashlib.sha1()  # Service checksum, not a signature/security primitive.
    if kind == "sha256Hash":
        return hashlib.sha256()
    raise IntegrityError("No supported authoritative remote checksum")
