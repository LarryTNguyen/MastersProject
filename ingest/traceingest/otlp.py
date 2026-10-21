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
from .spans import STATUS_ERROR, STATUS_OK, STATUS_UNKNOWN, STATUS_UNSET, Span

KIND_TO_OTLP = {None: 0, "internal": 1, "server": 2, "client": 3, "producer": 4, "consumer": 5}
OTLP_TO_KIND = {v: k for k, v in KIND_TO_OTLP.items()}
_KIND_NAMES = {"SPAN_KIND_UNSPECIFIED": 0, "SPAN_KIND_INTERNAL": 1, "SPAN_KIND_SERVER": 2,
               "SPAN_KIND_CLIENT": 3, "SPAN_KIND_PRODUCER": 4, "SPAN_KIND_CONSUMER": 5}
STATUS_TO_OTLP = {STATUS_UNSET: 0, STATUS_UNKNOWN: 0, STATUS_OK: 1, STATUS_ERROR: 2}
OTLP_TO_STATUS = {0: STATUS_UNSET, 1: STATUS_OK, 2: STATUS_ERROR}
_STATUS_NAMES = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}

# reserved attribute keys used to carry fields OTLP has no slot for
A_TRACE = "sta.trace_id"
A_SPAN = "sta.span_id"
A_PARENT = "sta.parent_span_id"
A_SOURCE = "sta.source"
A_NO_DURATION = "sta.duration_missing"
A_PEER = "peer.service"
A_RECORD_KEY = "sta.source_record_key"
A_QUALITY = "sta.quality_flags"
SCOPE_NAME = "traceingest"


# ---------- attribute values ----------
def _encode_value(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, bytes):
        return {"bytesValue": base64.b64encode(v).decode("ascii")}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [_encode_value(x) for x in v]}}
    if isinstance(v, dict):
        return {"kvlistValue": {"values": _attrs_to_list(v)}}
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
        try:
            return base64.b64decode(v["bytesValue"], validate=True)
        except (binascii.Error, ValueError):
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
    """Group spans by resource and instrumentation scope into an OTLP document."""
    groups = OrderedDict()
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
        if s.source_record_key:
            attrs[A_RECORD_KEY] = s.source_record_key
        if s.quality_flags:
            attrs[A_QUALITY] = list(s.quality_flags)
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
        if s.status_message and s.status == STATUS_ERROR:
            span["status"]["message"] = s.status_message
        if s.span_flags is not None:
            span["flags"] = s.span_flags
        if s.trace_state:
            span["traceState"] = s.trace_state
        if s.events:
            span["events"] = [{"name": event["name"], "timeUnixNano": str(event["time_ns"]),
                               "attributes": _attrs_to_list(event.get("attributes", {})),
                               "droppedAttributesCount": event.get("dropped_attributes_count", 0)}
                              for event in s.events]
        if s.links:
            span["links"] = [{"traceId": link["trace_id"], "spanId": link["span_id"],
                              "attributes": _attrs_to_list(link.get("attributes", {})),
                              "flags": link.get("span_flags", 0),
                              "traceState": link.get("trace_state") or "",
                              "droppedAttributesCount": link.get("dropped_attributes_count", 0)}
                             for link in s.links]
        if s.dropped_counts.get("span_attributes"):
            span["droppedAttributesCount"] = s.dropped_counts["span_attributes"]
        if s.dropped_counts.get("events"):
            span["droppedEventsCount"] = s.dropped_counts["events"]
        if s.dropped_counts.get("links"):
            span["droppedLinksCount"] = s.dropped_counts["links"]
        if pid:
            span["parentSpanId"] = pid
        resource_attrs = dict(s.resource_attributes)
        resource_attrs["service.name"] = s.service
        if s.service_namespace:
            resource_attrs["service.namespace"] = s.service_namespace
        if s.service_instance_id:
            resource_attrs["service.instance.id"] = s.service_instance_id
        scope = {"name": s.scope_name or SCOPE_NAME}
        if s.scope_version:
            scope["version"] = s.scope_version
        if s.scope_attributes:
            scope["attributes"] = _attrs_to_list(s.scope_attributes)
        resource_dropped = int(s.dropped_counts.get("resource_attributes", 0))
        scope_dropped = int(s.dropped_counts.get("scope_attributes", 0))
        key = repr((resource_attrs, s.resource_schema_url, resource_dropped,
                    scope, s.scope_schema_url, scope_dropped))
        if key not in groups:
            groups[key] = {"resource_attrs": resource_attrs, "resource_schema": s.resource_schema_url,
                           "resource_dropped": resource_dropped,
                           "scope": scope, "scope_schema": s.scope_schema_url,
                           "scope_dropped": scope_dropped, "spans": []}
        groups[key]["spans"].append(span)
    out = []
    for group in groups.values():
        rs = {"resource": {"attributes": _attrs_to_list(group["resource_attrs"])},
              "scopeSpans": [{"scope": group["scope"], "spans": group["spans"]}]}
        if group["resource_dropped"]:
            rs["resource"]["droppedAttributesCount"] = group["resource_dropped"]
        if group["scope_dropped"]:
            rs["scopeSpans"][0]["scope"]["droppedAttributesCount"] = group["scope_dropped"]
        if group["resource_schema"]:
            rs["schemaUrl"] = group["resource_schema"]
        if group["scope_schema"]:
            rs["scopeSpans"][0]["schemaUrl"] = group["scope_schema"]
        out.append(rs)
    return {"resourceSpans": out}


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
        service = str(res_attrs.pop("service.name", None) or "unknown_service")
        service_namespace = str(res_attrs.pop("service.namespace", "") or "")
        service_instance_id = res_attrs.pop("service.instance.id", None)
        for ss in (rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []):
            scope = ss.get("scope") or ss.get("instrumentationLibrary") or {}
            scope_attrs = _attrs_from_list(scope.get("attributes"))
            for sp in ss.get("spans", []) or []:
                attrs = _attrs_from_list(sp.get("attributes"))
                tid = attrs.pop(A_TRACE, None) or _normalize_id(sp.get("traceId"), 32)
                sid = attrs.pop(A_SPAN, None) or _normalize_id(sp.get("spanId"), 16)
                pid = attrs.pop(A_PARENT, None) or _normalize_id(sp.get("parentSpanId"), 16)
                source = attrs.pop(A_SOURCE, "otlp")
                peer = attrs.pop(A_PEER, None)
                source_record_key = attrs.pop(A_RECORD_KEY, None)
                quality_flags = attrs.pop(A_QUALITY, [])
                no_dur = bool(attrs.pop(A_NO_DURATION, False))
                start_ns = int(sp.get("startTimeUnixNano") or 0)
                end_ns = int(sp.get("endTimeUnixNano") or start_ns)
                start_us = start_ns // 1000
                events = [{"name": event.get("name") or "", "time_ns": int(event.get("timeUnixNano") or 0),
                           "attributes": _attrs_from_list(event.get("attributes")),
                           "dropped_attributes_count": int(event.get("droppedAttributesCount") or 0)}
                          for event in sp.get("events", []) or []]
                links = [{"trace_id": _normalize_id(link.get("traceId"), 32),
                          "span_id": _normalize_id(link.get("spanId"), 16), "identity_kind": "OTEL",
                          "attributes": _attrs_from_list(link.get("attributes")),
                          "span_flags": link.get("flags"), "trace_state": link.get("traceState") or None,
                          "dropped_attributes_count": int(link.get("droppedAttributesCount") or 0)}
                         for link in sp.get("links", []) or []]
                status = _status(sp.get("status"))
                dropped_counts = {key: value for key, value in {
                    "span_attributes": int(sp.get("droppedAttributesCount") or 0),
                    "events": int(sp.get("droppedEventsCount") or 0),
                    "links": int(sp.get("droppedLinksCount") or 0),
                    "resource_attributes": int((rs.get("resource") or {}).get("droppedAttributesCount") or 0),
                    "scope_attributes": int(scope.get("droppedAttributesCount") or 0),
                }.items() if value}
                yield Span(
                    trace_id=tid, span_id=sid, parent_span_id=pid or None,
                    service=service, operation=sp.get("name") or "",
                    start_us=start_us,
                    duration_us=None if no_dur else max(0, end_ns // 1000 - start_us),
                    status=status, kind=_kind(sp.get("kind")),
                    peer_service=peer, source=source, attributes=attrs,
                    service_namespace=service_namespace, service_instance_id=service_instance_id,
                    status_message=(sp.get("status") or {}).get("message") if status == STATUS_ERROR else None,
                    span_flags=sp.get("flags"), trace_state=sp.get("traceState") or None,
                    resource_attributes=dict(res_attrs), resource_schema_url=rs.get("schemaUrl"),
                    scope_name=None if source != "otlp" and scope.get("name") == SCOPE_NAME else scope.get("name"),
                    scope_version=scope.get("version"),
                    scope_attributes=scope_attrs, scope_schema_url=ss.get("schemaUrl"),
                    events=events, links=links,
                    dropped_counts=dropped_counts, quality_flags=quality_flags,
                    source_record_key=source_record_key,
                )


def split_batches(spans: List[Span], max_spans: int) -> Iterator[List[Span]]:
    for i in range(0, len(spans), max_spans):
        yield spans[i:i + max_spans]


def is_otlp_doc(doc) -> bool:
    return isinstance(doc, dict) and "resourceSpans" in doc
