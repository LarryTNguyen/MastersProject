"""Regenerate the checked-in JSON Schema and fixtures; standard library only."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}

def enum(*values):
    return {"enum": list(values)}

def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}

text = {"type": "string", "minLength": 1}
ns = {"type": "string", "pattern": "^(0|[1-9][0-9]{0,18})$"}
count = {"type": "integer", "minimum": 0}
attrs = {"type": "object", "additionalProperties": {"$ref": "#/$defs/value"}}
value = {"oneOf": [
    obj({"type": {"const": "string"}, "value": {"type": "string"}}),
    obj({"type": {"const": "bool"}, "value": {"type": "boolean"}}),
    obj({"type": {"const": "int"}, "value": {"type": "string", "pattern": "^(0|-?[1-9][0-9]{0,18})$"}}),
    obj({"type": {"const": "double"}, "value": {"type": "number"}}),
    obj({"type": {"const": "bytes"}, "value": {"type": "string", "contentEncoding": "base64"}}),
    obj({"type": {"const": "array"}, "value": {"type": "array", "items": {"$ref": "#/$defs/value"}}}),
    obj({"type": {"const": "map"}, "value": attrs}),
    obj({"type": {"const": "empty"}, "value": {"type": "null"}})
]}
span = obj({
    "schema_version": {"const": "1.0.0"},
    "run_id": {"type": "string", "format": "uuid"},
    "identity_kind": enum("OTEL", "DATASET"),
    "record_kind": enum("SPAN", "CALL_OBSERVATION"),
    "trace_id": {**text, "maxLength": 256},
    "span_id": {**text, "maxLength": 256},
    "parent_span_id": nullable({**text, "maxLength": 256}),
    "parent_relation": enum("ROOT", "EXPLICIT", "INFERRED", "UNKNOWN"),
    "service_namespace": {"type": "string"},
    "service_name": text,
    "service_instance_id": nullable(text),
    "operation_name": nullable(text),
    "span_kind": enum("INTERNAL", "SERVER", "CLIENT", "PRODUCER", "CONSUMER", "UNKNOWN"),
    "start_time_ns": nullable(ns),
    "end_time_ns": nullable(ns),
    "duration_ns": nullable(ns),
    "time_domain": enum("UNIX", "RELATIVE", "UNKNOWN"),
    "timing_quality": enum("MEASURED", "DERIVED", "DURATION_ONLY", "SYNTHETIC", "UNKNOWN"),
    "status": obj({"code": enum("UNSET", "OK", "ERROR", "UNKNOWN"), "message": nullable(text)}),
    "span_flags": nullable({"type": "integer", "minimum": 0, "maximum": 4294967295}),
    "trace_state": nullable({"type": "string"}),
    "attributes": attrs,
    "resource": obj({"attributes": attrs, "schema_url": nullable(text)}),
    "scope": obj({"name": nullable(text), "version": nullable(text),
                  "attributes": attrs, "schema_url": nullable(text)}),
    "events": {"type": "array", "items": obj({"name": text, "time_ns": ns, "attributes": attrs,
                   "dropped_attributes_count": count})},
    "links": {"type": "array", "items": obj({"trace_id": text, "span_id": text,
                   "identity_kind": enum("OTEL", "DATASET"), "attributes": attrs,
                   "span_flags": nullable({"type": "integer", "minimum": 0, "maximum": 4294967295}),
                   "trace_state": nullable({"type": "string"}), "dropped_attributes_count": count})},
    "dropped_counts": obj({k: count for k in ["span_attributes", "events", "links", "resource_attributes", "scope_attributes"]}),
    "received_at_unix_ns": ns,
    "source": obj({"format": text, "adapter_version": text, "record_key": text,
                   "raw_reference": nullable(text), "details": {"type": "object"}}),
    "quality_flags": {"type": "array", "uniqueItems": True, "items": text}
})
schema = {"$schema": "https://json-schema.org/draft/2020-12/schema",
          "title": "Canonical span or call observation version 1",
          "description": "Internal normalized record, not an OTLP ExportTraceServiceRequest. Run validate.py for cross-field rules.",
          "$defs": {"value": value}, **span}

run_id = "00000000-0000-4000-8000-000000000001"
trace_id = "11111111111111111111111111111111"
base_time = 1791936000000000000
def attr(v):
    return {"type": "string", "value": v}

def make_span(sid, parent, service, start_ms, end_ms, kind="SERVER"):
    return {"schema_version": "1.0.0", "run_id": run_id, "identity_kind": "OTEL",
            "record_kind": "SPAN", "trace_id": trace_id, "span_id": sid,
            "parent_span_id": parent, "parent_relation": "ROOT" if parent is None else "EXPLICIT",
            "service_namespace": "shop", "service_name": service, "service_instance_id": None,
            "operation_name": "POST /checkout" if parent is None else "authorize" if service == "payment" else "checkStock",
            "span_kind": kind, "start_time_ns": str(base_time + start_ms * 1000000),
            "end_time_ns": str(base_time + end_ms * 1000000), "duration_ns": str((end_ms - start_ms) * 1000000),
            "time_domain": "UNIX", "timing_quality": "MEASURED", "status": {"code": "UNSET", "message": None},
            "span_flags": 1, "trace_state": None, "attributes": {},
            "resource": {"attributes": {"service.name": attr(service), "service.namespace": attr("shop")}, "schema_url": None},
            "scope": {"name": "example.instrumentation", "version": "1.0.0", "attributes": {}, "schema_url": None},
            "events": [], "links": [], "dropped_counts": {k: 0 for k in span["properties"]["dropped_counts"]["properties"]},
            "received_at_unix_ns": str(base_time + 2000000000),
            "source": {"format": "otlp_json", "adapter_version": "example-1", "record_key": sid,
                       "raw_reference": None, "details": {}}, "quality_flags": []}

root = make_span("0000000000000001", None, "checkout", 0, 1000)
root["attributes"] = {"http.route": attr("/checkout"), "http.request.method": attr("POST")}
payment = make_span("0000000000000002", root["span_id"], "payment", 100, 800)
inventory = make_span("0000000000000003", root["span_id"], "inventory", 200, 600)
parallel = [root, payment, inventory]
missing = copy.deepcopy(payment)
missing["trace_id"] = "22222222222222222222222222222222"
missing["parent_span_id"] = "0000000000000099"
alibaba = copy.deepcopy(root)
alibaba.update(run_id="00000000-0000-4000-8000-000000000002", identity_kind="DATASET",
               record_kind="CALL_OBSERVATION", trace_id="T_11560863075",
               span_id="MSCallGraph/0d0/part-000.csv:row-1", parent_span_id=None,
               parent_relation="UNKNOWN", service_namespace="alibaba-v2022",
               service_name="MS_71712", service_instance_id="MS_71712_POD_244",
               operation_name="xOuy6-80Vt", span_kind="UNKNOWN", start_time_ns=None,
               end_time_ns=None, duration_ns="2000000", time_domain="UNKNOWN", timing_quality="DURATION_ONLY",
               status={"code": "UNKNOWN", "message": None}, span_flags=None, attributes={},
               resource={"attributes": {}, "schema_url": None},
               scope={"name": None, "version": None, "attributes": {}, "schema_url": None},
               quality_flags=["call_observation", "missing_interval", "unknown_error_status", "ambiguous_observation_side"])
alibaba["source"] = {"format": "alibaba_ms_call_graph_v2022", "adapter_version": "example-1",
    "record_key": alibaba["span_id"], "raw_reference": None,
    "details": {"traceid": alibaba["trace_id"], "rpc_id": "0.1", "rpc_parent_path": "0",
                "um": "MS_58845", "uminstanceid": "MS_58845_POD_0", "dm": "MS_71712",
                "dminstanceid": "MS_71712_POD_244", "rpctype": "rpc", "timestamp_raw": "115352",
                "rt_raw": "2.0", "duration_unit_assumption": "milliseconds; verify selected release"}}
typed = copy.deepcopy(payment)
typed["attributes"] = {"large.counter": {"type": "int", "value": "9223372036854775807"},
                       "custom.bytes": {"type": "bytes", "value": "AAEC"},
                       "empty": {"type": "empty", "value": None},
                       "mixed": {"type": "array", "value": [attr("x"), {"type": "bool", "value": True}]}}
typed["events"] = [{"name": "exception", "time_ns": str(base_time + 100000000),
                    "attributes": {"exception.type": attr("TimeoutError")}, "dropped_attributes_count": 0}]
typed["status"] = {"code": "ERROR", "message": "payment timeout"}
typed["links"] = [{"trace_id": "33333333333333333333333333333333", "span_id": "0000000000000004",
                   "identity_kind": "OTEL", "attributes": {}, "span_flags": 1, "trace_state": None,
                   "dropped_attributes_count": 0}]

def dump(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")

if __name__ == "__main__":
    dump(ROOT / "canonical-span.schema.json", schema)
    dump(ROOT / "examples" / "parallel-trace.json", parallel)
    dump(ROOT / "examples" / "missing-parent.json", [missing])
    dump(ROOT / "examples" / "alibaba-call.json", [alibaba])
    dump(ROOT / "examples" / "typed-values.json", [typed])
    dump(ROOT / "examples" / "expected-parallel-analysis.json", {
        "trace_duration_ns": "1000000000", "root_observed_self_time_ns": "300000000",
        "explanation": "Child intervals [100,800) and [200,600) ms have union length 700 ms. Root residual is 1000 - 700 = 300 ms.",
        "critical_path": None, "critical_path_unavailable_reason": "Parent links alone do not establish blocking dependencies.",
        "note": "This is a synthetic test fixture, not a measured result from an application."})
