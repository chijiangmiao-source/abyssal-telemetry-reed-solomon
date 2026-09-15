"""End-to-end API chain: HTTP status codes, payload recovery, and
recomputable syndromes on rejection."""

import random

import pytest
from conftest import corrupt
from fastapi.testclient import TestClient

from app.main import app
from app.rs.decoder import K, N, compute_syndromes

client = TestClient(app)


def post(word: bytes, erasures=()):
    return client.post(
        "/decode", json={"codeword": word.hex(), "erasures": list(erasures)}
    )


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_clean_frame(codeword, payload):
    resp = post(codeword)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["payload"] == payload.hex()
    assert body["corrected_codeword"] == codeword.hex()
    assert body["corrected_positions"] == []


def test_clean_frame_with_erasures_reports_no_corrections(codeword):
    resp = post(codeword, erasures=[5, 200])
    assert resp.status_code == 200
    assert resp.json()["corrected_positions"] == []


def test_16_errors_recovered(codeword, payload, rng):
    positions = rng.sample(range(N), 16)
    resp = post(corrupt(codeword, positions, rng))
    assert resp.status_code == 200
    body = resp.json()
    assert body["payload"] == payload.hex()
    assert body["corrected_codeword"] == codeword.hex()
    assert body["corrected_positions"] == sorted(positions)


def test_10_erasures_plus_11_errors_recovered(codeword, payload, rng):
    era = rng.sample(range(N), 10)
    err = rng.sample([i for i in range(N) if i not in era], 11)
    resp = post(corrupt(codeword, era + err, rng), erasures=era)
    assert resp.status_code == 200
    body = resp.json()
    assert body["payload"] == payload.hex()
    assert body["corrected_codeword"] == codeword.hex()
    assert body["corrected_positions"] == sorted(era + err)


def test_17_errors_uncorrectable_with_recomputable_syndromes(codeword, rng):
    positions = rng.sample(range(N), 17)
    damaged = corrupt(codeword, positions, rng)
    resp = post(damaged)
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == "uncorrectable"
    assert len(body["syndromes"]) == 32
    assert body["syndromes"] == [f"{s:02x}" for s in compute_syndromes(damaged)]


def test_33_erasures_uncorrectable_with_recomputable_syndromes(codeword, rng):
    era = rng.sample(range(N), 33)
    damaged = corrupt(codeword, era, rng)
    resp = post(damaged, erasures=era)
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == "uncorrectable"
    assert len(body["syndromes"]) == 32
    assert body["syndromes"] == [f"{s:02x}" for s in compute_syndromes(damaged)]


@pytest.mark.parametrize("erasures", [[3, 3], [0, 0, 1], [255], [-1], [254, 254]])
def test_invalid_erasures_return_400(codeword, erasures):
    resp = post(codeword, erasures=erasures)
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "codeword",
    ["", "ab" * 100, "zz" * 255, "0" * 511],
    ids=["empty", "too_short", "not_hex", "odd_length"],
)
def test_malformed_codeword_returns_400(codeword):
    resp = client.post("/decode", json={"codeword": codeword, "erasures": []})
    assert resp.status_code == 400


@pytest.mark.parametrize("whitespace", [" ", "\t", "\n", "\r"])
def test_whitespace_in_codeword_returns_400_not_500(codeword, whitespace):
    # bytes.fromhex() silently skips ASCII whitespace, so a 510-char string
    # with whitespace would decode to fewer than 255 bytes; it must be
    # rejected as a client error, not crash the decoder.
    frame = codeword.hex()
    damaged = frame[:100] + whitespace * 2 + frame[102:]
    assert len(damaged) == 510
    resp = client.post("/decode", json={"codeword": damaged, "erasures": []})
    assert resp.status_code == 400


def test_whitespace_only_codeword_returns_400():
    resp = client.post("/decode", json={"codeword": " " * 510, "erasures": []})
    assert resp.status_code == 400


def test_missing_fields_return_400():
    assert client.post("/decode", json={}).status_code == 400
    assert client.post("/decode", json={"erasures": []}).status_code == 400
    assert client.post("/decode", data="not json").status_code == 400


def test_erasures_default_to_empty(codeword):
    resp = client.post("/decode", json={"codeword": codeword.hex()})
    assert resp.status_code == 200
    assert resp.json()["corrected_positions"] == []


def test_uppercase_hex_accepted(codeword, payload):
    resp = client.post(
        "/decode", json={"codeword": codeword.hex().upper(), "erasures": []}
    )
    assert resp.status_code == 200
    assert resp.json()["payload"] == payload.hex()
