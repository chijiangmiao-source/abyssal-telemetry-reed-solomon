"""Errors-and-erasures decoding: boundary radius, uniqueness, and
randomized round trips."""

import pytest
from conftest import corrupt

from app.rs.decoder import N, NSYM, UncorrectableError, compute_syndromes, decode


def test_clean_frame_reports_no_corrections(codeword):
    corrected, positions = decode(codeword)
    assert corrected == list(codeword)
    assert positions == []


def test_clean_frame_with_declared_erasures_reports_no_corrections(codeword):
    # A legitimate zero-syndrome frame must not report corrections even
    # when positions are (needlessly) marked as erasures.
    corrected, positions = decode(codeword, erasures=[0, 100, 222, 254])
    assert corrected == list(codeword)
    assert positions == []


@pytest.mark.parametrize("errors", [1, 2, 8, 15, 16])
def test_errors_only_within_radius(codeword, rng, errors):
    positions = rng.sample(range(N), errors)
    damaged = corrupt(codeword, positions, rng)
    corrected, changed = decode(damaged)
    assert bytes(corrected) == codeword
    assert changed == sorted(positions)


@pytest.mark.parametrize("erasures", [1, 10, 31, 32])
def test_erasures_only_within_capacity(codeword, rng, erasures):
    positions = rng.sample(range(N), erasures)
    damaged = corrupt(codeword, positions, rng)
    corrected, changed = decode(damaged, erasures=positions)
    assert bytes(corrected) == codeword
    assert changed == sorted(positions)


@pytest.mark.parametrize(
    "errors,erasures",
    [(1, 1), (5, 10), (11, 10), (8, 16), (1, 30), (0, 32), (16, 0)],
)
def test_mixed_errors_and_erasures(codeword, rng, errors, erasures):
    assert 2 * errors + erasures <= NSYM
    era = rng.sample(range(N), erasures)
    err = rng.sample([i for i in range(N) if i not in era], errors)
    damaged = corrupt(codeword, era + err, rng)
    corrected, changed = decode(damaged, erasures=era)
    assert bytes(corrected) == codeword
    assert changed == sorted(era + err)


def test_exactly_16_errors_recovered(codeword, rng):
    positions = rng.sample(range(N), 16)
    damaged = corrupt(codeword, positions, rng)
    corrected, changed = decode(damaged)
    assert bytes(corrected) == codeword
    assert changed == sorted(positions)


def test_10_erasures_plus_11_errors_recovered(codeword, rng):
    era = rng.sample(range(N), 10)
    err = rng.sample([i for i in range(N) if i not in era], 11)
    damaged = corrupt(codeword, era + err, rng)
    corrected, changed = decode(damaged, erasures=era)
    assert bytes(corrected) == codeword
    assert changed == sorted(era + err)


def test_17_errors_rejected_with_syndromes(codeword, rng):
    positions = rng.sample(range(N), 17)
    damaged = corrupt(codeword, positions, rng)
    with pytest.raises(UncorrectableError) as excinfo:
        decode(damaged)
    assert excinfo.value.syndromes == compute_syndromes(damaged)
    assert any(excinfo.value.syndromes)


def test_33_erasures_rejected_with_syndromes(codeword, rng):
    era = rng.sample(range(N), 33)
    damaged = corrupt(codeword, era, rng)
    with pytest.raises(UncorrectableError) as excinfo:
        decode(damaged, erasures=era)
    assert excinfo.value.syndromes == compute_syndromes(damaged)


def test_33_erasures_rejected_even_on_clean_frame(codeword):
    # 33 erasures exceed the 32 parity symbols: the payload is ambiguous
    # even though the received frame itself is a valid codeword.
    with pytest.raises(UncorrectableError):
        decode(codeword, erasures=list(range(33)))


def test_16_errors_plus_1_erasure_rejected(codeword, rng):
    era = rng.sample(range(N), 1)
    err = rng.sample([i for i in range(N) if i not in era], 16)
    damaged = corrupt(codeword, era + err, rng)
    with pytest.raises(UncorrectableError):
        decode(damaged, erasures=era)


def test_randomized_round_trips(rng):
    for _ in range(60):
        payload = bytes(rng.randrange(256) for _ in range(223))
        word = __import__("app.rs.encoder", fromlist=["encode"]).encode(payload)
        erasures = rng.randrange(0, 17)
        errors = rng.randrange(0, (NSYM - erasures) // 2 + 1)
        era = rng.sample(range(N), erasures)
        err = rng.sample([i for i in range(N) if i not in era], errors)
        damaged = corrupt(word, era + err, rng)
        corrected, changed = decode(damaged, erasures=era)
        assert bytes(corrected) == word
        assert changed == sorted(era + err)


def test_decoder_rejects_bad_input_shape(codeword):
    with pytest.raises(ValueError):
        decode(codeword[:100])
    with pytest.raises(ValueError):
        decode(codeword, erasures=[3, 3])
    with pytest.raises(ValueError):
        decode(codeword, erasures=[255])
