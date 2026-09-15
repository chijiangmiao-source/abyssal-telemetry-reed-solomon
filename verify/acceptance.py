"""One-shot acceptance suite for the RS(255,223) decoding service.

Runs against a live API (API_BASE_URL, default http://localhost:8000),
builds its own frames with the local systematic encoder, and checks the
contract end to end:

* a clean zero-syndrome frame decodes with no reported corrections;
* 16 unknown errors are fully recovered;
* 10 erasures + 11 unknown errors are fully recovered (same payload);
* 17 errors are rejected with 32 recomputable syndromes;
* 33 erasures are rejected;
* duplicate / out-of-range erasures are rejected with 400;
* out-of-order fragments assemble into a correctable frame and decode;
* duplicate fragments and repeated submits are idempotent;
* contradictory overlaps reject the session irreversibly (409 replay);
* gapped submits return 409 with normalized missing ranges;
* concurrent submits and a last-fragment race share one frozen terminal.

Exits 0 when every check passes, 1 otherwise.
"""

import os
import random
import sys
import threading
import time

import httpx

from app.rs.decoder import K, N, NSYM, compute_syndromes
from app.rs.encoder import encode

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")

failures = []


def check(name, condition, extra=""):
    status = "PASS" if condition else "FAIL"
    line = f"[{status}] {name}"
    if extra and not condition:
        line += f" -- {extra}"
    print(line, flush=True)
    if not condition:
        failures.append(name)
    return bool(condition)


def wait_for_api(client):
    for _ in range(60):
        try:
            resp = client.get("/health")
            if resp.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


def main():
    rng = random.Random(0xACCE97)
    payload = bytes(rng.randrange(256) for _ in range(K))
    codeword = encode(payload)

    def damage(positions):
        frame = bytearray(codeword)
        for pos in positions:
            frame[pos] ^= rng.randrange(1, 256)
        return bytes(frame)

    with httpx.Client(base_url=API_BASE_URL, timeout=10.0) as client:
        check("api reachable", wait_for_api(client))
        if failures:
            return report()

        def decode(frame, erasures=()):
            return client.post(
                "/decode", json={"codeword": frame.hex(), "erasures": list(erasures)}
            )

        # 1. Clean frame: zero syndromes, no corrections reported.
        resp = decode(codeword)
        body = resp.json() if resp.status_code == 200 else {}
        check(
            "clean frame -> 200, payload intact, no corrections",
            resp.status_code == 200
            and body.get("status") == "ok"
            and body.get("payload") == payload.hex()
            and body.get("corrected_codeword") == codeword.hex()
            and body.get("corrected_positions") == [],
            f"got {resp.status_code}: {resp.text[:200]}",
        )

        # 1b. Clean frame with erasures declared: still no corrections.
        resp = decode(codeword, erasures=[0, 111, 254])
        body = resp.json() if resp.status_code == 200 else {}
        check(
            "zero-syndrome frame with erasures -> 200, no corrections",
            resp.status_code == 200 and body.get("corrected_positions") == [],
            f"got {resp.status_code}: {resp.text[:200]}",
        )

        # 2. Exactly 16 unknown errors (2e = 32, at the radius).
        err16 = rng.sample(range(N), 16)
        resp = decode(damage(err16))
        body = resp.json() if resp.status_code == 200 else {}
        check(
            "16 unknown errors -> payload and frame restored",
            resp.status_code == 200
            and body.get("payload") == payload.hex()
            and body.get("corrected_codeword") == codeword.hex()
            and body.get("corrected_positions") == sorted(err16),
            f"got {resp.status_code}: {resp.text[:200]}",
        )

        # 3. 10 erasures + 11 unknown errors (2*11 + 10 = 32, at the radius).
        era10 = rng.sample(range(N), 10)
        err11 = rng.sample([i for i in range(N) if i not in era10], 11)
        resp = decode(damage(era10 + err11), erasures=era10)
        body = resp.json() if resp.status_code == 200 else {}
        check(
            "10 erasures + 11 unknown errors -> same payload restored",
            resp.status_code == 200
            and body.get("payload") == payload.hex()
            and body.get("corrected_codeword") == codeword.hex()
            and body.get("corrected_positions") == sorted(era10 + err11),
            f"got {resp.status_code}: {resp.text[:200]}",
        )

        # 4. 17 unknown errors: beyond the radius, must be rejected with
        #    syndromes that recomputation reproduces exactly.
        err17 = rng.sample(range(N), 17)
        frame17 = damage(err17)
        resp = decode(frame17)
        body = resp.json() if resp.headers.get("content-type", "").startswith(
            "application/json"
        ) else {}
        expected_syndromes = [f"{s:02x}" for s in compute_syndromes(frame17)]
        check(
            "17 unknown errors -> 422 uncorrectable with 32 recomputable syndromes",
            resp.status_code == 422
            and body.get("status") == "uncorrectable"
            and len(body.get("syndromes", [])) == NSYM
            and body.get("syndromes") == expected_syndromes,
            f"got {resp.status_code}: {resp.text[:200]}",
        )

        # 5. 33 erasures: more erasures than parity symbols, must be rejected.
        era33 = rng.sample(range(N), 33)
        frame33 = damage(era33)
        resp = decode(frame33, erasures=era33)
        body = resp.json() if resp.headers.get("content-type", "").startswith(
            "application/json"
        ) else {}
        expected_syndromes = [f"{s:02x}" for s in compute_syndromes(frame33)]
        check(
            "33 erasures -> 422 uncorrectable with 32 recomputable syndromes",
            resp.status_code == 422
            and body.get("status") == "uncorrectable"
            and len(body.get("syndromes", [])) == NSYM
            and body.get("syndromes") == expected_syndromes,
            f"got {resp.status_code}: {resp.text[:200]}",
        )

        # 6. Malformed requests -> 400.
        resp = decode(codeword, erasures=[7, 7])
        check("duplicate erasure positions -> 400", resp.status_code == 400,
              f"got {resp.status_code}")
        resp = decode(codeword, erasures=[255])
        check("erasure position 255 -> 400", resp.status_code == 400,
              f"got {resp.status_code}")
        resp = decode(codeword, erasures=[-1])
        check("erasure position -1 -> 400", resp.status_code == 400,
              f"got {resp.status_code}")

        # 6b. Right length but containing whitespace: bytes.fromhex would
        # silently skip it, so the frame must be rejected as malformed (400),
        # never as an internal error.
        damaged_ws = codeword.hex()
        damaged_ws = damaged_ws[:100] + "  " + damaged_ws[102:]
        resp = client.post(
            "/decode", json={"codeword": damaged_ws, "erasures": []}
        )
        check("whitespace inside codeword -> 400", resp.status_code == 400,
              f"got {resp.status_code}")

        run_assembly_checks(client, codeword, payload, frame17)

    return report()


# Fragment partition used by every assembly scenario (offsets + lengths).
PARTS = [(0, 60), (60, 50), (110, 45), (155, 60), (215, 40)]


def _put_fragment(client, assembly_id, offset, data, erasures=()):
    return client.put(
        f"/assemblies/{assembly_id}/fragments",
        json={
            "offset": offset,
            "data": bytes(data).hex(),
            "erasures": list(erasures),
        },
    )


def run_assembly_checks(client, codeword, payload, uncorrectable_frame):
    rng = random.Random(0xF1A6)
    era = sorted(rng.sample(range(N), 10))
    err = sorted(rng.sample([i for i in range(N) if i not in era], 11))
    frame = bytearray(codeword)
    for pos in era + err:
        frame[pos] ^= rng.randrange(1, 256)
    frame = bytes(frame)
    expected_positions = sorted(era + err)
    # Unique per process run: sessions are process-local, so a repeated run
    # against the same server would otherwise replay old terminals.
    run = f"{os.getpid()}-{int(time.time() * 1000)}"

    def rid(name):
        return f"{name}-{run}"

    # 7. Out-of-order, overlapping, retried fragments assemble the frame and
    #    decode through the existing decoder.
    aid = rid("acc-reassembly")
    order = [4, 1, 3, 0, 2]
    for idx in order:
        off, length = PARTS[idx]
        local_er = [p - off for p in era if off <= p < off + length]
        resp = _put_fragment(client, aid, off, frame[off : off + length], local_er)
        if not check(f"fragment {idx} accepted", resp.status_code == 200,
                     f"got {resp.status_code}: {resp.text[:200]}"):
            return
    resp = client.post(f"/assemblies/{aid}/decode")
    body = resp.json() if resp.status_code == 200 else {}
    decode = body.get("decode", {})
    check(
        "out-of-order fragments -> assembled frame corrected",
        resp.status_code == 200
        and body.get("status") == "completed"
        and body.get("complete") is True
        and body.get("received_bytes") == N
        and body.get("missing_ranges") == []
        and body.get("erasures") == era
        and decode.get("status") == "ok"
        and decode.get("payload") == payload.hex()
        and decode.get("corrected_codeword") == codeword.hex()
        and decode.get("corrected_positions") == expected_positions,
        f"got {resp.status_code}: {resp.text[:300]}",
    )

    # The embedded result must be byte-identical to plain POST /decode.
    plain = client.post(
        "/decode", json={"codeword": frame.hex(), "erasures": era}
    ).json()
    check("assembly decode body equals /decode body", decode == plain)

    # 8. Idempotency: duplicate fragment, repeated submit and a late
    #    fragment all replay the same immutable terminal.
    off, length = PARTS[0]
    dup = _put_fragment(client, aid, off, frame[off : off + length],
                        [p - off for p in era if off <= p < off + length])
    again = client.post(f"/assemblies/{aid}/decode")
    late = _put_fragment(client, aid, 100, frame[100:120])
    check(
        "duplicate fragment / resubmit / late fragment replay terminal",
        dup.status_code == 200
        and dup.json().get("decode") == decode
        and again.status_code == 200
        and again.json().get("decode") == decode
        and late.status_code == 200
        and late.json().get("status") == "completed"
        and late.json().get("decode") == decode,
        f"{dup.status_code},{again.status_code},{late.status_code}",
    )

    # 9. Gaps: a submit with missing bytes is a 409 carrying canonical,
    #    normalized missing ranges and never runs the decoder.
    gap_id = rid("gaps")
    resp = client.post(f"/assemblies/{gap_id}/decode")
    check(
        "submit before any fragment -> 409 full gap",
        resp.status_code == 409
        and resp.json().get("missing_ranges") == [{"start": 0, "end": 254}],
        f"got {resp.status_code}: {resp.text[:200]}",
    )
    _put_fragment(client, gap_id, 10, frame[10:20])
    _put_fragment(client, gap_id, 11, frame[11:30])  # identical overlap
    resp = client.post(f"/assemblies/{gap_id}/decode")
    check(
        "partial submit -> 409 normalized missing ranges",
        resp.status_code == 409
        and resp.json().get("status") == "collecting"
        and resp.json().get("missing_ranges") == [
            {"start": 0, "end": 9},
            {"start": 30, "end": 254},
        ]
        and resp.json().get("decode") is None,
        f"got {resp.status_code}: {resp.text[:200]}",
    )

    # 10. Conflicting overlapping bytes atomically reject the session, and
    #     the rejection is terminal and identical on every later call.
    conf_id = rid("conflict")
    _put_fragment(client, conf_id, 0, frame[0:10])
    resp = _put_fragment(client, conf_id, 5, b"\x00" * 5)
    conflict_body = resp.json() if resp.status_code == 409 else {}
    check(
        "byte conflict -> 409 rejected with conflict range [5,9]",
        resp.status_code == 409
        and conflict_body.get("status") == "rejected"
        and conflict_body.get("conflict_ranges") == [{"start": 5, "end": 9}],
        f"got {resp.status_code}: {resp.text[:200]}",
    )
    consistent = _put_fragment(client, conf_id, 5, frame[5:10])
    after_new = _put_fragment(client, conf_id, 100, frame[100:120])
    submit = client.post(f"/assemblies/{conf_id}/decode")
    check(
        "rejection is irreversible; late calls replay same 409",
        all(r.status_code == 409 for r in (consistent, after_new, submit))
        and consistent.json() == conflict_body
        and after_new.json() == conflict_body
        and submit.json() == conflict_body,
        f"{consistent.status_code},{after_new.status_code},{submit.status_code}",
    )

    # 11. An erasure claim contradicting earlier reliable data rejects too.
    er_id = rid("erasure-conflict")
    _put_fragment(client, er_id, 0, frame[0:10])
    resp = _put_fragment(client, er_id, 5, frame[5:8], erasures=[0])
    check(
        "erasure contradicting reliable data -> 409 at position 5",
        resp.status_code == 409
        and resp.json().get("conflict_ranges") == [{"start": 5, "end": 5}],
        f"got {resp.status_code}: {resp.text[:200]}",
    )

    # 12. Two fragments carrying different bytes at a position both declare
    #     erased contradict each other just like any overlapping byte -- and
    #     the rejection is the same in either arrival order. Identical filler
    #     at a mutually erased overlap is still accepted.
    fill_a = rid("erased-filler-conflict-a")
    fill_b = rid("erased-filler-conflict-b")
    r_a = _put_fragment(client, fill_a, 0, frame[0:5], erasures=[3])
    r_a = _put_fragment(client, fill_a, 3, b"\xff" + frame[4:5], erasures=[0])
    _put_fragment(client, fill_b, 3, b"\xff" + frame[4:5], erasures=[0])
    r_b = _put_fragment(client, fill_b, 0, frame[0:5], erasures=[3])
    fill_ok = rid("erased-filler-identical")
    _put_fragment(client, fill_ok, 0, frame[0:5], erasures=[3])
    r_ok = _put_fragment(client, fill_ok, 3, frame[3:5], erasures=[0])
    check(
        "different bytes at mutually erased position -> 409, order-independent",
        r_a.status_code == 409
        and r_a.json().get("conflict_ranges") == [{"start": 3, "end": 3}]
        and r_b.status_code == 409
        and r_b.json().get("conflict_ranges") == [{"start": 3, "end": 3}],
        f"got {r_a.status_code}/{r_b.status_code}",
    )
    check(
        "identical filler at mutually erased position accepted",
        r_ok.status_code == 200 and r_ok.json().get("erasures") == [3],
        f"got {r_ok.status_code}: {r_ok.text[:200]}",
    )

    # 13. An uncorrectable assembly completes into a frozen 422, replayed by
    #     every later submit exactly like POST /decode.
    bad_id = rid("uncorrectable")
    for off, length in PARTS:
        _put_fragment(client, bad_id, off, uncorrectable_frame[off : off + length])
    resp = client.post(f"/assemblies/{bad_id}/decode")
    body = resp.json() if resp.status_code == 422 else {}
    plain_bad = client.post(
        "/decode", json={"codeword": uncorrectable_frame.hex(), "erasures": []}
    ).json()
    check(
        "uncorrectable assembly -> 422 with /decode-identical body, frozen",
        resp.status_code == 422
        and body.get("status") == "completed"
        and body.get("decode") == plain_bad
        and client.post(f"/assemblies/{bad_id}/decode").json() == body,
        f"got {resp.status_code}: {resp.text[:200]}",
    )

    # 14. Malformed fragments are plain 400s (never 409).
    malformed = [
        {"offset": 250, "data": "ab" * 10},  # spills past byte 254
        {"offset": -1, "data": "ab"},
        {"offset": 0, "data": "zz"},
        {"offset": 0, "data": "abc"},
        {"offset": 0, "data": "aabb", "erasures": [2, 2]},
        {"offset": 0, "data": "aabb", "erasures": [9]},
    ]
    bad_codes = [
        client.put("/assemblies/acc-malformed/fragments", json=payload).status_code
        for payload in malformed
    ]
    check("malformed fragments -> 400", bad_codes == [400] * len(malformed),
          f"got {bad_codes}")

    # 15. Last-fragment race: N-1 bytes are in; appenders and submitters race
    #     concurrently. Exactly one decode may happen, and every waiter must
    #     observe the same reproducible terminal.
    race_id = rid("race")
    _put_fragment(client, race_id, 0, frame[: N - 1],
                  [p for p in era if p < N - 1])
    barrier = threading.Barrier(12)
    terminal = []
    terminal_lock = threading.Lock()
    errors = []

    def submitter():
        barrier.wait()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            resp = client.post(f"/assemblies/{race_id}/decode")
            if resp.status_code == 200:
                with terminal_lock:
                    terminal.append(resp.json())
                return
            if resp.status_code != 409:
                errors.append(f"submitter got {resp.status_code}")
                return
            time.sleep(0.01)
        errors.append("submitter timed out waiting for completion")

    def appender():
        barrier.wait()
        resp = _put_fragment(client, race_id, N - 1, frame[N - 1 :])
        if resp.status_code != 200:
            errors.append(
                f"appender PUT got {resp.status_code} {resp.text[:120]}"
            )
            return
        # PUT only gathers; delivering the final byte still leaves the
        # terminal to be produced by a submit (status collecting, complete).
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            resp = client.post(f"/assemblies/{race_id}/decode")
            if resp.status_code == 200:
                with terminal_lock:
                    terminal.append(resp.json())
                return
            if resp.status_code != 409:
                errors.append(f"appender submit got {resp.status_code}")
                return
            time.sleep(0.01)
        errors.append("appender timed out waiting for completion")

    threads = [threading.Thread(target=submitter) for _ in range(10)]
    threads += [threading.Thread(target=appender) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    reference = terminal[0]["decode"] if terminal else None
    check(
        "last-fragment race: all waiters share one reproducible terminal",
        not errors
        and len(terminal) == 12
        and all(view["decode"] == reference for view in terminal)
        and all(view["received_bytes"] == N for view in terminal)
        and reference is not None
        and reference.get("status") == "ok"
        and reference.get("payload") == payload.hex()
        and reference.get("corrected_positions") == expected_positions,
        f"errors={errors[:2]} terminals={len(terminal)}",
    )

    # 16. Sessions are independent: a rejected id never blocks another id.
    check(
        "rejected session does not block a fresh session",
        client.post(f"/assemblies/{conf_id}/decode").status_code == 409
        and client.post(f"/assemblies/{aid}/decode").status_code == 200,
    )


def report():
    if failures:
        print(f"\nACCEPTANCE FAILED: {len(failures)} check(s) failed", flush=True)
        return 1
    print("\nACCEPTANCE PASSED: all checks succeeded", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
