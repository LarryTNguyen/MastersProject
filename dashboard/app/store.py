"""Where the API gets its data.

JsonlStore reads normalized span files from a folder (good for local work and
demos). SqlStore (sql_store.py) is a small SQLite layout. TraceV1Store
(tracev1_store.py) reads the reconstruction step's PostgreSQL tables, which
is the real one. All three return plain dicts.
"""
from __future__ import annotations

import glob
import json
import os
import time
from typing import Dict, Iterable, List, Optional, Tuple

from . import analysis
from .canonical import span_from_any
from .stitch import group_by_trace, percentile, trace_summary


class TraceQuery:
    def __init__(self, service: Optional[str] = None, operation: Optional[str] = None,
                 status: Optional[str] = None, min_duration_ms: Optional[float] = None,
                 max_duration_ms: Optional[float] = None, start_after_us: Optional[int] = None,
                 start_before_us: Optional[int] = None, complete: Optional[bool] = None,
                 sort: str = "start_desc", offset: int = 0, limit: int = 50):
        self.service, self.operation, self.status = service, operation, status
        self.min_duration_ms, self.max_duration_ms = min_duration_ms, max_duration_ms
        self.start_after_us, self.start_before_us, self.complete = start_after_us, start_before_us, complete
        self.sort, self.offset, self.limit = sort, offset, limit

    def matches(self, t: dict, services_in_trace: set) -> bool:
        if self.service and self.service not in services_in_trace:
            return False
        if self.operation and self.operation.lower() not in t["root_operation"].lower():
            return False
        if self.status and t["status"] != self.status:
            return False
        if self.min_duration_ms is not None and t["duration_us"] < self.min_duration_ms * 1000:
            return False
        if self.max_duration_ms is not None and t["duration_us"] > self.max_duration_ms * 1000:
            return False
        if self.start_after_us is not None and t["start_us"] < self.start_after_us:
            return False
        if self.start_before_us is not None and t["start_us"] > self.start_before_us:
            return False
        if self.complete is not None and t["complete"] != self.complete:
            return False
        return True


SORTS = {
    "start_desc": (lambda t: t["start_us"], True),
    "start_asc": (lambda t: t["start_us"], False),
    "duration_desc": (lambda t: t["duration_us"], True),
    "duration_asc": (lambda t: t["duration_us"], False),
}


class Store:
    name = "base"

    def count_traces(self) -> int:
        raise NotImplementedError

    def services(self) -> List[dict]:
        raise NotImplementedError

    def operations(self, service: Optional[str] = None) -> List[str]:
        raise NotImplementedError

    def search_traces(self, q: TraceQuery) -> dict:
        raise NotImplementedError

    def get_trace(self, trace_id: str) -> Optional[dict]:
        raise NotImplementedError

    def rankings(self, endpoint: Optional[str] = None, limit: int = 10) -> List[dict]:
        raise NotImplementedError

    def traces_with_spans(self, endpoint: Optional[str] = None, limit: int = 2000,
                          subset: str = "all") -> List[Tuple[dict, List[dict]]]:
        """(summary, spans) pairs for an endpoint, newest first. ``subset`` is
        all / slow / normal (slow = slowest 10%)."""
        raise NotImplementedError

    def all_spans(self) -> Iterable[dict]:
        raise NotImplementedError

    def all_traces(self) -> Iterable[dict]:
        raise NotImplementedError

    # ---- derived views, same for every store ----
    def compare(self, endpoint: Optional[str] = None, threshold_ms: Optional[float] = None, limit: int = 2000) -> dict:
        out = analysis.compare(self.traces_with_spans(endpoint, limit), threshold_ms)
        out["endpoint"] = endpoint
        return out

    def flame(self, endpoint: Optional[str] = None, subset: str = "all", limit: int = 500) -> dict:
        return analysis.flame(spans for _, spans in self.traces_with_spans(endpoint, limit, subset))

    def pipeline(self, now_us: int, window_s: int = 900) -> dict:
        return analysis.pipeline_stats(self.all_spans(), self.all_traces(), now_us, window_s)

    @staticmethod
    def _pick_subset(pairs, subset: str):
        if subset == "all":
            return pairs
        slow, normal = analysis.split_slow(pairs)
        return slow if subset == "slow" else normal


class JsonlStore(Store):
    """Reads span files into memory. Reloads when the files change."""
    name = "jsonl"

    CHECK_INTERVAL_S = 2.0   # how often to look for new or changed files

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._stamp = None
        self._checked_at = 0.0
        self._traces: Dict[str, dict] = {}
        self._spans: Dict[str, List[dict]] = {}
        self._trace_services: Dict[str, set] = {}
        self._services: List[dict] = []
        self._ops: Dict[Optional[str], List[str]] = {}
        self._rankings: List[dict] = []
        self.reload(force=True)

    def _files(self) -> List[str]:
        return sorted(glob.glob(os.path.join(self.data_dir, "**", "*.jsonl"), recursive=True))

    def _current_stamp(self):
        files = self._files()
        rk = os.path.join(self.data_dir, "rankings.json")
        if os.path.exists(rk):
            files.append(rk)
        return tuple((f, os.path.getmtime(f)) for f in files)

    def reload(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._checked_at < self.CHECK_INTERVAL_S:
            return
        self._checked_at = now
        stamp = self._current_stamp()
        if stamp == self._stamp:
            return
        spans: List[dict] = []
        for f in self._files():
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        spans.append(span_from_any(json.loads(line)))
        # the same span can appear in more than one file (re-upload); keep the last one
        unique: Dict[tuple, dict] = {}
        for s in spans:
            unique[(s["trace_id"], s["span_id"])] = s
        self._spans = group_by_trace(unique.values())
        for lst in self._spans.values():
            lst.sort(key=lambda s: (s["start_us"], s["span_id"]))
        self._traces = {tid: trace_summary(tid, lst) for tid, lst in self._spans.items()}
        self._trace_services = {tid: {s["service"] for s in lst} for tid, lst in self._spans.items()}
        self._services = self._compute_services()
        self._ops = {}
        rk = os.path.join(self.data_dir, "rankings.json")
        self._rankings = []
        if os.path.exists(rk):
            with open(rk, encoding="utf-8") as fh:
                data = json.load(fh)
            self._rankings = data if isinstance(data, list) else [data]
        self._stamp = stamp

    def count_traces(self) -> int:
        self.reload()
        return len(self._traces)

    def services(self) -> List[dict]:
        self.reload()
        return self._services

    def _compute_services(self) -> List[dict]:
        acc: Dict[str, dict] = {}
        for tid, spans in self._spans.items():
            for s in spans:
                a = acc.setdefault(s["service"], {"service": s["service"], "span_count": 0, "traces": set(),
                                                   "errors": 0, "with_status": 0, "durations": []})
                a["span_count"] += 1
                a["traces"].add(tid)
                if s.get("status") in ("ok", "error"):
                    a["with_status"] += 1
                    a["errors"] += s["status"] == "error"
                if s.get("duration_us") is not None:
                    a["durations"].append(s["duration_us"] / 1000)
        out = []
        for a in sorted(acc.values(), key=lambda a: a["service"]):
            out.append({"service": a["service"], "span_count": a["span_count"], "trace_count": len(a["traces"]),
                        "error_rate": (a["errors"] / a["with_status"]) if a["with_status"] else 0.0,
                        "p50_ms": percentile(a["durations"], 0.5), "p99_ms": percentile(a["durations"], 0.99)})
        return out

    def operations(self, service: Optional[str] = None) -> List[str]:
        self.reload()
        if service not in self._ops:
            self._ops[service] = sorted({s["operation"] for lst in self._spans.values() for s in lst
                                         if not service or s["service"] == service})
        return self._ops[service]

    def search_traces(self, q: TraceQuery) -> dict:
        self.reload()
        hits = [t for tid, t in self._traces.items() if q.matches(t, self._trace_services[tid])]
        key, rev = SORTS.get(q.sort, SORTS["start_desc"])
        hits.sort(key=key, reverse=rev)
        return {"items": hits[q.offset:q.offset + q.limit], "total": len(hits), "offset": q.offset, "limit": q.limit}

    def get_trace(self, trace_id: str) -> Optional[dict]:
        self.reload()
        if trace_id not in self._traces:
            return None
        return {"summary": self._traces[trace_id], "spans": self._spans[trace_id]}

    def traces_with_spans(self, endpoint=None, limit=2000, subset="all"):
        self.reload()
        ts = [t for t in self._traces.values() if not endpoint or t["root_operation"] == endpoint]
        ts.sort(key=lambda t: t["start_us"], reverse=True)
        pairs = [(t, self._spans[t["trace_id"]]) for t in ts[:limit]]
        return self._pick_subset(pairs, subset)

    def all_spans(self):
        self.reload()
        return (s for lst in self._spans.values() for s in lst)

    def all_traces(self):
        self.reload()
        return self._traces.values()

    def rankings(self, endpoint: Optional[str] = None, limit: int = 10) -> List[dict]:
        self.reload()
        out = []
        for w in self._rankings:
            if endpoint and w.get("endpoint") not in (None, endpoint):
                continue
            w = dict(w)
            w["rankings"] = sorted(w.get("rankings", []), key=lambda r: r["rank"])[:limit]
            out.append(w)
        out.sort(key=lambda w: w.get("window_end_us", 0), reverse=True)
        return out
