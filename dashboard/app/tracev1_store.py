"""Store that reads the reconstruction step's PostgreSQL tables (schema trace_v1).

Uses: ingestion_runs, spans (JSONB + generated columns), trace_inventory,
current_trace_analyses, service_window_results. See trace_schema/schema.sql.

Self time, critical path and slow-vs-normal are computed from the spans here
(app.analysis), same as the JSONL store. When the reconstruction step starts
filling span_features / critical_path they can replace that.

TraceV1Store(psycopg.connect(DATABASE_URL)). Shows one run at a time: run_id
arg, else the newest run.
"""
from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Tuple

from .canonical import span_from_record
from .stitch import percentile, trace_summary
from .store import SORTS, Store, TraceQuery

_CONFIDENCE = {"HIGH": 0.9, "MEDIUM": 0.6, "LOW": 0.3, "UNAVAILABLE": None}


class TraceV1Store(Store):
    name = "trace_v1"

    def __init__(self, conn, run_id: Optional[str] = None):
        self.conn = conn
        self._run_id = run_id

    # ---- helpers ----
    def _rows(self, sql: str, params=()) -> List[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def run_id(self) -> Optional[str]:
        if self._run_id:
            return self._run_id
        rows = self._rows("SELECT run_id::text FROM trace_v1.ingestion_runs ORDER BY created_at DESC LIMIT 1")
        return rows[0][0] if rows else None

    def runs(self) -> List[dict]:
        return [{"run_id": r, "application": a, "environment": e, "source_format": f, "identity_kind": k,
                 "created_at": c.isoformat(), "traces": n}
                for r, a, e, f, k, c, n in self._rows(
                    "SELECT r.run_id::text, r.application_name, r.environment, r.source_format, r.identity_kind, "
                    "r.created_at, (SELECT count(*) FROM trace_v1.traces t WHERE t.run_id = r.run_id) "
                    "FROM trace_v1.ingestion_runs r ORDER BY r.created_at DESC")]

    def _spans_by_trace(self, trace_ids: List[str]) -> Dict[str, List[dict]]:
        run = self.run_id()
        out: Dict[str, List[dict]] = {t: [] for t in trace_ids}
        if not run or not trace_ids:
            return out
        for tid, data in self._rows(
                "SELECT trace_id, data FROM trace_v1.spans WHERE run_id = %s AND trace_id = ANY(%s) "
                "ORDER BY trace_id, start_time_ns NULLS LAST, span_id", (run, trace_ids)):
            rec = data if isinstance(data, dict) else json.loads(data)
            out[tid].append(span_from_record(rec))
        for lst in out.values():
            lst.sort(key=lambda s: (s["start_us"], s["span_id"]))
        return out

    def _summaries(self, trace_ids: List[str]) -> Dict[str, dict]:
        """Trace summaries from the spans, plus state from the analysis row."""
        spans = self._spans_by_trace(trace_ids)
        run = self.run_id()
        analysis = {}
        if run and trace_ids:
            for tid, topology, state, reason in self._rows(
                    "SELECT t.trace_id, a.topology, t.state, t.settlement_reason FROM trace_v1.traces t "
                    "LEFT JOIN trace_v1.current_trace_analyses a USING (run_id, trace_id) "
                    "WHERE t.run_id = %s AND t.trace_id = ANY(%s)", (run, trace_ids)):
                analysis[tid] = (topology, state, reason)
        out = {}
        for tid, lst in spans.items():
            if not lst:
                continue
            t = trace_summary(tid, lst)
            topology, state, reason = analysis.get(tid, (None, None, None))
            if topology:
                t["complete"] = topology == "CONNECTED" and t["complete"]
            t["state"] = state
            t["settlement_reason"] = reason
            out[tid] = t
        return out

    # ---- Store interface ----
    def count_traces(self) -> int:
        run = self.run_id()
        return self._rows("SELECT count(*) FROM trace_v1.traces WHERE run_id = %s", (run,))[0][0] if run else 0

    def services(self) -> List[dict]:
        run = self.run_id()
        if not run:
            return []
        rows = self._rows(
            "SELECT service_name, count(*), count(DISTINCT trace_id), "
            "count(*) FILTER (WHERE status_code = 'ERROR'), count(*) FILTER (WHERE status_code IN ('OK','ERROR')) "
            "FROM trace_v1.spans WHERE run_id = %s GROUP BY service_name ORDER BY service_name", (run,))
        durs: Dict[str, List[float]] = {}
        for svc, d in self._rows("SELECT service_name, duration_ns FROM trace_v1.spans "
                                 "WHERE run_id = %s AND duration_ns IS NOT NULL ORDER BY service_name, duration_ns", (run,)):
            durs.setdefault(svc, []).append(d / 1_000_000)
        return [{"service": svc, "span_count": n, "trace_count": tn, "error_rate": (err / obs) if obs else 0.0,
                 "p50_ms": percentile(durs.get(svc, []), 0.5), "p99_ms": percentile(durs.get(svc, []), 0.99)}
                for svc, n, tn, err, obs in rows]

    def operations(self, service: Optional[str] = None) -> List[str]:
        run = self.run_id()
        if not run:
            return []
        if service:
            rows = self._rows("SELECT DISTINCT operation_name FROM trace_v1.spans WHERE run_id = %s AND service_name = %s "
                              "AND operation_name IS NOT NULL ORDER BY 1", (run, service))
        else:
            rows = self._rows("SELECT DISTINCT operation_name FROM trace_v1.spans WHERE run_id = %s "
                              "AND operation_name IS NOT NULL ORDER BY 1", (run,))
        return [r[0] for r in rows]

    def _candidate_ids(self, q: TraceQuery) -> List[str]:
        """Filter in SQL where that is cheap (service, time, status), the rest in Python."""
        run = self.run_id()
        if not run:
            return []
        where, params = ["t.run_id = %s"], [run]
        if q.service:
            where.append("EXISTS (SELECT 1 FROM trace_v1.spans s WHERE s.run_id = t.run_id AND s.trace_id = t.trace_id "
                         "AND s.service_name = %s)")
            params.append(q.service)
        if q.status == "error":
            where.append("t.error_span_count > 0")
        elif q.status == "ok":
            where.append("t.error_span_count = 0 AND t.observed_status_count > 0")
        elif q.status == "unset":
            where.append("t.observed_status_count = 0")
        if q.start_after_us is not None:
            where.append("t.observed_start_ns >= %s")
            params.append(q.start_after_us * 1000)
        if q.start_before_us is not None:
            where.append("t.observed_start_ns <= %s")
            params.append(q.start_before_us * 1000)
        sql = "SELECT t.trace_id FROM trace_v1.trace_inventory t WHERE " + " AND ".join(where) + \
              " ORDER BY t.observed_start_ns DESC NULLS LAST, t.trace_id"
        return [r[0] for r in self._rows(sql, params)]

    def search_traces(self, q: TraceQuery) -> dict:
        ids = self._candidate_ids(q)
        summaries = self._summaries(ids)
        # service, status and time were applied in SQL; the rest is cheap to do here
        rest = TraceQuery(operation=q.operation, min_duration_ms=q.min_duration_ms,
                          max_duration_ms=q.max_duration_ms, complete=q.complete)
        hits = [t for t in summaries.values() if rest.matches(t, set())]
        key, rev = SORTS.get(q.sort, SORTS["start_desc"])
        hits.sort(key=key, reverse=rev)
        return {"items": hits[q.offset:q.offset + q.limit], "total": len(hits), "offset": q.offset, "limit": q.limit}

    def get_trace(self, trace_id: str) -> Optional[dict]:
        summaries = self._summaries([trace_id])
        if trace_id not in summaries:
            return None
        return {"summary": summaries[trace_id], "spans": self._spans_by_trace([trace_id])[trace_id]}

    def rankings(self, endpoint: Optional[str] = None, limit: int = 10) -> List[dict]:
        run = self.run_id()
        if not run:
            return []
        where, params = ["run_id = %s"], [run]
        if endpoint:
            where.append("(endpoint_key = %s OR endpoint_key = '*')")
            params.append(endpoint)
        rows = self._rows(
            "SELECT window_start_ns, window_end_ns, endpoint_key, algorithm_version, service_name, score, confidence, "
            "evidence, signals FROM trace_v1.service_window_results WHERE " + " AND ".join(where) +
            " ORDER BY window_end_ns DESC, score DESC NULLS LAST, service_name", params)
        windows: Dict[tuple, dict] = {}
        for ws, we, ep, algo, svc, score, conf, evidence, signals in rows:
            w = windows.setdefault((ws, we, ep, algo), {
                "window_start_us": ws // 1000, "window_end_us": we // 1000,
                "endpoint": None if ep == "*" else ep, "method": algo,
                "confidence": None, "confidence_label": None, "note": None, "rankings": []})
            if len(w["rankings"]) < limit:
                ev = _evidence_dict(evidence, signals)
                w["rankings"].append({"rank": len(w["rankings"]) + 1, "service": svc,
                                      "score": float(score) if score is not None else 0.0, "evidence": ev})
                if w["confidence_label"] is None:
                    w["confidence_label"] = conf
                    w["confidence"] = _CONFIDENCE.get(conf)
        out = list(windows.values())
        for w in out:
            if not w["rankings"] or all(r["score"] == 0.0 for r in w["rankings"]) or w["confidence_label"] == "UNAVAILABLE":
                w["note"] = "no service clearly explains a slowdown in this window"
        return out

    def traces_with_spans(self, endpoint=None, limit=2000, subset="all") -> List[Tuple[dict, List[dict]]]:
        ids = self._candidate_ids(TraceQuery())[:limit * 3 if endpoint else limit]
        summaries = self._summaries(ids)
        spans = self._spans_by_trace(list(summaries))
        pairs = [(summaries[i], spans[i]) for i in ids if i in summaries
                 and (not endpoint or summaries[i]["root_operation"] == endpoint)][:limit]
        return self._pick_subset(pairs, subset)

    def all_spans(self) -> Iterable[dict]:
        run = self.run_id()
        if not run:
            return []
        return (span_from_record(d if isinstance(d, dict) else json.loads(d))
                for (d,) in self._rows("SELECT data FROM trace_v1.spans WHERE run_id = %s", (run,)))

    def all_traces(self) -> Iterable[dict]:
        ids = self._candidate_ids(TraceQuery())
        return self._summaries(ids).values()


def _evidence_dict(evidence, signals) -> dict:
    """evidence is a list and signals a dict in the table; flatten both for the page."""
    out: dict = {}
    if isinstance(signals, dict):
        for k, v in signals.items():
            out[k] = v if not isinstance(v, (dict, list)) else json.dumps(v)
    if isinstance(evidence, list):
        for i, item in enumerate(evidence):
            if isinstance(item, dict):
                name = item.get("name") or item.get("kind") or f"evidence_{i + 1}"
                value = item.get("value", item.get("detail", item))
                out[name] = value if not isinstance(value, (dict, list)) else json.dumps(value)
            else:
                out[f"evidence_{i + 1}"] = item
    return out
