"""Minimal ULID generation (Crockford base32, 48-bit ms time + 80 random bits)."""

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(ms: int | None = None) -> str:
    """Return a new 26 character ULID string."""
    if ms is None:
        ms = time.time_ns() // 1_000_000
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))
