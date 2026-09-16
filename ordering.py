"""Reorder gate for chunk-based providers.

Why this exists
---------------
Chunk providers (Groq, Whisper local, and the OpenAI-compatible ones) segment
audio into utterances, then process each one through a thread pool. A short
chunk submitted second can finish before a long chunk submitted first, so the
overlay receives "…and that concludes it" before "Good morning everyone".
During a live event that reads as the speaker talking backwards.

Streaming providers (Azure, OpenAI Realtime) do NOT have this problem — their
wire protocol delivers results in order. Those providers declare
`ordered_by_protocol=True` and the controller bypasses this gate entirely.
Running the gate on them would add its bounded wait to results that were
already correct, i.e. pure latency for zero benefit.

The trade
---------
Ordering costs latency: to release seq N you must either have N, or decide N
is never coming. A fixed deadline is wrong — a venue with 2 s round-trip
would blow a 1.2 s deadline on every chunk and skip constantly (the failure
mode the panel flagged). So the deadline is derived from what this session
has actually observed: p95 of recent chunk latencies, plus margin, clamped to
a sane range.

Partial (non-final) events bypass the gate. They are cosmetic — the overlay
replaces them in place — and delaying them defeats the point of having them.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


# Deadline bounds. Below the floor we would skip healthy chunks on any real
# network; above the ceiling the caption is too stale to be useful live.
MIN_DEADLINE_S = 0.8
MAX_DEADLINE_S = 6.0
DEFAULT_DEADLINE_S = 1.5

# How many recent latencies feed the p95 estimate.
LATENCY_WINDOW = 30

# Multiplier applied to observed p95 before using it as a deadline. A chunk
# that takes longer than 1.6x the 95th percentile is genuinely anomalous.
DEADLINE_FACTOR = 1.6

# Hard cap on buffered out-of-order items. A provider emitting nonsense must
# not grow memory without bound.
MAX_BUFFERED = 24


class ReorderGate:
    """Releases sequenced payloads in `seq` order, with an adaptive deadline.

    Thread-safe: `submit()` is called from provider worker threads, `tick()`
    from the GUI timer. Release callbacks always run on the caller's thread —
    the controller marshals to the GUI thread downstream.
    """

    def __init__(
        self,
        on_release: Callable[[object], None],
        now_fn: Callable[[], float] | None = None,
        deadline_s: float = DEFAULT_DEADLINE_S,
    ):
        self._on_release = on_release
        self._now = now_fn or _monotonic
        # RLock, and every release happens while HOLDING it.
        #
        # Draining under a lock only orders the DECISION about what to send.
        # If the lock is dropped before delivering, two worker threads that
        # each drained correctly then race to call on_release, and the OS
        # can run the later one first — reinstating exactly the scrambling
        # this class exists to prevent. Delivery must be serialised too.
        #
        # Re-entrant because a release callback that (directly or not) calls
        # back into the gate on the SAME thread would otherwise self-deadlock.
        self._lock = threading.RLock()
        self._pending: dict[int, tuple[object, float]] = {}   # seq -> (payload, arrived_at)
        self._next_seq = 0
        self._latencies: list[float] = []
        self._deadline_s = deadline_s
        self._skipped = 0
        self._released = 0

    # -- metrics --------------------------------------------------------

    @property
    def skipped(self) -> int:
        """Chunks given up on. Non-zero means captions had gaps."""
        return self._skipped

    @property
    def released(self) -> int:
        return self._released

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def deadline_s(self) -> float:
        with self._lock:
            return self._deadline_s

    # -- lifecycle ------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self._pending.clear()
            self._next_seq = 0
            self._latencies.clear()
            self._deadline_s = DEFAULT_DEADLINE_S
            self._skipped = 0
            self._released = 0

    def observe_latency(self, seconds: float) -> None:
        """Feed an observed chunk processing latency to the deadline estimator."""
        if seconds <= 0:
            return
        with self._lock:
            self._latencies.append(seconds)
            if len(self._latencies) > LATENCY_WINDOW:
                self._latencies.pop(0)
            self._deadline_s = _clamp(
                _p95(self._latencies) * DEADLINE_FACTOR,
                MIN_DEADLINE_S,
                MAX_DEADLINE_S,
            )

    # -- the gate -------------------------------------------------------

    def submit(self, seq: int | None, payload: object) -> None:
        """Queue a payload. Releases whatever became contiguous, in order.

        `seq is None` ⇒ caller declared the payload unsequenced; released
        immediately. That is the escape hatch for streaming providers and for
        partial events.
        """
        with self._lock:
            if seq is None:
                # Unsequenced (streaming provider, or a cosmetic partial).
                # Still delivered under the lock so it cannot overtake a
                # sequenced release that is mid-flight on another thread.
                self._emit([payload])
                return
            if seq < self._next_seq:
                # A straggler for a slot we already gave up on. Dropping it is
                # correct: releasing it now would put it AFTER later text,
                # which is the exact scrambling this gate exists to prevent.
                log.info("reorder: dropping late seq=%s (already at %s)",
                         seq, self._next_seq)
                self._skipped += 1
                return
            self._pending[seq] = (payload, self._now())
            ready = self._drain_contiguous_locked()
            if len(self._pending) > MAX_BUFFERED:
                ready.extend(self._force_forward_locked())
            self._emit(ready)

    def tick(self) -> None:
        """Called periodically. Gives up on a chunk whose deadline passed."""
        with self._lock:
            if not self._pending:
                return
            oldest_wait = self._now() - min(t for _, t in self._pending.values())
            if oldest_wait < self._deadline_s:
                return
            self._emit(self._force_forward_locked())

    def flush(self) -> None:
        """Release everything still buffered, in seq order. Used on stop()."""
        with self._lock:
            ready = [self._pending[s][0] for s in sorted(self._pending)]
            self._released += len(ready)
            self._pending.clear()
            self._emit(ready)

    # -- internals ------------------------------------------------------

    def _drain_contiguous_locked(self) -> list[object]:
        out: list[object] = []
        while self._next_seq in self._pending:
            payload, _ = self._pending.pop(self._next_seq)
            out.append(payload)
            self._next_seq += 1
            self._released += 1
        return out

    def _force_forward_locked(self) -> list[object]:
        """Skip the missing seq and resume from the lowest buffered one."""
        if not self._pending:
            return []
        lowest = min(self._pending)
        gap = lowest - self._next_seq
        if gap > 0:
            log.warning("reorder: giving up on seq %s..%s after %.1fs (deadline)",
                        self._next_seq, lowest - 1, self._deadline_s)
            self._skipped += gap
        self._next_seq = lowest
        return self._drain_contiguous_locked()

    def _emit(self, payloads: list[object]) -> None:
        for p in payloads:
            try:
                self._on_release(p)
            except Exception:
                log.exception("reorder: on_release raised")


def _monotonic() -> float:
    import time
    return time.monotonic()


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _p95(values: list[float]) -> float:
    if not values:
        return DEFAULT_DEADLINE_S
    ordered = sorted(values)
    # Nearest-rank p95; for tiny samples this lands on the max, which is the
    # conservative choice while we are still learning the venue's latency.
    idx = max(0, round(0.95 * len(ordered)) - 1)
    return ordered[idx]


# ---------------------------------------------------------------- self-check


def _selfcheck() -> None:
    """Run with `python ordering.py`. Fails loudly if the gate regresses."""
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    # 1. Out-of-order arrivals come out in order.
    got: list[object] = []
    g = ReorderGate(got.append, now_fn=now)
    g.submit(2, "c")
    g.submit(0, "a")
    assert got == ["a"], got            # 1 still missing, c must wait
    g.submit(1, "b")
    assert got == ["a", "b", "c"], got
    assert g.skipped == 0

    # 2. A chunk that never arrives is skipped after the deadline, not forever.
    got.clear()
    g = ReorderGate(got.append, now_fn=now)
    g.submit(0, "a")
    g.submit(2, "c")                    # 1 is missing
    assert got == ["a"], got
    clock["t"] += 0.1
    g.tick()
    assert got == ["a"], got            # deadline not reached yet
    clock["t"] += DEFAULT_DEADLINE_S
    g.tick()
    assert got == ["a", "c"], got       # gave up on 1, kept going
    assert g.skipped == 1, g.skipped

    # 3. A straggler for a skipped slot is dropped, never appended out of order.
    g.submit(1, "b")
    assert got == ["a", "c"], got
    assert g.skipped == 2, g.skipped

    # 4. seq=None bypasses the gate entirely (streaming / partials).
    got.clear()
    g = ReorderGate(got.append, now_fn=now)
    g.submit(None, "live")
    assert got == ["live"], got

    # 5. Deadline adapts to observed latency and stays inside its bounds.
    #    A slow venue must widen the deadline instead of skipping every chunk.
    g = ReorderGate(lambda _: None, now_fn=now)
    for _ in range(20):
        g.observe_latency(2.0)
    assert abs(g.deadline_s() - 2.0 * DEADLINE_FACTOR) < 1e-6, g.deadline_s()
    assert g.deadline_s() > DEFAULT_DEADLINE_S, "slow venue must widen the deadline"
    # Absurdly slow: clamped at the ceiling, not unbounded.
    g.reset()
    for _ in range(20):
        g.observe_latency(30.0)
    assert g.deadline_s() == MAX_DEADLINE_S, g.deadline_s()
    # Very fast: clamped at the floor, so we never skip a healthy chunk.
    g.reset()
    for _ in range(20):
        g.observe_latency(0.05)
    assert g.deadline_s() == MIN_DEADLINE_S, g.deadline_s()

    # 6. A flood of out-of-order items does not grow without bound.
    got.clear()
    g = ReorderGate(got.append, now_fn=now)
    for s in range(MAX_BUFFERED + 5, 0, -1):
        g.submit(s, s)
    assert g.pending_count <= MAX_BUFFERED, g.pending_count

    # 7. flush() drains in order (end of session must not lose the last line).
    got.clear()
    g = ReorderGate(got.append, now_fn=now)
    g.submit(5, "e")
    g.submit(3, "c")
    g.flush()
    assert got == ["c", "e"], got

    # 8. Concurrency: delivery is serialised, not just the decision.
    #    Draining under a lock and releasing it before calling on_release
    #    lets two worker threads race to deliver, so the later one can land
    #    first. This reproduces that with many threads and a callback slow
    #    enough to lose the race reliably.
    import threading as _th

    delivered: list[int] = []
    start_gun = _th.Barrier(MAX_BUFFERED)

    def slow_append(item) -> None:
        # A slow callback widens the window in which a competing thread could
        # interleave. With delivery outside the lock, this loses the race.
        time.sleep(0.002)
        delivered.append(item)

    def submit_at(gate, i):
        start_gun.wait()          # all threads pile in at the same instant
        gate.submit(i, i)

    g = ReorderGate(slow_append)
    # Stay at or under MAX_BUFFERED so this case tests ORDERING only; the
    # overflow policy is exercised by case 6.
    n_items = MAX_BUFFERED
    threads = [_th.Thread(target=submit_at, args=(g, i)) for i in range(n_items)]
    import random as _rnd
    _rnd.seed(7)
    _rnd.shuffle(threads)         # submit in a shuffled order
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    g.flush()

    assert delivered == sorted(delivered), (
        "entrega fora de ordem sob concorrencia; "
        f"primeiras divergencias: {[(i, v) for i, v in enumerate(delivered) if i != v][:5]}"
    )
    assert len(delivered) == n_items, (len(delivered), n_items)
    assert g.skipped == 0, g.skipped

    print("ordering.py self-check OK (8 cases)")


if __name__ == "__main__":
    _selfcheck()
