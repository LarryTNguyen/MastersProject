"""Simple ranking methods to compare against.

All of them look like: method(normal, anomalous) -> [service, ...] best guess
first, where normal/anomalous are lists of traces (each a list of span dicts)
from before and after the fault. The real score and MicroRCA plug in through
load_method with the same signature.
"""
from __future__ import annotations

import importlib
from typing import Callable, Dict, List

from . import _paths

_paths.setup()
from app import analysis  # dashboard/app/analysis.py

Method = Callable[[List[List[dict]], List[List[dict]]], List[str]]


def _rank(scores: Dict[str, float]) -> List[str]:
    return [s for s, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]


def slowest_average(normal, anomalous) -> List[str]:
    """Rank services by their mean span duration in the anomalous period."""
    tot: Dict[str, float] = {}
    n: Dict[str, int] = {}
    for spans in anomalous:
        for s in spans:
            if s.get("duration_us") is not None:
                tot[s["service"]] = tot.get(s["service"], 0) + s["duration_us"]
                n[s["service"]] = n.get(s["service"], 0) + 1
    return _rank({k: tot[k] / n[k] for k in tot})


def longest_span(normal, anomalous) -> List[str]:
    """Each slow trace votes for the service holding its longest self-time span."""
    pairs = [({"duration_us": _duration(spans)}, spans) for spans in anomalous]
    slow, _ = analysis.split_slow(pairs)
    votes: Dict[str, float] = {}
    for _, spans in (slow or pairs):
        st = analysis.self_times(spans)
        best = max(spans, key=lambda s: st.get(s["span_id"]) or -1)
        votes[best["service"]] = votes.get(best["service"], 0) + 1
    return _rank(votes)


def critical_path_only(normal, anomalous) -> List[str]:
    """Rank services by self time on the critical path, summed over anomalous traces."""
    tot: Dict[str, float] = {}
    for spans in anomalous:
        for svc, v in analysis.critical_path_share(spans).items():
            tot[svc] = tot.get(svc, 0) + v
    return _rank(tot)


def _duration(spans: List[dict]) -> int:
    start = min(s["start_us"] for s in spans)
    end = max(s["start_us"] + (s.get("duration_us") or 0) for s in spans)
    return end - start


BUILTIN: Dict[str, Method] = {
    "slowest_average": slowest_average,
    "longest_span": longest_span,
    "critical_path_only": critical_path_only,
}


def load_method(spec: str) -> Method:
    """'slowest_average' for a built-in, or 'package.module:function' for anything else
    (the bottleneck score, MicroRCA) as long as it has the same signature."""
    if spec in BUILTIN:
        return BUILTIN[spec]
    if ":" not in spec:
        raise ValueError(f"unknown method {spec!r}; built-ins are {sorted(BUILTIN)} or use module:function")
    mod, fn = spec.split(":", 1)
    return getattr(importlib.import_module(mod), fn)
