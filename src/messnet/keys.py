"""Node identity: an ed25519 key stored as a 32-byte seed.

The public key in hex is the node id.  It is also the node's iroh
endpoint id, since iroh keys are ed25519 keys built from the same seed.
"""

import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def load_seed(path: Path, create: bool = True) -> bytes:
    """Return the 32 byte secret seed at PATH, creating it (mode 0600) if allowed."""
    path = Path(path)
    if path.exists():
        seed = path.read_bytes()
        if len(seed) != 32:
            raise ValueError(f"bad node key file (expect 32 bytes): {path}")
        return seed
    if not create:
        raise FileNotFoundError(f"no node key: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = os.urandom(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fp:
        fp.write(seed)
    return seed


def public_id(seed: bytes) -> str:
    key = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def sign(seed: bytes, message: bytes) -> str:
    return Ed25519PrivateKey.from_private_bytes(seed).sign(message).hex()


def verify(pubkey_hex: str, message: bytes, signature_hex: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex)).verify(
            bytes.fromhex(signature_hex), message)
        return True
    except (InvalidSignature, ValueError):
        return False
