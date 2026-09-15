"""HTTP contract for out-of-order fragment assembly."""

import random

import pytest
from fastapi.testclient import TestClient

from app.main import app, assemblies
from app.rs.decoder import N, compute_syndromes
from app.rs.encoder import encode

client = TestClient(app)


@pytest.fixture(autouse=True)
def empty_store():
    assemblies.clear()
    yield
    assemblies.clear()


@pytest.fixture()
def frame():
    rng = random.Random(0xBEEF)
    payload = bytes(rng.randrange(256) for _ in range(223))
    codeword = bytearray(encode(payload))
    era = [3, 77, 120, 200]
    err = [50, 51, 90, 254]
    for pos in era + err:
        codeword[pos] ^= rng.randrange(1, 256)
    return bytes(codeword), payload, era, sorted(era + err)


PARTS = [(0, 40), (40, 40), (80, 40), (120, 80), (200, 55)]


def put_fragment(assembly_id, offset, data, erasures=()):
    return client.put(
        f"/assemblies/{assembly_id}/fragments",
        json={
            "offset": offset,
            "data": bytes(data).hex(),
            "erasures": list(erasures),
        },
    )


def assemble(frame, assembly_id="a", order=None, erasures=()):
    """Deliver all five fragments; default order is shuffled."""
    word = frame
    order = order or [4, 1, 3, 0, 2]
    for idx in order:
        off, length = PARTS[idx]
        local_er = [p - off for p in erasures if off <= p < off + length]
        resp = put_fragment(assembly_id, off, word[off : off + length], local_er)
        assert resp.status_code == 200, resp.text
    return client.post(f"/assemblies/{assembly_id}/decode")


def test_out_of_order_fragments_restore_correctable_frame(frame):
    word, payload, era, changed = frame
    resp = assemble(word, "storm-7", erasures=era)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["complete"] is True
    assert body["received_bytes"] == N
    assert body["missing_ranges"] == []
    assert body["erasures"] == era
    decode = body["decode"]
    assert decode["status"] == "ok"
    assert decode["payload"] == payload.hex()
    assert decode["corrected_codeword"] == encode(payload).hex()
    assert decode["corrected_positions"] == changed


def test_progress_shape_while_collecting(frame):
    word, _, _, _ = frame
    resp = put_fragment("p", 10, word[10:20])
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "status": "collecting",
        "assembly_id": "p",
        "received_bytes": 10,
        "total_bytes": N,
        "complete": False,
        "missing_ranges": [
            {"start": 0, "end": 9},
            {"start": 20, "end": 254},
        ],
        "erasures": [],
        "decode": None,
    }


def test_duplicate_fragment_is_idempotent(frame):
    word, _, era, _ = frame
    r1 = put_fragment("dup", 0, word[0:40], [p for p in era if p < 40])
    r2 = put_fragment("dup", 0, word[0:40], [p for p in era if p < 40])
    assert r1.json() == r2.json()
    assert r1.json()["received_bytes"] == 40


def test_submit_with_gaps_returns_409_and_normalized_ranges(frame):
    word, _, _, _ = frame
    put_fragment("g", 10, word[10:20])
    put_fragment("g", 11, word[11:30])  # overlapping identical bytes
    resp = client.post("/assemblies/g/decode")
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "collecting"
    assert body["complete"] is False
    assert body["missing_ranges"] == [
        {"start": 0, "end": 9},
        {"start": 30, "end": 254},
    ]
    assert body["decode"] is None


def test_submit_on_unknown_assembly_is_gapped_409():
    resp = client.post("/assemblies/never-seen/decode")
    assert resp.status_code == 409
    assert resp.json()["missing_ranges"] == [{"start": 0, "end": 254}]


def test_repeated_submit_replays_first_result(frame):
    word, _, era, changed = frame
    assemble(word, "once", erasures=era)
    r1 = client.post("/assemblies/once/decode")
    r2 = client.post("/assemblies/once/decode")
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()
    assert r1.json()["decode"]["corrected_positions"] == changed


def test_late_fragment_after_completion_replays_terminal(frame):
    word, payload, era, _ = frame
    assemble(word, "late", erasures=era)
    resp = put_fragment("late", 0, word[0:40])
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["complete"] is True
    assert body["decode"]["payload"] == payload.hex()


def test_byte_conflict_atomically_rejects(frame):
    word, _, _, _ = frame
    put_fragment("c", 0, word[0:10])
    resp = put_fragment("c", 5, b"\x00" * 5)
    assert resp.status_code == 409
    body = resp.json()
    assert body == {
        "status": "rejected",
        "assembly_id": "c",
        "conflict_ranges": [{"start": 5, "end": 9}],
    }


def test_conflict_applies_no_bytes_and_is_irreversible(frame):
    word, _, _, _ = frame
    put_fragment("c", 0, word[0:10])
    bad = put_fragment("c", 5, b"\x00" * 5)
    assert bad.status_code == 409
    # A fragment entirely after the clash cannot undo the rejection.
    later = put_fragment("c", 100, word[100:120])
    assert later.status_code == 409
    assert later.json()["status"] == "rejected"
    assert later.json()["conflict_ranges"] == [{"start": 5, "end": 9}]
    # Submit replays the same rejection; decode is never offered.
    submit = client.post("/assemblies/c/decode")
    assert submit.status_code == 409
    assert submit.json()["conflict_ranges"] == [{"start": 5, "end": 9}]
    # Even a now-consistent duplicate of the clashing fragment stays rejected.
    consistent = put_fragment("c", 5, word[5:10])
    assert consistent.status_code == 409
    assert consistent.json()["status"] == "rejected"


def test_erasure_contradicts_earlier_reliable_data(frame):
    word, _, _, _ = frame
    put_fragment("e", 0, word[0:10])
    resp = put_fragment("e", 5, word[5:8], erasures=[0])
    assert resp.status_code == 409
    assert resp.json()["conflict_ranges"] == [{"start": 5, "end": 5}]


def test_reliable_redelivery_contradicts_earlier_erasure(frame):
    word, _, _, _ = frame
    put_fragment("e", 5, word[5:8], erasures=[0])
    resp = put_fragment("e", 5, word[5:8])
    assert resp.status_code == 409
    assert resp.json()["conflict_ranges"] == [{"start": 5, "end": 5}]


def test_different_bytes_at_mutually_erased_position_are_rejected(frame):
    word, _, _, _ = frame
    # First fragment erases position 3 with the received byte; a second
    # fragment overlaps it with a different filler byte, also erased: the
    # two copies contradict each other -> atomic 409, order-independent.
    put_fragment("f", 0, word[0:5], erasures=[3])
    resp = put_fragment("f", 3, b"\xff" + word[4:5], erasures=[0])
    assert resp.status_code == 409
    assert resp.json()["conflict_ranges"] == [{"start": 3, "end": 3}]

    # Reverse the arrival order: same rejection.
    put_fragment("f2", 3, b"\xff" + word[4:5], erasures=[0])
    resp = put_fragment("f2", 0, word[0:5], erasures=[3])
    assert resp.status_code == 409
    assert resp.json()["conflict_ranges"] == [{"start": 3, "end": 3}]


def test_identical_filler_at_mutually_erased_overlap_is_accepted(frame):
    word, _, _, _ = frame
    put_fragment("g", 0, word[0:5], erasures=[3])
    resp = put_fragment("g", 3, word[3:5], erasures=[0])
    assert resp.status_code == 200
    assert resp.json()["erasures"] == [3]


def test_uncorrectable_assembly_returns_422_and_freezes_it():
    bad = bytes((i * 37) % 256 for i in range(N))
    resp = assemble(bad, "bad")
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == "completed"
    assert body["decode"]["status"] == "uncorrectable"
    expected = [f"{s:02x}" for s in compute_syndromes(bad)]
    assert body["decode"]["syndromes"] == expected
    # Same 422 body replays forever, byte-for-byte.
    replay = client.post("/assemblies/bad/decode")
    assert replay.status_code == 422
    assert replay.json() == body
    late = put_fragment("bad", 0, bad[0:1])
    assert late.status_code == 422
    assert late.json()["decode"] == body["decode"]


def test_assembly_decode_matches_plain_decode(frame):
    word, _, era, _ = frame
    assembled = assemble(word, "mirror", erasures=era).json()["decode"]
    plain = client.post(
        "/decode", json={"codeword": word.hex(), "erasures": era}
    )
    assert plain.status_code == 200
    assert assembled == plain.json()


def test_assembly_with_gaps_decoder_is_never_called(frame, monkeypatch):
    from app.assembly import AssemblyStore

    word, _, _, _ = frame

    def boom(*_):
        raise AssertionError("decoder must not run with gaps")

    monkeypatch.setattr("app.main.assemblies", AssemblyStore(decoder=boom))
    resp = put_fragment("x", 0, word[0:10])
    assert resp.status_code == 200
    resp = client.post("/assemblies/x/decode")
    assert resp.status_code == 409
    assert resp.json()["missing_ranges"] == [
        {"start": 10, "end": 254},
    ]


def test_independent_assembly_ids_do_not_interfere(frame):
    word, _, _, _ = frame
    put_fragment("one", 0, word[0:10])
    resp = put_fragment("two", 0, b"\x00" * 10)
    assert resp.status_code == 200
    # Rejecting "two" must leave "one" collecting.
    put_fragment("two", 0, b"\xff" * 10)
    one = put_fragment("one", 0, word[0:10])
    assert one.status_code == 200
    assert one.json()["status"] == "collecting"
    other = put_fragment("two", 0, word[0:10])
    assert other.status_code == 409
    assert other.json()["status"] == "rejected"


@pytest.mark.parametrize(
    "payload",
    [
        {"offset": 250, "data": "ab" * 10},  # spills past byte 254
        {"offset": 255, "data": "ab"},  # offset out of range
        {"offset": -1, "data": "ab"},
        {"offset": 0, "data": "zz"},
        {"offset": 0, "data": "abc"},  # odd hex length
        {"offset": 0, "data": "ab cd"},  # whitespace rejected like /decode
        {"offset": 0, "data": ""},
        {"offset": 0, "data": "aabb", "erasures": [1, 1]},
        {"offset": 0, "data": "aabb", "erasures": [2]},
        {"offset": 0, "data": "aabb", "erasures": [-1]},
    ],
)
def test_malformed_fragments_return_400(payload):
    resp = client.put("/assemblies/bad/fragments", json=payload)
    assert resp.status_code == 400, resp.text


def test_missing_fragment_fields_return_400():
    assert client.put("/assemblies/x/fragments", json={}).status_code == 400
    assert (
        client.put("/assemblies/x/fragments", json={"offset": 0}).status_code == 400
    )
    assert (
        client.put(
            "/assemblies/x/fragments", json={"data": "aabb"}
        ).status_code
        == 400
    )
    assert (
        client.put("/assemblies/x/fragments", data="not json").status_code == 400
    )


def test_uppercase_hex_fragment_accepted(frame):
    word, _, _, _ = frame
    resp = client.put(
        "/assemblies/u/fragments",
        json={"offset": 0, "data": word[0:4].hex().upper()},
    )
    assert resp.status_code == 200
    assert resp.json()["received_bytes"] == 4


def test_decode_endpoint_and_health_unchanged():
    assert client.get("/health").json() == {"status": "healthy"}
