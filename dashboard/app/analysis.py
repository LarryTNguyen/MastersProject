"""Stuff the dashboard computes from raw spans.

self_times        duration minus the time covered by children
critical_path     the chain of spans that decided when the trace ended
compare           slow vs normal self time per service, for one endpoint
flame             many traces merged into one tree
pipeline_stats    freshness and health from the ingest stamps

The diagnosis step has its own fuller versions of the first two. These are
here so the page can show evidence without waiting for it.
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Tuple

from .stitch import percentile

RECEIVED = "sta.received_us"
NORMALIZED = "sta.normalized_us"


def _children(spans: List[dict]) -> Dict[Optional[str], List[dict]]:
    ids = {s["span_id"] for s in spans}
    kids: Dict[Optional[str], List[dict]] = {}
    for s in spans:
        parent = s.get("parent_span_id")
        kids.setdefault(parent if parent in ids else None, []).append(s)
    return kids


def _end(s: dict) -> int:
    return s["start_us"] + (s.get("duration_us") or 0)


def self_times(spans: List[dict]) -> Dict[str, Optional[int]]:
    """Self time per span id, in microseconds. None when the span has no duration."""
    kids = _children(spans)
    out: Dict[str, Optional[int]] = {}
    for s in spans:
        if s.get("duration_us") is None:
            out[s["span_id"]] = None
            continue
        start, end = s["start_us"], _end(s)
        iv = sorted((max(k["start_us"], start), min(_end(k), end))
                    for k in kids.get(s["span_id"], []) if k.get("duration_us") is not None)
        covered, cur = 0, None
        for a, b in iv:
            if b <= a:
                continue
            if cur is None or a > cur[1]:
                if cur:
                    covered += cur[1] - cur[0]
                cur = [a, b]
            else:
                cur[1] = max(cur[1], b)
        if cur:
            covered += cur[1] - cur[0]
        out[s["span_id"]] = max(0, s["duration_us"] - covered)
    return out


def critical_path(spans: List[dict]) -> List[dict]:
    """Spans on the critical path, root first.

    Walk back from a span's end: the child that finished last is on the path,
    then from that child's start look for the one that finished before it, etc.
    So sequential children all count, overlapping ones only the last.
    """
    if not spans:
        return []
    kids = _children(spans)
    roots = kids.get(None, [])
    root = min(roots, key=lambda s: s["start_us"]) if roots else min(spans, key=lambda s: s["start_us"])
    path: List[dict] = []

    def walk(node):
        path.append(node)
        t = _end(node)
        remaining = sorted(kids.get(node["span_id"], []), key=_end, reverse=True)
        for c in remaining:
            if c.get("duration_us") is None or _end(c) > t or c["start_us"] < node["start_us"]:
                continue
            walk(c)
            t = c["start_us"]
    walk(root)
    return path


def critical_path_share(spans: List[dict]) -> Dict[str, int]:
    """Time on the critical path attributed per service: each on-path span's
    duration minus its on-path children. The shares add up to the root's duration."""
    path = critical_path(spans)
    on_path = {s["span_id"] for s in path}
    kids = _children(spans)
    out: Dict[str, int] = {}
    for s in path:
        own = s.get("duration_us") or 0
        own -= sum((k.get("duration_us") or 0) for k in kids.get(s["span_id"], []) if k["span_id"] in on_path)
        out[s["service"]] = out.get(s["service"], 0) + max(0, own)
    return out


def split_slow(traces: List[Tuple[dict, List[dict]]], threshold_ms: Optional[float] = None,
               slow_fraction: float = 0.10):
    """Split (summary, spans) pairs into slow and normal: over the threshold, or
    the slowest 10% if no threshold. Under 10 traces there's no real tail, so
    everything counts as normal."""
    if not traces:
        return [], []
    if threshold_ms is None:
        if len(traces) < 10:
            return [], list(traces)
        durations = sorted(t["duration_us"] for t, _ in traces)
        cut = durations[int(math.ceil(len(durations) * (1 - slow_fraction)))]
    else:
        cut = threshold_ms * 1000
    slow = [(t, s) for t, s in traces if t["duration_us"] >= cut]
    normal = [(t, s) for t, s in traces if t["duration_us"] < cut]
    return slow, normal


def compare(traces: List[Tuple[dict, List[dict]]], threshold_ms: Optional[float] = None) -> dict:
    """Slow-versus-normal self time per service, for the evidence panel."""
    slow, normal = split_slow(traces, threshold_ms)

    def per_service(group):
        acc: Dict[str, List[float]] = {}
        cp: Dict[str, List[float]] = {}
        for _, spans in group:
            st = self_times(spans)
            tot: Dict[str, float] = {}
            for s in spans:
                v = st.get(s["span_id"])
                if v is not None:
                    tot[s["service"]] = tot.get(s["service"], 0) + v / 1000
            for svc, v in tot.items():
                acc.setdefault(svc, []).append(v)
            for svc, v in critical_path_share(spans).items():
                cp.setdefault(svc, []).append(v / 1000)
        return acc, cp

    s_acc, s_cp = per_service(slow)
    n_acc, n_cp = per_service(normal)
    rows = []
    for svc in sorted(set(s_acc) | set(n_acc)):
        s_med = percentile(s_acc.get(svc, []), 0.5)
        n_med = percentile(n_acc.get(svc, []), 0.5)
        rows.append({
            "service": svc,
            "slow_self_ms_p50": s_med, "normal_self_ms_p50": n_med,
            "slow_self_ms_p95": percentile(s_acc.get(svc, []), 0.95),
            "normal_self_ms_p95": percentile(n_acc.get(svc, []), 0.95),
            "slow_critical_ms_p50": percentile(s_cp.get(svc, []), 0.5),
            "normal_critical_ms_p50": percentile(n_cp.get(svc, []), 0.5),
            "added_ms": (s_med or 0) - (n_med or 0),
            "ratio": ((s_med or 0) / n_med) if n_med else None,
            "slow_traces": len(s_acc.get(svc, [])), "normal_traces": len(n_acc.get(svc, [])),
        })
    rows.sort(key=lambda r: r["added_ms"], reverse=True)
    return {"slow_traces": len(slow), "normal_traces": len(normal),
            "threshold_ms": (min(t["duration_us"] for t, _ in slow) / 1000) if slow else None,
            "services": rows}


def flame(traces: Iterable[List[dict]], max_depth: int = 12) -> dict:
    """Merge trace trees into one. value = self time (ms) summed over traces,
    total = value + children, which is the width in the picture."""
    root = {"name": "all requests", "value": 0.0, "count": 0, "children": {}}
    for spans in traces:
        st = self_times(spans)
        kids = _children(spans)

        def walk(span, node, depth):
            key = f"{span['service']} {span['operation']}"
            child = node["children"].get(key)
            if child is None:
                child = node["children"][key] = {"name": key, "service": span["service"], "value": 0.0, "count": 0, "children": {}}
            child["value"] += (st.get(span["span_id"]) or 0) / 1000
            child["count"] += 1
            if depth < max_depth:
                for k in kids.get(span["span_id"], []):
                    walk(k, child, depth + 1)

        for r in kids.get(None, []):
            walk(r, root, 1)
        root["count"] += 1

    def finish(node):
        node["children"] = sorted((finish(c) for c in node["children"].values()), key=lambda c: -c["total"])
        node["total"] = node["value"] + sum(c["total"] for c in node["children"])
        node["value"] = round(node["value"], 3)
        node["total"] = round(node["total"], 3)
        return node
    return finish(root)


def pipeline_stats(spans: Iterable[dict], traces: Iterable[dict], now_us: int, window_s: int = 900,
                   bucket_s: int = 60) -> dict:
    """Health panel numbers from the ingest stamps on each span.

    staleness = (normalized or received time) - span end
    lag       = normalized - received, when both are there
    """
    stale: List[float] = []
    lag: List[float] = []
    buckets: Dict[int, int] = {}
    stamped = 0
    total = 0
    last_seen = 0
    for s in spans:
        total += 1
        a = s.get("attributes") or {}
        n = a.get(NORMALIZED)
        r = a.get(RECEIVED)
        visible = n or r
        if not visible:
            continue
        stamped += 1
        last_seen = max(last_seen, visible)
        stale.append((visible - _end(s)) / 1000)
        if n and r:
            lag.append((n - r) / 1000)
        if visible >= now_us - window_s * 1_000_000:
            b = (visible // 1_000_000) // bucket_s * bucket_s
            buckets[b] = buckets.get(b, 0) + 1
    traces = list(traces)
    incomplete = sum(1 for t in traces if not t.get("complete"))
    series = [{"t_s": b, "spans": c} for b, c in sorted(buckets.items())]
    recent = sum(c for c in buckets.values())
    return {
        "spans": total, "traces": len(traces), "spans_with_stamps": stamped,
        "incomplete_traces": incomplete,
        "incomplete_rate": (incomplete / len(traces)) if traces else 0.0,
        "last_normalized_us": last_seen or None,
        "seconds_since_last": ((now_us - last_seen) / 1_000_000) if last_seen else None,
        "ingest_rate_per_min": recent / (window_s / 60),
        "window_s": window_s,
        "staleness_ms": {"p50": percentile(stale, 0.5), "p95": percentile(stale, 0.95), "p99": percentile(stale, 0.99)},
        "lag_ms": {"p50": percentile(lag, 0.5), "p95": percentile(lag, 0.95)},
        "series": series,
    }
