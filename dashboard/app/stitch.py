"""Group spans into traces for display.

This is the light version used by the dashboard: it only needs to know which
spans belong together, which one is the root, and whether the tree is
complete. The real reconstruction (self-time, critical path, quiet-window
completion) is the reconstruction Lambda's job and lives in its own package.
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional


def trace_summary(trace_id: str, spans: List[dict]) -> dict:
    ids = {s["span_id"] for s in spans}
    roots = [s for s in spans if not s.get("parent_span_id")]
    orphans = [s for s in spans if s.get("parent_span_id") and s["parent_span_id"] not in ids]
    # root: a real root span, else the earliest orphan (its parent never arrived)
    root = min(roots or orphans or spans, key=lambda s: s["start_us"])
    start = min(s["start_us"] for s in spans)
    end = max(s["start_us"] + (s.get("duration_us") or 0) for s in spans)
    statuses = {s.get("status", "unset") for s in spans}
    status = "error" if "error" in statuses else ("ok" if "ok" in statuses else "unset")
    return {
        "trace_id": trace_id,
        "root_service": root["service"],
        "root_operation": root["operation"],
        "start_us": start,
        "duration_us": end - start,
        "span_count": len(spans),
        "service_count": len({s["service"] for s in spans}),
        "status": status,
        "complete": not orphans and len(roots) <= 1
                    and all(s.get("duration_us") is not None for s in spans),
        "source": root.get("source", "unknown"),
    }


def group_by_trace(spans: Iterable[dict]) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for s in spans:
        out.setdefault(s["trace_id"], []).append(s)
    return out


def percentile(values: List[float], p: float) -> Optional[float]:
    """Nearest-rank percentile (p in 0..1)."""
    if not values:
        return None
    vs = sorted(values)
    k = max(0, min(len(vs) - 1, math.ceil(p * len(vs)) - 1))
    return vs[k]
