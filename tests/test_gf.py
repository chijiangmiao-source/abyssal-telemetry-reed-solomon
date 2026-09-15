"""Finite-field sanity checks: table coherence, primitive element order,
and the ring axioms on sampled elements."""

import random

import pytest

from app.rs import gf


def test_reduction_vector():
    # x^8 mod p(x) = x^4 + x^3 + x + 1 = 0x1d for p(x) = 0x11d.
    assert gf.EXP[8] == 0x1D
    assert gf.ALPHA == 0x02
    assert gf.PRIMITIVE_POLYNOMIAL == 0x11D


def test_alpha_is_primitive():
    x = 1
    for _ in range(1, gf.MULTIPLICATIVE_ORDER):
        x = gf.mul(x, gf.ALPHA)
        assert x != 1
    assert gf.mul(x, gf.ALPHA) == 1


def test_exp_log_are_inverse():
    for a in range(1, gf.FIELD_SIZE):
        assert gf.EXP[gf.LOG[a]] == a
    for i in range(gf.MULTIPLICATIVE_ORDER):
        assert gf.LOG[gf.EXP[i]] == i


def test_tables_cover_all_nonzero_elements():
    assert len(set(gf.EXP[: gf.MULTIPLICATIVE_ORDER])) == gf.MULTIPLICATIVE_ORDER


def test_add_sub_are_xor():
    rng = random.Random(7)
    for _ in range(500):
        a, b = rng.randrange(256), rng.randrange(256)
        assert gf.add(a, b) == a ^ b
        assert gf.sub(a, b) == a ^ b
        assert gf.sub(gf.add(a, b), b) == a


def test_mul_div_inverse_identities():
    rng = random.Random(11)
    for _ in range(500):
        a = rng.randrange(1, 256)
        b = rng.randrange(1, 256)
        assert gf.mul(a, 1) == a
        assert gf.mul(a, gf.inverse(a)) == 1
        assert gf.div(gf.mul(a, b), b) == a
        assert gf.mul(a, b) == gf.mul(b, a)


def test_zero_rules():
    rng = random.Random(13)
    for _ in range(100):
        a = rng.randrange(256)
        assert gf.mul(a, 0) == 0
        assert gf.mul(0, a) == 0
    assert gf.div(0, 5) == 0
    with pytest.raises(ZeroDivisionError):
        gf.div(1, 0)
    with pytest.raises(ZeroDivisionError):
        gf.inverse(0)


def test_distributive_and_associative():
    rng = random.Random(17)
    for _ in range(500):
        a, b, c = (rng.randrange(256) for _ in range(3))
        assert gf.mul(a, gf.add(b, c)) == gf.add(gf.mul(a, b), gf.mul(a, c))
        assert gf.mul(gf.mul(a, b), c) == gf.mul(a, gf.mul(b, c))


def test_pow_matches_repeated_multiplication():
    rng = random.Random(19)
    for _ in range(200):
        a = rng.randrange(1, 256)
        n = rng.randrange(0, 600)
        acc = 1
        for _ in range(n):
            acc = gf.mul(acc, a)
        assert gf.pow(a, n) == acc
