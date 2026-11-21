"""The diagnosis package's ranking (tail-share-excess) as an RCAEval method.

    "extra_methods": ["experiments.diagnosis_method:tail_share_excess"]

Same signature as the baselines: method(normal, anomalous) -> [service, ...].
What it does, without changing the diagnosis code:

1. converts every span of the case (before and after the fault) into a canonical
   record with the ingest package's normaliser;
2. runs diagnosis.episodes.diagnose with its defaults (300 s windows, 10 traces per
   window, slow = 1.5x the window median, 5 ms minimum excess) and the synchronous
   critical-path estimate;
3. keeps the windows that overlap the anomalous period (including the one the fault
   starts in), i.e. the ones an engineer would look at during the incident, and adds
   up each service's score weighted by the window's number of slow traces. Highest
   total first.

Timing policy is explicit: derived intervals remain DERIVED. The default
RCAEval run accepts them and labels the resulting configuration accordingly.

LAST holds the details of the most recent call (excluded traces, windows with and
without a ranking) for reporting.
"""
from __future__ import annotations

import contextlib
from typing import Dict, List

from . import _paths

_paths.setup()
from traceingest.canonical import NormalizationContext, to_record  # noqa: E402
from traceingest.spans import Span  # noqa: E402

ACCEPT_DERIVED = True
LAST: dict = {}


def _records(traces: List[List[dict]], accept_derived: bool) -> List[dict]:
    ctx = NormalizationContext.for_source("rcaeval:evaluation")
    out = []
    for spans in traces:
        for s in spans:
            r = to_record(Span.from_dict(s), ctx)
            out.append(r)
    return out


def rank_windows(windows: List[dict], from_ns: int) -> List[str]:
    """Sum score x slow traces per service over the windows that end after from_ns."""
    total: Dict[str, float] = {}
    for w in windows:
        if int(w["window_end_ns"]) <= from_ns:
            continue
        weight = w.get("slow_traces") or 0
        for r in w.get("rankings", []):
            name = r.get("service_name") or r["service"]
            total[name] = total.get(name, 0.0) + r["score"] * weight
    return [s for s, v in sorted(total.items(), key=lambda kv: (-kv[1], kv[0])) if v > 0]


def _root_start_us(spans: List[dict]) -> int:
    roots = [s for s in spans if not s.get("parent_span_id")]
    return min(s["start_us"] for s in (roots or spans))


def diagnose_by_window(traces: List[List[dict]], accept_derived: bool = True, **settings) -> dict:
    """diagnose() one time window at a time. Every window's result only depends on the
    traces whose root starts in it, so this gives the same windows as one call over the
    whole case, with a fraction of the memory (a Train Ticket case is about 1M spans).
    Score-spike alerts compare neighbouring windows, so they are not computed here."""
    from diagnosis.episodes import diagnose          # repo root must be on sys.path (python -m from the root)
    settings.setdefault("assume_synchronous", True)
    settings.setdefault("timing_policy", "measured-or-derived" if accept_derived else "measured")
    width_us = settings.get("window_seconds", 300) * 1_000_000
    chunks: Dict[int, List[List[dict]]] = {}
    for spans in traces:
        chunks.setdefault(_root_start_us(spans) // width_us, []).append(spans)
    windows, excluded, config, method = [], {}, None, None
    for key in sorted(chunks):
        r = diagnose(_records(chunks[key], accept_derived), **settings)
        windows += r["windows"]
        for k, v in r["excluded_traces"].items():
            excluded[k] = excluded.get(k, 0) + v
        config, method = r["configuration"], r["method"]
    windows.sort(key=lambda w: (w["run_id"], w["endpoint"], int(w["window_start_ns"])))
    return {"method": method, "configuration": config, "excluded_traces": excluded, "windows": windows}


def tail_share_excess(normal, anomalous, accept_derived: bool = None, **settings) -> List[str]:
    accept = ACCEPT_DERIVED if accept_derived is None else accept_derived
    result = diagnose_by_window(list(normal) + list(anomalous), accept, **settings)
    from_ns = min(min(s["start_us"] for s in spans) for spans in anomalous) * 1000 if anomalous else 0
    ranking = rank_windows(result["windows"], from_ns)
    after = [w for w in result["windows"] if int(w["window_end_ns"]) > from_ns]
    LAST.clear()
    LAST.update({"method": result["method"], "configuration": result["configuration"], "excluded_traces": result["excluded_traces"], "windows_after": len(after),
                 "windows_after_ranked": sum(1 for w in after if w["rankings"]),
                 "notes": sorted({w["note"] for w in after if w["note"]})})
    return ranking


def score_v2(normal, anomalous, *, accept_derived=True, **settings):
    """Explicit pre-injection reference; no ground-truth service enters scoring."""
    from diagnosis.score_v2 import diagnose
    result = diagnose(_records(normal, accept_derived), _records(anomalous, accept_derived),
                      timing_policy="measured-or-derived" if accept_derived else "measured",
                      assume_synchronous=True, **settings)
    total = {}
    for window in result["windows"]:
        for row in window["rankings"]:
            name = row["service_name"]
            if row["score"] is not None:
                total[name] = total.get(name, 0) + row["score"] * window["trace_count"]
    LAST.clear()
    LAST.update(result)
    return sorted(total, key=lambda name: (-total[name], name))


VALIDATION_SAMPLE = 1000


@contextlib.contextmanager
def _sampled_validation(records: List[dict], sample: int = VALIDATION_SAMPLE):
    """Check a random sample of the records against the canonical schema; if every one
    passes, skip the per-record check inside the diagnosis code for this case.

    The per-record JSON Schema check is about 80% of score_v2's run time (roughly an
    hour per Online Boutique case), and every record here comes from the ingest
    normalizer, whose output the schema tests already cover. If any sampled record
    fails, nothing is skipped and the case runs with full validation."""
    import random
    import trace_schema.validate as schema
    import diagnosis.reliability as reliability
    picked = random.Random(len(records)).sample(records, min(sample, len(records)))
    if any(schema.validate_record(r) for r in picked):
        yield False
        return
    real = schema.validate_record
    schema.validate_record = reliability.validate_record = lambda record: []
    try:
        yield True
    finally:
        schema.validate_record = reliability.validate_record = real


def score_v2_fast(normal, anomalous, *, accept_derived=True, **settings):
    """score_v2, unchanged, with the schema check done on a sample instead of every record
    (see _sampled_validation). Same rankings, about 25x faster on RCAEval."""
    from diagnosis.score_v2 import diagnose
    base, target = _records(normal, accept_derived), _records(anomalous, accept_derived)
    with _sampled_validation(base + target) as skipped:
        result = diagnose(base, target, timing_policy="measured-or-derived" if accept_derived else "measured",
                          assume_synchronous=True, **settings)
    total = {}
    for window in result["windows"]:
        for row in window["rankings"]:
            if row["score"] is not None:
                total[row["service_name"]] = total.get(row["service_name"], 0) + row["score"] * window["trace_count"]
    LAST.clear()
    LAST.update(result)
    LAST["validation"] = f"sampled ({VALIDATION_SAMPLE} records, all valid)" if skipped else "full"
    return sorted(total, key=lambda name: (-total[name], name))


def v1_measured(normal, anomalous):
    return tail_share_excess(normal, anomalous, accept_derived=False)


def v2_measured(normal, anomalous):
    return score_v2(normal, anomalous, accept_derived=False)
