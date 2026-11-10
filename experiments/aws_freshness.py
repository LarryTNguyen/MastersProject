"""Freshness (N1) on the deployed pipeline.

Replay a dataset into the collector's Function URL at 1x with timestamps moved to
now, then measure from either end:

    python -m experiments.aws_freshness files <folder of normalized *.jsonl> [--since 2026-11-10T18:00Z]
    python -m experiments.aws_freshness dashboard https://<dashboard function URL>

files      works before Aurora is up: sync the normalized bucket down first
           (aws s3 sync s3://<bucket>/normalized/ <folder>) and it reads the
           ingest stamps the adapter Lambda put on every span.
dashboard  once reconstruction and the dashboard Lambda are up: reads the same
           numbers from the dashboard's /api/pipeline.

Staleness = when the adapter finished normalizing a span minus when the span ended
(both wall clock, so the replay must use --rebase-to-now); lag = normalized minus
received at the collector. Results go to <out>/freshness.json and summary.md, in the
same shape as experiments.local_pipeline, so the runner and figures pick them up.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from typing import Iterator, Optional

from . import _paths

_paths.setup()
from app import analysis                     # noqa: E402  dashboard/app
from app.canonical import span_from_any      # noqa: E402

ROOT = _paths.ROOT
TARGET_P95_MS = 60000


def _parse_since(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000)


def read_spans(folder: str) -> Iterator[dict]:
    for path in sorted(glob.glob(os.path.join(folder, "**", "*.jsonl"), recursive=True)):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield span_from_any(json.loads(line))


def from_files(folder: str, since_us: Optional[int] = None, now_us: Optional[int] = None) -> dict:
    """Pipeline numbers straight from normalized files. since_us drops spans normalized
    before the replay started (older runs left in the bucket)."""
    spans = [s for s in read_spans(folder)
             if since_us is None or ((s.get("attributes") or {}).get(analysis.NORMALIZED) or 0) >= since_us]
    if not spans:
        raise SystemExit(f"no spans in {folder}" + (" after --since" if since_us else ""))
    by_trace = {}
    for s in spans:
        by_trace.setdefault(s["trace_id"], []).append(s)
    from app.stitch import trace_summary
    traces = [trace_summary(t, lst) for t, lst in by_trace.items()]
    now_us = now_us or int(time.time() * 1_000_000)
    stats = analysis.pipeline_stats(spans, traces, now_us, window_s=86400)
    stats["source"] = f"normalized files in {folder}"
    return stats


def from_dashboard(url: str, window_s: int = 86400, opener=urllib.request.urlopen) -> dict:
    with opener(url.rstrip("/") + f"/api/pipeline?window_s={window_s}", timeout=30) as r:
        stats = json.load(r)
    stats["source"] = f"{url.rstrip('/')}/api/pipeline"
    return stats


def summary_md(stats: dict, command: str) -> str:
    s, lag = stats.get("staleness_ms") or {}, stats.get("lag_ms") or {}
    fmt = lambda v: "–" if v is None else f"{v / 1000:.2f} s"
    ok = s.get("p95") is not None and s["p95"] < TARGET_P95_MS
    return (f"# Freshness on AWS\n\n"
            f"- source: {stats['source']}\n- command: `{command}`\n"
            f"- spans with ingest stamps: {stats.get('spans_with_stamps')} of {stats.get('spans')}; "
            f"traces {stats.get('traces')}, incomplete {100 * (stats.get('incomplete_rate') or 0):.1f}%\n\n"
            f"| | p50 | p95 | p99 |\n|---|---|---|---|\n"
            f"| staleness (span end → normalized) | {fmt(s.get('p50'))} | {fmt(s.get('p95'))} | {fmt(s.get('p99'))} |\n"
            f"| lag (collector → normalized) | {fmt(lag.get('p50'))} | {fmt(lag.get('p95'))} | – |\n\n"
            f"N1 target: p95 staleness under 60 s → **{'met' if ok else 'not met'}**.\n")


def write(stats: dict, out: str, command: str) -> None:
    os.makedirs(out, exist_ok=True)
    stats = {k: v for k, v in stats.items() if k != "series"}
    stats.update({"target_p95_ms": TARGET_P95_MS, "where": "AWS", "command": command})
    with open(os.path.join(out, "freshness.json"), "w") as f:
        json.dump(stats, f, indent=2)
    with open(os.path.join(out, "summary.md"), "w") as f:
        f.write(summary_md(stats, command))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("files", help="from normalized JSONL synced down from S3")
    f.add_argument("folder")
    f.add_argument("--since", help="only spans normalized after this time (ISO, e.g. 2026-11-10T18:00Z)")
    d = sub.add_parser("dashboard", help="from the deployed dashboard's /api/pipeline")
    d.add_argument("url")
    d.add_argument("--window-s", type=int, default=86400)
    for p in (f, d):
        p.add_argument("--out", default=os.path.join(ROOT, "experiments", "results",
                                                     time.strftime("aws-freshness-%Y%m%d-%H%M%S")))
    a = ap.parse_args(argv)
    command = "python -m experiments.aws_freshness " + " ".join(sys.argv[1:] if argv is None else argv)
    stats = from_files(a.folder, _parse_since(a.since)) if a.cmd == "files" else from_dashboard(a.url, a.window_s)
    write(stats, a.out, command)
    print(summary_md(stats, command))
    print(f"wrote {a.out}/freshness.json")


if __name__ == "__main__":
    main()
