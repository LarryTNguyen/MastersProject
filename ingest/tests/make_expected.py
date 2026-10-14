"""Regenerate the golden normalized outputs in tests/fixtures/expected/.

Only run this after a change you meant to make, then review the diff.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from traceingest import formats  # noqa: E402
from traceingest.spans import write_jsonl  # noqa: E402

FX = os.path.join(os.path.dirname(__file__), "fixtures")
for name in ("alibaba_small.csv", "rcaeval_small.csv", "otlp_small.json"):
    out = os.path.join(FX, "expected", name.rsplit(".", 1)[0] + ".jsonl")
    with open(out, "w") as f:
        n = write_jsonl(formats.load_path(os.path.join(FX, name)).spans, f)
    print(f"{out}: {n} spans")
