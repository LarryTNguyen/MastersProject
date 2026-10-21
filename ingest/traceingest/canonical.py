"""Bridge between ingestion's working model and trace schema v1 records.

Adapters keep a compact :class:`Span` while parsing and replaying.  The
normalized JSONL handed to reconstruction uses the richer, explicit v1 schema.
This module is the only place where those representations are translated.
"""
from __future__ import annotations

import base64
import json
import math
import re
import uuid
from dataclasses import dataclass
from typing import IO, Iterable, Iterator, Optional

from .spans import Span, validate

SCHEMA_VERSION = "1.0.0"
ADAPTER_VERSION = "traceingest-1"
RUN_NAMESPACE = uuid.UUID("9c324ad0-41dc-4a19-b2de-3c8794611e49")
_OTEL_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_OTEL_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")


@dataclass(frozen=True)
class NormalizationContext:
    """Metadata shared by every record produced from one input object/run."""

    run_id: str
    received_at_unix_ns: str = "0"
    raw_reference: Optional[str] = None
    adapter_version: str = ADAPTER_VERSION

    def __post_init__(self):
        try:
            uuid.UUID(self.run_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError("run_id must be a UUID") from exc
        received = int(self.received_at_unix_ns)
        if received < 0 or received > 2**63 - 1:
            raise ValueError("received_at_unix_ns must fit a non-negative PostgreSQL BIGINT")
        if not self.adapter_version:
            raise ValueError("adapter_version must not be empty")

    @classmethod
    def for_source(cls, source_reference: str, run_id: Optional[str] = None,
                   received_at_unix_ns: int = 0) -> "NormalizationContext":
        rid = run_id or str(uuid.uuid5(RUN_NAMESPACE, source_reference))
        return cls(rid, str(received_at_unix_ns), source_reference)


def _typed(value):
    if value is None:
        return {"type": "empty", "value": None}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        if value < -(2**63) or value > 2**63 - 1:
            raise ValueError("attribute integer must fit signed int64")
        return {"type": "int", "value": str(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("attribute double must be finite")
        return {"type": "double", "value": value}
    if isinstance(value, bytes):
        return {"type": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return {"type": "array", "value": [_typed(item) for item in value]}
    if isinstance(value, dict):
        return {"type": "map", "value": {str(k): _typed(v) for k, v in value.items()}}
    return {"type": "string", "value": str(value)}


def _plain(value):
    kind, data = value["type"], value["value"]
    if kind == "int":
        return int(data)
    if kind == "bytes":
        return base64.b64decode(data)
    if kind == "array":
        return [_plain(item) for item in data]
    if kind == "map":
        return {key: _plain(item) for key, item in data.items()}
    return data


def _typed_map(values: dict) -> dict:
    return {str(key): _typed(value) for key, value in values.items()}


def _plain_map(values: dict) -> dict:
    return {key: _plain(value) for key, value in values.items()}


def _is_otel_identity(span: Span) -> bool:
    ids = [span.trace_id, span.span_id]
    if span.parent_span_id:
        ids.append(span.parent_span_id)
    return (bool(_OTEL_TRACE_ID.fullmatch(ids[0])) and ids[0] != "0" * 32
            and all(bool(_OTEL_SPAN_ID.fullmatch(value)) and value != "0" * 16
                    for value in ids[1:]))


def to_record(span: Span, context: NormalizationContext) -> dict:
    """Convert one valid working span to a canonical trace-schema record."""
    problems = validate(span)
    if problems:
        raise ValueError("invalid span: " + "; ".join(problems))

    source = span.source or "unknown"
    alibaba = source == "alibaba"
    identity_kind = "OTEL" if _is_otel_identity(span) and not alibaba else "DATASET"
    record_kind = "CALL_OBSERVATION" if alibaba else "SPAN"
    parent_relation = "ROOT" if span.parent_span_id is None else ("INFERRED" if alibaba else "EXPLICIT")

    # Alibaba's call-graph timestamp is useful for replay ordering but is not a
    # verified operation interval. Keep it in provenance and publish only its
    # response time as duration-only evidence.
    details = {"legacy_source": source, "replay_start_ns": str(span.start_us * 1000)}
    if span.peer_service is not None:
        details["peer_service"] = span.peer_service
    if alibaba:
        start_ns = end_ns = None
        duration_ns = None if span.duration_us is None else str(span.duration_us * 1000)
        time_domain = "UNKNOWN"
        timing_quality = "UNKNOWN" if duration_ns is None else "DURATION_ONLY"
    else:
        start_ns = str(span.start_us * 1000)
        duration_ns = None if span.duration_us is None else str(span.duration_us * 1000)
        end_ns = None if duration_ns is None else str(int(start_ns) + int(duration_ns))
        if end_ns is None:
            start_ns = None
            time_domain, timing_quality = "UNKNOWN", "UNKNOWN"
        else:
            time_domain = "UNIX"
            timing_quality = "MEASURED" if source == "otlp" else "DERIVED"

    status_code = span.status.upper()
    if alibaba and status_code == "UNSET":
        status_code = "UNKNOWN"
    service_resource = dict(span.resource_attributes)
    service_resource.setdefault("service.name", span.service)
    if span.service_namespace:
        service_resource.setdefault("service.namespace", span.service_namespace)
    if span.service_instance_id:
        service_resource.setdefault("service.instance.id", span.service_instance_id)

    quality_flags = list(dict.fromkeys(span.quality_flags))
    if context.received_at_unix_ns == "0":
        quality_flags.append("ingestion_time_unavailable")
    if alibaba:
        quality_flags.extend(["call_observation", "missing_interval", "unknown_error_status"])
    quality_flags = list(dict.fromkeys(quality_flags))

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": context.run_id,
        "identity_kind": identity_kind,
        "record_kind": record_kind,
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id,
        "parent_relation": parent_relation,
        "service_namespace": span.service_namespace,
        "service_name": span.service,
        "service_instance_id": span.service_instance_id,
        "operation_name": span.operation or None,
        "span_kind": (span.kind or "UNKNOWN").upper(),
        "start_time_ns": start_ns,
        "end_time_ns": end_ns,
        "duration_ns": duration_ns,
        "time_domain": time_domain,
        "timing_quality": timing_quality,
        "status": {"code": status_code, "message": span.status_message if status_code == "ERROR" else None},
        "span_flags": span.span_flags,
        "trace_state": span.trace_state,
        "attributes": _typed_map(span.attributes),
        "resource": {"attributes": _typed_map(service_resource), "schema_url": span.resource_schema_url},
        "scope": {"name": span.scope_name, "version": span.scope_version,
                  "attributes": _typed_map(span.scope_attributes), "schema_url": span.scope_schema_url},
        "events": [{"name": event["name"], "time_ns": str(event["time_ns"]),
                    "attributes": _typed_map(event.get("attributes", {})),
                    "dropped_attributes_count": event.get("dropped_attributes_count", 0)}
                   for event in span.events],
        "links": [{"trace_id": link["trace_id"], "span_id": link["span_id"],
                   "identity_kind": link.get("identity_kind", "OTEL"),
                   "attributes": _typed_map(link.get("attributes", {})),
                   "span_flags": link.get("span_flags"), "trace_state": link.get("trace_state"),
                   "dropped_attributes_count": link.get("dropped_attributes_count", 0)}
                  for link in span.links],
        "dropped_counts": {key: int(span.dropped_counts.get(key, 0)) for key in
                           ("span_attributes", "events", "links", "resource_attributes", "scope_attributes")},
        "received_at_unix_ns": context.received_at_unix_ns,
        "source": {"format": source, "adapter_version": context.adapter_version,
                   "record_key": span.source_record_key or span.span_id,
                   "raw_reference": context.raw_reference, "details": details},
        "quality_flags": quality_flags,
    }


def from_record(record: dict) -> Span:
    """Rehydrate a working span from canonical JSONL for replay or inspection."""
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported canonical schema version {record.get('schema_version')!r}")
    details = record.get("source", {}).get("details", {})
    start = record.get("start_time_ns") or details.get("replay_start_ns")
    duration = record.get("duration_ns")
    status = record.get("status", {}).get("code", "UNKNOWN").lower()
    resource = _plain_map(record.get("resource", {}).get("attributes", {}))
    resource.pop("service.name", None)
    resource.pop("service.namespace", None)
    resource.pop("service.instance.id", None)
    scope = record.get("scope", {})
    return Span(
        trace_id=record["trace_id"], span_id=record["span_id"],
        parent_span_id=record.get("parent_span_id"), service=record["service_name"],
        operation=record.get("operation_name") or "", start_us=int(start or 0) // 1000,
        duration_us=None if duration is None else int(duration) // 1000,
        status=status, kind=None if record.get("span_kind") == "UNKNOWN" else record["span_kind"].lower(),
        peer_service=details.get("peer_service"), source=details.get("legacy_source", record["source"]["format"]),
        attributes=_plain_map(record.get("attributes", {})),
        service_namespace=record.get("service_namespace", ""),
        service_instance_id=record.get("service_instance_id"),
        status_message=record.get("status", {}).get("message"),
        span_flags=record.get("span_flags"), trace_state=record.get("trace_state"),
        resource_attributes=resource, resource_schema_url=record.get("resource", {}).get("schema_url"),
        scope_name=scope.get("name"), scope_version=scope.get("version"),
        scope_attributes=_plain_map(scope.get("attributes", {})), scope_schema_url=scope.get("schema_url"),
        events=[{"name": e["name"], "time_ns": int(e["time_ns"]),
                 "attributes": _plain_map(e.get("attributes", {})),
                 "dropped_attributes_count": e.get("dropped_attributes_count", 0)}
                for e in record.get("events", [])],
        links=[{"trace_id": link["trace_id"], "span_id": link["span_id"],
                "identity_kind": link.get("identity_kind", "OTEL"),
                "attributes": _plain_map(link.get("attributes", {})),
                "span_flags": link.get("span_flags"), "trace_state": link.get("trace_state"),
                "dropped_attributes_count": link.get("dropped_attributes_count", 0)}
               for link in record.get("links", [])],
        dropped_counts={key: value for key, value in record.get("dropped_counts", {}).items() if value},
        quality_flags=record.get("quality_flags", []),
        source_record_key=record.get("source", {}).get("record_key"),
    )


def write_jsonl(spans: Iterable[Span], fh: IO[str], context: NormalizationContext) -> int:
    count = 0
    for span in spans:
        fh.write(json.dumps(to_record(span, context), sort_keys=True, separators=(",", ":")))
        fh.write("\n")
        count += 1
    return count


def read_jsonl(fh: IO[str]) -> Iterator[Span]:
    for line in fh:
        if line.strip():
            yield from_record(json.loads(line))

