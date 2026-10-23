"""Download the RCAEval cases that have traces (RE2 and RE3, Online Boutique and
Train Ticket) from Hugging Face into data/rcaeval. RE1 and the Sock Shop cases
have no traces, so they are skipped.

    pip install huggingface_hub
    python -m experiments.get_rcaeval                 # all 240 trace cases, about 2.6 GB
    python -m experiments.get_rcaeval --suites re2ob  # one suite
    python -m experiments.get_rcaeval --with-metrics  # also metrics.parquet / logs.parquet

The layout matches experiments/config.json: data/rcaeval/<case>/traces.parquet + inject_time.txt.
"""
from __future__ import annotations

import argparse
import time

REPO = "phamquiluan/RCAEval"
SUITES = ("re2ob", "re2tt", "re3ob", "re3tt")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/rcaeval")
    ap.add_argument("--suites", nargs="+", default=list(SUITES), choices=SUITES)
    ap.add_argument("--with-metrics", action="store_true", help="also download metrics and logs")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args(argv)
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        raise SystemExit("pip install huggingface_hub")
    files = ["traces.parquet", "inject_time.txt"] + (["metrics.parquet", "logs.parquet"] if a.with_metrics else [])
    patterns = ["cases.parquet"] + [f"{suite}*/{name}" for suite in a.suites for name in files]
    t = time.time()
    path = snapshot_download(repo_id=REPO, repo_type="dataset", local_dir=a.out,
                             allow_patterns=patterns, max_workers=a.workers)
    print(f"done: {path} ({time.time() - t:.0f} s)")


if __name__ == "__main__":
    main()
