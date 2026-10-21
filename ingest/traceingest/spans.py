"""Working span model shared by every input path.

Every source (Alibaba call graphs, RCAEval traces, OTLP uploads, the live
collector) is converted into this one shape before trace reconstruction, so the
stitcher and the diagnosis code never need to know where a span came from.

Times are integer microseconds here because replay scheduling only needs that
resolution.  Normalized output is emitted through :mod:`traceingest.canonical`
using the repository's nanosecond trace-schema contract.
"""
from __future__ import annotations

import json
from dataclasses import MISSING, asdict, dataclass, field, fields
from typing import IO, Iterable, Iterator, List, Optional

SCHEMA_VERSION = 1

STATUS_UNSET = "unset"
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_UNKNOWN = "unknown"
STATUSES = (STATUS_UNSET, STATUS_OK, STATUS_ERROR, STATUS_UNKNOWN)


@dataclass(slots=True)
class Span:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    service: str
    operation: str
    start_us: int
    duration_us: Optional[int]          # None when the source did not record it
    status: str = STATUS_UNSET
    kind: Optional[str] = None           # server / client / internal / producer / consumer
    peer_service: Optional[str] = None   # the calling service, when the source knows it
    source: str = "unknown"              # alibaba / rcaeval / otlp
    attributes: dict = field(default_factory=dict)
    service_namespace: str = ""
    service_instance_id: Optional[str] = None
    status_message: Optional[str] = None
    span_flags: Optional[int] = None
    trace_state: Optional[str] = None
    resource_attributes: dict = field(default_factory=dict)
    resource_schema_url: Optional[str] = None
    scope_name: Optional[str] = None
    scope_version: Optional[str] = None
    scope_attributes: dict = field(default_factory=dict)
    scope_schema_url: Optional[str] = None
    events: list = field(default_factory=list)
    links: list = field(default_factory=list)
    dropped_counts: dict = field(default_factory=dict)
    quality_flags: list = field(default_factory=list)
    source_record_key: Optional[str] = None

    @property
    def end_us(self) -> int:
        return self.start_us + (self.duration_us or 0)

    def to_dict(self) -> dict:
        # Kept for reading old normalized files and for adapter-level golden
        # tests. New normalized output uses canonical.write_jsonl().
        d = {name: getattr(self, name) for name in _LEGACY_FIELDS}
        d["v"] = SCHEMA_VERSION
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Span":
        if "schema_version" in d:
            from .canonical import from_record
            return from_record(d)
        kw = {k: d.get(k, _DEFAULTS.get(k)) for k in _FIELDS}
        for name in ("attributes", "resource_attributes", "scope_attributes", "dropped_counts"):
            if kw[name] is None:
                kw[name] = {}
        for name in ("events", "links", "quality_flags"):
            if kw[name] is None:
                kw[name] = []
        return cls(**kw)

    def sort_key(self):
        return (self.trace_id, self.start_us, self.span_id)


_FIELDS = tuple(f.name for f in fields(Span))
_LEGACY_FIELDS = ("trace_id", "span_id", "parent_span_id", "service", "operation",
                  "start_us", "duration_us", "status", "kind", "peer_service",
                  "source", "attributes")
_DEFAULTS = {f.name: (f.default if f.default is not MISSING else None) for f in fields(Span)}
for _name in ("attributes", "resource_attributes", "scope_attributes", "dropped_counts",
              "events", "links", "quality_flags"):
    _DEFAULTS[_name] = None   # from_dict creates a fresh collection


def validate(span: Span) -> List[str]:
    """Return a list of problems (empty when the span is usable)."""
    problems = []
    if not span.trace_id:
        problems.append("missing trace_id")
    if not span.span_id:
        problems.append("missing span_id")
    if span.parent_span_id == span.span_id:
        problems.append("span is its own parent")
    if not span.service:
        problems.append("missing service")
    if not isinstance(span.start_us, int):
        problems.append("start_us must be an integer")
    if span.duration_us is not None and (not isinstance(span.duration_us, int) or span.duration_us < 0):
        problems.append("duration_us must be a non-negative integer or null")
    if span.status not in STATUSES:
        problems.append(f"unknown status {span.status!r}")
    return problems


def write_jsonl(spans: Iterable[Span], fh: IO[str]) -> int:
    n = 0
    for s in spans:
        fh.write(json.dumps(s.to_dict(), sort_keys=True, separators=(",", ":")))
        fh.write("\n")
        n += 1
    return n


def read_jsonl(fh: IO[str]) -> Iterator[Span]:
    for line in fh:
        line = line.strip()
        if line:
            yield Span.from_dict(json.loads(line))
