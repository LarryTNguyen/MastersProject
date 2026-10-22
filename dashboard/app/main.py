"""Dashboard API.

Routes (all JSON, under /api):
  GET  /api/health
  GET  /api/services                      per-service counts, error rate, p50/p99
  GET  /api/operations?service=           operation names for the filter dropdown
  GET  /api/traces?...                    trace search (see TraceQuery for filters)
  GET  /api/traces/{trace_id}             one trace with all its spans
  GET  /api/rankings?endpoint=&limit=     bottleneck rankings per window
  POST /api/uploads                       presigned PUT URL for the raw bucket
  GET  /                                  the dashboard page

Configuration (environment):
  STORE=jsonl|sqlite        default jsonl
  DATA_DIR=./data           folder of normalized *.jsonl (+ rankings.json) for the jsonl store
  SQLITE_PATH=./traces.db   for the sqlite store
  RAW_BUCKET, RAW_PREFIX    where uploads go (presign needs these plus AWS credentials)
  UPLOAD_EXPIRES=900        seconds a presigned URL stays valid

Run locally:  uvicorn app.main:app --reload
"""
from __future__ import annotations

import os
import re
import time
import uuid
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import models
from .store import JsonlStore, Store, TraceQuery

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

app = FastAPI(title="Trace dashboard API", version="0.1.0")
_store: Optional[Store] = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = make_store()
    return _store


def make_store() -> Store:
    kind = os.environ.get("STORE", "jsonl")
    if kind == "sqlite":
        import sqlite3
        from .sql_store import SqlStore
        path = os.environ.get("SQLITE_PATH", "traces.db")
        s = SqlStore(sqlite3.connect(path, check_same_thread=False))
        s.create_schema()
        return s
    return JsonlStore(os.environ.get("DATA_DIR", "data"))


def use_store(store: Store) -> None:
    """Swap the store (tests)."""
    global _store
    _store = store


@app.get("/api/health", response_model=models.Health)
def health():
    s = get_store()
    return {"ok": True, "store": s.name, "traces": s.count_traces()}


@app.get("/api/services", response_model=List[models.ServiceStats])
def services():
    return get_store().services()


@app.get("/api/operations", response_model=List[str])
def operations(service: Optional[str] = None):
    return get_store().operations(service)


@app.get("/api/traces", response_model=models.TraceSearchResult)
def search_traces(
    service: Optional[str] = None,
    operation: Optional[str] = None,
    status: Optional[str] = Query(None, pattern="^(ok|error|unset)$"),
    min_duration_ms: Optional[float] = Query(None, ge=0),
    max_duration_ms: Optional[float] = Query(None, ge=0),
    start_after_us: Optional[int] = None,
    start_before_us: Optional[int] = None,
    complete: Optional[bool] = None,
    sort: str = Query("start_desc", pattern="^(start|duration)_(asc|desc)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
):
    q = TraceQuery(service=service, operation=operation, status=status, min_duration_ms=min_duration_ms,
                   max_duration_ms=max_duration_ms, start_after_us=start_after_us,
                   start_before_us=start_before_us, complete=complete, sort=sort, offset=offset, limit=limit)
    return get_store().search_traces(q)


@app.get("/api/traces/{trace_id}", response_model=models.TraceDetail)
def get_trace(trace_id: str):
    t = get_store().get_trace(trace_id)
    if t is None:
        raise HTTPException(404, f"trace {trace_id} not found")
    return t


@app.get("/api/rankings", response_model=List[models.RankingWindow])
def rankings(endpoint: Optional[str] = None, limit: int = Query(10, ge=1, le=100)):
    return get_store().rankings(endpoint, limit)


class UploadRequest(BaseModel):
    filename: str
    content_type: str = "application/json"


@app.post("/api/uploads", response_model=models.UploadTicket)
def presign_upload(req: UploadRequest):
    bucket = os.environ.get("RAW_BUCKET")
    if not bucket:
        raise HTTPException(503, "uploads are not configured on this deployment (RAW_BUCKET is not set)")
    if req.content_type not in ("application/json", "text/csv", "application/x-ndjson", "application/octet-stream"):
        raise HTTPException(400, "unsupported content type")
    safe = SAFE_NAME.sub("_", os.path.basename(req.filename))[:120] or "upload"
    key = f"{os.environ.get('RAW_PREFIX', 'uploads/').rstrip('/')}/{time.strftime('%Y/%m/%d')}/{uuid.uuid4().hex[:8]}-{safe}"
    expires = int(os.environ.get("UPLOAD_EXPIRES", "900"))
    url = _presign_put(bucket, key, req.content_type, expires)
    return {"url": url, "key": key, "method": "PUT", "headers": {"Content-Type": req.content_type}, "expires_in": expires}


def _presign_put(bucket: str, key: str, content_type: str, expires: int) -> str:
    import boto3  # present in the Lambda runtime; pip install boto3 locally
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION"))
    return s3.generate_presigned_url("put_object",
                                     Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
                                     ExpiresIn=expires)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.exception_handler(Exception)
async def unhandled(request, exc):
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})
