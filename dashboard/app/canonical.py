"""Turn trace-schema v1 ("canonical") records into the flat span dicts the page uses.

The adapter writes these, and the reconstruction step stores them as JSONB.
The older flat format is still accepted (demo data, old files).
"""
from __future__ import annotations

from typing import Optional

SCHEMA_VERSION = "1.0.0"

_STATUS = {"OK": "ok", "ERROR": "error", "UNSET": "unset", "UNKNOWN": "unset"}
_KIND = {"INTERNAL": "internal", "SERVER": "server", "CLIENT": "client", "PRODUCER": "producer",
         "CONSUMER": "consumer"}


def is_canonical(record: dict) -> bool:
    return "schema_version" in record and "service_name" in record


def _plain(value):
    """Typed attribute values ({"type": ..., "value": ...}) back to plain Python."""
    if isinstance(value, dict) and "type" in value and "value" in value:
        v = value["value"]
        if value["type"] in ("int", "int64") and isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                return v
        if value["type"] == "array" and isinstance(v, list):
            return [_plain(x) for x in v]
        if value["type"] in ("map", "kvlist") and isinstance(v, dict):
            return {k: _plain(x) for k, x in v.items()}
        return v
    return value


def _ns_to_us(value) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value) // 1000


def span_from_record(record: dict) -> dict:
    """Canonical record -> dashboard span dict."""
    source = record.get("source") or {}
    details = source.get("details") or {}
    start = record.get("start_time_ns")
    if start is None:
        # Alibaba call observations keep the replay timestamp in provenance only
        start = details.get("replay_start_ns")
    attrs = {k: _plain(v) for k, v in (record.get("attributes") or {}).items()}
    received = record.get("received_at_unix_ns")
    if received not in (None, "0", 0, ""):
        attrs.setdefault("sta.received_us", int(received) // 1000)
    quality = record.get("quality_flags") or []
    if quality:
        attrs["sta.quality_flags"] = list(quality)
    if record.get("timing_quality"):
        attrs["sta.timing_quality"] = record["timing_quality"]
    status = (record.get("status") or {}).get("code", "UNSET")
    return {
        "trace_id": record["trace_id"],
        "span_id": record["span_id"],
        "parent_span_id": record.get("parent_span_id") or None,
        "service": record["service_name"],
        "operation": record.get("operation_name") or "",
        "start_us": _ns_to_us(start) or 0,
        "duration_us": _ns_to_us(record.get("duration_ns")),
        "status": _STATUS.get(str(status).upper(), "unset"),
        "kind": _KIND.get(str(record.get("span_kind", "UNKNOWN")).upper()),
        "peer_service": details.get("peer_service"),
        "source": details.get("legacy_source") or source.get("format") or "unknown",
        "attributes": attrs,
        "v": 1,
    }


def span_from_any(record: dict) -> dict:
    """Accept either a canonical record or the older common-span shape."""
    if is_canonical(record):
        return span_from_record(record)
    record.setdefault("attributes", {})
    return record
