"""Cut a replayable sample out of one Alibaba v2021 MSCallGraph file.

One MSCallGraph_<n>.csv covers 5 minutes of the whole sampled cluster (about 6M
rows), far more than one application. This keeps the traces that enter through
one entry service (the callee of the root call, rpcid "0"), optionally only a
fraction of them, so the result looks like one application's traffic.

    python tools/alibaba_sample.py MSCallGraph_0.csv --list              # entry services by traffic
    python tools/alibaba_sample.py MSCallGraph_0.csv -o sample.csv --entry top1 --fraction 0.05

--entry takes "top1", "top2", ... (by number of traces) or a service-id prefix.
--fraction keeps that share of the entry's traces, picked by a hash of the trace
id, so the same command always gives the same sample. Every row of a kept trace is
kept, so caller/callee duplicates are still there for the reader to merge.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from typing import Dict, Set


def _keep(trace_id: str, fraction: float) -> bool:
    if fraction >= 1:
        return True
    h = int(hashlib.blake2b(trace_id.encode(), digest_size=8).hexdigest(), 16)
    return h / 2 ** 64 < fraction


def entry_counts(path: str) -> Dict[str, Set[str]]:
    """entry service -> trace ids whose root call goes to it"""
    entries: Dict[str, Set[str]] = {}
    with open(path, newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            if row["rpcid"] == "0":
                entries.setdefault(row["dm"], set()).add(row["traceid"])
    return entries


def pick_entry(entries: Dict[str, Set[str]], spec: str) -> str:
    ranked = sorted(entries, key=lambda s: len(entries[s]), reverse=True)
    if spec.startswith("top") and spec[3:].isdigit():
        n = int(spec[3:])
        if not 1 <= n <= len(ranked):
            raise SystemExit(f"--entry {spec}: there are only {len(ranked)} entry services")
        return ranked[n - 1]
    hits = [s for s in ranked if s.startswith(spec)]
    if len(hits) != 1:
        raise SystemExit(f"--entry {spec}: matches {len(hits)} entry services")
    return hits[0]


def write_sample(path: str, out: str, keep: Set[str]) -> dict:
    rows = 0
    services: Counter = Counter()
    t_min, t_max = None, None
    with open(path, newline="") as f, open(out, "w", newline="") as g:
        r = csv.reader(f)
        header = next(r)
        w = csv.writer(g)
        w.writerow(header)
        i_trace, i_time, i_dm = header.index("traceid"), header.index("timestamp"), header.index("dm")
        for row in r:
            if row[i_trace] in keep:
                w.writerow(row)
                rows += 1
                services[row[i_dm]] += 1
                t = int(row[i_time])
                t_min = t if t_min is None else min(t_min, t)
                t_max = t if t_max is None else max(t_max, t)
    span_s = ((t_max - t_min) / 1000) if rows else 0
    return {"rows": rows, "traces": len(keep), "services": len(services),
            "seconds": round(span_s, 1), "rows_per_second": round(rows / span_s, 1) if span_s else None}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="an MSCallGraph_<n>.csv from the v2021 release")
    p.add_argument("-o", "--output", help="where to write the sample CSV")
    p.add_argument("--entry", default="top1", help='"top1", "top2", ... or a service-id prefix (default top1)')
    p.add_argument("--fraction", type=float, default=1.0, help="share of the entry's traces to keep (default 1)")
    p.add_argument("--list", action="store_true", help="only list the entry services and their trace counts")
    a = p.parse_args(argv)

    entries = entry_counts(a.input)
    if a.list or not a.output:
        ranked = sorted(entries.items(), key=lambda kv: len(kv[1]), reverse=True)
        for i, (svc, tids) in enumerate(ranked[:15], start=1):
            print(f"top{i:<3} {len(tids):>8} traces  {svc}")
        return
    svc = pick_entry(entries, a.entry)
    keep = {t for t in entries[svc] if _keep(t, a.fraction)}
    stats = write_sample(a.input, a.output, keep)
    stats.update({"entry": svc, "fraction": a.fraction, "source": a.input.replace("\\", "/").split("/")[-1]})
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    sys.exit(main())
