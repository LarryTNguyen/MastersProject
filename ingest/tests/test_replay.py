import json
import os
import tempfile
import unittest

from traceingest import formats, otlp
from traceingest.replay import FakeClock, Replayer, emission_order
from traceingest.sinks import FileSink, MemorySink
from traceingest.spans import Span

from .helpers import load


class ReplayTests(unittest.TestCase):
    def run_replay(self, spans, speed, **kw):
        clock = FakeClock()
        sink = MemorySink(clock=clock)
        r = Replayer(sink, speed=speed, clock=clock, sleep=clock.sleep,
                     wall_epoch_us=lambda: 1_800_000_000_000_000, **kw)
        return r.run(spans), sink, clock

    def test_original_timing(self):
        spans = load("alibaba_small.csv")
        stats, sink, clock = self.run_replay(spans, 1.0, flush_interval_s=0.0, max_batch=1)
        self.assertEqual(stats.spans, len(spans))
        ordered = emission_order(spans)
        t0 = ordered[0].end_us
        for s, sent in zip(sink.spans, sink.sent_at):
            self.assertAlmostEqual(sent - 1000.0, (s.end_us - t0) / 1e6, places=6)
        self.assertEqual(stats.max_lag_s, 0.0)

    def test_speedups(self):
        spans = load("alibaba_small.csv")
        span_s = (emission_order(spans)[-1].end_us - emission_order(spans)[0].end_us) / 1e6
        for speed in (1.0, 2.0, 10.0):
            with self.subTest(speed=speed):
                stats, _, _ = self.run_replay(spans, speed, flush_interval_s=0.0)
                self.assertAlmostEqual(stats.wall_s, span_s / speed, places=6)

    def test_max_speed_never_sleeps(self):
        stats, sink, clock = self.run_replay(load("rcaeval_small.csv"), float("inf"))
        self.assertEqual(clock.slept, 0.0)
        self.assertEqual(len(sink.spans), 6)

    def test_emits_in_end_time_order_one_span_per_call(self):
        spans = load("alibaba_small.csv")
        _, sink, _ = self.run_replay(spans, float("inf"))
        ends = [s.end_us for s in sink.spans]
        self.assertEqual(ends, sorted(ends))
        self.assertEqual(len({s.span_id for s in sink.spans}), len(spans))

    def test_batch_size(self):
        _, sink, _ = self.run_replay(load("alibaba_small.csv"), float("inf"), max_batch=3)
        self.assertEqual([len(b) for b in sink.batches], [3, 3, 1])

    def test_rebase_keeps_trace_shape(self):
        spans = load("rcaeval_small.csv")
        _, sink, _ = self.run_replay(spans, 10.0, rebase_to_now=True)
        orig = {s.span_id: s for s in spans}
        new = {s.span_id: s for s in sink.spans}
        for tid in {s.trace_id for s in spans}:
            ids = [s.span_id for s in spans if s.trace_id == tid]
            root = min(ids, key=lambda i: orig[i].start_us)
            for i in ids:
                self.assertEqual(new[i].start_us - new[root].start_us, orig[i].start_us - orig[root].start_us)
                self.assertEqual(new[i].duration_us, orig[i].duration_us)
        self.assertTrue(all(s.start_us > 1_799_000_000_000_000 for s in sink.spans))

    def test_bad_speed(self):
        with self.assertRaises(ValueError):
            Replayer(MemorySink(), speed=0)


class UploadEqualsReplayTests(unittest.TestCase):
    """E3 acceptance: a converted (uploaded) file and the equivalent replay give
    the same spans, so they will produce identical stitched traces."""

    def test_convert_vs_replay(self):
        for name in ("alibaba_small.csv", "rcaeval_small.csv"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                direct = load(name)
                # upload path: convert to one OTLP JSON file, then normalize it
                path = os.path.join(tmp, "upload.json")
                with open(path, "w") as f:
                    json.dump(otlp.to_otlp(direct), f)
                uploaded = formats.load_path(path).spans
                # replay path: replay into OTLP batch files, then normalize them all
                out = os.path.join(tmp, "replay")
                Replayer(FileSink(out), speed=float("inf"), max_batch=2).run(direct)
                replayed = []
                for fn in sorted(os.listdir(out)):
                    replayed.extend(formats.load_path(os.path.join(out, fn)).spans)
                replayed.sort(key=Span.sort_key)
                self.assertEqual(uploaded, direct)
                self.assertEqual(replayed, direct)


if __name__ == "__main__":
    unittest.main()
