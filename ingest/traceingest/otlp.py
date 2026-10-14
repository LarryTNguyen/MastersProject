"""OTLP/JSON <-> common spans.

The OTLP JSON encoding (ExportTraceServiceRequest) is what OpenTelemetry SDKs
send to an OTLP/HTTP endpoint with ``Content-Type: application/json``, and it is
the format of uploaded files. IDs are hex strings and 64-bit integers are
encoded as strings, per the OTLP/JSON spec.

Round trip guarantee: ``from_otlp(to_otlp(spans)) == spans`` for any valid
spans, including sources whose IDs are not OTLP-shaped (originals are kept in
``sta.*`` attributes).
"""
from __future__ import annotations

import base64
import binascii
from collections import OrderedDict
from typing import Iterable, Iterator, List, Optional

from .ids import otlp_span_id, otlp_trace_id
from .spans import STATUS_ERROR, STATUS_OK, STATUS_UNSET, Span

KIND_TO_OTLP = {None: 0, "internal": 1, "server": 2, "client": 3, "producer": 4, "consumer": 5}
OTLP_TO_KIND = {v: k for k, v in KIND_TO_OTLP.items()}
_KIND_NAMES = {"SPAN_KIND_UNSPECIFIED": 0, "SPAN_KIND_INTERNAL": 1, "SPAN_KIND_SERVER": 2,
               "SPAN_KIND_CLIENT": 3, "SPAN_KIND_PRODUCER": 4, "SPAN_KIND_CONSUMER": 5}
STATUS_TO_OTLP = {STATUS_UNSET: 0, STATUS_OK: 1, STATUS_ERROR: 2}
OTLP_TO_STATUS = {v: k for k, v in STATUS_TO_OTLP.items()}
_STATUS_NAMES = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}

# reserved attribute keys used to carry fields OTLP has no slot for
A_TRACE = "sta.trace_id"
A_SPAN = "sta.span_id"
A_PARENT = "sta.parent_span_id"
A_SOURCE = "sta.source"
A_NO_DURATION = "sta.duration_missing"
A_PEER = "peer.service"
SCOPE_NAME = "traceingest"


# ---------- attribute values ----------
def _encode_value(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def _decode_value(v: dict):
    if "stringValue" in v:
        return v["stringValue"]
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "arrayValue" in v:
        return [_decode_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return {kv["key"]: _decode_value(kv.get("value", {})) for kv in v["kvlistValue"].get("values", [])}
    if "bytesValue" in v:
        return v["bytesValue"]
    return None


def _attrs_to_list(d: dict) -> List[dict]:
    return [{"key": k, "value": _encode_value(d[k])} for k in sorted(d) if d[k] is not None]


def _attrs_from_list(items: Optional[list]) -> dict:
    return {kv["key"]: _decode_value(kv.get("value", {})) for kv in (items or [])}


# ---------- ids ----------
def _normalize_id(value: Optional[str], length: int) -> Optional[str]:
    """OTLP/JSON uses hex, but some exporters send base64; accept both."""
    if not value:
        return None
    v = value.strip()
    if len(v) == length:
        try:
            int(v, 16)
            return v.lower()
        except ValueError:
            pass
    try:
        raw = base64.b64decode(v, validate=True)
        if len(raw) * 2 == length:
            return binascii.hexlify(raw).decode()
    except (binascii.Error, ValueError):
        pass
    return v


# ---------- encode ----------
def to_otlp(spans: Iterable[Span]) -> dict:
    """Group spans by service into one ExportTraceServiceRequest JSON document."""
    by_service: "OrderedDict[str, List[dict]]" = OrderedDict()
    for s in spans:
        attrs = dict(s.attributes)
        tid = otlp_trace_id(s.trace_id)
        sid = otlp_span_id(s.span_id)
        pid = otlp_span_id(s.parent_span_id) if s.parent_span_id else ""
        if tid != s.trace_id:
            attrs[A_TRACE] = s.trace_id
        if sid != s.span_id:
            attrs[A_SPAN] = s.span_id
        if s.parent_span_id and pid != s.parent_span_id:
            attrs[A_PARENT] = s.parent_span_id
        attrs[A_SOURCE] = s.source
        if s.peer_service:
            attrs[A_PEER] = s.peer_service
        if s.duration_us is None:
            attrs[A_NO_DURATION] = True
        start_ns = s.start_us * 1000
        end_ns = (s.start_us + (s.duration_us or 0)) * 1000
        span = {
            "traceId": tid,
            "spanId": sid,
            "name": s.operation,
            "kind": KIND_TO_OTLP.get(s.kind, 0),
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(end_ns),
            "attributes": _attrs_to_list(attrs),
            "status": {"code": STATUS_TO_OTLP[s.status]},
        }
        if pid:
            span["parentSpanId"] = pid
        by_service.setdefault(s.service, []).append(span)
    return {"resourceSpans": [
        {"resource": {"attributes": _attrs_to_list({"service.name": svc})},
         "scopeSpans": [{"scope": {"name": SCOPE_NAME}, "spans": items}]}
        for svc, items in by_service.items()
    ]}


# ---------- decode ----------
def _kind(v) -> Optional[str]:
    if isinstance(v, str):
        v = _KIND_NAMES.get(v, 0)
    return OTLP_TO_KIND.get(int(v or 0))


def _status(st: Optional[dict]) -> str:
    code = (st or {}).get("code", 0)
    if isinstance(code, str):
        code = _STATUS_NAMES.get(code, 0)
    return OTLP_TO_STATUS.get(int(code), STATUS_UNSET)


def from_otlp(doc: dict) -> Iterator[Span]:
    """Yield common spans from an ExportTraceServiceRequest JSON document."""
    for rs in doc.get("resourceSpans", []) or []:
        res_attrs = _attrs_from_list((rs.get("resource") or {}).get("attributes"))
        service = str(res_attrs.get("service.name") or "unknown_service")
        for ss in (rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []):
            for sp in ss.get("spans", []) or []:
                attrs = _attrs_from_list(sp.get("attributes"))
                tid = attrs.pop(A_TRACE, None) or _normalize_id(sp.get("traceId"), 32)
                sid = attrs.pop(A_SPAN, None) or _normalize_id(sp.get("spanId"), 16)
                pid = attrs.pop(A_PARENT, None) or _normalize_id(sp.get("parentSpanId"), 16)
                source = attrs.pop(A_SOURCE, "otlp")
                peer = attrs.pop(A_PEER, None)
                no_dur = bool(attrs.pop(A_NO_DURATION, False))
                start_ns = int(sp.get("startTimeUnixNano") or 0)
                end_ns = int(sp.get("endTimeUnixNano") or start_ns)
                start_us = start_ns // 1000
                yield Span(
                    trace_id=tid, span_id=sid, parent_span_id=pid or None,
                    service=service, operation=sp.get("name") or "",
                    start_us=start_us,
                    duration_us=None if no_dur else max(0, end_ns // 1000 - start_us),
                    status=_status(sp.get("status")), kind=_kind(sp.get("kind")),
                    peer_service=peer, source=source, attributes=attrs,
                )


def split_batches(spans: List[Span], max_spans: int) -> Iterator[List[Span]]:
    for i in range(0, len(spans), max_spans):
        yield spans[i:i + max_spans]


def is_otlp_doc(doc) -> bool:
    return isinstance(doc, dict) and "resourceSpans" in doc
