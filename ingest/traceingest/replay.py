"""Trace replayer: plays a dataset back as if it were live traffic.

Spans are emitted when they *finish* (start + duration), which is when a real
OpenTelemetry SDK exports them, so the pipeline sees spans of one trace arrive
spread out over time, the same as with a live application. That is what the
quiet-window trace completion has to cope with.

Speed: 1.0 replays at the original pace, 2.0 / 10.0 compress the timeline, and
``float('inf')`` sends everything as fast as possible. Span durations are never
scaled; only the gaps between spans are.

With ``rebase_to_now`` each trace is moved, as a whole, so the replayed spans
look like they are happening right now (what the freshness measurement N1
needs). Timing inside a trace is never changed; only the gaps between traces
are compressed by the speed-up.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, List, Optional

from .sinks import Sink
from .spans import Span


@dataclass
class ReplayStats:
    spans: int = 0
    batches: int = 0
    speed: float = 1.0
    dataset_span_s: float = 0.0          # original time covered by the data
    wall_s: float = 0.0                  # how long the replay took
    max_lag_s: float = 0.0               # worst lateness vs the schedule
    lags_s: List[float] = field(default_factory=list, repr=False)

    def summary(self) -> dict:
        lags = sorted(self.lags_s)
        p99 = lags[min(len(lags) - 1, int(math.ceil(0.99 * len(lags))) - 1)] if lags else 0.0
        return {"spans": self.spans, "batches": self.batches, "speed": self.speed,
                "dataset_span_s": round(self.dataset_span_s, 3), "wall_s": round(self.wall_s, 3),
                "max_lag_s": round(self.max_lag_s, 4), "p99_lag_s": round(p99, 4)}


def emission_order(spans: Iterable[Span]) -> List[Span]:
    return sorted(spans, key=lambda s: (s.end_us, s.trace_id, s.span_id))


class Replayer:
    def __init__(self, sink: Sink, speed: float = 1.0, max_batch: int = 512,
                 flush_interval_s: float = 1.0, rebase_to_now: bool = False,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 wall_epoch_us: Callable[[], int] = lambda: int(time.time() * 1_000_000),
                 on_batch: Optional[Callable[[int, int], None]] = None):
        if speed <= 0:
            raise ValueError("speed must be > 0 (use float('inf') for as-fast-as-possible)")
        self.sink, self.speed, self.max_batch = sink, speed, max_batch
        self.flush_interval_s, self.rebase_to_now = flush_interval_s, rebase_to_now
        self._clock, self._sleep, self._wall_epoch_us = clock, sleep, wall_epoch_us
        self._on_batch = on_batch

    def run(self, spans: Iterable[Span], limit: Optional[int] = None) -> ReplayStats:
        ordered = emission_order(spans)
        if limit is not None:
            ordered = ordered[:limit]
        stats = ReplayStats(speed=self.speed)
        if not ordered:
            return stats
        t0 = ordered[0].end_us
        trace_start = {}
        for s in ordered:
            if s.trace_id not in trace_start or s.start_us < trace_start[s.trace_id]:
                trace_start[s.trace_id] = s.start_us
        stats.dataset_span_s = (ordered[-1].end_us - t0) / 1e6
        fast = math.isinf(self.speed)
        start = self._clock()
        epoch0 = self._wall_epoch_us()
        last_flush = start
        buf: List[Span] = []

        def flush():
            nonlocal buf, last_flush
            if buf:
                self.sink.send(buf)
                stats.batches += 1
                if self._on_batch:
                    self._on_batch(stats.batches, stats.spans)
                buf = []
            last_flush = self._clock()

        for s in ordered:
            offset_s = (s.end_us - t0) / 1e6
            if not fast:
                target = start + offset_s / self.speed
                now = self._clock()
                if target > now:
                    # flush what we have before waiting, so batches do not sit around
                    if buf and target - last_flush >= self.flush_interval_s:
                        flush()
                    wait = target - self._clock()
                    if wait > 0:
                        self._sleep(wait)
                lag = max(0.0, self._clock() - target)
                stats.lags_s.append(lag)
                stats.max_lag_s = max(stats.max_lag_s, lag)
            if self.rebase_to_now:
                # move each trace as a unit so parent/child timing inside it stays exact
                tmin = trace_start[s.trace_id]
                anchor = epoch0 + int((tmin - t0) / (1.0 if fast else self.speed))
                s = replace(s, start_us=anchor + (s.start_us - tmin))
            buf.append(s)
            stats.spans += 1
            if len(buf) >= self.max_batch:
                flush()
        flush()
        stats.wall_s = self._clock() - start
        return stats


class FakeClock:
    """Deterministic clock for tests: sleep() advances time instantly."""

    def __init__(self, t: float = 1000.0):
        self.t = t
        self.slept = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        self.slept += s
