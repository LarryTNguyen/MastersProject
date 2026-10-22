"""Response shapes for the dashboard API.

These are the contract between the FastAPI backend and the dashboard page.
Times are integer microseconds, the same as the common span format.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class SpanOut(BaseModel):
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    service: str
    operation: str
    start_us: int
    duration_us: Optional[int] = None
    status: str = "unset"
    kind: Optional[str] = None
    peer_service: Optional[str] = None
    source: str = "unknown"
    attributes: Dict[str, Any] = {}


class TraceSummary(BaseModel):
    trace_id: str
    root_service: str
    root_operation: str
    start_us: int
    duration_us: int
    span_count: int
    service_count: int
    status: str                 # "ok" | "error" | "unset"
    complete: bool              # every span's parent was found
    source: str


class TraceDetail(BaseModel):
    summary: TraceSummary
    spans: List[SpanOut]


class ServiceStats(BaseModel):
    service: str
    span_count: int
    trace_count: int
    error_rate: float           # 0..1 over spans with a status
    p50_ms: Optional[float] = None
    p99_ms: Optional[float] = None


class Ranking(BaseModel):
    """One ranked bottleneck candidate, as written by the diagnosis step."""
    rank: int
    service: str
    score: float
    evidence: Dict[str, Any] = {}   # free-form: e.g. critical_path_ms, self_time_ratio, error_rate


class RankingWindow(BaseModel):
    window_start_us: int
    window_end_us: int
    endpoint: Optional[str] = None    # root operation the ranking is for, or None for all
    method: str = "bottleneck_score"
    rankings: List[Ranking]
    note: Optional[str] = None        # e.g. "no service clearly explains the slowdown"


class TraceSearchResult(BaseModel):
    items: List[TraceSummary]
    total: int
    offset: int
    limit: int


class UploadTicket(BaseModel):
    url: str
    key: str
    method: str = "PUT"
    headers: Dict[str, str] = {}
    expires_in: int


class Health(BaseModel):
    ok: bool
    store: str
    traces: int
