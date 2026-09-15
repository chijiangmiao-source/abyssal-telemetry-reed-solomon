"""Unit tests for the assembly session domain object.

Covers out-of-order merging, idempotent retries, atomic conflicts, terminal
immutability and linearization of concurrent appends/submits.
"""

import random
import threading
import time

import pytest

from app.assembly import (
    COLLECTING,
    COMPLETED,
    GAPS,
    REJECTED,
    RESULT,
    AssemblySession,
    AssemblyStore,
    missing_ranges,
    normalize_ranges,
)
from app.rs.decoder import N
from app.rs.encoder import encode


@pytest.fixture()
def frame():
    rng = random.Random(0xA55E)
    payload = bytes(rng.randrange(256) for _ in range(223))
    codeword = bytearray(encode(payload))
    era = [3, 77, 120, 200]
    err = [50, 51, 90, 254]
    for pos in era + err:
        codeword[pos] ^= rng.randrange(1, 256)
    return bytes(codeword), payload, sorted(era), sorted(era + err)


class TestRanges:
    def test_normalize_merges_adjacent_and_dedupes(self):
        assert normalize_ranges([8, 1, 2, 3, 2, 10, 11]) == [
            {"start": 1, "end": 3},
            {"start": 8, "end": 8},
            {"start": 10, "end": 11},
        ]

    def test_normalize_empty(self):
        assert normalize_ranges([]) == []

    def test_missing_ranges_full_and_empty(self):
        assert missing_ranges(set(range(N))) == []
        assert missing_ranges(set()) == [{"start": 0, "end": N - 1}]

    def test_missing_ranges_pieces(self):
        assert missing_ranges({0, 1, 5, 254}) == [
            {"start": 2, "end": 4},
            {"start": 6, "end": 253},
        ]


class TestMerging:
    def test_out_of_order_fragments_complete_frame(self, frame):
        word, payload, era, changed = frame
        s = AssemblySession("a")
        parts = [(0, 40), (40, 40), (80, 40), (120, 80), (200, 55)]
        for off, length in reversed(parts):
            frag_er = [p for p in era if off <= p < off + length]
            tag, view = s.append(off, word[off : off + length], frag_er)
            assert tag == COLLECTING
        assert s.status == COLLECTING
        tag, view = s.submit()
        assert tag == RESULT
        assert view["status"] == COMPLETED
        kind, corrected, positions = view["result"]
        assert kind == "ok"
        assert bytes(corrected[:223]).hex() == payload.hex()
        assert bytes(corrected).hex() == encode(payload).hex()
        assert positions == changed
        assert view["erasures"] == era

    def test_progress_reports_canonical_gaps(self, frame):
        word, _, _, _ = frame
        s = AssemblySession("a")
        s.append(10, word[10:20], [])
        tag, view = s.submit()
        assert tag == GAPS
        assert view["status"] == COLLECTING
        assert view["received_bytes"] == 10
        assert view["complete"] is False
        assert view["missing_ranges"] == [
            {"start": 0, "end": 9},
            {"start": 20, "end": N - 1},
        ]

    def test_gap_submit_does_not_finalize(self, frame):
        word, _, _, _ = frame
        s = AssemblySession("a")
        s.submit()
        assert s.status == COLLECTING
        for off in range(0, N, 16):
            s.append(off, word[off : min(off + 16, N)], [])
        tag, view = s.submit()
        assert tag == RESULT
        assert view["result"][0] == "ok"

    def test_same_content_retry_is_idempotent(self, frame):
        word, _, era, _ = frame
        s = AssemblySession("a")
        _, first = s.append(0, word[0:40], [p for p in era if p < 40])
        _, second = s.append(0, word[0:40], [p for p in era if p < 40])
        assert first == second
        assert first["received_bytes"] == 40
        _, third = s.append(40, word[40:80], [])
        assert third["received_bytes"] == 80
        # overlapping identical bytes do not grow the count
        _, fourth = s.append(30, word[30:80], [])
        assert fourth["received_bytes"] == 80

    def test_garbage_filler_at_erased_position_is_not_a_conflict(self, frame):
        word, _, era, _ = frame
        s = AssemblySession("a")
        s.append(0, word[0:10], [3])
        # Position 3 is re-delivered with a different byte but still erased.
        tag, view = s.append(3, b"\xff", [3])
        assert tag == COLLECTING
        assert view["erasures"] == [3]

    def test_redeclaring_erasure_is_deduplicated(self, frame):
        word, _, _, _ = frame
        s = AssemblySession("a")
        s.append(0, word[0:10], [2])
        _, view = s.append(0, word[0:10], [2])
        assert view["erasures"] == [2]


class TestConflicts:
    def test_differing_overlapping_bytes_reject_atomically(self):
        s = AssemblySession("a")
        s.append(0, b"\x01\x02\x03", [])
        tag, ranges = s.append(2, b"\xff\x07", [])
        assert tag == REJECTED
        # Position 2 differs; the otherwise-new byte at 3 must not be kept.
        assert ranges == [{"start": 2, "end": 2}]
        assert s.status == REJECTED
        _, replay = s.append(100, b"\x00" * 10, [])
        assert replay == ranges

    def test_conflicting_fragment_applies_nothing(self):
        s = AssemblySession("a")
        s.append(0, b"\x01\x02\x03", [])
        s.append(2, b"\xff\x07", [])
        # Even a valid later fragment is rejected; byte 3 never landed.
        tag, ranges = s.append(3, b"\x07", [])
        assert tag == REJECTED
        assert ranges == [{"start": 2, "end": 2}]

    def test_conflict_ranges_normalized(self):
        s = AssemblySession("a")
        s.append(0, b"\x00" * 20, [])
        tag, ranges = s.append(5, b"\xff" * 5, [])
        assert tag == REJECTED
        assert ranges == [{"start": 5, "end": 9}]

    def test_erasure_after_reliable_data_is_conflict(self, frame):
        word, _, _, _ = frame
        s = AssemblySession("a")
        s.append(0, word[0:10], [])
        tag, ranges = s.append(5, word[5:8], [5])  # erases frame pos 5
        assert tag == REJECTED
        assert ranges == [{"start": 5, "end": 5}]

    def test_reliable_data_after_erasure_is_conflict(self, frame):
        word, _, _, _ = frame
        s = AssemblySession("a")
        s.append(5, word[5:8], [5])  # frame pos 5 erased
        tag, ranges = s.append(5, word[5:8], [])  # now claimed reliable
        assert tag == REJECTED
        assert ranges == [{"start": 5, "end": 5}]

    def test_rejected_submit_replays_conflict(self):
        s = AssemblySession("a")
        s.append(0, b"\x01\x02", [])
        _, ranges = s.append(1, b"\xff", [])
        tag, replay = s.submit()
        assert tag == REJECTED
        assert replay == ranges
        assert s.status == REJECTED


class TestTerminalImmutability:
    def _full_session(self, word, era):
        s = AssemblySession("a")
        parts = [(0, 40), (40, 40), (80, 40), (120, 80), (200, 55)]
        for off, length in parts:
            s.append(
                off,
                word[off : off + length],
                [p for p in era if off <= p < off + length],
            )
        return s

    def test_completed_decodes_once_and_replays(self, frame):
        word, payload, era, _ = frame
        calls = []

        def counting_decoder(w, er):
            calls.append((list(w), list(er)))
            return AssemblySession.decoder(w, er)

        s = AssemblySession("a", decoder=counting_decoder)
        parts = [(0, 40), (40, 40), (80, 40), (120, 80), (200, 55)]
        for off, length in parts:
            s.append(
                off,
                word[off : off + length],
                [p for p in era if off <= p < off + length],
            )
        tag, first = s.submit()
        assert tag == RESULT
        tag, second = s.submit()
        assert tag == RESULT
        assert first == second
        assert len(calls) == 1
        assert calls[0][1] == era

    def test_late_fragment_replays_terminal_even_if_conflicting(self, frame):
        word, _, era, _ = frame
        s = self._full_session(word, era)
        s.submit()
        # A contradictory late fragment must not change the terminal state.
        tag, view = s.append(0, b"\xff" * 10, [])
        assert tag == COMPLETED
        assert view["status"] == COMPLETED
        assert view["result"][0] == "ok"
        assert s.status == COMPLETED

    def test_uncorrectable_result_is_frozen_and_replayed(self):
        bad = bytes((i * 37) % 256 for i in range(N))
        s = AssemblySession("a")
        for off in range(0, N, 16):
            s.append(off, bad[off : min(off + 16, N)], [])
        tag, view = s.submit()
        assert tag == RESULT
        assert view["result"][0] == "uncorrectable"
        syndromes = view["result"][1]
        assert len(syndromes) == 32
        tag, replay = s.submit()
        assert replay["result"][1] == syndromes


class TestConcurrency:
    def test_concurrent_duplicate_appends_and_submits_linearize(self, frame):
        word, payload, era, changed = frame
        calls = []
        lock = threading.Lock()

        def counting_decoder(w, er):
            with lock:
                calls.append(1)
            return AssemblySession.decoder(w, er)

        s = AssemblySession("a", decoder=counting_decoder)
        parts = [(0, 40), (40, 40), (80, 40), (120, 80), (200, 55)]
        barrier = threading.Barrier(10)
        results = []
        results_lock = threading.Lock()

        def worker(spec):
            off, length = spec
            frag_er = [p for p in era if off <= p < off + length]
            barrier.wait()
            # Every fragment is delivered by two workers: pure duplicates.
            tag, view = s.append(off, word[off : off + length], frag_er)
            sub_tag, sub_view = s.submit()
            terminal = view if tag == COMPLETED else (
                sub_view if sub_tag == RESULT else None
            )
            with results_lock:
                results.append((tag, terminal))

        threads = [
            threading.Thread(target=worker, args=(spec,))
            for spec in parts
            for _ in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(calls) == 1
        assert s.status == COMPLETED
        terminals = [view for _, view in results if view is not None]
        assert terminals, "at least one worker observed completion"
        reference = terminals[0]
        for view in terminals[1:]:
            assert view["result"] == reference["result"]
        assert reference["result"][0] == "ok"
        assert bytes(reference["result"][1][:223]).hex() == payload.hex()
        assert reference["result"][2] == changed

    def test_last_fragment_race_yields_single_terminal(self, frame):
        word, payload, era, _ = frame
        calls = []
        call_lock = threading.Lock()

        def counting_decoder(w, er):
            with call_lock:
                calls.append(1)
            return AssemblySession.decoder(w, er)

        s = AssemblySession("fin", decoder=counting_decoder)
        # Everything except the final byte is already present.
        s.append(0, word[: N - 1], [p for p in era if p < N - 1])
        barrier = threading.Barrier(16)
        outcomes = []
        outcomes_lock = threading.Lock()

        def racer(i):
            barrier.wait()
            if i < 2:
                # Two racers carry the last fragment (duplicate retries);
                # the rest only submit.
                s.append(N - 1, word[N - 1 :], [])
            # Submit until the terminal appears; yield on gaps so the
            # appenders are not starved by submitters re-acquiring the lock.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                tag, view = s.submit()
                if tag == RESULT:
                    with outcomes_lock:
                        outcomes.append(view)
                    return
                assert tag == GAPS  # rejection is impossible here
                time.sleep(0)
        threads = [threading.Thread(target=racer, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert s.status == COMPLETED
        assert len(calls) == 1
        assert len(outcomes) == 16
        for view in outcomes:
            assert view["status"] == COMPLETED
            assert view["result"][0] == "ok"
            assert bytes(view["result"][1][:223]).hex() == payload.hex()
            assert view["received_bytes"] == N
            assert view["missing_ranges"] == []

    def test_conflicting_concurrent_appends_leave_one_rejection(self):
        s = AssemblySession("a")
        barrier = threading.Barrier(2)
        outcome = {}

        def worker(kind):
            barrier.wait()
            if kind == "good":
                outcome[kind] = s.append(0, b"\x01" * 20, [])
            else:
                outcome[kind] = s.append(5, b"\x02" * 10, [])

        t1 = threading.Thread(target=worker, args=("good",))
        t2 = threading.Thread(target=worker, args=("bad",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert s.status == REJECTED
        # Whichever order linearizes, exactly one request sees collecting
        # and the other sees the rejection replay. The reported clash is the
        # symmetric overlap [5, 14] in both orders: a late request reports
        # the positions where its own bytes contradict stored ones.
        tags = sorted(tag for tag, _ in outcome.values())
        assert tags == [COLLECTING, REJECTED]
        collecting = next(view for tag, view in outcome.values() if tag == COLLECTING)
        rejected = next(view for tag, view in outcome.values() if tag == REJECTED)
        assert collecting["received_bytes"] in (20, 10)
        assert rejected == [{"start": 5, "end": 14}]

    def test_independent_sessions_do_not_block_each_other(self, frame):
        word, _, era, _ = frame

        decode_started = threading.Event()
        release_decode = threading.Event()

        def slow_decoder(w, er):
            decode_started.set()
            release_decode.wait(timeout=5)
            return AssemblySession.decoder(w, er)

        slow = AssemblySession("slow", decoder=slow_decoder)
        for off in range(0, N, 16):
            slow.append(off, word[off : min(off + 16, N)], [])

        other = AssemblySession("other")
        result = {}

        def submit_slow():
            result["slow"] = slow.submit()

        t = threading.Thread(target=submit_slow)
        t.start()
        assert decode_started.wait(2)
        # While slow's decoder is parked, another session stays responsive.
        tag, view = other.append(0, word[:10], [])
        assert tag == COLLECTING
        assert view["received_bytes"] == 10
        release_decode.set()
        t.join()
        assert result["slow"][0] == RESULT


class TestStore:
    def test_get_creates_then_reuses(self):
        store = AssemblyStore()
        first = store.session("id-1")
        second = store.session("id-1")
        assert first is second
        assert store.session("id-2") is not first

    def test_ids_are_independent(self, frame):
        word, _, _, _ = frame
        store = AssemblyStore()
        store.session("a").append(0, b"\x01\x02", [])
        store.session("b").append(0, b"\x03\x04", [])
        _, view_a = store.session("a").append(0, b"\x01\x02", [])
        _, view_b = store.session("b").append(0, b"\x03\x04", [])
        assert view_a["received_bytes"] == 2
        assert view_b["received_bytes"] == 2

    def test_clear_resets(self):
        store = AssemblyStore()
        store.session("a")
        store.clear()
        before = store.session("a")
        store.clear()
        assert store.session("a") is not before
