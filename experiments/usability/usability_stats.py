"""Summarize timing_log.csv from the usability study.

    python -m experiments.usability.usability_stats timing_log.csv [--out summary.json]
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from typing import Dict, List


def percentile(xs: List[float], p: float) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    k = max(0, min(len(xs) - 1, round(p * (len(xs) - 1))))
    return xs[k]


def summarize(rows: List[dict]) -> dict:
    tools: Dict[str, dict] = {}
    for tool in sorted({r["tool"] for r in rows}):
        rs = [r for r in rows if r["tool"] == tool]
        secs = [float(r["seconds"]) for r in rs]
        tools[tool] = {"n": len(rs), "participants": len({r["participant"] for r in rs}),
                       "median_s": statistics.median(secs), "mean_s": statistics.mean(secs),
                       "p25_s": percentile(secs, 0.25), "p75_s": percentile(secs, 0.75),
                       "accuracy": sum(int(r["correct"]) for r in rs) / len(rs),
                       "gave_up": sum(1 for r in rs if r["answer"] == "gave_up")}
    # paired: per person, dashboard time minus jaeger time
    paired = []
    for p in sorted({r["participant"] for r in rows}):
        j = [float(r["seconds"]) for r in rows if r["participant"] == p and r["tool"] == "jaeger"]
        d = [float(r["seconds"]) for r in rows if r["participant"] == p and r["tool"] == "dashboard"]
        if j and d:
            paired.append({"participant": p, "jaeger_s": statistics.mean(j), "dashboard_s": statistics.mean(d),
                           "diff_s": statistics.mean(d) - statistics.mean(j)})
    diffs = [x["diff_s"] for x in paired]
    return {"tools": tools, "paired": paired,
            "paired_mean_diff_s": statistics.mean(diffs) if diffs else None,
            "faster_with_dashboard": sum(1 for x in diffs if x < 0), "paired_n": len(diffs)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    with open(a.log, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("seconds")]
    if not rows:
        print("no rows yet")
        return
    s = summarize(rows)
    for tool, t in s["tools"].items():
        print(f"{tool:10s} n={t['n']:3d} median={t['median_s']:.0f}s (p25 {t['p25_s']:.0f}, p75 {t['p75_s']:.0f}) accuracy={t['accuracy']:.0%}")
    if s["paired_n"]:
        print(f"paired: {s['faster_with_dashboard']}/{s['paired_n']} participants faster with the dashboard, "
              f"mean difference {s['paired_mean_diff_s']:+.0f}s")
    if a.out:
        with open(a.out, "w") as f:
            json.dump(s, f, indent=2)


if __name__ == "__main__":
    main()
