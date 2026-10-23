"""Score ranking methods on RCAEval cases.

For each case folder: read traces, split at inject_time into normal/anomalous,
run each method, record where the true service landed.

    python -m experiments.rcaeval_eval "data/rcaeval/re2ob_*" --methods slowest_average longest_span critical_path_only
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from typing import Dict, List, Optional

from . import _paths

_paths.setup()
from .baselines import load_method
from .metrics import rank_of, summarize

from traceingest import rcaeval  # ingest/


def split_case(spans: List[dict], inject_time_s: Optional[int]):
    """Group spans into traces and split them at the injection time."""
    by_trace: Dict[str, List[dict]] = {}
    for s in spans:
        by_trace.setdefault(s["trace_id"], []).append(s)
    traces = list(by_trace.values())
    if inject_time_s is None:
        return [], traces
    cut = inject_time_s * 1_000_000
    normal = [t for t in traces if min(s["start_us"] for s in t) < cut]
    anomalous = [t for t in traces if min(s["start_us"] for s in t) >= cut]
    return normal, anomalous


def evaluate_case(case_dir: str, methods: Dict[str, object]) -> List[dict]:
    spans, label = rcaeval.read_case(case_dir)
    normal, anomalous = split_case([s.to_dict() for s in spans], label.inject_time_s)
    rows = []
    for name, fn in methods.items():
        ranking = fn(normal, anomalous) if anomalous else []
        rows.append({"case": label.case, "benchmark": label.benchmark, "fault": label.fault,
                     "truth": label.root_cause_service, "method": name,
                     "rank": rank_of(label.root_cause_service, ranking),
                     "top3": ";".join(ranking[:3]), "normal_traces": len(normal), "anomalous_traces": len(anomalous)})
    return rows


def _one(args):
    """Worker: evaluate one case; returns (rows, error)."""
    case_dir, method_specs = args
    methods = {spec.split(":")[-1] if ":" in spec else spec: load_method(spec) for spec in method_specs}
    try:
        return evaluate_case(case_dir, methods), None
    except Exception as e:  # one bad case must not stop the run
        return [], repr(e)


def evaluate(case_dirs: List[str], method_specs: List[str], log=print, workers: int = 1) -> dict:
    """Score every method on every case. ``workers`` > 1 runs cases in parallel
    (a real Train Ticket case is about 1M spans, so two workers halve the time)."""
    names = [spec.split(":")[-1] if ":" in spec else spec for spec in method_specs]
    rows: List[dict] = []
    failed = []
    jobs = [(d, method_specs) for d in case_dirs]
    if workers > 1 and len(jobs) > 1:
        from multiprocessing import Pool
        with Pool(workers) as pool:
            results = pool.imap(_one, jobs)
            for d, (case_rows, err) in zip(case_dirs, results):
                _collect(d, case_rows, err, rows, failed, log)
    else:
        for d, job in zip(case_dirs, jobs):
            case_rows, err = _one(job)
            _collect(d, case_rows, err, rows, failed, log)
    summary = {}
    for name in names:
        ranks = [r["rank"] for r in rows if r["method"] == name]
        summary[name] = summarize(ranks)
        for key, field in (("by_fault", "fault"), ("by_benchmark", "benchmark")):
            groups: Dict[str, list] = {}
            for r in rows:
                if r["method"] == name:
                    groups.setdefault(r[field], []).append(r["rank"])
            summary[name][key] = {g: summarize(v) for g, v in sorted(groups.items())}
    return {"rows": rows, "summary": summary, "failed": failed}


def _collect(case_dir, case_rows, err, rows, failed, log):
    if err:
        failed.append({"case": os.path.basename(case_dir), "error": err})
        log(f"skipped {case_dir}: {err}")
    else:
        rows.extend(case_rows)
        log(f"{os.path.basename(case_dir)}: " + ", ".join(f"{r['method']} rank {r['rank']}" for r in case_rows))


def expand(patterns: List[str]) -> List[str]:
    out = []
    for p in patterns:
        hits = sorted(x for x in glob.glob(p) if os.path.isdir(x))
        out.extend(hits or ([p] if os.path.isdir(p) else []))
    return out


def write_results(result: dict, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "rcaeval_results.csv"), "w", newline="") as f:
        cols = ["case", "benchmark", "fault", "truth", "method", "rank", "top3", "normal_traces", "anomalous_traces"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(result["rows"])
    with open(os.path.join(out_dir, "rcaeval_summary.json"), "w") as f:
        json.dump({"summary": result["summary"], "failed": result["failed"]}, f, indent=2)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", nargs="+", help="case folders or glob patterns")
    ap.add_argument("--methods", nargs="+", default=["slowest_average", "longest_span", "critical_path_only"])
    ap.add_argument("--out", default="experiments/results/latest")
    ap.add_argument("--workers", type=int, default=1, help="cases evaluated in parallel")
    a = ap.parse_args(argv)
    dirs = expand(a.cases)
    if not dirs:
        sys.exit("no case folders matched")
    result = evaluate(dirs, a.methods, workers=a.workers)
    write_results(result, a.out)
    for name, s in result["summary"].items():
        print(f"{name:22s} cases={s['cases']:4d} acc@1={s['acc@1']:.3f} acc@3={s['acc@3']:.3f} mrr={s['mrr']:.3f}")
    if result["failed"]:
        print(f"{len(result['failed'])} case(s) skipped, see rcaeval_summary.json")


if __name__ == "__main__":
    main()
