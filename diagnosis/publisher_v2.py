"""Publish bottleneck-score-v2 results into trace_v1.service_window_results.

Same table and the same input-manifest lookup as publisher.py (v1); only the row
contents differ. Signals are flattened to a few numbers per signal so the
dashboard can show them next to the ranking, and v2's confidence value (a support
heuristic, not a probability) is mapped onto the table's LOW/MEDIUM/HIGH labels.
Rows for services with no score (no matching reference) are not published.
"""
from __future__ import annotations

import json
import uuid

from .publisher import _manifest

ALGORITHM_VERSION = "bottleneck-score-v2"
SIGNALS = ("critical_path", "latency", "prevalence", "error")
TOP_TRACES = 5


def confidence_label(row: dict) -> str:
    if row.get("score") is None:
        return "UNAVAILABLE"
    value = (row.get("confidence") or {}).get("value") or 0.0
    return "HIGH" if value >= 0.8 else "MEDIUM" if value >= 0.5 else "LOW"


def flat_signals(row: dict) -> dict:
    """{signal: normalized value, signal_change: raw change, ...} plus weights and confidence parts."""
    out = {}
    for name in SIGNALS:
        sig = (row.get("signals") or {}).get(name) or {}
        norm, raw, base = sig.get("normalized"), sig.get("raw"), sig.get("baseline")
        out[name] = round(norm, 3) if norm is not None else None
        if name in ("critical_path", "latency"):     # nanoseconds -> milliseconds
            out[f"{name}_change_ms"] = round(raw / 1e6, 3) if raw is not None else None
            out[f"{name}_reference_ms"] = round(base / 1e6, 3) if base is not None else None
        else:                                         # fractions
            out[f"{name}_change"] = round(raw, 4) if raw is not None else None
            out[f"{name}_reference"] = round(base, 4) if base is not None else None
        if sig.get("reason"):
            out[f"{name}_unavailable"] = sig["reason"]
    conf = row.get("confidence") or {}
    out["confidence_value"] = round(conf.get("value") or 0.0, 3)
    out["sample_support"] = round(conf.get("sample_support") or 0.0, 3)
    out["signal_coverage"] = round(conf.get("signal_coverage") or 0.0, 3)
    out["reference_samples"] = max(((row.get("signals") or {}).get(n) or {}).get("baseline_sample_count", 0) for n in SIGNALS)
    out["window_samples"] = max(((row.get("signals") or {}).get(n) or {}).get("sample_count", 0) for n in SIGNALS)
    out["effective_weights"] = {k: round(v, 3) for k, v in (row.get("effective_weights") or {}).items()}
    return out


def evidence(row: dict) -> list:
    """A few supporting trace ids from the strongest available signal."""
    signals = row.get("signals") or {}
    usable = [n for n in SIGNALS if (signals.get(n) or {}).get("normalized") is not None]
    if not usable:
        return []
    best = max(usable, key=lambda n: signals[n]["normalized"])
    ids = [e["trace_id"] for e in signals[best].get("evidence") or []][:TOP_TRACES]
    return [{"name": "strongest_signal", "value": best}, {"name": "supporting_trace_ids", "value": ids}]


def rows_for_window(conn, result: dict, window: dict) -> list[dict]:
    ranked = [r for r in window.get("rankings") or [] if r.get("score") is not None]
    if not ranked:
        return []
    run_id = window["run_id"]
    manifest = _manifest(conn, run_id, window)
    configuration = dict(result.get("configuration") or {})
    configuration.update({"method": result.get("method"), "data_label": result.get("data_label"),
                          "reference": result.get("reference")})
    rows = []
    for ranking in ranked:
        result_id = str(uuid.uuid5(uuid.NAMESPACE_URL, ":".join([
            run_id, str(window["window_start_ns"]), str(window["window_end_ns"]), window["root_operation"],
            ranking["service"], ALGORITHM_VERSION, str(result.get("configuration_id"))])))
        rows.append({
            "result_id": result_id, "run_id": run_id,
            "window_start_ns": int(window["window_start_ns"]), "window_end_ns": int(window["window_end_ns"]),
            "time_domain": "UNIX", "endpoint_key": window["root_operation"],
            "service_namespace": ranking["service"].split("/", 1)[0] if "/" in ranking["service"] else "",
            "service_name": ranking["service_name"], "algorithm_version": ALGORITHM_VERSION,
            "configuration": configuration, "input_manifest": manifest,
            "signals": flat_signals(ranking), "score": min(1.0, max(0.0, float(ranking["score"]))),
            "confidence": confidence_label(ranking), "evidence": evidence(ranking),
        })
    return rows


def publish(conn, result: dict) -> int:
    """Insert every scored window row; repeats of the same publication are no-ops."""
    rows = []
    for window in result.get("windows", []):
        rows.extend(rows_for_window(conn, result, window))
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                INSERT INTO trace_v1.service_window_results
                  (result_id, run_id, window_start_ns, window_end_ns, time_domain,
                   endpoint_key, service_namespace, service_name, algorithm_version,
                   configuration, input_manifest, signals, score, confidence, evidence)
                VALUES (%(result_id)s::uuid, %(run_id)s::uuid, %(window_start_ns)s,
                        %(window_end_ns)s, %(time_domain)s, %(endpoint_key)s,
                        %(service_namespace)s, %(service_name)s, %(algorithm_version)s,
                        %(configuration)s::jsonb, %(input_manifest)s::jsonb,
                        %(signals)s::jsonb, %(score)s, %(confidence)s, %(evidence)s::jsonb)
                ON CONFLICT (result_id) DO NOTHING
                """,
                {**row, "configuration": json.dumps(row["configuration"]),
                 "input_manifest": json.dumps(row["input_manifest"]),
                 "signals": json.dumps(row["signals"]), "evidence": json.dumps(row["evidence"])},
            )
    conn.commit()
    return len(rows)
