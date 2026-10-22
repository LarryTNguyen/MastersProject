"""Response shapes for the API. Times are integer microseconds, same as the span format."""
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
    state: Optional[str] = None             # OPEN / SETTLED / TIMED_OUT, when the store knows (trace_v1)
    settlement_reason: Optional[str] = None  # QUIET_WINDOW / END_OF_INPUT / HARD_TIMEOUT


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
    confidence: Optional[float] = None  # 0..1 from the diagnosis step, when it reports one
    confidence_label: Optional[str] = None  # LOW / MEDIUM / HIGH / UNAVAILABLE, as the diagnosis step words it
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


class CompareRow(BaseModel):
    service: str
    slow_self_ms_p50: Optional[float] = None
    normal_self_ms_p50: Optional[float] = None
    slow_self_ms_p95: Optional[float] = None
    normal_self_ms_p95: Optional[float] = None
    slow_critical_ms_p50: Optional[float] = None
    normal_critical_ms_p50: Optional[float] = None
    added_ms: float
    ratio: Optional[float] = None
    slow_traces: int
    normal_traces: int


class Compare(BaseModel):
    endpoint: Optional[str] = None
    slow_traces: int
    normal_traces: int
    threshold_ms: Optional[float] = None
    services: List[CompareRow]


class FlameNode(BaseModel):
    name: str
    service: Optional[str] = None
    value: float                 # self time, ms, summed over traces
    total: float                 # value + children
    count: int
    children: List["FlameNode"] = []


class PipelineStats(BaseModel):
    spans: int
    traces: int
    spans_with_stamps: int
    incomplete_traces: int
    incomplete_rate: float
    last_normalized_us: Optional[int] = None
    seconds_since_last: Optional[float] = None
    ingest_rate_per_min: float
    window_s: int
    staleness_ms: Dict[str, Optional[float]]
    lag_ms: Dict[str, Optional[float]]
    series: List[Dict[str, int]]
