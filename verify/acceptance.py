"""One-shot acceptance suite for the RS(255,223) decoding service.

Runs against a live API (API_BASE_URL, default http://localhost:8000),
builds its own frames with the local systematic encoder, and checks the
contract end to end:

* a clean zero-syndrome frame decodes with no reported corrections;
* 16 unknown errors are fully recovered;
* 10 erasures + 11 unknown errors are fully recovered (same payload);
* 17 errors are rejected with 32 recomputable syndromes;
* 33 erasures are rejected;
* duplicate / out-of-range erasures are rejected with 400.

Exits 0 when every check passes, 1 otherwise.
"""

import os
import random
import sys
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

    return report()


def report():
    if failures:
        print(f"\nACCEPTANCE FAILED: {len(failures)} check(s) failed", flush=True)
        return 1
    print("\nACCEPTANCE PASSED: all checks succeeded", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
