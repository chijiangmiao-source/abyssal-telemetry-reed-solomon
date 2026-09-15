"""In-process assembly sessions for out-of-order frame fragments.

Sea-trial downlinks split a 255-byte codeword into independently retried
fragments that arrive in any order. An ``AssemblySession`` merges those
fragments, rejects contradictory overlaps atomically, and -- once every byte
is present -- hands the assembled word and the de-duplicated erasure set to
the existing RS decoder exactly once.

State machine (only three states)::

    collecting --complete submit--> completed
    collecting --contradiction----> rejected

``completed`` and ``rejected`` are terminal: later calls replay the stored
terminal outcome and never mutate anything.

Concurrency
-----------
Every session owns an :class:`threading.RLock`; appends and submits execute
under it, so all operations on one session are linearized (the first submit
to observe a complete frame produces the one immutable decode result).
Sessions have independent locks, so different sessions never block each
other. All state lives in the creating process; after a restart the same id
starts a brand-new session.
"""

from dataclasses import dataclass, field
from threading import Lock, RLock
from typing import Callable, Optional

from .rs.decoder import N, UncorrectableError, decode as rs_decode

COLLECTING = "collecting"
COMPLETED = "completed"
REJECTED = "rejected"

# Outcome tags returned by session.submit() (append reuses the state tags).
GAPS = "gaps"
RESULT = "result"


def normalize_ranges(positions) -> list[dict]:
    """Merge positions into canonical inclusive, ascending ranges.

    ``{1, 2, 3, 8}`` -> ``[{"start": 1, "end": 3}, {"start": 8, "end": 8}]``.
    Adjacent positions are coalesced; duplicates and order do not matter.
    """
    ranges: list[tuple[int, int]] = []
    for pos in sorted(set(positions)):
        if ranges and pos <= ranges[-1][1] + 1:
            if pos > ranges[-1][1]:
                ranges[-1] = (ranges[-1][0], pos)
        else:
            ranges.append((pos, pos))
    return [{"start": lo, "end": hi} for lo, hi in ranges]


def missing_ranges(filled) -> list[dict]:
    """Normalized gaps inside ``[0, N)`` given the filled byte positions."""
    ranges: list[tuple[int, int]] = []
    expect = 0
    for pos in sorted(filled):
        if pos > expect:
            ranges.append((expect, pos - 1))
        expect = pos + 1
    if expect < N:
        ranges.append((expect, N - 1))
    return [{"start": lo, "end": hi} for lo, hi in ranges]


@dataclass
class AssemblySession:
    """One frame being reassembled.

    ``decoder`` is injectable so tests can observe/serialize the single
    decode invocation; production passes the real RS decoder.
    """

    id: str
    decoder: Callable = rs_decode
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    # Frame-absolute byte position -> byte value. Also holds the filler
    # bytes delivered for erased positions; _erasures marks unreliability.
    _bytes: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _erasures: set[int] = field(default_factory=set, init=False, repr=False)
    # Positions ever delivered as reliable (not declared erased) by a
    # fragment; a later erasure claim on any of them is a contradiction.
    _known: set[int] = field(default_factory=set, init=False, repr=False)
    _status: str = COLLECTING
    _conflict_ranges: Optional[list[dict]] = field(default=None, init=False, repr=False)
    # ("ok", corrected_word, positions) | ("uncorrectable", syndromes)
    _result: Optional[tuple] = field(default=None, init=False, repr=False)

    @property
    def status(self) -> str:
        return self._status

    @property
    def erasures(self) -> list[int]:
        return sorted(self._erasures)

    def append(self, offset: int, data: bytes, erasures) -> tuple:
        """Merge one fragment.

        ``erasures`` are frame-absolute positions this fragment declares
        unreliable. The merge is atomic: on any contradiction the session
        becomes ``rejected`` and no fragment byte is applied.

        Returns:
          * ``(COLLECTING, progress)`` while bytes are still missing;
          * ``(REJECTED, conflict_ranges)`` for a contradiction (or a replay
            of the earlier rejection on a terminal session);
          * ``(COMPLETED, terminal_view)`` when the session was already
            completed by a submit (late fragments replay the terminal).
        """
        erasures = set(erasures)
        with self._lock:
            if self._status == REJECTED:
                return REJECTED, self._conflict_ranges
            if self._status == COMPLETED:
                return COMPLETED, self.terminal_view()

            clashes: set[int] = set()
            for i, byte in enumerate(data):
                pos = offset + i
                if pos in erasures:
                    # Unreliable filler: no claim about its value, so it can
                    # neither cause nor settle a byte-value conflict.
                    continue
                if pos in self._erasures:
                    # Reliable data where an earlier fragment claimed an
                    # erasure: the erasure claims disagree.
                    clashes.add(pos)
                elif pos in self._bytes and self._bytes[pos] != byte:
                    # Overlapping reliable byte delivered with a new value.
                    clashes.add(pos)
            # This fragment declares an erasure where earlier fragments
            # reliably delivered data (re-declaring an already-erased
            # position is merely de-duplicated, not a conflict).
            clashes.update(pos for pos in erasures if pos in self._known)

            if clashes:
                # Atomic rejection: discard the pending fragment entirely.
                self._status = REJECTED
                self._conflict_ranges = normalize_ranges(clashes)
                return REJECTED, self._conflict_ranges

            for i, byte in enumerate(data):
                pos = offset + i
                self._bytes[pos] = byte
                if pos in erasures:
                    self._erasures.add(pos)
                else:
                    self._known.add(pos)
            return COLLECTING, self.progress_view()

    def submit(self) -> tuple:
        """Submit the current assembly for decoding.

        Returns:
          * ``(GAPS, progress)`` with 409-worthy normalized missing ranges;
          * ``(RESULT, terminal_view)`` running -- or replaying -- the
            decoder;
          * ``(REJECTED, conflict_ranges)`` on a rejected session.

        The decoder runs at most once: the first submit after the final byte
        lands caches its outcome and every later submit replays it.
        """
        with self._lock:
            if self._status == REJECTED:
                return REJECTED, self._conflict_ranges
            if self._status == COMPLETED:
                return RESULT, self.terminal_view()

            gaps = missing_ranges(self._bytes)
            if gaps:
                return GAPS, self.progress_view()

            word = [self._bytes[i] for i in range(N)]
            erasures = sorted(self._erasures)
            try:
                corrected, positions = self.decoder(word, erasures)
            except UncorrectableError as exc:
                self._result = ("uncorrectable", list(exc.syndromes))
            else:
                self._result = ("ok", corrected, positions)
            self._status = COMPLETED
            return RESULT, self.terminal_view()

    def progress_view(self) -> dict:
        return {
            "status": COLLECTING,
            "id": self.id,
            "received_bytes": len(self._bytes),
            "total_bytes": N,
            "complete": len(self._bytes) == N,
            "missing_ranges": missing_ranges(self._bytes),
            "erasures": sorted(self._erasures),
            "result": None,
        }

    def terminal_view(self) -> dict:
        """Immutable terminal snapshot (must be COMPLETED)."""
        return {
            "status": COMPLETED,
            "id": self.id,
            "received_bytes": N,
            "total_bytes": N,
            "complete": True,
            "missing_ranges": [],
            "erasures": sorted(self._erasures),
            "result": self._result,
        }


class AssemblyStore:
    """Process-wide registry of assembly sessions.

    Session creation is guarded by one short-lived gate; all per-session
    work happens on that session's own lock, so independent sessions never
    contend. State dies with the process: nothing is persisted, and after a
    restart a request for the same id simply creates a fresh session.
    """

    def __init__(self, decoder: Callable = rs_decode):
        self._decoder = decoder
        self._gate = Lock()
        self._sessions: dict[str, AssemblySession] = {}

    def session(self, assembly_id: str) -> AssemblySession:
        """Get the session for the id, creating it (collecting) on first use."""
        with self._gate:
            sess = self._sessions.get(assembly_id)
            if sess is None:
                sess = AssemblySession(id=assembly_id, decoder=self._decoder)
                self._sessions[assembly_id] = sess
            return sess

    def clear(self) -> None:
        """Drop every session (tests only)."""
        with self._gate:
            self._sessions.clear()
