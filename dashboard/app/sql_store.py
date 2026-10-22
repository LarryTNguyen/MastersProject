"""SQLite-backed store for local development without Docker.

Same queries would run on Postgres with placeholders swapped from ? to %s.
load_spans fills the tables from span files (tools/load_sqlite.py).
"""
from __future__ import annotations

import json
import os
from typing import Iterable, List, Optional

from .stitch import group_by_trace, percentile, trace_summary
from .store import Store, TraceQuery

SCHEMA = open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8").read()
SPAN_COLS = ("trace_id", "span_id", "parent_span_id", "service", "operation", "start_us",
             "duration_us", "status", "kind", "peer_service", "source", "attributes",
             "received_us", "normalized_us")
TRACE_COLS = ("trace_id", "root_service", "root_operation", "start_us", "duration_us",
              "span_count", "service_count", "status", "complete", "source")
_ORDER = {"start_desc": "start_us DESC", "start_asc": "start_us ASC",
          "duration_desc": "duration_us DESC", "duration_asc": "duration_us ASC"}


class SqlStore(Store):
    name = "sql"

    def __init__(self, conn, paramstyle: str = "?"):
        self.conn = conn
        self.ph = paramstyle

    def _q(self, sql: str) -> str:
        return sql if self.ph == "?" else sql.replace("?", self.ph)

    def _rows(self, sql: str, params=()) -> List[tuple]:
        cur = self.conn.cursor()
        cur.execute(self._q(sql), tuple(params))
        return cur.fetchall()

    def create_schema(self) -> None:
        body = "\n".join(l for l in SCHEMA.splitlines() if not l.strip().startswith("--"))
        for stmt in body.split(";"):
            if stmt.strip():
                self.conn.cursor().execute(stmt)
        self.conn.commit()

    # ---- loading (local dev / tests) ----
    def load_spans(self, spans: Iterable[dict]) -> int:
        groups = group_by_trace(spans)
        cur = self.conn.cursor()
        n = 0
        for tid, lst in groups.items():
            for s in lst:
                attrs = s.get("attributes") or {}
                row = [s.get(c) for c in SPAN_COLS]
                row[SPAN_COLS.index("attributes")] = json.dumps(attrs)
                row[SPAN_COLS.index("received_us")] = attrs.get("sta.received_us")
                row[SPAN_COLS.index("normalized_us")] = attrs.get("sta.normalized_us")
                cur.execute(self._q(
                    f"INSERT OR REPLACE INTO spans ({','.join(SPAN_COLS)}) VALUES ({','.join('?' * len(SPAN_COLS))})"
                    if self.ph == "?" else
                    f"INSERT INTO spans ({','.join(SPAN_COLS)}) VALUES ({','.join('?' * len(SPAN_COLS))}) "
                    "ON CONFLICT (trace_id, span_id) DO UPDATE SET start_us = EXCLUDED.start_us, duration_us = EXCLUDED.duration_us"),
                    row)
                n += 1
            t = trace_summary(tid, lst)
            t["complete"] = int(t["complete"])
            cur.execute(self._q(
                f"INSERT OR REPLACE INTO traces ({','.join(TRACE_COLS)}) VALUES ({','.join('?' * len(TRACE_COLS))})"
                if self.ph == "?" else
                f"INSERT INTO traces ({','.join(TRACE_COLS)}) VALUES ({','.join('?' * len(TRACE_COLS))}) "
                "ON CONFLICT (trace_id) DO UPDATE SET duration_us = EXCLUDED.duration_us, span_count = EXCLUDED.span_count"),
                [t[c] for c in TRACE_COLS])
            for svc in {s["service"] for s in lst}:
                cur.execute(self._q("INSERT OR IGNORE INTO trace_services (trace_id, service) VALUES (?, ?)"
                                    if self.ph == "?" else
                                    "INSERT INTO trace_services (trace_id, service) VALUES (?, ?) ON CONFLICT DO NOTHING"),
                            (tid, svc))
        self.conn.commit()
        return n

    @staticmethod
    def window_id(w: dict) -> str:
        return f"{w['window_start_us']}:{w['window_end_us']}:{w.get('endpoint') or ''}:{w.get('method', 'bottleneck_score')}"

    def load_rankings(self, windows: Iterable[dict]) -> int:
        cur = self.conn.cursor()
        n = 0
        for w in windows:
            wid = self.window_id(w)
            cur.execute(self._q(
                "INSERT OR REPLACE INTO ranking_windows (window_id, window_start_us, window_end_us, endpoint, method, confidence, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)" if self.ph == "?" else
                "INSERT INTO ranking_windows (window_id, window_start_us, window_end_us, endpoint, method, confidence, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (window_id) DO UPDATE SET note = EXCLUDED.note, confidence = EXCLUDED.confidence"),
                (wid, w["window_start_us"], w["window_end_us"], w.get("endpoint"), w.get("method", "bottleneck_score"),
                 w.get("confidence"), w.get("note")))
            cur.execute(self._q("DELETE FROM rankings WHERE window_id = ?"), (wid,))
            for r in w.get("rankings", []):
                cur.execute(self._q("INSERT INTO rankings (window_id, rank, service, score, evidence) VALUES (?, ?, ?, ?, ?)"),
                            (wid, r["rank"], r["service"], r["score"], json.dumps(r.get("evidence") or {})))
                n += 1
        self.conn.commit()
        return n

    # ---- reads ----
    def count_traces(self) -> int:
        return self._rows("SELECT COUNT(*) FROM traces")[0][0]

    def services(self) -> List[dict]:
        rows = self._rows(
            "SELECT service, COUNT(*), COUNT(DISTINCT trace_id), "
            "SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status IN ('ok', 'error') THEN 1 ELSE 0 END) "
            "FROM spans GROUP BY service ORDER BY service")
        # one pass over durations for every service (sorted, so percentiles are a slice)
        durs: dict = {}
        for service, d in self._rows("SELECT service, duration_us FROM spans WHERE duration_us IS NOT NULL "
                                     "ORDER BY service, duration_us"):
            durs.setdefault(service, []).append(d / 1000)
        out = []
        for service, span_count, trace_count, errors, with_status in rows:
            ds = durs.get(service, [])
            out.append({"service": service, "span_count": span_count, "trace_count": trace_count,
                        "error_rate": (errors / with_status) if with_status else 0.0,
                        "p50_ms": percentile(ds, 0.5), "p99_ms": percentile(ds, 0.99)})
        return out

    def operations(self, service: Optional[str] = None) -> List[str]:
        if service:
            rows = self._rows("SELECT DISTINCT operation FROM spans WHERE service = ? ORDER BY operation", (service,))
        else:
            rows = self._rows("SELECT DISTINCT operation FROM spans ORDER BY operation")
        return [r[0] for r in rows]

    def _where(self, q: TraceQuery):
        where, params = [], []
        if q.service:
            where.append("trace_id IN (SELECT trace_id FROM trace_services WHERE service = ?)")
            params.append(q.service)
        if q.operation:
            where.append("LOWER(root_operation) LIKE ?")
            params.append(f"%{q.operation.lower()}%")
        if q.status:
            where.append("status = ?")
            params.append(q.status)
        if q.min_duration_ms is not None:
            where.append("duration_us >= ?")
            params.append(int(q.min_duration_ms * 1000))
        if q.max_duration_ms is not None:
            where.append("duration_us <= ?")
            params.append(int(q.max_duration_ms * 1000))
        if q.start_after_us is not None:
            where.append("start_us >= ?")
            params.append(q.start_after_us)
        if q.start_before_us is not None:
            where.append("start_us <= ?")
            params.append(q.start_before_us)
        if q.complete is not None:
            where.append("complete = ?")
            params.append(int(q.complete))
        return (" WHERE " + " AND ".join(where)) if where else "", params

    def search_traces(self, q: TraceQuery) -> dict:
        where, params = self._where(q)
        total = self._rows(f"SELECT COUNT(*) FROM traces{where}", params)[0][0]
        order = _ORDER.get(q.sort, _ORDER["start_desc"])
        rows = self._rows(f"SELECT {','.join(TRACE_COLS)} FROM traces{where} ORDER BY {order} LIMIT ? OFFSET ?",
                          params + [q.limit, q.offset])
        items = [self._trace_row(r) for r in rows]
        return {"items": items, "total": total, "offset": q.offset, "limit": q.limit}

    @staticmethod
    def _trace_row(r) -> dict:
        t = dict(zip(TRACE_COLS, r))
        t["complete"] = bool(t["complete"])
        return t

    @staticmethod
    def _span_row(r) -> dict:
        s = dict(zip(SPAN_COLS, r))
        s["attributes"] = json.loads(s["attributes"]) if s["attributes"] else {}
        if s.pop("received_us", None) is not None:
            s["attributes"]["sta.received_us"] = r[SPAN_COLS.index("received_us")]
        if s.pop("normalized_us", None) is not None:
            s["attributes"]["sta.normalized_us"] = r[SPAN_COLS.index("normalized_us")]
        return s

    def _spans_for(self, trace_ids: List[str]) -> dict:
        out: dict = {t: [] for t in trace_ids}
        for i in range(0, len(trace_ids), 500):
            chunk = trace_ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in self._rows(f"SELECT {','.join(SPAN_COLS)} FROM spans WHERE trace_id IN ({marks}) "
                                "ORDER BY trace_id, start_us, span_id", chunk):
                out[r[0]].append(self._span_row(r))
        return out

    def get_trace(self, trace_id: str) -> Optional[dict]:
        rows = self._rows(f"SELECT {','.join(TRACE_COLS)} FROM traces WHERE trace_id = ?", (trace_id,))
        if not rows:
            return None
        return {"summary": self._trace_row(rows[0]), "spans": self._spans_for([trace_id])[trace_id]}

    def traces_with_spans(self, endpoint=None, limit=2000, subset="all"):
        where, params = ("", [])
        if endpoint:
            where, params = " WHERE root_operation = ?", [endpoint]
        rows = self._rows(f"SELECT {','.join(TRACE_COLS)} FROM traces{where} ORDER BY start_us DESC LIMIT ?",
                          params + [limit])
        ts = [self._trace_row(r) for r in rows]
        spans = self._spans_for([t["trace_id"] for t in ts])
        return self._pick_subset([(t, spans[t["trace_id"]]) for t in ts], subset)

    def all_spans(self):
        return (self._span_row(r) for r in self._rows(f"SELECT {','.join(SPAN_COLS)} FROM spans"))

    def all_traces(self):
        return (self._trace_row(r) for r in self._rows(f"SELECT {','.join(TRACE_COLS)} FROM traces"))

    def rankings(self, endpoint: Optional[str] = None, limit: int = 10) -> List[dict]:
        sql = "SELECT window_id, window_start_us, window_end_us, endpoint, method, confidence, note FROM ranking_windows"
        params: list = []
        if endpoint:
            sql += " WHERE endpoint = ? OR endpoint IS NULL"
            params.append(endpoint)
        sql += " ORDER BY window_end_us DESC"
        out = []
        for wid, ws, we, ep, method, confidence, note in self._rows(sql, params):
            rows = self._rows("SELECT rank, service, score, evidence FROM rankings WHERE window_id = ? ORDER BY rank LIMIT ?",
                              (wid, limit))
            out.append({"window_start_us": ws, "window_end_us": we, "endpoint": ep, "method": method,
                        "confidence": confidence, "note": note,
                        "rankings": [{"rank": r, "service": svc, "score": sc, "evidence": json.loads(ev) if ev else {}}
                                     for r, svc, sc, ev in rows]})
        return out
