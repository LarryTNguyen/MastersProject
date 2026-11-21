"""Trace-only MicroRCA baseline.

Follows the paper's service graph, anomaly prioritization, and personalized
PageRank ideas. Simplifications: p90 slowdown threshold replaces BIRCH,
span duration substitutes for service response time, and CPU/memory metrics
are omitted. Edge strength is the callee's slowdown ratio.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List


def _p90(values):
    values = sorted(values)
    if not values:
        return 0.0
    # Nearest-rank percentile, avoiding a dependency on numpy.
    return values[max(0, (9 * len(values) + 9) // 10 - 1)]


def _durations(traces):
    """Collect service latency samples, including isolated child-call gaps.

    When a span has exactly one cross-service child, its caller-to-child
    interval is attributed to the child service. This captures delays that
    inflate the caller's wall time while leaving the callee's own span length
    unchanged.
    """
    out = defaultdict(list)
    for trace in traces:
        by_id = {s.get("span_id"): s for s in trace if s.get("span_id") is not None}
        children = defaultdict(list)
        for span in trace:
            parent_id = span.get("parent_span_id")
            if parent_id in by_id:
                children[parent_id].append(span)
        for span in trace:
            svc, duration = span.get("service"), span.get("duration_us")
            if svc is not None and duration is not None and duration >= 0:
                out[svc].append(float(duration))
        for caller in trace:
            child_spans = children.get(caller.get("span_id"), [])
            if len(child_spans) != 1:
                continue
            callee = child_spans[0]
            if caller.get("service") == callee.get("service") or callee.get("service") is None:
                continue
            caller_start, child_start = caller.get("start_us"), callee.get("start_us")
            child_duration = callee.get("duration_us")
            if caller_start is None or child_start is None or child_duration is None:
                continue
            # Parent and child start times are relative to the same trace clock.
            gap_and_child = max(0.0, float(child_start) + float(child_duration) - float(caller_start))
            out[callee["service"]].append(gap_and_child)
    return out


def microrca(normal, anomalous) -> List[str]:
    """Rank likely root-cause services using before/after trace spans."""
    before, after = _durations(normal), _durations(anomalous)
    services = set(before) | set(after)
    if not services:
        return []

    scores: Dict[str, float] = {}
    for svc in services:
        base, current = _p90(before.get(svc, [])), _p90(after.get(svc, []))
        # A missing baseline is not evidence of an anomaly; zero durations are
        # protected to keep the score finite and deterministic.
        ratio = current / max(base, 1.0) if after.get(svc) else 0.0
        scores[svc] = max(0.0, ratio - 1.0) if current > base * 1.5 else 0.0

    graph = defaultdict(dict)
    for trace in list(normal) + list(anomalous):
        by_id = {s.get("span_id"): s for s in trace if s.get("span_id") is not None}
        for child in trace:
            parent = by_id.get(child.get("parent_span_id"))
            if parent and parent.get("service") != child.get("service"):
                a, b = parent.get("service"), child.get("service")
                if a is not None and b is not None:
                    graph[a][b] = max(graph[a].get(b, 0.0), max(1.0, scores.get(b, 0.0)))
                    graph[b].setdefault(a, 1.0)  # undirected propagation

    seeds = {s for s, score in scores.items() if score > 0}
    if not seeds:
        # Stable fallback still provides a ranking for cases with no detected
        # threshold crossing, using observed anomalous span latency.
        scores = {s: _p90(after.get(s, [])) for s in services}
        seeds = {s for s in services if after.get(s)}
    active = set(seeds)
    for svc in tuple(seeds):
        active.update(graph.get(svc, {}))
    active = active or services
    personalization = {s: max(scores.get(s, 0.0), 0.0) for s in active}
    total = sum(personalization.values())
    if total <= 0:
        personalization = {s: 1.0 / len(active) for s in active}
    else:
        personalization = {s: v / total for s, v in personalization.items()}

    rank = dict(personalization)
    for _ in range(50):
        nxt = {s: 0.15 * personalization[s] for s in active}
        for source in active:
            neighbors = [(t, w) for t, w in graph.get(source, {}).items() if t in active]
            weight = sum(w for _, w in neighbors)
            if weight:
                for target, edge_weight in neighbors:
                    nxt[target] += 0.85 * rank[source] * edge_weight / weight
            else:
                for target in active:
                    nxt[target] += 0.85 * rank[source] * personalization[target]
        rank = nxt
    return sorted(active, key=lambda s: (-rank[s], s))
