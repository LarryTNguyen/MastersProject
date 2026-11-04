"""Prepare and load the four episodes (A-D) for the usability study.

    python -m experiments.usability.episodes prepare data/rcaeval            # once
    python -m experiments.usability.episodes load B --tool both              # before each episode

prepare  cuts each chosen RCAEval case down to 5 minutes before and 5 minutes after
         the fault was injected, picks the endpoint whose requests got slowest (not
         served by the culprit itself, so the question is not answered by the
         endpoint name), and writes
             episodes/<X>/spans.jsonl      the spans
             episodes/task_sheet.md        what to read to the participant
             episodes/answer_key.json      the culprit per episode (keep it away from them)
load     moves an episode in time so it ends half a minute ago, sends it to Jaeger over
         OTLP/HTTP and/or writes it into the dashboard's data folder, and prints the
         sentence to read aloud, with the clock time the slowdown started.

Jaeger keeps everything in memory, so restart it between episodes (load refuses to
add to a Jaeger that already has services, unless --force).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from .. import _paths

_paths.setup()

from traceingest import spans as spanio            # noqa: E402
from traceingest.canonical import NormalizationContext, write_jsonl as write_canonical  # noqa: E402
from traceingest.rcaeval import read_case          # noqa: E402
from traceingest.sinks import HttpSink             # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(HERE, "episodes")

# Two Online Boutique and two Train Ticket delay faults, all with a slowdown you can
# see at the endpoint. Each tool gets one of each benchmark per participant (see schedule.py).
DEFAULT_CASES = {
    "A": "re2ob_checkoutservice_delay_1",
    "B": "re2ob_recommendationservice_delay_1",
    "C": "re2tt_ts-route-service_delay_1",
    "D": "re2tt_ts-travel-service_delay_1",
}
WINDOW_S = 300                 # kept on each side of the injection time
MIN_TRACES = 20                # an endpoint needs this many traces before and after
MIN_RATIO = 1.5                # p95 after / before for a slowdown a person can see
SKIP_OPERATIONS = ("opentelemetry.proto",)   # the exporter's own spans, not an endpoint


def _root(spans: List[spanio.Span]) -> spanio.Span:
    """Same rule as the dashboard: a real root, else the earliest orphan."""
    ids = {s.span_id for s in spans}
    roots = [s for s in spans if not s.parent_span_id]
    orphans = [s for s in spans if s.parent_span_id and s.parent_span_id not in ids]
    return min(roots or orphans or spans, key=lambda s: s.start_us)


def _p95(xs: List[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(0.95 * len(xs)))]


def by_trace(spans) -> Dict[str, List[spanio.Span]]:
    out: Dict[str, List[spanio.Span]] = defaultdict(list)
    for s in spans:
        out[s.trace_id].append(s)
    return out


def cut_window(spans, inject_us: int, window_s: int = WINDOW_S) -> List[spanio.Span]:
    """Whole traces that start within window_s of the injection."""
    lo, hi = inject_us - window_s * 1_000_000, inject_us + window_s * 1_000_000
    kept = []
    for lst in by_trace(spans).values():
        start = min(s.start_us for s in lst)
        if lo <= start < hi:
            kept.extend(lst)
    return kept


def endpoint_effects(spans, inject_us: int) -> List[dict]:
    """Per root (service, operation): p95 trace duration before and after the injection."""
    acc: Dict[tuple, dict] = defaultdict(lambda: {"before": [], "after": []})
    for lst in by_trace(spans).values():
        r = _root(lst)
        if r.operation.startswith(SKIP_OPERATIONS):
            continue
        dur = (max(s.end_us for s in lst) - min(s.start_us for s in lst)) / 1000
        acc[(r.service, r.operation)]["after" if r.start_us >= inject_us else "before"].append(dur)
    out = []
    for (svc, op), d in acc.items():
        if len(d["before"]) >= MIN_TRACES and len(d["after"]) >= MIN_TRACES:
            b, a = _p95(d["before"]), _p95(d["after"])
            out.append({"service": svc, "operation": op, "traces": len(d["before"]) + len(d["after"]),
                        "p95_before_ms": round(b, 1), "p95_after_ms": round(a, 1),
                        "ratio": round(a / b, 1) if b else None})
    return sorted(out, key=lambda e: e["ratio"] or 0, reverse=True)


def pick_endpoint(effects: List[dict], culprit: str) -> dict:
    """The busiest endpoint that clearly got slower and is not served by the culprit
    (so the endpoint name does not give the answer away). Health checks under a
    millisecond are skipped. Falls back to the biggest slowdown."""
    if not effects:
        raise ValueError("no endpoint has enough traces on both sides of the injection")
    clear = [e for e in effects if e["service"] != culprit and (e["ratio"] or 0) >= MIN_RATIO
             and e["p95_before_ms"] >= 1]
    if clear:
        return max(clear, key=lambda e: e["traces"])
    return effects[0]


def prepare_episode(case_dir: str, out_dir: str) -> dict:
    spans, label = read_case(case_dir)
    if label.inject_time_s is None:
        raise ValueError(f"{case_dir} has no inject_time.txt")
    inject_us = label.inject_time_s * 1_000_000
    kept = cut_window(spans, inject_us)
    effects = endpoint_effects(kept, inject_us)
    ep = pick_endpoint(effects, label.root_cause_service)
    os.makedirs(out_dir, exist_ok=True)
    kept.sort(key=spanio.Span.sort_key)
    with open(os.path.join(out_dir, "spans.jsonl"), "w", encoding="utf-8") as f:
        spanio.write_jsonl(kept, f)
    start = min(s.start_us for s in kept)
    end = max(s.end_us for s in kept)
    return {"case": label.case, "benchmark": label.benchmark, "fault": label.fault,
            "culprit": label.root_cause_service, "endpoint_service": ep["service"],
            "endpoint": ep["operation"], "endpoint_p95_before_ms": ep["p95_before_ms"],
            "endpoint_p95_after_ms": ep["p95_after_ms"], "spans": len(kept),
            "traces": len({s.trace_id for s in kept}), "inject_us": inject_us,
            "start_us": start, "end_us": end,
            "slow_after_s": round((inject_us - start) / 1e6), "data_s": round((end - start) / 1e6)}


def task_sentence(ep: dict, clock: str = "HH:MM") -> str:
    return (f"Requests to \"{ep['endpoint']}\" on {ep['endpoint_service']} became slow around {clock}. "
            "Which service is most likely responsible?")


def write_sheets(episodes: Dict[str, dict], out_dir: str) -> None:
    with open(os.path.join(out_dir, "answer_key.json"), "w") as f:
        json.dump(episodes, f, indent=2, sort_keys=True)
    lines = ["# Usability study: task sheet", "",
             "Read the sentence for the episode aloud. `load` prints it again with the real clock",
             "time in place of HH:MM; use that one. Do not show the participant the answer key.", ""]
    for x, ep in sorted(episodes.items()):
        lines += [f"## Episode {x}", "",
                  f"- Endpoint: `{ep['endpoint']}` on `{ep['endpoint_service']}`",
                  f"- Data: {round(ep['data_s'] / 60)} minutes, {ep['traces']:,} traces; the slowdown starts "
                  f"about {round(ep['slow_after_s'] / 60)} minutes in.",
                  f"- Read aloud: {task_sentence(ep)}", ""]
    with open(os.path.join(out_dir, "task_sheet.md"), "w") as f:
        f.write("\n".join(lines))


def prepare(data_dir: str, out_dir: str, cases: Dict[str, str]) -> Dict[str, dict]:
    episodes = {}
    for x, case in sorted(cases.items()):
        episodes[x] = prepare_episode(os.path.join(data_dir, case), os.path.join(out_dir, x))
        e = episodes[x]
        print(f"{x}: {e['case']}: {e['traces']:,} traces, endpoint {e['endpoint_service']} {e['endpoint']!r} "
              f"p95 {e['endpoint_p95_before_ms']} -> {e['endpoint_p95_after_ms']} ms")
    write_sheets(episodes, out_dir)
    print(f"wrote {out_dir}/task_sheet.md and answer_key.json")
    return episodes


# ---------------------------------------------------------------- load

def shift_to_now(spans: List[spanio.Span], end_us: int, now_us: int, gap_s: float = 30) -> (List[spanio.Span], int):
    """Move every span by one constant so the episode ends gap_s before now."""
    delta = now_us - int(gap_s * 1_000_000) - end_us
    return [replace(s, start_us=s.start_us + delta) for s in spans], delta


def jaeger_services(query_url: str, opener=urllib.request.urlopen) -> List[str]:
    with opener(query_url.rstrip("/") + "/api/services", timeout=5) as r:
        return [s for s in (json.load(r).get("data") or []) if s != "jaeger-all-in-one"]


def send_otlp(spans: List[spanio.Span], url: str, batch: int = 1000, sink=None) -> int:
    sink = sink or HttpSink(url)
    for i in range(0, len(spans), batch):
        sink.send(spans[i:i + batch])
    return len(spans)


def write_dashboard(spans: List[spanio.Span], data_dir: str, episode: str) -> str:
    """Replace whatever episode the dashboard folder held with this one."""
    os.makedirs(data_dir, exist_ok=True)
    for old in glob.glob(os.path.join(data_dir, "episode_*.jsonl")):
        os.remove(old)
    path = os.path.join(data_dir, f"episode_{episode}.jsonl")
    ctx = NormalizationContext.for_source(f"usability:episode_{episode}")
    with open(path, "w", encoding="utf-8") as f:
        write_canonical(spans, f, ctx)
    return path


def load(episode: str, ep_dir: str, tool: str = "both", otlp_url: str = "http://localhost:4318",
         jaeger_query: str = "http://localhost:16686", dashboard_dir: Optional[str] = None,
         force: bool = False, now_us: Optional[int] = None, sink=None, opener=urllib.request.urlopen) -> dict:
    with open(os.path.join(ep_dir, "answer_key.json")) as f:
        ep = json.load(f)[episode]
    with open(os.path.join(ep_dir, episode, "spans.jsonl"), encoding="utf-8") as f:
        spans = list(spanio.read_jsonl(f))
    now_us = now_us if now_us is not None else int(time.time() * 1_000_000)
    moved, delta = shift_to_now(spans, ep["end_us"], now_us)
    out = {"episode": episode, "spans": len(moved)}
    if tool in ("jaeger", "both"):
        if not force and sink is None:
            have = jaeger_services(jaeger_query, opener)
            if have:
                raise SystemExit(f"Jaeger already has data ({', '.join(have[:3])}...). Restart it first, or pass --force.")
        out["jaeger_spans"] = send_otlp(moved, otlp_url, sink=sink)
    if tool in ("dashboard", "both"):
        out["dashboard_file"] = write_dashboard(moved, dashboard_dir or os.path.join(ep_dir, "live_dashboard"), episode)
    slow_at = datetime.fromtimestamp((ep["inject_us"] + delta) / 1e6)
    out["slow_from"] = slow_at.strftime("%H:%M")
    out["read_aloud"] = task_sentence(ep, out["slow_from"])
    return out


def main(argv: Optional[Sequence[str]] = None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="cut the RCAEval cases into episodes A-D")
    p.add_argument("data_dir", help="folder with the RCAEval case folders")
    p.add_argument("--out", default=DEFAULT_DIR)
    p.add_argument("--case", action="append", default=[], metavar="X=CASE",
                   help="override an episode, e.g. --case C=re2tt_ts-train-service_delay_1")
    l = sub.add_parser("load", help="load one episode into Jaeger and/or the dashboard")
    l.add_argument("episode", choices=list("ABCD"))
    l.add_argument("--dir", default=DEFAULT_DIR)
    l.add_argument("--tool", choices=("jaeger", "dashboard", "both"), default="both")
    l.add_argument("--otlp", default="http://localhost:4318", help="Jaeger's OTLP/HTTP address")
    l.add_argument("--jaeger", default="http://localhost:16686", help="Jaeger UI address")
    l.add_argument("--dashboard-data", help="the dashboard's DATA_DIR (default <dir>/live_dashboard)")
    l.add_argument("--force", action="store_true", help="load even if Jaeger already has data")
    a = ap.parse_args(argv)
    if a.cmd == "prepare":
        cases = dict(DEFAULT_CASES)
        for item in a.case:
            x, _, case = item.partition("=")
            cases[x.upper()] = case
        prepare(a.data_dir, a.out, cases)
        return
    r = load(a.episode, a.dir, a.tool, a.otlp, a.jaeger, a.dashboard_data, a.force)
    print(json.dumps({k: v for k, v in r.items() if k != "read_aloud"}, indent=2))
    print("\nRead aloud:\n  " + r["read_aloud"])


if __name__ == "__main__":
    sys.exit(main())
