"""RCAEval (RE2 / RE3) traces -> common spans, plus each case's ground truth.

RCAEval case folders are named ``{benchmark}_{service}_{fault}_{instance}``
(for example ``re2ob_checkoutservice_delay_1``) and contain ``traces.csv`` (or
``traces.parquet``) exported from Jaeger, plus ``inject_time.txt`` with the
fault injection time in unix seconds. The columns we use are the ones RCAEval's
own trace methods read: traceID, spanID, parentSpanID, serviceName,
operationName, methodName (optional), startTime and duration (microseconds),
and a status code column when the dataset has one.
"""
from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import asdict, dataclass, field
from typing import IO, Dict, Iterable, List, Optional

from .spans import STATUS_ERROR, STATUS_OK, STATUS_UNSET, Span

SOURCE = "rcaeval"

_ALIASES = {
    "trace_id": ("traceid", "trace_id"),
    "span_id": ("spanid", "span_id"),
    "parent_span_id": ("parentspanid", "parent_span_id", "parentid", "parent_id"),
    "service": ("servicename", "service_name", "service"),
    "operation": ("operationname", "operation_name", "operation"),
    "method": ("methodname", "method_name"),
    "start": ("starttime", "start_time", "start"),
    "duration": ("duration",),
    "status": ("statuscode", "status_code", "http.status_code", "status", "grpc.status_code"),
    "error": ("error",),
}
_REQUIRED = ("trace_id", "span_id", "service", "operation", "start", "duration")
_NULL_PARENTS = {"", "0", "nan", "none", "null", "0000000000000000"}


@dataclass
class RCAEvalStats:
    rows: int = 0
    spans: int = 0
    bad_rows: int = 0
    errors: int = 0
    traces: int = 0
    problems: List[str] = field(default_factory=list)


@dataclass
class CaseLabel:
    """Ground truth for one RCAEval failure case."""
    case: str
    benchmark: str
    root_cause_service: str
    fault: str
    instance: str
    inject_time_s: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_columns(header: Iterable[str]) -> Dict[str, str]:
    lower = {h.strip().lower(): h for h in header}
    cols = {}
    for name, aliases in _ALIASES.items():
        for a in aliases:
            if a in lower:
                cols[name] = lower[a]
                break
    missing = [c for c in _REQUIRED if c not in cols]
    if missing:
        raise ValueError(f"not an RCAEval trace file, missing columns: {missing}")
    return cols


def looks_like_rcaeval(header: Iterable[str]) -> bool:
    try:
        resolve_columns(header)
        return True
    except ValueError:
        return False


def to_us(value: float) -> int:
    """Jaeger exports microseconds since the epoch; accept ms or ns too."""
    if value > 1e17:        # nanoseconds
        return int(round(value / 1000))
    if 1e11 < value < 1e14:  # milliseconds since the epoch
        return int(round(value * 1000))
    return int(round(value))


def duration_to_us(value: float, start_raw: float) -> int:
    if start_raw > 1e17:
        return int(round(value / 1000))
    if 1e11 < start_raw < 1e14:
        return int(round(value * 1000))
    return int(round(value))


def status_from(code: Optional[str], error_flag: Optional[str]) -> str:
    if error_flag is not None and str(error_flag).strip().lower() in ("true", "1"):
        return STATUS_ERROR
    if code is None:
        return STATUS_UNSET
    c = str(code).strip()
    if c == "" or c.lower() == "nan":
        return STATUS_UNSET
    if c.upper() in ("ERROR", "STATUS_CODE_ERROR"):
        return STATUS_ERROR
    if c.upper() in ("OK", "STATUS_CODE_OK", "UNSET", "STATUS_CODE_UNSET"):
        return STATUS_OK if "OK" in c.upper() else STATUS_UNSET
    try:
        n = int(float(c))
    except ValueError:
        return STATUS_UNSET
    if n >= 100:                    # HTTP status
        return STATUS_ERROR if n >= 500 else STATUS_OK
    return STATUS_OK if n == 0 else STATUS_ERROR   # gRPC status


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or str(v).strip() == ""


def spans_from_rows(rows: Iterable[dict], cols: Dict[str, str],
                    stats: Optional[RCAEvalStats] = None) -> List[Span]:
    stats = stats if stats is not None else RCAEvalStats()
    spans: List[Span] = []
    traces = set()
    for i, row in enumerate(rows):
        stats.rows += 1
        try:
            trace_id = str(row[cols["trace_id"]]).strip()
            span_id = str(row[cols["span_id"]]).strip()
            start_raw = float(row[cols["start"]])
            dur_raw = float(row[cols["duration"]])
        except (KeyError, TypeError, ValueError):
            stats.bad_rows += 1
            if len(stats.problems) < 20:
                stats.problems.append(f"row {i}: unreadable")
            continue
        if not trace_id or not span_id or math.isnan(start_raw) or math.isnan(dur_raw):
            stats.bad_rows += 1
            continue
        parent = row.get(cols["parent_span_id"]) if "parent_span_id" in cols else None
        parent = None if _blank(parent) or str(parent).strip().lower() in _NULL_PARENTS else str(parent).strip()
        method = row.get(cols["method"]) if "method" in cols else None
        operation = str(method).strip() if not _blank(method) else str(row[cols["operation"]]).strip()
        code = row.get(cols["status"]) if "status" in cols else None
        status = status_from(None if _blank(code) else code,
                             row.get(cols["error"]) if "error" in cols else None)
        if status == STATUS_ERROR:
            stats.errors += 1
        attrs = {}
        if not _blank(code):
            attrs["rcaeval.status_code"] = str(code).strip()
        if not _blank(method):
            attrs["rcaeval.operation_name"] = str(row[cols["operation"]]).strip()
        spans.append(Span(
            trace_id=trace_id, span_id=span_id, parent_span_id=parent,
            service=str(row[cols["service"]]).strip(), operation=operation,
            start_us=to_us(start_raw), duration_us=max(0, duration_to_us(dur_raw, start_raw)),
            status=status, kind=None, peer_service=None, source=SOURCE, attributes=attrs,
        ))
        traces.add(trace_id)
    spans.sort(key=Span.sort_key)
    stats.spans = len(spans)
    stats.traces = len(traces)
    return spans


def read_rcaeval_csv(fh: IO[str], stats: Optional[RCAEvalStats] = None) -> List[Span]:
    reader = csv.DictReader(fh)
    cols = resolve_columns(reader.fieldnames or [])
    return spans_from_rows(reader, cols, stats)


def read_rcaeval_parquet(path: str, stats: Optional[RCAEvalStats] = None) -> List[Span]:
    try:
        import pandas as pd  # optional: only needed for the Hugging Face Parquet copy
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("reading traces.parquet needs pandas and pyarrow installed") from e
    df = pd.read_parquet(path)
    cols = resolve_columns(df.columns)
    return spans_from_rows(df.to_dict("records"), cols, stats)


def parse_case_name(name: str) -> CaseLabel:
    """``re2ob_checkoutservice_delay_1`` -> benchmark re2ob, service checkoutservice, ..."""
    parts = os.path.basename(os.path.normpath(name)).split("_")
    if len(parts) < 4:
        raise ValueError(f"case name {name!r} is not benchmark_service_fault_instance")
    return CaseLabel(case="_".join(parts), benchmark=parts[0], root_cause_service="_".join(parts[1:-2]),
                     fault=parts[-2], instance=parts[-1])


def read_case(case_dir: str, stats: Optional[RCAEvalStats] = None):
    """Return (spans, label) for one RCAEval case directory."""
    label = parse_case_name(case_dir)
    inject = os.path.join(case_dir, "inject_time.txt")
    if os.path.exists(inject):
        with open(inject) as f:
            label.inject_time_s = int(float(f.read().strip().splitlines()[0]))
    pq = os.path.join(case_dir, "traces.parquet")
    csv_path = os.path.join(case_dir, "traces.csv")
    if os.path.exists(pq):
        spans = read_rcaeval_parquet(pq, stats)
    elif os.path.exists(csv_path):
        with open(csv_path, newline="") as f:
            spans = read_rcaeval_csv(f, stats)
    else:
        raise FileNotFoundError(f"no traces.csv or traces.parquet in {case_dir} (RE1 cases have no traces)")
    return spans, label


def write_label(label: CaseLabel, path: str) -> None:
    with open(path, "w") as f:
        json.dump(label.to_dict(), f, indent=2, sort_keys=True)
