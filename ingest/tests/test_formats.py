import io
import json
import os
import unittest

from traceingest import formats, otlp
from traceingest.spans import read_jsonl, write_jsonl

from .helpers import FX, fx, load


class FormatTests(unittest.TestCase):
    def test_detect(self):
        cases = {"alibaba_small.csv": "alibaba", "alibaba_real_rows.csv": "alibaba",
                 "rcaeval_small.csv": "rcaeval", "otlp_small.json": "otlp"}
        for name, want in cases.items():
            with open(fx(name)) as f:
                self.assertEqual(formats.detect_format(f.read(8192), name), want, name)

    def test_detect_spans_jsonl(self):
        buf = io.StringIO()
        write_jsonl(load("rcaeval_small.csv"), buf)
        self.assertEqual(formats.detect_format(buf.getvalue()), "jsonl")

    def test_otlp_one_document_per_line(self):
        doc = json.dumps(otlp.to_otlp(load("otlp_small.json")))
        res = formats.load_text(io.StringIO(doc + "\n" + doc + "\n"))
        self.assertEqual(len(res.spans), 4)

    def test_unknown_format(self):
        with self.assertRaises(ValueError):
            formats.detect_format("hello,world\n1,2\n")

    def test_jsonl_round_trip(self):
        spans = load("alibaba_small.csv")
        buf = io.StringIO()
        write_jsonl(spans, buf)
        buf.seek(0)
        self.assertEqual(list(read_jsonl(buf)), spans)


class GoldenTests(unittest.TestCase):
    """Normalized output must not change by accident; regenerate with
    ``python tests/make_expected.py`` after an intended change."""

    def test_expected_outputs(self):
        for name in ("alibaba_small.csv", "rcaeval_small.csv", "otlp_small.json"):
            with self.subTest(name=name):
                buf = io.StringIO()
                write_jsonl(load(name), buf)
                with open(os.path.join(FX, "expected", name.rsplit(".", 1)[0] + ".jsonl")) as f:
                    self.assertEqual(buf.getvalue(), f.read())


if __name__ == "__main__":
    unittest.main()
