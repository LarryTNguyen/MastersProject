"""Validate canonical records with JSON Schema plus cross-field invariants."""
import argparse
import base64
import json
import math
import re
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent
MAX_I64 = 2**63 - 1
MIN_I64 = -(2**63)
SCHEMA = json.loads((ROOT / "canonical-span.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())

def valid_otel_id(value, length):
    return bool(re.fullmatch(r"[0-9a-f]{%d}" % length, value)) and int(value, 16) != 0

def validate_record(record):
    errors = [f"{'/'.join(map(str, e.absolute_path)) or '<record>'}: {e.message}" for e in VALIDATOR.iter_errors(record)]
    if errors:
        return errors

    def check(condition, message):
        if not condition:
            errors.append(message)

    def timestamp(value, label):
        if value is not None:
            check(0 <= int(value) <= MAX_I64, f"{label}: exceeds signed BIGINT range")

    def database_values(node, path):
        if isinstance(node, str):
            check('\x00' not in node and not any(0xD800 <= ord(c) <= 0xDFFF for c in node),
                  f"{path}: string cannot be represented in PostgreSQL UTF-8 JSONB")
        elif isinstance(node, float):
            check(math.isfinite(node), f"{path}: nonfinite JSON number")
        elif isinstance(node, dict):
            for key, value in node.items():
                database_values(key, f"{path}/<key>")
                database_values(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                database_values(value, f"{path}/{index}")
    database_values(record, 'record')

    for key in ["start_time_ns", "end_time_ns", "duration_ns", "received_at_unix_ns"]:
        timestamp(record[key], key)
    if record["identity_kind"] == "OTEL":
        for key, length in [("trace_id", 32), ("span_id", 16), ("parent_span_id", 16)]:
            if record[key] is not None:
                check(valid_otel_id(record[key], length), f"{key}: invalid OpenTelemetry ID")
        check(record["record_kind"] == "SPAN", "OTEL identity requires SPAN record kind")
    check(record["parent_span_id"] != record["span_id"], "span cannot be its own parent")
    relation = record["parent_relation"]
    check((relation in ["ROOT", "UNKNOWN"]) == (record["parent_span_id"] is None), "parent_relation conflicts with parent_span_id")
    if record["status"]["code"] != "ERROR":
        check(record["status"]["message"] is None, "status.message is only valid for ERROR")
    start, end, duration = (record[k] for k in ["start_time_ns", "end_time_ns", "duration_ns"])
    check((start is None) == (end is None), "start and end must both be present or both be null")
    has_interval = start is not None and end is not None
    if has_interval:
        check(int(end) >= int(start), "end precedes start")
        check(duration is not None and int(duration) == int(end) - int(start), "duration differs from end minus start")
        check(record["time_domain"] != "UNKNOWN", "interval requires a known time domain")
    else:
        check(record["time_domain"] == "UNKNOWN", "records without intervals use UNKNOWN time domain")
    timing = record["timing_quality"]
    check((timing in ["MEASURED", "DERIVED", "SYNTHETIC"]) == has_interval, "timing_quality conflicts with interval availability")
    if timing == "DURATION_ONLY":
        check(duration is not None, "DURATION_ONLY requires a duration")
    if timing == "UNKNOWN":
        check(duration is None, "UNKNOWN timing cannot carry a duration")

    def values(node, path):
        if isinstance(node, dict):
            if set(node) == {"type", "value"}:
                typ, v = node["type"], node["value"]
                if typ == "int":
                    check(MIN_I64 <= int(v) <= MAX_I64, f"{path}: int outside signed int64")
                elif typ == "double":
                    check(math.isfinite(v), f"{path}: double must be finite")
                elif typ == "bytes":
                    try:
                        decoded = base64.b64decode(v, validate=True)
                        check(base64.b64encode(decoded).decode() == v, f"{path}: noncanonical base64")
                    except ValueError:
                        errors.append(f"{path}: invalid base64")
            for k, v in node.items():
                values(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                values(v, f"{path}/{i}")

    for name in ["attributes", "resource", "scope", "events", "links"]:
        values(record[name], name)
    for event in record["events"]:
        timestamp(event["time_ns"], "event.time_ns")
        check(record["time_domain"] != "UNKNOWN", "events require the record's known time domain")
    for link in record["links"]:
        if link["identity_kind"] == "OTEL":
            check(valid_otel_id(link["trace_id"], 32), "invalid linked trace ID")
            check(valid_otel_id(link["span_id"], 16), "invalid linked span ID")
    for key, top in [("service.name", "service_name"), ("service.namespace", "service_namespace"), ("service.instance.id", "service_instance_id")]:
        resource_value = record["resource"]["attributes"].get(key)
        if resource_value is not None:
            check(resource_value == {"type": "string", "value": record[top]}, f"resource {key} differs from {top}")
    return errors

def trace_quality(records):
    """Inspect one trace without inventing missing parents or blocking edges."""
    if not records:
        return {"topology": "UNKNOWN", "root_count": 0, "missing_parent_ids": [], "cycle": False,
                "inferred_parent_count": 0, "self_time_ready": False, "critical_path_ready": False}
    if len({(s["run_id"], s["trace_id"]) for s in records}) != 1:
        raise ValueError("trace_quality expects one run and one trace")
    if len({s["span_id"] for s in records}) != len(records):
        raise ValueError("deduplicate before computing trace quality")
    nodes = {s["span_id"]: s for s in records}
    roots = [s for s in records if s["parent_relation"] == "ROOT"]
    missing = sorted({s["parent_span_id"] for s in records if s["parent_span_id"] is not None and s["parent_span_id"] not in nodes})
    unknown = any(s["parent_relation"] == "UNKNOWN" for s in records)
    done, cycle = set(), False
    for sid in nodes:
        path = set()
        current = sid
        while current in nodes and current not in done:
            if current in path:
                cycle = True
                break
            path.add(current)
            current = nodes[current]["parent_span_id"]
        done.update(path)
    topology = "INVALID" if cycle or len(roots) > 1 else "UNKNOWN" if unknown else "INCOMPLETE" if missing or len(roots) != 1 else "CONNECTED"
    measured = all(s["record_kind"] == "SPAN" and s["timing_quality"] == "MEASURED" for s in records)
    same_domain = len({s["time_domain"] for s in records}) == 1
    inferred = sum(s["parent_relation"] == "INFERRED" for s in records)
    out_of_bounds = 0
    if measured and same_domain:
        for child in records:
            parent = nodes.get(child['parent_span_id'])
            if parent is not None and (int(child['start_time_ns']) < int(parent['start_time_ns'])
                                       or int(child['end_time_ns']) > int(parent['end_time_ns'])):
                out_of_bounds += 1
    return {"topology": topology, "root_count": len(roots), "missing_parent_ids": missing, "cycle": cycle,
            "inferred_parent_count": inferred, "out_of_bounds_child_count": out_of_bounds,
            "self_time_ready": topology == "CONNECTED" and measured and same_domain and inferred == 0 and out_of_bounds == 0,
            "critical_path_ready": False}

def observed_self_time_ns(parent, children):
    """Residual wall time after the UNION of clipped immediate-child intervals.

    This is neither CPU time nor proven blocking wait attribution.
    """
    if parent["timing_quality"] != "MEASURED" or parent["record_kind"] != "SPAN":
        raise ValueError("self-time requires measured span intervals")
    start, end = int(parent["start_time_ns"]), int(parent["end_time_ns"])
    intervals = []
    for child in children:
        if child["parent_span_id"] != parent["span_id"] or (child["run_id"], child["trace_id"]) != (parent["run_id"], parent["trace_id"]):
            raise ValueError("only immediate children in the same trace are valid")
        if child["timing_quality"] != "MEASURED" or child["time_domain"] != parent["time_domain"]:
            raise ValueError("children require measured intervals in the same time domain")
        a, b = max(start, int(child["start_time_ns"])), min(end, int(child["end_time_ns"]))
        if b > a:
            intervals.append((a, b))
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return end - start - sum(b - a for a, b in merged)

def load_records(path):
    def reject_constant(value):
        raise ValueError(f"Nonstandard JSON number: {value}")
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    content = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject_constant, object_pairs_hook=reject_duplicates)
    return content if isinstance(content, list) else [content]

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()
    failures = 0
    for filename in args.files:
        records = load_records(filename)
        for index, record in enumerate(records):
            errors = validate_record(record)
            for error in errors:
                print(f"{filename}[{index}]: {error}")
            failures += bool(errors)
        print(f"{filename}: {len(records)} records checked")
    raise SystemExit(1 if failures else 0)
