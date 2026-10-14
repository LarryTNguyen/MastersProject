import unittest

from traceingest import otlp
from traceingest.spans import STATUS_ERROR, Span

from .helpers import load


class OtlpTests(unittest.TestCase):
    def test_round_trip_every_source(self):
        for name in ("alibaba_small.csv", "rcaeval_small.csv", "otlp_small.json", "alibaba_real_rows.csv"):
            with self.subTest(name=name):
                spans = load(name)
                back = sorted(otlp.from_otlp(otlp.to_otlp(spans)), key=Span.sort_key)
                self.assertEqual(back, spans)

    def test_alibaba_ids_become_valid_otlp_ids(self):
        doc = otlp.to_otlp(load("alibaba_small.csv"))
        for rs in doc["resourceSpans"]:
            for sp in rs["scopeSpans"][0]["spans"]:
                self.assertEqual(len(sp["traceId"]), 32)
                self.assertEqual(len(sp["spanId"]), 16)
                int(sp["traceId"], 16)

    def test_sdk_style_document(self):
        spans = {s.service: s for s in load("otlp_small.json")}
        cart = spans["cartservice"]
        self.assertEqual(cart.parent_span_id, spans["frontend"].span_id)
        self.assertEqual(cart.status, STATUS_ERROR)          # string enum
        self.assertEqual(cart.kind, "server")                # string enum
        self.assertEqual(cart.duration_us, 70_000)
        self.assertEqual(spans["frontend"].attributes["http.status_code"], 200)

    def test_base64_ids_accepted(self):
        doc = {"resourceSpans": [{"resource": {}, "scopeSpans": [{"spans": [{
            "traceId": "W47/95gDgQPSabYzgT/GDA==", "spanId": "7uGbfsPBsXQ=", "name": "x",
            "startTimeUnixNano": "1000", "endTimeUnixNano": "2000"}]}]}]}
        s = next(otlp.from_otlp(doc))
        self.assertEqual(s.trace_id, "5b8efff798038103d269b633813fc60c")
        self.assertEqual(s.span_id, "eee19b7ec3c1b174")


if __name__ == "__main__":
    unittest.main()
