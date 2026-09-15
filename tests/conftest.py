import random

import pytest

from app.rs.encoder import K, N, encode


@pytest.fixture()
def rng():
    return random.Random(0xC0DE)


@pytest.fixture()
def payload(rng):
    return bytes(rng.randrange(256) for _ in range(K))


@pytest.fixture()
def codeword(payload):
    return encode(payload)


def corrupt(word: bytes, positions, rng) -> bytes:
    """Xor a non-zero mask into each listed position."""
    damaged = bytearray(word)
    for pos in positions:
        damaged[pos] ^= rng.randrange(1, 256)
    return bytes(damaged)
