"""Diagnose a normalized JSONL object and publish rankings to Aurora.

Triggers:
  * SQS wrapping an S3 ObjectCreated notification (prefix normalized/)
  * Direct Test event: {"bucket": "...", "key": "normalized/...jsonl"}

Env:
  DATABASE_URL (required)
  NORMALIZED_PREFIX (default normalized/)
  ASSUME_SYNCHRONOUS (default true)
  MIN_SAMPLES (default 10)
  WINDOW_SECONDS (default 300)
  DATA_LABEL (default synthetic)
  SYNTHETIC_AS_MEASURED (default true) — promote DERIVED→MEASURED when
    quality_flags contains synthetic_demo (adapter re-upload side-effect)
  DIAGNOSIS_METHOD (default v2) — v2: bottleneck-score-v2 (diagnosis/score_v2.py);
    v1: tail-share-excess-v1 (diagnosis/episodes.py)
  REFERENCE_FRACTION (default 0.3) — v2 only: the earliest traces of the file, by start
    time, up to this fraction of its time span, are the healthy reference; the rest is scored
  TIMING_POLICY (default measured-or-derived) — v2 only

Score v2 compares each window against a healthy reference capture from the same run
and endpoint. An uploaded file has no separate reference, so the start of the file is
used as one: upload data that begins with normal traffic.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from urllib.parse import unquote_plus

from diagnosis import publisher_v2, score_v2
from diagnosis.episodes import diagnose
from diagnosis.publisher import publish

log = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _promote_synthetic_measured(records: list[dict]) -> list[dict]:
    if not _env_bool("SYNTHETIC_AS_MEASURED", True):
        return records
    out = []
    for record in records:
        item = dict(record)
        flags = item.get("quality_flags") or []
        if item.get("timing_quality") == "DERIVED" and "synthetic_demo" in flags:
            item["timing_quality"] = "MEASURED"
        out.append(item)
    return out


def _load_jsonl(s3, bucket: str, key: str) -> list[dict]:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        text = body.read().decode("utf-8")
    finally:
        body.close()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _s3_objects(notification: dict) -> list[tuple[str, str]]:
    prefix = os.environ.get("NORMALIZED_PREFIX", "normalized/")
    objects = []
    for item in notification.get("Records", []):
        key = unquote_plus(item["s3"]["object"]["key"])
        if not key.startswith(prefix):
            raise ValueError("expected a normalized object")
        objects.append((item["s3"]["bucket"]["name"], key))
    return objects


def split_reference(records: list[dict], fraction: float) -> tuple[list[dict], list[dict], dict]:
    """Earliest traces (by first span start) up to `fraction` of the time span -> reference."""
    if not 0 < fraction < 1:
        raise ValueError("REFERENCE_FRACTION must be between 0 and 1")
    starts: dict = {}
    for r in records:
        if r.get("start_time_ns") is None:
            continue
        key, start = (r.get("run_id"), r.get("trace_id")), int(r["start_time_ns"])
        starts[key] = min(starts.get(key, start), start)
    info = {"fraction": fraction, "reference_traces": 0, "scored_traces": 0, "cut_ns": None}
    if not starts:
        return [], records, info
    lo, hi = min(starts.values()), max(starts.values())
    cut = lo + int((hi - lo) * fraction)
    ref_keys = {k for k, v in starts.items() if v < cut}
    reference = [r for r in records if (r.get("run_id"), r.get("trace_id")) in ref_keys]
    scored = [r for r in records if (r.get("run_id"), r.get("trace_id")) not in ref_keys]
    info.update(reference_traces=len(ref_keys), scored_traces=len(starts) - len(ref_keys), cut_ns=str(cut))
    return reference, scored, info


def _diagnose_v2(records: list[dict]) -> dict:
    reference, scored, info = split_reference(records, float(os.environ.get("REFERENCE_FRACTION", "0.3")))
    result = score_v2.diagnose(
        reference, scored,
        timing_policy=os.environ.get("TIMING_POLICY", "measured-or-derived"),
        assume_synchronous=_env_bool("ASSUME_SYNCHRONOUS", True),
        window_seconds=int(os.environ.get("WINDOW_SECONDS", "300")),
        min_samples=int(os.environ.get("MIN_SAMPLES", "10")),
        slow_ratio=float(os.environ.get("SLOW_RATIO", "1.5")),
    )
    result["reference"] = info
    return result


def _diagnose_and_publish(conn, s3, bucket: str, key: str) -> dict:
    records = _load_jsonl(s3, bucket, key)
    records = _promote_synthetic_measured(records)
    method = os.environ.get("DIAGNOSIS_METHOD", "v2").lower()
    if method == "v2":
        result = _diagnose_v2(records)
        payload = "\n".join(json.dumps(r, sort_keys=True) for r in records).encode()
        result["input_files"] = [{"path": f"s3://{bucket}/{key}", "sha256": hashlib.sha256(payload).hexdigest()}]
        result["data_label"] = os.environ.get("DATA_LABEL", "synthetic")
        published = publisher_v2.publish(conn, result)
        return {"bucket": bucket, "key": key, "method": result["method"],
                "windows": len(result.get("windows") or []), "published": published,
                "reference": result["reference"], "excluded_traces": len(result.get("excluded_traces") or [])}
    if method != "v1":
        raise ValueError("DIAGNOSIS_METHOD must be v1 or v2")
    result = diagnose(
        records,
        assume_synchronous=_env_bool("ASSUME_SYNCHRONOUS", True),
        window_seconds=int(os.environ.get("WINDOW_SECONDS", "300")),
        min_samples=int(os.environ.get("MIN_SAMPLES", "10")),
        slow_ratio=float(os.environ.get("SLOW_RATIO", "1.5")),
        min_excess_ms=float(os.environ.get("MIN_EXCESS_MS", "5")),
        alert_delta=float(os.environ.get("ALERT_DELTA", "0.25")),
    )
    payload = "\n".join(json.dumps(r, sort_keys=True) for r in records).encode()
    result["input_files"] = [{"path": f"s3://{bucket}/{key}", "sha256": hashlib.sha256(payload).hexdigest()}]
    result["data_label"] = os.environ.get("DATA_LABEL", "synthetic")
    published = publish(conn, result)
    return {
        "bucket": bucket,
        "key": key,
        "windows": len(result.get("windows") or []),
        "published": published,
        "excluded_traces": result.get("excluded_traces") or {},
    }


def handler(event, context=None, *, s3=None, conn=None):
    owned = conn is None
    if owned:
        import psycopg
        conn = psycopg.connect(os.environ["DATABASE_URL"])
    if s3 is None:
        import boto3
        s3 = boto3.client("s3")
    try:
        if event.get("bucket") and event.get("key"):
            summary = _diagnose_and_publish(conn, s3, event["bucket"], unquote_plus(event["key"]))
            return {"ok": True, **summary}

        failures = []
        results = []
        for message in event.get("Records", []):
            mid = message.get("messageId")
            try:
                notification = json.loads(message["body"])
                for bucket, key in _s3_objects(notification):
                    results.append(_diagnose_and_publish(conn, s3, bucket, key))
            except Exception:
                log.warning("Diagnosis message failed: %s", mid)
                if mid:
                    failures.append({"itemIdentifier": mid})
        return {
            "batchItemFailures": failures,
            "results": results,
            "windows": sum(r["windows"] for r in results),
            "published": sum(r["published"] for r in results),
        }
    finally:
        if owned:
            conn.close()
