"""Syndrome computation and encoder/decoder convention checks."""

import random

from app.rs import gf
from app.rs.decoder import K, N, NSYM, compute_syndromes
from app.rs.encoder import GENERATOR, encode


def test_generator_polynomial_shape():
    assert len(GENERATOR) == NSYM + 1
    assert GENERATOR[-1] == 1  # monic
    # Roots are exactly alpha^0 .. alpha^31.
    from app.rs.poly import evaluate

    for j in range(NSYM):
        assert evaluate(GENERATOR, gf.EXP[j]) == 0


def test_encode_is_systematic(payload):
    word = encode(payload)
    assert len(word) == N
    assert word[:K] == payload


def test_codewords_have_zero_syndromes(rng):
    for _ in range(20):
        payload = bytes(rng.randrange(256) for _ in range(K))
        assert compute_syndromes(encode(payload)) == [0] * NSYM


def test_syndrome_of_single_error_matches_definition(codeword, rng):
    pos = rng.randrange(N)
    magnitude = rng.randrange(1, 256)
    damaged = bytearray(codeword)
    damaged[pos] ^= magnitude
    syndromes = compute_syndromes(bytes(damaged))
    # S_j = magnitude * (alpha^(254-pos))^j, since byte i holds x^(254-i).
    x = gf.EXP[(N - 1 - pos) % 255]
    for j in range(NSYM):
        assert syndromes[j] == gf.mul(magnitude, gf.pow(x, j))


def test_syndrome_of_two_errors_is_additive(codeword, rng):
    p1, p2 = rng.sample(range(N), 2)
    m1, m2 = rng.randrange(1, 256), rng.randrange(1, 256)
    damaged = bytearray(codeword)
    damaged[p1] ^= m1
    damaged[p2] ^= m2
    syndromes = compute_syndromes(bytes(damaged))
    x1 = gf.EXP[(N - 1 - p1) % 255]
    x2 = gf.EXP[(N - 1 - p2) % 255]
    for j in range(NSYM):
        expected = gf.add(gf.mul(m1, gf.pow(x1, j)), gf.mul(m2, gf.pow(x2, j)))
        assert syndromes[j] == expected


def test_syndrome_length_and_type(codeword):
    syndromes = compute_syndromes(codeword)
    assert len(syndromes) == 32
    assert all(isinstance(s, int) and 0 <= s < 256 for s in syndromes)
