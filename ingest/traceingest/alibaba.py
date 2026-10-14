"""Alibaba cluster-trace-microservices (v2021 / v2022) call graphs -> common spans.

Each CSV row is one RPC from an upstream microservice (``um``) to a downstream
one (``dm``). The ``rpcid`` is hierarchical ("0.1.3" is a child call of "0.1"),
which is what we use to rebuild parent/child links.

Things the raw data does that we have to handle (see Luo et al., SoCC 2021):

* The same call can be logged twice, once by the caller and once by the callee.
  We keep one record per (traceid, rpcid), preferring a non-negative response
  time and, among those, the larger one (the caller side includes the network).
* ``rt`` can be negative when the response time was not captured. Those spans
  keep ``duration_us = None`` and are marked so the stitcher can flag them.
* Timestamps are milliseconds relative to the start of the collection window,
  so ``base_epoch_us`` lets callers anchor them to a real date.

Note: deduplication needs every row of a trace, so the whole file is held in
memory. That is fine for the samples we replay; split very large files by time
window first.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from typing import IO, Dict, Iterator, List, Optional, Tuple

from .ids import hashed_span_id
from .spans import STATUS_UNSET, Span

SOURCE = "alibaba"

# column aliases seen across dataset versions and exported samples
_ALIASES = {
    "traceid": ("traceid", "trace_id"),
    "timestamp": ("timestamp", "ts"),
    "rpcid": ("rpcid", "rpc_id"),
    "um": ("um",),
    "dm": ("dm",),
    "rpctype": ("rpctype", "rpc_type"),
    "interface": ("interface",),
    "rt": ("rt",),
    "service": ("service",),
    "uminstanceid": ("uminstanceid",),
    "dminstanceid": ("dminstanceid",),
}
_REQUIRED = ("traceid", "timestamp", "rpcid", "um", "dm", "rt")
_MISSING_VALUES = {"", "NAN", "NULL", "NONE", "UNKNOWN", "(?)"}


@dataclass
class AlibabaStats:
    rows: int = 0
    spans: int = 0
    duplicates_dropped: int = 0
    duplicate_conflicts: int = 0      # same rpcid but different downstream service
    missing_rt: int = 0
    orphans: int = 0                  # parent rpcid not present in the file
    bad_rows: int = 0
    traces: int = 0
    problems: List[str] = field(default_factory=list)


def resolve_columns(header: List[str]) -> Dict[str, str]:
    lower = {h.strip().lower(): h for h in header}
    cols = {}
    for name, aliases in _ALIASES.items():
        for a in aliases:
            if a in lower:
                cols[name] = lower[a]
                break
    missing = [c for c in _REQUIRED if c not in cols]
    if missing:
        raise ValueError(f"not an Alibaba call-graph file, missing columns: {missing}")
    return cols


def looks_like_alibaba(header: List[str]) -> bool:
    try:
        resolve_columns(header)
        return True
    except ValueError:
        return False


def parent_rpcid(rpcid: str) -> Optional[str]:
    return rpcid.rsplit(".", 1)[0] if "." in rpcid else None


def _clean(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.strip()
    return v if v and v.upper() not in _MISSING_VALUES else None


def _better(new_rt: float, old_rt: float) -> bool:
    """Prefer a known (non-negative) response time, then the larger one."""
    if (new_rt >= 0) != (old_rt >= 0):
        return new_rt >= 0
    return new_rt > old_rt


def span_id_for(traceid: str, rpcid: str) -> str:
    return hashed_span_id(SOURCE, traceid, rpcid)


def read_alibaba(fh: IO[str], base_epoch_us: int = 0,
                 stats: Optional[AlibabaStats] = None) -> List[Span]:
    """Read an Alibaba call-graph CSV and return deduplicated common spans,
    sorted by (trace, start time)."""
    stats = stats if stats is not None else AlibabaStats()
    reader = csv.reader(fh)
    header = next(reader, [])
    cols = resolve_columns(header)
    idx = {name: header.index(col) for name, col in cols.items()}
    i_trace, i_ts, i_rpc, i_um, i_dm, i_rt = (idx[k] for k in ("traceid", "timestamp", "rpcid", "um", "dm", "rt"))
    i_type, i_iface, i_svc = idx.get("rpctype"), idx.get("interface"), idx.get("service")
    width = len(header)

    def col(row, i):
        return _clean(row[i]) if i is not None and i < len(row) else None

    best: Dict[Tuple[str, str], dict] = {}
    for line_no, row in enumerate(reader, start=2):
        stats.rows += 1
        if len(row) < width - 1:
            stats.bad_rows += 1
            continue
        try:
            traceid = _clean(row[i_trace])
            rpcid = _clean(row[i_rpc])
            ts_ms = float(row[i_ts])
            rt_raw = _clean(row[i_rt])
            rt_ms = float(rt_raw) if rt_raw is not None else -1.0
        except (TypeError, ValueError, IndexError):
            stats.bad_rows += 1
            if len(stats.problems) < 20:
                stats.problems.append(f"line {line_no}: unreadable row")
            continue
        if not traceid or not rpcid:
            stats.bad_rows += 1
            continue
        key = (traceid, rpcid)
        old = best.get(key)
        if old is None:
            best[key] = {"ts_ms": ts_ms, "rt_ms": rt_ms, "um": col(row, i_um), "dm": col(row, i_dm),
                         "rpctype": col(row, i_type), "interface": col(row, i_iface),
                         "entry_service": col(row, i_svc)}
            continue
        stats.duplicates_dropped += 1
        dm = col(row, i_dm)
        if old["dm"] != dm:
            stats.duplicate_conflicts += 1
        if _better(rt_ms, old["rt_ms"]):
            old.update(rt_ms=rt_ms, um=col(row, i_um), dm=dm, rpctype=col(row, i_type),
                       interface=col(row, i_iface), entry_service=col(row, i_svc))
        old["ts_ms"] = min(old["ts_ms"], ts_ms)   # keep the earliest start seen for the call

    # one hash per call; the parent's id is just the parent's own hash
    span_ids = {key: span_id_for(*key) for key in best}
    spans: List[Span] = []
    traces = set()
    for (traceid, rpcid), rec in best.items():
        traces.add(traceid)
        prpc = parent_rpcid(rpcid)
        parent_id = None
        if prpc is not None:
            parent_id = span_ids.get((traceid, prpc))
            if parent_id is None:
                stats.orphans += 1
                parent_id = span_id_for(traceid, prpc)   # keep the link so a late parent can still attach
        attrs = {"alibaba.rpcid": rpcid}
        if rec["rpctype"]:
            attrs["alibaba.rpctype"] = rec["rpctype"]
        if rec["entry_service"]:
            attrs["alibaba.service"] = rec["entry_service"]
        if rec["rt_ms"] < 0:
            stats.missing_rt += 1
            duration_us = None
        else:
            duration_us = int(round(rec["rt_ms"] * 1000))
        spans.append(Span(
            trace_id=traceid,
            span_id=span_ids[(traceid, rpcid)],
            parent_span_id=parent_id,
            service=rec["dm"] or "unknown",
            operation=rec["interface"] or rec["rpctype"] or "call",
            start_us=base_epoch_us + int(round(rec["ts_ms"] * 1000)),
            duration_us=duration_us,
            status=STATUS_UNSET,          # the dataset has no error field
            kind=None,
            peer_service=rec["um"],
            source=SOURCE,
            attributes=attrs,
        ))
    spans.sort(key=Span.sort_key)
    stats.spans = len(spans)
    stats.traces = len(traces)
    return spans


def iter_alibaba(fh: IO[str], **kw) -> Iterator[Span]:
    yield from read_alibaba(fh, **kw)
